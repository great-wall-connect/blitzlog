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


def _git_identity_ssm_block(owner_login: str) -> str:
    """Reserved for symmetry with `_local_llm_env_block`; the SSM
    fetch itself lives in `_read_git_identity_ssm_script` so it runs
    AFTER `$REGION` is set by `_read_secrets_from_ssm_script`.
    Calling this from `script_header` would place the `aws ssm
    get-parameter` call before `$REGION` is bound, which trips
    `set -u` on a fresh EC2 instance (issue #119 regression — the
    fetched value isn't visible to the header, and the `--region
    "$REGION"` expansion aborts the bootstrap). Returns "" so the
    header's consolidated export line and the byte layout are
    unchanged.
    """
    return ""


def _git_identity_env_block(ssm_block: str) -> str:
    """Emit the GIT_AUTHOR_* export line for the consolidated export
    preamble. `ssm_block` is the output of `_git_identity_ssm_block`.
    Returns "" when no SSM block was emitted (no `owner_login`) so
    the no-identity case keeps the byte layout of the pre-#119
    prologue verbatim.
    """
    if not ssm_block:
        return ""
    return "export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL\n"


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
    git_identity_ssm: str = "",
    git_identity_env: str = "",
    local_llm_log_line: str = "",
    opencode_prompt: str | None = None,
) -> str:
    """Return the shebang + `set -euo pipefail` + defensive env scrub +
    `LOG_FILE`/`log()` + env-export preamble every bootstrap needs.

    `local_llm_env` is the output of `_local_llm_env_block(local_llm)`
    (possibly ""), prepended before the export line. Both modes need
    the same defensive `unset OPENCODE_API_KEY HTTPS_PROXY HTTP_PROXY
    https_proxy http_proxy` so a leaked proxy var can't redirect the
    SSM reads or the LLM API calls.

    `git_identity_env` is the output of `_git_identity_env_block(...)`.
    When non-empty, the consolidated export line carries
    `GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL` so the values the
    `_read_git_identity_ssm_script` block assigns downstream
    (after `$REGION` is set) are visible to the watchdog and
    forwarded to the container. The `git_identity_ssm` block
    itself is NOT emitted here — that fetch is concatenated with
    `_read_secrets_from_ssm_script` (see
    `_read_git_identity_ssm_script`) so `$REGION` is bound before
    the `--region "$REGION"` expansion. Emitting it here would
    trip `set -u` on a fresh EC2 instance (issue #119 regression).

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
    """
    if opencode_prompt is not None:
        opencode_interactive_lines = (
            f"OPENCODE_NONINTERACTIVE=1\n" f'OPENCODE_PROMPT="{opencode_prompt}"\n'
        )
        interactive_export = " OPENCODE_NONINTERACTIVE OPENCODE_PROMPT"
    else:
        opencode_interactive_lines = ""
        interactive_export = ""

    git_identity_export = (
        " GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL" if git_identity_env else ""
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

# Issue #123: source the host-side S3 abstraction installed at AMI
# bake time under /usr/local/bin/. All S3 verbs in this bootstrap go
# through the `blitzlog_s3_*` helpers from this library — one
# canonical call site per S3 verb (cf. watchdog.sh). Sourced AFTER
# `log()` is defined so library ERROR output uses the same channel.
source /usr/local/bin/blitzlog_uploader.sh

ISSUE_NUMBER={issue_number}
REPO="{repo}"
{opencode_interactive_lines}OPENCODE_MODEL="{opencode_model}"
OPENCODE_AGENT_MAX_STEPS="{opencode_max_steps}"
S3_LOGS_BUCKET="{s3_bucket}"
SESSION_ARCHIVE_BUCKET="{s3_bucket}"
SESSION_ARCHIVE_PREFIX="{s3_archive_prefix}"
S3_LOG_PREFIX="{s3_log_prefix}"
{local_llm_env}{git_identity_env}export ISSUE_NUMBER OPENCODE_MODEL OPENCODE_AGENT_MAX_STEPS S3_LOGS_BUCKET{interactive_export} SESSION_ARCHIVE_BUCKET SESSION_ARCHIVE_PREFIX S3_LOG_PREFIX{git_identity_export}

log "=== Cloud-coder bootstrap starting ({mode}) ==="
log "Repo: $REPO, Issue: $ISSUE_NUMBER"
{local_llm_log_line}

log "Reading secrets from SSM..."
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

_CC_GITHUB_TOKEN=$(aws ssm get-parameter --name "{ssm_root}/ephemeral/gh/$REPO/{issue_number}" --with-decryption --query Parameter.Value --output text --region "$REGION")
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


def _read_git_identity_ssm_script(owner_login: str) -> str:
    """Render the bootstrap snippet that fetches the operator-configured
    git author identity from SSM and assigns it to GIT_AUTHOR_NAME /
    GIT_AUTHOR_EMAIL. Concatenated with `_read_secrets_from_ssm_script`
    so the fetch runs AFTER `$REGION` is resolved (the IMDSv2 token +
    region dance is the first thing the secrets block does — by the
    time this fires, `$REGION` is set, so the `--region "$REGION"`
    expansion no longer trips `set -u` on a fresh EC2 instance).

    The `2>/dev/null || echo ""` swallow keeps the bootstrap working
    when the operator hasn't provisioned the user-pool yet (the
    git-author params are emitted by `infra/user-pool/main.tf`; a
    fresh deploy that's running the lambda before the user-pool
    stack falls back to root@<hostname>). The container's
    `entrypoint.sh` already gates the `git config --global` step on
    `GIT_AUTHOR_NAME` being non-empty, so the fallback is
    behaviour-preserving for unconfigured pools.

    `owner_login` empty → return "". The autonomous no-sender case
    (`build_autonomous_user_data(...)` with no webhook payload)
    keeps the pre-#119 byte layout — no SSM fetch, no export, no
    `/etc/blitzlog.env` git-author lines.
    """
    if not owner_login:
        return ""
    root = f"/blitzlog/users/{owner_login}/git-author"
    return (
        f"\nGIT_AUTHOR_NAME=$(aws ssm get-parameter "
        f'--name "{root}/name" '
        f'--query Parameter.Value --output text --region "$REGION" '
        f'2>/dev/null || echo "")\n'
        f"GIT_AUTHOR_EMAIL=$(aws ssm get-parameter "
        f'--name "{root}/email" '
        f'--query Parameter.Value --output text --region "$REGION" '
        f'2>/dev/null || echo "")\n'
    )


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
