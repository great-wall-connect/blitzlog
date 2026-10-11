"""Shared bootstrap-script helpers used by both `build_autonomous_user_data`
and `build_assisted_user_data`. The two builders share a long prologue
(`set -euo pipefail` + defensive env scrubbing + `LOG_FILE` + `log()`
function + env exports). Centralizing the prologue eliminates a class of
"fix the bug in autonomous but forget to mirror it in assisted" drift.

The host bootstrap is now minimal: it reads secrets from SSM and writes
`/etc/blitzlog.env` for the watchdog. All agent-runtime concerns — Tailscale
install, opencode config, local-LLM preflight, cloud fallback — live in
the container's entrypoint.sh. The container has the tools and the env
vars to do those steps itself.

Remaining host-only concerns:
- `_read_secrets_from_ssm_script` — the host has the IAM role for SSM
- `_decode_api_errors_script` — scans the container log for actionable
  LLM-provider errors and emits ACTIONABLE log lines

Note: the previous `_install_opencode_script`, `_install_whisper_stt_script`,
`_install_toolchain_script`, `_install_tailscale_script`,
`_write_opencode_config_script`, `_preflight_local_llm_script`,
`_switch_to_cloud_fallback_script`, `_tailscale_install_block`,
`_preflight_definitions`, and `_preflight_block` helpers were removed when
the docker refactor moved those concerns into the container image and
entrypoint.sh. The host bootstrap no longer needs to install or configure
the agent runtime — that's the container's job now.
"""

from _env import _blitzlog_env, _ssm_root


def _git_identity_block(sender_login: str, sender_id: str) -> str:
    """Return the host-side `git config --global user.name/email` snippet,
    or "" if either sender field is missing (the helper requires BOTH
    `sender_login` and `sender_id` to compose the canonical
    `<sender_id>+<sender_login>@users.noreply.github.com` noreply
    email; a partial sender set would emit a malformed "+oct
    @users.noreply.github.com" string. The bootstrap silently skips the
    identity step when either is missing).

    The same identity is surfaced to the container via the
    GIT_USER_NAME / GIT_USER_EMAIL env exports in `script_header`
    (which the watchdog passes through `-e` flags into the container;
    the container's entrypoint.sh then runs the matching
    `git config --global` so the commits the agent makes inside the
    container are attributed to the same human).
    """
    if not sender_login or not sender_id:
        return ""
    return (
        f'\ngit config --global user.name "{sender_login}"\n'
        f'git config --global user.email "{sender_id}+{sender_login}@users.noreply.github.com"\n'
    )


def _git_identity_env_block(sender_login: str, sender_id: str) -> str:
    """Return the GIT_USER_NAME / GIT_USER_EMAIL env-export block for
    `script_header`. Returns "" when either sender field is missing
    so the bootstrap is a no-op when the webhook payload doesn't
    carry one.

    The values are exported as shell variables so the
    `cat > /etc/blitzlog.env <<ENVEOF ... ENVEOF` heredoc in the
    per-mode builder can reference `${GIT_USER_NAME}` /
    `${GIT_USER_EMAIL}` (matching the OPENCODE_* /
    LOCAL_LLM_* pattern). The watchdog then passes them through `-e`
    to the container.
    """
    if not sender_login or not sender_id:
        return ""
    email = f"{sender_id}+{sender_login}@users.noreply.github.com"
    return (
        f'GIT_USER_NAME="{sender_login}"\n'
        f'GIT_USER_EMAIL="{email}"\n'
        "export GIT_USER_NAME GIT_USER_EMAIL\n"
    )


def _local_llm_env_block(local_llm: dict | None) -> str:
    """Emit the LOCAL_LLM_* env-export block for the bootstrap prologue.
    Returns "" if no local LLM is configured — both builders then
    emit the cloud-only prologue without any local_llm-related exports.
    """
    if not local_llm:
        return ""
    block = (
        f'LOCAL_LLM_ENDPOINT="{local_llm["endpoint"]}"\n'
        f'LOCAL_LLM_MODEL="{local_llm["model"]}"\n'
        f'LOCAL_LLM_API_KEY="{local_llm.get("api_key", "")}"\n'
        f'LOCAL_LLM_FALLBACK="{local_llm["fallback"]}"\n'
    )
    tailscale_key = local_llm.get("tailscale_auth_key") or ""
    if tailscale_key:
        block += f'TAILSCALE_AUTH_KEY="{tailscale_key}"\n'
    block += (
        "export LOCAL_LLM_ENDPOINT LOCAL_LLM_MODEL LOCAL_LLM_API_KEY "
        "LOCAL_LLM_FALLBACK"
    )
    if tailscale_key:
        block += " TAILSCALE_AUTH_KEY"
    return block + "\n"


