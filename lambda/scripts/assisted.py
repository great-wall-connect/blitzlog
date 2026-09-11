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
    _configure_git_script,
    _install_opencode_script,
    _install_system_packages_script,
    _install_toolchain_script,
    _install_whisper_stt_script,
    _local_llm_env_block,
    _local_llm_log_line,
    _preflight_block,
    _preflight_definitions,
    _read_secrets_from_ssm_script,
    _session_export_to_s3_script,
    _tailscale_install_block,
    _write_opencode_config_script,
    script_header,
)
from plugins import (
    _write_idle_watchdog_plugin_script,
    _write_periodic_autosave_plugin_script,
    _write_session_archive_plugin_script,
    _write_shutdown_tool_script,
    _write_spot_watchdog_plugin_script,
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
    if local_llm:
        opencode_model = f"local/{local_llm['model']}"
        opencode_model_provider = "local"
        opencode_model_id = local_llm["model"]
    else:
        opencode_model = base_opencode_model
        opencode_model_provider = "minimax-coding-plan"
        _, opencode_model_id = (
            base_opencode_model.split("/", 1)
            if "/" in base_opencode_model
            else ("", base_opencode_model)
        )
    s3_archive_prefix = f"{repo}/issue/{issue_number}"

    git_user_name = sender_login or ""
    git_user_email = (
        f"{sender_id}+{sender_login}@users.noreply.github.com" if sender_login else ""
    )

    local_llm_env = _local_llm_env_block(local_llm)
    local_llm_log = _local_llm_log_line(local_llm)
    tailscale_block = _tailscale_install_block(local_llm)
    preflight_defs = _preflight_definitions("assisted") if local_llm else ""
    preflight_call = _preflight_block(local_llm, "assisted")

    header = script_header(
        mode="assisted",
        repo=repo,
        issue_number=issue_number,
        opencode_model=opencode_model,
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

log "Installing system packages..."
{_install_system_packages_script()}

log "Installing Node.js 24 via dnf..."
dnf install -y nodejs24 nodejs24-npm 2>&1 | tail -5
alternatives --set node /usr/bin/node-24
node --version
npm --version

log "Setting up git credentials..."
{_configure_git_script(git_user_name, git_user_email)}

log "Installing opencode..."
{_install_opencode_script()}

log "Cloning repository..."
mkdir -p /workspace
git clone "https://github.com/${{REPO}}.git" /workspace/repo
cd /workspace/repo

{_install_toolchain_script()}

log "Restoring previous session state..."
{_session_restore_script(repo, issue_number, s3_bucket)}

log "Writing opencode config and session archive plugin..."
{_write_opencode_config_script(autonomous=False, local_provider=local_llm)}
{_write_session_archive_plugin_script()}

log "Effective opencode config: model=$OPENCODE_MODEL, provider=$(grep -oE '"minimax[a-z-]*"|"local"' /root/.config/opencode/opencode.json | head -1 | tr -d '\"'){", api_key_prefix=${OPENCODE_API_KEY:0:8}..." if not local_llm else "..."}"

log "Writing spot watchdog and periodic autosave plugins..."
{_write_spot_watchdog_plugin_script()}
{_write_periodic_autosave_plugin_script()}

log "Writing shutdown tool..."
{_write_shutdown_tool_script()}

log "Writing idle watchdog plugin..."
{_write_idle_watchdog_plugin_script()}

log "Starting opencode server on port 4096..."
cd /workspace/repo
export PATH=/root/.opencode/bin:$PATH
export SESSION_ARCHIVE_BUCKET SESSION_ARCHIVE_PREFIX
OPENCODE_SERVER_USERNAME=agent
OPENCODE_SERVER_PASSWORD=$(openssl rand -hex 16)
export OPENCODE_SERVER_USERNAME OPENCODE_SERVER_PASSWORD
opencode serve --hostname 127.0.0.1 --port 4096 &
OPENCODE_PID=$!

for i in $(seq 1 30); do
    if curl -s http://localhost:4096/health > /dev/null 2>&1; then
        log "OpenCode server is healthy"
        break
    fi
    if ! kill -0 $OPENCODE_PID 2>/dev/null; then
        log "ERROR: OpenCode server process died"
        exit 1
    fi
    log "Waiting for opencode server... ($i/30)"
    sleep 2
done

log "Importing previous session if available..."
if [ "$RESUMED" = "true" ] && [ -f /tmp/session-import.json ]; then
    opencode import /tmp/session-import.json 2>&1 || log "WARNING: Session import failed"
    log "Session imported from S3"
fi

log "Configuring opencode-telegram-bot..."
mkdir -p /root/.config/opencode-telegram-bot
cat > /root/.config/opencode-telegram-bot/.env <<TELEGRAMCFG
TELEGRAM_BOT_TOKEN=${{TELEGRAM_BOT_TOKEN}}
TELEGRAM_ALLOWED_USER_ID=${{TELEGRAM_USER_ID}}
OPENCODE_API_URL=http://localhost:4096
OPENCODE_SERVER_USERNAME=agent
OPENCODE_SERVER_PASSWORD=${{OPENCODE_SERVER_PASSWORD}}
OPENCODE_MODEL_PROVIDER={opencode_model_provider}
OPENCODE_MODEL_ID={opencode_model_id}
BOT_LOCALE=en
TELEGRAM_FORCE_IPV4=true
STT_API_URL=${{STT_API_URL}}
STT_API_KEY=${{STT_API_KEY}}
STT_MODEL=${{STT_MODEL}}
STT_LANGUAGE=${{STT_LANGUAGE}}
STT_REQUEST_FORMAT=multipart
TELEGRAMCFG

log "Auto-selecting project and session in bot settings..."
PROJECT_JSON=$(curl -sf -u agent:$OPENCODE_SERVER_PASSWORD http://localhost:4096/project 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
for p in data if isinstance(data, list) else [data]:
    if p.get('worktree','').startswith('/workspace'):
        print(json.dumps({{'id': p['id'], 'worktree': p['worktree'], 'name': p.get('name', p['worktree'])}}))
        break
" 2>/dev/null || echo "")

SESSION_JSON=""
if [ "$RESUMED" = "true" ] && [ -f /tmp/session-import.json ]; then
    RESTORE_SESSION_ID=$(python3 -c "import json; d=json.load(open('/tmp/session-import.json')); print(d.get('id',''))" 2>/dev/null || echo "")
    if [ -n "$RESTORE_SESSION_ID" ]; then
        SESSION_TITLE=$(curl -sf -u agent:$OPENCODE_SERVER_PASSWORD "http://localhost:4096/session/${{RESTORE_SESSION_ID}}" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps({{'id': d['id'], 'title': d.get('title',''), 'directory': d.get('directory','')}}))" 2>/dev/null || echo "")
        if [ -n "$SESSION_TITLE" ]; then
            SESSION_JSON=$SESSION_TITLE
        fi
    fi
fi

if [ -n "$PROJECT_JSON" ]; then
    if [ -n "$SESSION_JSON" ]; then
        cat > /root/.config/opencode-telegram-bot/settings.json <<SETTINGS_EOF
{{"currentProject": $PROJECT_JSON, "currentSession": $SESSION_JSON}}
SETTINGS_EOF
        log "Project and session pre-selected (resumed)"
    else
        cat > /root/.config/opencode-telegram-bot/settings.json <<SETTINGS_EOF
{{"currentProject": $PROJECT_JSON}}
SETTINGS_EOF
        log "Project pre-selected (new session)"
    fi
else
    log "WARNING: Could not auto-select project, user will need /projects"
fi

log "Fetching issue title..."
ISSUE_TITLE=$(gh issue view "$ISSUE_NUMBER" --repo "${{REPO}}" --json title --jq .title 2>/dev/null || echo "unknown")
RESUME_STATUS=""

log "Pre-warming opencode-telegram-bot (downloads package to npx cache)..."
{_install_whisper_stt_script()}
npx -y @grinev/opencode-telegram-bot@latest status > /var/log/pre-warm.log 2>&1
PRE_WARM_EXIT=$?
if [ "$PRE_WARM_EXIT" -ne 0 ]; then
    log "WARNING: Pre-warm failed with exit code $PRE_WARM_EXIT; will attempt bot start anyway and notify user"
    curl -s -X POST "https://api.telegram.org/bot${{TELEGRAM_BOT_TOKEN}}/sendMessage" \\
        -d chat_id="${{TELEGRAM_USER_ID}}" \\
        -d parse_mode="Markdown" \\
        -d text="Assisted agent cannot be started [Bot: {bot_name}]

Repo: ${{REPO}}
[Issue #${{ISSUE_NUMBER}}: ${{ISSUE_TITLE}}](https://github.com/${{REPO}}/issues/${{ISSUE_NUMBER}})
Mode: Assisted (interactive via Telegram)$RESUME_STATUS" || true
fi

log "Sending Telegram notification..."
if [ "$RESUMED" = "true" ]; then
    RESTORED_TITLE=$(echo "$SESSION_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('title',''))" 2>/dev/null || echo "")
    RESUME_STATUS="

Resumed session: ${{RESTORED_TITLE}}"
fi
curl -s -X POST "https://api.telegram.org/bot${{TELEGRAM_BOT_TOKEN}}/sendMessage" \\
    -d chat_id="${{TELEGRAM_USER_ID}}" \\
    -d parse_mode="Markdown" \\
    -d text="Assisted agent ready [Bot: {bot_name}]

Repo: ${{REPO}}
[Issue #${{ISSUE_NUMBER}}: ${{ISSUE_TITLE}}](https://github.com/${{REPO}}/issues/${{ISSUE_NUMBER}})
Mode: Assisted (interactive via Telegram)$RESUME_STATUS

Connect to this bot to start working on the task." || true

log "Installing shutdown helper..."
cat > /etc/blitzlog.env <<ENVEOF
ISSUE_NUMBER={issue_number}
S3_LOGS_BUCKET={s3_bucket}
REPO={repo}
SESSION_ARCHIVE_BUCKET={s3_bucket}
SESSION_ARCHIVE_PREFIX={s3_archive_prefix}
OPENCODE_SERVER_USERNAME=$OPENCODE_SERVER_USERNAME
OPENCODE_SERVER_PASSWORD=$OPENCODE_SERVER_PASSWORD
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_USER_ID=$TELEGRAM_USER_ID
ENVEOF
cat > /usr/local/bin/assisted-shutdown.sh << 'SHUTDOWN_SCRIPT'
#!/bin/bash
set -euo pipefail
source /etc/blitzlog.env
export HOME=/root
export PATH=/root/.opencode/bin:$PATH
export SESSION_ARCHIVE_BUCKET SESSION_ARCHIVE_PREFIX
export OPENCODE_SERVER_USERNAME OPENCODE_SERVER_PASSWORD
LOG_FILE="/var/log/backend-bootstrap.log"
log() {{
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}}

log "=== Assisted shutdown initiated ==="

TOKEN=$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/dynamic/instance-identity/document | python3 -c "import sys,json; print(json.load(sys.stdin)['region'])")

# Detect shutdown reason
SHUTDOWN_REASON="${{_SHUTDOWN_REASON:-}}"
if [ -z "$SHUTDOWN_REASON" ]; then
    SPOT_ACTION=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/spot/instance-action 2>/dev/null || echo "")
    if [ -n "$SPOT_ACTION" ]; then
        SHUTDOWN_REASON="spot_interruption"
    elif [ "${{_SKIP_SHUTDOWN:-}}" = "1" ]; then
        SHUTDOWN_REASON="system_shutdown"
    else
        SHUTDOWN_REASON="unknown"
    fi
fi
log "Shutdown reason: $SHUTDOWN_REASON"

# Export session to S3
{_session_export_to_s3_script()}

# Upload logs to S3
LOG_KEY="{s3_archive_prefix}/logs/${{INSTANCE_ID}}-$(date +%Y%m%d-%H%M%S).log"
aws s3 cp /var/log/backend-bootstrap.log "s3://{s3_bucket}/${{LOG_KEY}}" --region "$REGION" || true
log "Logs uploaded to S3"

# Release bot pool lock
S3_LOGS_BUCKET="{s3_bucket}"
if [ -n "{bot_name}" ]; then
    aws s3 rm "s3://${{S3_LOGS_BUCKET}}/bot-pool-locks/{sender_login}/{bot_name}.json" --region "$REGION" 2>/dev/null || true
    log "Released bot pool lock for {sender_login}/{bot_name}"
fi

# Notify via Telegram
curl -s -X POST "https://api.telegram.org/bot${{TELEGRAM_BOT_TOKEN}}/sendMessage" \\
    -d chat_id="${{TELEGRAM_USER_ID}}" \\
    -d text="Assisted agent shutting down [Bot: {bot_name}] (reason: ${{SHUTDOWN_REASON}}). Session archived to S3. Logs uploaded." || true

if [ "${{_SKIP_SHUTDOWN:-}}" != "1" ]; then
    log "Shutting down..."
    shutdown -h now
fi
SHUTDOWN_SCRIPT
chmod +x /usr/local/bin/assisted-shutdown.sh

log "Installing systemd shutdown service..."
cat > /etc/systemd/system/blitzlog-cleanup.service << 'CLEANUP_UNIT'
[Unit]
Description=Cloud Coder Assisted Cleanup
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/true
Environment=_SKIP_SHUTDOWN=1
ExecStop=/usr/local/bin/assisted-shutdown.sh
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
CLEANUP_UNIT
systemctl enable blitzlog-cleanup.service
systemctl start blitzlog-cleanup.service

log "Starting opencode-telegram-bot (foreground)..."
cd /workspace/repo
npx -y @grinev/opencode-telegram-bot@latest start 2>&1 | tee -a "$LOG_FILE"
"""
