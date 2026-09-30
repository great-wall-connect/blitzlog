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
    """Return the `git config --global user.name/email` snippet, or "" if
    no identity is supplied (autonomous launches without a sender would
    have no name to attribute commits to — the bootstrap silently skips
    the identity step in that case).
    """
    if not sender_login:
        return ""
    return (
        f'\ngit config --global user.name "{sender_login}"\n'
        f'git config --global user.email "{sender_id}+{sender_login}@users.noreply.github.com"\n'
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
    local_llm_env: str,
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
{local_llm_env}export ISSUE_NUMBER OPENCODE_MODEL OPENCODE_AGENT_MAX_STEPS S3_LOGS_BUCKET{interactive_export} SESSION_ARCHIVE_BUCKET SESSION_ARCHIVE_PREFIX

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