def script_header(
    *,
    mode: str,
    repo: str,
    issue_number: int,
    opencode_model: str,
    opencode_max_steps: int,
    s3_bucket: str,
    s3_archive_prefix: str,
    s3_log_prefix: str,
    local_llm_env: str,
    local_llm_log_line: str = "",
    opencode_prompt: str | None = None,
    sender_login: str = "",
    sender_id: str = "",
) -> str:
    """Return the shebang + `set -euo pipefail` + defensive env scrub +
    `LOG_FILE`/`log()` + env-export preamble every bootstrap needs.

    `local_llm_env` is the output of `_local_llm_env_block(local_llm)`
    (possibly ""), prepended before the export line. Both modes need
    the same defensive `unset OPENCODE_API_KEY HTTPS_PROXY HTTP_PROXY
    https_proxy http_proxy` so a leaked proxy var can't redirect the
    SSM reads or the LLM API calls.

    Autonomous mode additionally sets `OPENCODE_NONINTERACTIVE=1` and
    `OPENCODE_PROMPT="<prompt>"`. Assisted mode does not — the opencode
    server runs interactively on port 4096 and the prompt comes from
    the Telegram user, not from the bootstrap.

    `local_llm_log_line` (typically the output of `_local_llm_log_line`,
    i.e. either `'log "Local LLM configured: ..."'` or "") is rendered
    between the "log Repo" line and the "log Reading secrets from SSM"
    line. When local_llm is not configured, "" produces a single blank
    line between the two logs (matching the byte layout of the
    pre-refactor single-file handler).

    `opencode_max_steps` is exported as `OPENCODE_AGENT_MAX_STEPS="N"` and
    is read by the container's entrypoint.sh when it writes opencode.json.

    `sender_login` / `sender_id` (issue #119) flow through to two
    places:

    1. The git-identity env block (`GIT_USER_NAME` / `GIT_USER_EMAIL`)
       is exported so the per-mode heredoc that writes
       `/etc/blitzlog.env` can reference them; the watchdog then
       passes them to the container, and the container's entrypoint.sh
       runs `git config --global user.{name,email}` so any `git commit`
       the agent makes inside the container is attributed to the
       human who triggered the run (not the EC2 default
       `root@<hostname>`).
    2. The host-side `git config --global` snippet is emitted just
       before the "Reading secrets from SSM" log so any host-side git
       operation (the assisted-mode `_session_restore_script`'s
       `git fetch` / `git checkout`) is also attributed correctly.

    Both are no-ops when `sender_login` is empty (autonomous launches
    without a webhook payload, manual test paths, etc.) — the helpers
    return "" in that case and the bootstrap silently skips the
    identity step.
    """
    if opencode_prompt is not None:
        opencode_interactive_lines = (
            f"OPENCODE_NONINTERACTIVE=1\n" f'OPENCODE_PROMPT="{opencode_prompt}"\n'
        )
        interactive_export = " OPENCODE_NONINTERACTIVE OPENCODE_PROMPT"
    else:
        opencode_interactive_lines = ""
        interactive_export = ""

    git_identity_env_block = _git_identity_env_block(sender_login, sender_id)
    git_identity_block = _git_identity_block(sender_login, sender_id)
    if git_identity_block:
        # git_identity_block from the helper already starts with "\n"
        # so it inserts a blank line in front of the `git config`
        # lines. We follow it with a one-liner so the bootstrap has
        # a visible "git identity set for ..." diagnostic in
        # /var/log/backend-bootstrap.log.
        host_git_identity_block = (
            git_identity_block + f'log "Git identity set for {sender_login}"\n'
        )
    else:
        host_git_identity_block = ""

    # Add GIT_USER_NAME / GIT_USER_EMAIL to the consolidated export
    # line only when the env-block is non-empty (i.e., a sender was
    # supplied). An empty identity case still emits the rest of the
    # prologue verbatim so the byte layout doesn't drift.
    git_identity_export = (
        " GIT_USER_NAME GIT_USER_EMAIL" if git_identity_env_block else ""
    )

    return f"""#!/bin/bash
set -euo pipefail
export HOME=/root
export GIT_TERMINAL_PROMPT=0

# Defensive env scrubbing (always — cheap hygiene).
unset OPENCODE_API_KEY HTTPS_PROXY HTTP_PROXY https_proxy http_proxy

LOG_FILE="/var/log/backend-bootstrap.log"
log() {{
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}}

ISSUE_NUMBER={issue_number}
REPO="{repo}"
{opencode_interactive_lines}OPENCODE_MODEL="{opencode_model}"
OPENCODE_AGENT_MAX_STEPS="{opencode_max_steps}"
S3_LOGS_BUCKET="{s3_bucket}"
SESSION_ARCHIVE_BUCKET="{s3_bucket}"
SESSION_ARCHIVE_PREFIX="{s3_archive_prefix}"
S3_LOG_PREFIX="{s3_log_prefix}"
{local_llm_env}{git_identity_env_block}export ISSUE_NUMBER OPENCODE_MODEL OPENCODE_AGENT_MAX_STEPS S3_LOGS_BUCKET{interactive_export} SESSION_ARCHIVE_BUCKET SESSION_ARCHIVE_PREFIX S3_LOG_PREFIX{git_identity_export}

log "=== Cloud-coder bootstrap starting ({mode}) ==="
log "Repo: $REPO, Issue: $ISSUE_NUMBER"
{local_llm_log_line}

{host_git_identity_block}log "Reading secrets from SSM..."
"""


