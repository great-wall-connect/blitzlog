"""Assisted-mode user-data bootstrap builder.

Builds the bash script that EC2 runs at first boot for an assisted
("interactive via Telegram") agent run. The script:
  - sets up the env (defensive scrub, LOG_FILE/log(), exports)
  - exports Telegram creds (token, user ID)
  - fetches secrets from SSM
  - optionally installs/authenticates Tailscale (for local-LLM transport)
  - probes the local LLM endpoint if configured (assisted mode: prompt the
    user on Telegram with [Retry]/[Abort]/[Use cloud fallback] buttons;
    cloud fallback switches the opencode config to use the cloud provider)
  - installs system packages + Node.js 24 (for the Telegram bot) + opencode
  - installs the toolchain via mise
  - restores any previous-session state from S3
  - writes the opencode config + session-archive plugin
  - writes the spot-watchdog and periodic-autosave plugins
  - writes the idle-watchdog plugin and the user-invoked shutdown tool
  - boots the opencode server on 127.0.0.1:4096
  - pre-warms the @grinev/opencode-telegram-bot package, sends Telegram
    notification, and starts the bot (foreground; the systemd cleanup
    unit invokes /usr/local/bin/assisted-shutdown.sh on instance stop)

Module-local helpers are kept here because they're only used by this
script. Helpers shared with autonomous mode live in `_common.py`.
"""

import os

from _common import (
    _local_llm_env_block,
    _local_llm_log_line,
    _preflight_block,
    _preflight_definitions,
    _read_secrets_from_ssm_script,
    _tailscale_install_block,
    _write_opencode_config_script,
    script_header,
)
from plugins import (
    _write_idle_watchdog_plugin_script,
    _write_periodic_autosave_plugin_script,
    _write_session_archive_plugin_script,
    _write_shutdown_tool_script,
)


def _session_restore_script(repo: str, issue_number: int, s3_bucket: str) -> str:
    s3_prefix = f"{repo}/issue/{issue_number}"
    return f"""
S3_RESTORE_PREFIX="{s3_prefix}"
S3_RESTORE_BUCKET="{s3_bucket}"
RESUMED=false

log "Checking S3 for existing session state..."
if aws s3 ls "s3://${{S3_RESTORE_BUCKET}}/${{S3_RESTORE_PREFIX}}/metadata.json" >/dev/null 2>&1; then
    log "Found previous session metadata, downloading..."
    aws s3 cp "s3://${{S3_RESTORE_BUCKET}}/${{S3_RESTORE_PREFIX}}/metadata.json" /tmp/session-metadata.json --region "$REGION" 2>/dev/null || true

    if [ -f /tmp/session-metadata.json ]; then
        RESTORE_BRANCH=$(python3 -c "import json; print(json.load(open('/tmp/session-metadata.json')).get('branch',''))" 2>/dev/null || echo "")
        RESTORE_SESSION_ID=$(python3 -c "import json; print(json.load(open('/tmp/session-metadata.json')).get('sessionId',''))" 2>/dev/null || echo "")

        if [ -n "$RESTORE_BRANCH" ]; then
            log "Checking out branch: $RESTORE_BRANCH"
            git -C /workspace/repo fetch origin "$RESTORE_BRANCH" 2>/dev/null || true
            git -C /workspace/repo checkout "$RESTORE_BRANCH" 2>/dev/null || log "WARNING: Could not checkout branch $RESTORE_BRANCH"
        fi

        if [ -n "$RESTORE_SESSION_ID" ]; then
            log "Downloading session $RESTORE_SESSION_ID..."
            aws s3 cp "s3://${{S3_RESTORE_BUCKET}}/${{S3_RESTORE_PREFIX}}/sessions/${{RESTORE_SESSION_ID}}.json" /tmp/session-import.json --region "$REGION" 2>/dev/null || true
        fi

        if [ -f /tmp/session-import.json ]; then
            RESUMED=true
            log "Session state downloaded for restore"
        fi
    fi
else
    log "No previous session state found"
fi
"""


