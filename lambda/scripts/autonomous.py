"""Autonomous-mode user-data bootstrap builder.

Builds the bash script that EC2 runs at first boot for an autonomous
("fire-and-forget") agent run. The script:
  - sets up the env (defensive scrub, LOG_FILE/log(), exports)
  - fetches secrets from SSM (GitHub token, optionally the cloud API key)
  - optionally installs/authenticates Tailscale (for local-LLM transport)
  - probes the local LLM endpoint if configured (abort on 5 min unreachable)
  - installs system packages, opencode, the toolchain
  - writes the opencode config + session-archive plugin
  - writes the spot-watchdog and periodic-autosave plugins
  - wraps the opencode run in a watchdog that uploads logs + session on
    exit and terminates the instance

Module-local helpers are kept here because they're only used by this
script. Helpers shared with assisted mode (`_decode_api_errors_script`,
`_read_secrets_from_ssm_script`, `_install_system_packages_script`, ...)
live in `_common.py`.
"""

import os

from _common import (
    _configure_git_script,
    _decode_api_errors_script,
    _install_opencode_script,
    _install_system_packages_script,
    _install_toolchain_script,
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
    _write_periodic_autosave_plugin_script,
    _write_session_archive_plugin_script,
    _write_spot_watchdog_plugin_script,
)


def _upload_logs_and_terminate_script(repo: str, issue_number: int) -> str:
    s3_bucket = os.environ.get("S3_LOGS_BUCKET", "<your-agent-logs-bucket>")
    s3_prefix = f"{repo}/issue/{issue_number}"
    return f"""
TOKEN=$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/dynamic/instance-identity/document | python3 -c "import sys,json; print(json.load(sys.stdin)['region'])")
LOG_KEY="{s3_prefix}/logs/${{INSTANCE_ID}}-$(date +%Y%m%d-%H%M%S).log"
aws s3 cp /var/log/backend-bootstrap.log "s3://{s3_bucket}/${{LOG_KEY}}" --region "$REGION" || true
"""


def build_autonomous_user_data(
    repo: str,
    issue_number: int,
    sender_login: str = "",
    sender_id: str = "",
    local_llm: dict | None = None,
) -> str:
    prompt = (
        f"Work on GitHub issue #{issue_number}. Follow AGENTS.md for branch naming, "
        f"implementation standards, testing, linting and commit conventions. "
        f"Create a PR when done."
    )

    s3_bucket = os.environ.get("S3_LOGS_BUCKET", "<your-agent-logs-bucket>")
    base_opencode_model = os.environ.get(
        "OPENCODE_MODEL", "minimax-coding-plan/MiniMax-M3"
    )
    if local_llm:
        opencode_model = local_llm["model"]
    else:
        opencode_model = base_opencode_model
    s3_archive_prefix = f"{repo}/issue/{issue_number}"

    git_user_name = sender_login or ""
    git_user_email = (
        f"{sender_id}+{sender_login}@users.noreply.github.com" if sender_login else ""
    )

    local_llm_env = _local_llm_env_block(local_llm)
    local_llm_log = _local_llm_log_line(local_llm)
    tailscale_block = _tailscale_install_block(local_llm)
    preflight_defs = _preflight_definitions("autonomous") if local_llm else ""
    preflight_call = _preflight_block(local_llm, "autonomous")

    header = script_header(
        mode="autonomous",
        repo=repo,
        issue_number=issue_number,
        opencode_model=opencode_model,
        s3_bucket=s3_bucket,
        s3_archive_prefix=s3_archive_prefix,
        local_llm_env=local_llm_env,
        local_llm_log_line=local_llm_log,
        opencode_prompt=prompt,
    )

    return f"""{header}{_read_secrets_from_ssm_script(issue_number, local_llm=bool(local_llm))}
{tailscale_block}{preflight_defs}{preflight_call}

log "Installing system packages..."
{_install_system_packages_script()}

log "Setting up git credentials..."
{_configure_git_script(git_user_name, git_user_email)}

log "Installing opencode..."
{_install_opencode_script()}

log "Cloning repository..."
mkdir -p /workspace
git clone "https://github.com/${{REPO}}.git" /workspace/repo
cd /workspace/repo

{_install_toolchain_script()}

log "Writing opencode config and session archive plugin..."
{_write_opencode_config_script(autonomous=True, local_provider=local_llm)}
{_write_session_archive_plugin_script()}

log "Effective opencode config: model=$OPENCODE_MODEL, provider=$(grep -oE '"minimax[a-z-]*"|"local"' /root/.config/opencode/opencode.json | head -1 | tr -d '\"'){", api_key_prefix=${OPENCODE_API_KEY:0:8}..." if not local_llm else "..."}"

log "Writing spot watchdog and periodic autosave plugins..."
{_write_spot_watchdog_plugin_script()}
{_write_periodic_autosave_plugin_script()}

log "Setting up watchdog (timeout: 7200s)..."
cat > /etc/blitzlog.env <<ENVEOF
ISSUE_NUMBER={issue_number}
S3_LOGS_BUCKET={s3_bucket}
REPO={repo}
SESSION_ARCHIVE_BUCKET={s3_bucket}
SESSION_ARCHIVE_PREFIX={s3_archive_prefix}
ENVEOF
cat > /usr/local/bin/watchdog.sh << 'WDOG_SCRIPT'
#!/bin/bash
set -euo pipefail
source /etc/blitzlog.env
export HOME=/root
export PATH=/root/.opencode/bin:$PATH
export SESSION_ARCHIVE_BUCKET SESSION_ARCHIVE_PREFIX
LOG_FILE="/var/log/backend-bootstrap.log"
log() {{
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}}
TIMEOUT=7200
COMMAND="$@"
timeout $TIMEOUT $COMMAND 2>&1
EXIT_CODE=$?

# Upload logs to S3 before terminating
{_upload_logs_and_terminate_script(repo, issue_number)}
log "Logs uploaded to S3"

{_decode_api_errors_script()}

# Export session to S3
{_session_export_to_s3_script()}

# Terminate instance
if [ $EXIT_CODE -eq 124 ]; then
    echo "Watchdog triggered: command exceeded ${{TIMEOUT}}s"
fi
aws ec2 terminate-instances --instance-id "$INSTANCE_ID" --region "$REGION" || true
WDOG_SCRIPT
chmod +x /usr/local/bin/watchdog.sh

log "Launching opencode agent..."
cd /workspace/repo
OPENCODE_NONINTERACTIVE=1 /usr/local/bin/watchdog.sh opencode run --agent build "$OPENCODE_PROMPT" 2>&1 | tee -a "$LOG_FILE"
"""