def _read_secrets_from_ssm_script(issue_number: int, local_llm: bool = False) -> str:
    """Render the bootstrap snippet that fetches the GitHub token and (when a
    cloud run is intended) the OPENCODE_API_KEY from SSM via IMDSv2.

    When local_llm is True the OPENCODE_API_KEY export is omitted so the agent
    has no cloud credentials in its environment. The cloud-fallback path
    re-reads the SSM param on demand.
    """
    ssm_root = _ssm_root()

    api_key_block = ""
    if not local_llm:
        api_key_block = (
            "\n"
            f"OPENCODE_API_KEY=$(aws ssm get-parameter --name "
            f'"{ssm_root}/opencode/api-key" --with-decryption --query '
            f'Parameter.Value --output text --region "$REGION")\n'
            "export OPENCODE_API_KEY\n"
        )

    return f"""
TOKEN=$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/dynamic/instance-identity/document | python3 -c "import sys,json; print(json.load(sys.stdin)['region'])")
export AWS_DEFAULT_REGION=$REGION
export BLITZLOG_ENV={_blitzlog_env()}

_CC_GITHUB_TOKEN=$(aws ssm get-parameter --name "{ssm_root}/ephemeral/github-token-{issue_number}" --with-decryption --query Parameter.Value --output text --region "$REGION")
export _CC_GITHUB_TOKEN{api_key_block}

STT_API_URL=$(aws ssm get-parameter --name "{ssm_root}/stt/api-url" --query Parameter.Value --output text --region "$REGION")
export STT_API_URL
STT_API_KEY=$(aws ssm get-parameter --name "{ssm_root}/stt/api-key" --with-decryption --query Parameter.Value --output text --region "$REGION")
export STT_API_KEY
STT_MODEL=$(aws ssm get-parameter --name "{ssm_root}/stt/model" --query Parameter.Value --output text --region "$REGION")
export STT_MODEL
STT_LANGUAGE=$(aws ssm get-parameter --name "{ssm_root}/stt/language" --query Parameter.Value --output text --region "$REGION")
export STT_LANGUAGE
STT_MODELS_BUCKET=$(aws ssm get-parameter --name "{ssm_root}/stt/models-bucket" --query Parameter.Value --output text --region "$REGION")
export STT_MODELS_BUCKET
"""


def _decode_api_errors_script() -> str:
    ssm_root = _ssm_root()
    return f"""
# Decode known LLM-provider API errors into actionable log lines.
# Run after the opencode process exits and before session export.
if [ -f "$LOG_FILE" ] && grep -qE "insufficient_balance|insufficient balance|\\(1008\\)" "$LOG_FILE"; then
    log "ACTIONABLE: LLM provider returned insufficient balance / HTTP 1008."
    log "ACTIONABLE: The token-plan account for the configured provider (${{OPENCODE_MODEL:-unknown}}) has zero credits."
    log "ACTIONABLE: Top up at the provider console (e.g. https://platform.minimax.io) before retrying."
    log "ACTIONABLE: Verify the API key at SSM parameter {ssm_root}/opencode/api-key belongs to a funded account."
fi
if [ -f "$LOG_FILE" ] && grep -qE "Unauthorized|invalid_api_key|\\(401\\)|Authentication" "$LOG_FILE"; then
    log "ACTIONABLE: LLM provider rejected the API key as unauthorized (HTTP 401)."
    log "ACTIONABLE: Rotate {ssm_root}/opencode/api-key in SSM and re-run terraform apply."
fi
if [ -f "$LOG_FILE" ] && grep -qE "rate.?limit|quota.?exceeded|too.?many.?requests|\\(429\\)" "$LOG_FILE"; then
    log "ACTIONABLE: LLM provider returned a rate-limit / quota error (HTTP 429)."
    log "ACTIONABLE: Wait for the quota window to reset or upgrade the plan, then re-trigger."
fi
"""


def _local_llm_log_line(local_llm: dict | None) -> str:
    """Emit the 'Local LLM configured: ...' diagnostic line (without
    trailing newline — the script_header places the surrounding
    newlines so the byte layout matches the pre-refactor single-file
    handler). Returns "" when no local LLM is in play.
    """
    if not local_llm:
        return ""
    return 'log "Local LLM configured: endpoint=$LOCAL_LLM_ENDPOINT, model=$LOCAL_LLM_MODEL"'