def build_assisted_user_data(
    repo: str,
    issue_number: int,
    sender_login: str = "",
    sender_id: str = "",
    bot_name: str = "",
    bot_token: str = "",
    telegram_user_id: str = "",
    local_llm: dict | None = None,
) -> str:
    s3_bucket = os.environ.get("S3_LOGS_BUCKET", "<your-agent-logs-bucket>")
    base_opencode_model = os.environ.get(
        "OPENCODE_MODEL", "minimax-coding-plan/MiniMax-M3"
    )
    opencode_model = local_llm["model"] if local_llm else base_opencode_model
    s3_archive_prefix = f"{repo}/issue/{issue_number}"

    local_llm_env = _local_llm_env_block(local_llm)
    local_llm_log = _local_llm_log_line(local_llm)
    tailscale_block = _tailscale_install_block(local_llm)
    preflight_defs = _preflight_definitions("assisted") if local_llm else ""
    preflight_call = _preflight_block(local_llm, "assisted")

    opencode_max_steps = int(os.environ.get("OPENCODE_AGENT_MAX_STEPS", "500"))

    header = script_header(
        mode="assisted",
        repo=repo,
        issue_number=issue_number,
        opencode_model=opencode_model,
        opencode_max_steps=opencode_max_steps,
        s3_bucket=s3_bucket,
        s3_archive_prefix=s3_archive_prefix,
        local_llm_env=local_llm_env,
        local_llm_log_line=local_llm_log,
        opencode_prompt=None,
    )

    return f"""{header}{_read_secrets_from_ssm_script(issue_number, local_llm=bool(local_llm))}
TELEGRAM_USER_ID="{telegram_user_id}"
TELEGRAM_BOT_TOKEN="{bot_token}"
export TELEGRAM_BOT_TOKEN TELEGRAM_USER_ID
{tailscale_block}{preflight_defs}{preflight_call}

log "Downloading whisper model..."
mkdir -p /opt/whisper-stt/models
if [ ! -f "/opt/whisper-stt/models/ggml-${{STT_MODEL:-base.en}}.bin" ]; then
    aws s3 cp "s3://${{STT_MODELS_BUCKET}}/models/ggml-${{STT_MODEL:-base.en}}.bin" \\
        "/opt/whisper-stt/models/ggml-${{STT_MODEL:-base.en}}.bin" \\
        --region "$REGION"
fi

log "Restoring previous session state..."
{_session_restore_script(repo, issue_number, s3_bucket)}

log "Writing opencode config and session archive plugin..."
{_write_opencode_config_script(autonomous=False, local_provider=local_llm, opencode_max_steps=opencode_max_steps)}
{_write_session_archive_plugin_script()}

log "Effective opencode config: model=$OPENCODE_MODEL, provider=$(grep -oE '"minimax[a-z-]*"|"local"' /root/.config/opencode/opencode.json | head -1 | tr -d '\"'){", api_key_prefix=${OPENCODE_API_KEY:0:8}..." if not local_llm else "..."}"

log "Writing periodic autosave plugin..."
{_write_periodic_autosave_plugin_script()}

log "Writing shutdown tool..."
{_write_shutdown_tool_script()}

log "Writing idle watchdog plugin..."
{_write_idle_watchdog_plugin_script()}

log "Starting blitzlog-agent via systemd watchdog..."
# The watchdog (Packer-baked to /usr/local/bin/watchdog.sh and registered
# via 02-systemd.sh as the blitzlog-agent.service unit) is the host's
# lifecycle manager. It loads the baked container image, runs it with
# the right env vars + mounts, waits for it to exit, uploads host +
# container logs and session artifacts to S3, releases the bot pool
# lock, and terminates the EC2 instance. The container itself has NO
# AWS credentials — all S3 ops happen on the host.
#
# Write the env file the watchdog reads.
mkdir -p /workspace/.blitzlog
cat > /etc/blitzlog.env <<ENVEOF
MODE=assisted
ISSUE_NUMBER={issue_number}
REPO={repo}
BLITZLOG_ENV=${{BLITZLOG_ENV}}
S3_LOGS_BUCKET={s3_bucket}
SESSION_ARCHIVE_BUCKET={s3_bucket}
SESSION_ARCHIVE_PREFIX={s3_archive_prefix}
OPENCODE_API_KEY=${{OPENCODE_API_KEY}}
OPENCODE_MODEL={opencode_model}
TELEGRAM_BOT_TOKEN=${{TELEGRAM_BOT_TOKEN}}
TELEGRAM_USER_ID=${{TELEGRAM_USER_ID}}
TELEGRAM_BOT_NAME={bot_name}
TELEGRAM_SENDER_LOGIN={sender_login}
STT_API_URL=${{STT_API_URL}}
STT_API_KEY=${{STT_API_KEY}}
STT_MODEL=${{STT_MODEL}}
STT_LANGUAGE=${{STT_LANGUAGE}}
GITHUB_TOKEN_SSM_PARAM=/blitzlog/${{BLITZLOG_ENV}}/ephemeral/github-token-${{ISSUE_NUMBER}}
ENVEOF

# The unit is already enabled (Packer 02-systemd.sh). Just start it; the
# watchdog will `docker run` the agent, wait for it to exit, then do all
# AWS-dependent cleanup + terminate the instance.
sudo systemctl start blitzlog-agent.service
"""
