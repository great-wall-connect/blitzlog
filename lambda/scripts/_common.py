"""Shared bootstrap-script helpers used by both `build_autonomous_user_data`
and `build_assisted_user_data`. The two builders share a long prologue
(`set -euo pipefail` + defensive env scrubbing + `LOG_FILE` + `log()`
function + env exports) and a long tail of mostly-identical install/config
helpers. Centralizing the prologue eliminates a class of "fix the bug in
autonomous but forget to mirror it in assisted" drift, and centralizing
the shared builders (`_read_secrets_from_ssm_script`,
`_install_system_packages_script`, `_configure_git_script`,
`_install_opencode_script`, `_install_tailscale_script`,
`_install_whisper_stt_script`, `_install_toolchain_script`,
`_write_opencode_config_script`, `_session_export_to_s3_script`,
`_preflight_local_llm_script`, `_decode_api_errors_script`) means a
change to e.g. the opencode install steps is one edit instead of two.
"""

import os
from urllib.parse import urlparse

from _env import WHISPER_STT_SHIM_SOURCE, _blitzlog_env, _ssm_root


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
    s3_bucket: str,
    s3_archive_prefix: str,
    local_llm_env: str,
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
    """
    if opencode_prompt is not None:
        opencode_interactive_lines = (
            f'OPENCODE_NONINTERACTIVE=1\n'
            f'OPENCODE_PROMPT="{opencode_prompt}"\n'
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
S3_LOGS_BUCKET="{s3_bucket}"
SESSION_ARCHIVE_BUCKET="{s3_bucket}"
SESSION_ARCHIVE_PREFIX="{s3_archive_prefix}"
{local_llm_env}export ISSUE_NUMBER OPENCODE_MODEL S3_LOGS_BUCKET{interactive_export} SESSION_ARCHIVE_BUCKET SESSION_ARCHIVE_PREFIX

log "=== Cloud-coder bootstrap starting ({mode}) ==="
log "Repo: $REPO, Issue: $ISSUE_NUMBER"

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


def _install_system_packages_script() -> str:
    return """
dnf install -y spal-release
dnf install -y git ripgrep amazon-ssm-agent
dnf install -y 'dnf-command(config-manager)'
dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo
dnf install -y gh
systemctl start amazon-ssm-agent || true
hash -r
"""


def _configure_git_script(git_user_name: str = "", git_user_email: str = "") -> str:
    identity = ""
    if git_user_name:
        identity = f"""
git config --global user.name "{git_user_name}"
git config --global user.email "{git_user_email}"
"""
    return f"""
mkdir -p /root/.git-credentials.d
echo "https://x-access-token:${{_CC_GITHUB_TOKEN}}@github.com" > /root/.git-credentials.d/github
chmod 600 /root/.git-credentials.d/github
git config --global credential.helper 'store --file /root/.git-credentials.d/github'
{identity}
echo "${{_CC_GITHUB_TOKEN}}" | gh auth login --with-token
export GITHUB_TOKEN="${{_CC_GITHUB_TOKEN}}"
"""


def _install_opencode_script() -> str:
    return """
curl -fsSL https://opencode.ai/install | bash
export PATH=/root/.opencode/bin:$PATH
hash -r
opencode --version
"""


def _install_tailscale_script() -> str:
    """Install Tailscale and enroll the EC2 instance into the user's Tailnet.

    Runs only when TAILSCALE_AUTH_KEY is set in the bootstrap environment
    (the Lambda passes it through from the per-user SSM param
    /blitzlog/users/<login>/local-llm/tailscale-auth-key). The auth key
    should be generated with Ephemeral: enabled and Tags: tag:blitzlog-agent
    so the node auto-removes when the EC2 terminates and the ACL can grant
    scoped access. --accept-routes=false prevents the EC2 from picking up
    advertised subnet routes from other Tailnet devices — the EC2 only
    needs peer-to-peer reachability to the LLM endpoint, not full split
    tunnel routing.
    """
    return """
if [ -n "$TAILSCALE_AUTH_KEY" ]; then
    log "Installing Tailscale..."
    dnf install -y yum-utils
    dnf config-manager --add-repo https://pkgs.tailscale.com/stable/amazonlinux/2023/tailscale.repo
    dnf install -y tailscale
    systemctl enable --now tailscaled

    log "Authenticating EC2 instance to Tailscale Tailnet..."
    TSC_HOSTNAME="blitzlog-agent-${ISSUE_NUMBER}-$(date +%s)"
    if ! tailscale up --authkey="$TAILSCALE_AUTH_KEY" \
                     --hostname="$TSC_HOSTNAME" \
                     --ephemeral \
                     --accept-routes=false; then
        log "WARNING: tailscale up failed; preflight_local_llm will likely time out"
    fi
    for i in $(seq 1 30); do
        STATE=$(tailscale status --json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('BackendState',''))" 2>/dev/null || echo "")
        if [ "$STATE" = "Running" ]; then break; fi
        log "Waiting for tailscaled to be Running... ($i/30)"
        sleep 2
    done
    TAILSCALE_IP=$(tailscale ip -4 2>/dev/null | head -1 || echo "")
    log "Tailscale connected: hostname=$TSC_HOSTNAME, ip=$TAILSCALE_IP"
fi
"""


_WHISPER_CPP_VERSION = "v1.7.6"
_WHISPER_CPP_RELEASE_URL = (
    f"https://github.com/ggml-org/whisper.cpp/releases/download/{_WHISPER_CPP_VERSION}"
    f"/whisper-bin-aarch64-linux-gnu.zip"
)
_WHISPER_CPP_RELEASE_FALLBACK_URL = (
    f"https://github.com/ggml-org/whisper.cpp/releases/download/{_WHISPER_CPP_VERSION}"
    f"/whisper-bin-aarch64-linux-gnu.tar.gz"
)
_WHISPER_CPP_SOURCE_TARBALL_URL = f"https://github.com/ggml-org/whisper.cpp/archive/refs/tags/{_WHISPER_CPP_VERSION}.tar.gz"


def _install_whisper_stt_script() -> str:
    shim_source = WHISPER_STT_SHIM_SOURCE
    systemd_unit = (
        "[Unit]\n"
        "Description=Blitzlog whisper.cpp STT shim\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "User=root\n"
        "WorkingDirectory=/opt/whisper-stt\n"
        "Environment=HOST=127.0.0.1\n"
        "Environment=PORT=7878\n"
        "Environment=WHISPER_CLI=/opt/whisper-stt/bin/whisper-cli\n"
        "EnvironmentFile=-/etc/blitzlog/whisper-stt.env\n"
        "ExecStart=/root/.local/share/mise/shims/python3 /opt/whisper-stt/server.py\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "StandardOutput=append:/var/log/whisper-stt-shim.log\n"
        "StandardError=append:/var/log/whisper-stt-shim.log\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    return f"""
log "Installing whisper.cpp STT backend..."

mkdir -p /opt/whisper-stt/bin /opt/whisper-stt/models /opt/whisper-stt/runtime
cd /opt/whisper-stt

# 1. Acquire whisper.cpp CLI binary.
#    Prefer prebuilt release; fall back to building from source if the
#    prebuilt asset is unavailable for the current release tag.
WHISPER_CLI=/opt/whisper-stt/bin/whisper-cli
if [ ! -x "$WHISPER_CLI" ]; then
    log "Downloading whisper.cpp {_WHISPER_CPP_VERSION} prebuilt (aarch64-linux-gnu)..."
    if curl -fsSL "{_WHISPER_CPP_RELEASE_URL}" -o /tmp/whisper-prebuilt.zip; then
        dnf install -y unzip || true
        unzip -q -o /tmp/whisper-prebuilt.zip -d /tmp/whisper-prebuilt
        find /tmp/whisper-prebuilt -name whisper-cli -type f -exec cp {{}} "$WHISPER_CLI" \\;
        chmod +x "$WHISPER_CLI"
    elif curl -fsSL "{_WHISPER_CPP_RELEASE_FALLBACK_URL}" -o /tmp/whisper-prebuilt.tar.gz; then
        tar -xzf /tmp/whisper-prebuilt.tar.gz -C /tmp/whisper-prebuilt
        find /tmp/whisper-prebuilt -name whisper-cli -type f -exec cp {{}} "$WHISPER_CLI" \\;
        chmod +x "$WHISPER_CLI"
    else
        log "Prebuilt download failed; building whisper.cpp from source (this takes a few minutes)..."
        dnf install -y cmake gcc gcc-c++ make
        curl -fsSL "{_WHISPER_CPP_SOURCE_TARBALL_URL}" -o /tmp/whisper-src.tar.gz
        tar -xzf /tmp/whisper-src.tar.gz -C /opt
        cmake -S /opt/whisper.cpp-{_WHISPER_CPP_VERSION.lstrip('v')} -B /opt/whisper.cpp-{_WHISPER_CPP_VERSION.lstrip('v')}/build -DCMAKE_BUILD_TYPE=Release
        cmake --build /opt/whisper.cpp-{_WHISPER_CPP_VERSION.lstrip('v')}/build --config Release -j$(nproc)
        cp /opt/whisper.cpp-{_WHISPER_CPP_VERSION.lstrip('v')}/build/bin/whisper-cli "$WHISPER_CLI"
    fi
fi
"$WHISPER_CLI" --help > /dev/null && log "whisper-cli ready: $("$WHISPER_CLI" --help 2>&1 | head -1)"

# 2. Download the configured model from the blitzlog-stt-models S3 bucket.
MODEL_FILE="ggml-${{STT_MODEL}}.bin"
MODEL_DEST="/opt/whisper-stt/models/${{MODEL_FILE}}"
if [ ! -f "$MODEL_DEST" ]; then
    log "Downloading whisper model ${{MODEL_FILE}} from s3://${{STT_MODELS_BUCKET}}/models/..."
    aws s3 cp "s3://${{STT_MODELS_BUCKET}}/models/${{MODEL_FILE}}" "$MODEL_DEST" --region "${{AWS_DEFAULT_REGION}}"
fi
test -s "$MODEL_DEST" && log "Whisper model ready: $MODEL_DEST ($(du -h "$MODEL_DEST" | cut -f1))"

# 3. Install pywhispercpp and write the Python shim.
#    Use the Python that mise installed (in `_install_toolchain_script`),
#    bind the global shim so the systemd service can find it, then
#    install pywhispercpp into that interpreter. System `python3` on
#    AL2023 is 3.9; pywhispercpp's PEP 604 syntax requires Python 3.10+.
log "Binding Python shim globally and installing pywhispercpp..."
mise use -g python
PIP_LOG=$(mktemp)
if ! python3 -m pip install pywhispercpp python-multipart imageio-ffmpeg >"$PIP_LOG" 2>&1; then
    log "ERROR: pywhispercpp install failed; last 30 lines:"
    tail -30 "$PIP_LOG"
    log "See $PIP_LOG for full output"
    exit 1
fi
rm -f "$PIP_LOG"

# Verify the install actually works (catches "installed but broken").
if ! python3 -c "import pywhispercpp; from pywhispercpp.model import Model" 2>&1; then
    log "ERROR: pywhispercpp installed but not importable"
    exit 1
fi

cat > /opt/whisper-stt/server.py <<'__WHISPER_SHIM_PY__'
{shim_source}
__WHISPER_SHIM_PY__
chmod +x /opt/whisper-stt/server.py

# 4. Write systemd unit and start the shim.
cat > /etc/systemd/system/whisper-stt-shim.service <<'__WHISPER_SHIM_UNIT__'
{systemd_unit}
__WHISPER_SHIM_UNIT__

mkdir -p /etc/blitzlog
cat > /etc/blitzlog/whisper-stt.env <<ENVEOF
WHISPER_MODEL=/opt/whisper-stt/models/${{MODEL_FILE}}
WHISPER_LANGUAGE=${{STT_LANGUAGE}}
REQUEST_TIMEOUT_MS=60000
ENVEOF

    systemctl daemon-reload
    systemctl enable whisper-stt-shim.service
    systemctl restart whisper-stt-shim.service

# 5. Health-check the shim before the bot starts.
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:7878/healthz > /dev/null 2>&1; then
        log "whisper-stt-shim is healthy on http://127.0.0.1:7878"
        break
    fi
    if ! systemctl is-active --quiet whisper-stt-shim.service; then
        log "WARNING: whisper-stt-shim service is not active; bot will start without STT"
        break
    fi
    log "Waiting for whisper-stt-shim... ($i/30)"
    sleep 2
done
"""


def _write_opencode_config_script(
    autonomous: bool = True, local_provider: dict | None = None
) -> str:
    """Render the bootstrap snippet that writes /root/.config/opencode/opencode.json.

    When local_provider is None (the cloud path) the single minimax-coding-plan
    provider block is emitted. When local_provider is a dict with keys
    {endpoint, model, api_key}, the cloud provider block is omitted entirely and
    a `local` provider block is emitted in its place — opencode has no cloud
    credentials to address even if it tries.
    """
    compaction = '"auto": false'
    agent_prompt = (
        ""
        if autonomous
        else ',\n      "prompt": "You have a `shutdown` tool available. Use it when the user asks to shut down or terminate the instance."'
    )

    if local_provider is None:
        provider_block = (
            '  "provider": {\n'
            '    "minimax-coding-plan": {\n'
            '      "options": {\n'
            '        "apiKey": "{env:OPENCODE_API_KEY}"\n'
            "      }\n"
            "    }\n"
            "  }"
        )
    else:
        api_key_line = ""
        if local_provider.get("api_key"):
            api_key_line = '        "apiKey": "{env:LOCAL_LLM_API_KEY}",\n'
        provider_block = (
            '  "provider": {\n'
            '    "local": {\n'
            '      "options": {\n'
            '        "baseURL": "{env:LOCAL_LLM_ENDPOINT}",\n'
            f"{api_key_line}"
            "      }\n"
            "    }\n"
            "  }"
        )

    return (
        """
mkdir -p /root/.config/opencode
cat > /root/.config/opencode/opencode.json <<'OPENCODECFG'
{
  "$schema": "https://opencode.ai/config.json",
  "model": "{env:OPENCODE_MODEL}",
  "default_agent": "build",
  "compaction": {
"""
        + compaction
        + """
  },
  "agent": {
    "build": {
      "steps": 75"""
        + agent_prompt
        + """
    }
  },
"""
        + provider_block
        + """
}
OPENCODECFG
"""
    )


def _session_export_to_s3_script() -> str:
    return """
log "Exporting session to S3..."
cd /workspace/repo
SESSION_ID=$(opencode session list --format json -n 1 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])" 2>/dev/null || echo "")
if [ -n "$SESSION_ID" ]; then
    TMP_SESSION="/tmp/session-export-${SESSION_ID}.json"
    opencode export "$SESSION_ID" > "$TMP_SESSION" 2>/dev/null || true
    if [ -s "$TMP_SESSION" ]; then
        aws s3 cp "$TMP_SESSION" "s3://${SESSION_ARCHIVE_BUCKET}/${SESSION_ARCHIVE_PREFIX}/sessions/${SESSION_ID}.json" --region "$REGION" 2>/dev/null || true
        BRANCH=$(git -C /workspace/repo branch --show-current 2>/dev/null || echo "")
        COMMIT=$(git -C /workspace/repo rev-parse HEAD 2>/dev/null || echo "")
        python3 -c "import json; print(json.dumps({'sessionId': '${SESSION_ID}', 'branch': '${BRANCH}', 'commit': '${COMMIT}', 'timestamp': $(date +%s000)}))" > /tmp/session-export-metadata.json 2>/dev/null || true
        aws s3 cp /tmp/session-export-metadata.json "s3://${SESSION_ARCHIVE_BUCKET}/${SESSION_ARCHIVE_PREFIX}/metadata.json" --region "$REGION" 2>/dev/null || true
        log "Session exported to S3: $SESSION_ID"
    fi
else
    log "WARNING: No session found to export"
fi
"""


def _preflight_local_llm_script(mode: str) -> str:
    """Render the bash function `preflight_local_llm` that probes the local
    LLM endpoint and either proceeds, switches to cloud (assisted only),
    retries, or aborts.

    Required env vars at call time:
        LOCAL_LLM_ENDPOINT  - URL to probe
        MODE                - "autonomous" or "assisted"
        TELEGRAM_BOT_TOKEN  - (assisted only)
        TELEGRAM_USER_ID    - (assisted only)
        LOCAL_LLM_FALLBACK  - "closed" or "cloud"
        HAS_CLOUD_KEY       - "true" if /blitzlog/opencode/api-key is configured

    On success: returns 0 and the bootstrap continues with the local LLM.
    On assisted-mode cloud-switch: returns 0 and the caller invokes
    switch_to_cloud_fallback (defined separately, only in assisted bootstrap).
    On autonomous unreachable / assisted abort / no-reply: calls `exit 1`
    (autonomous) or `/usr/local/bin/assisted-shutdown.sh` (assisted).
    """
    if mode == "autonomous":
        return r"""
preflight_local_llm() {
  local endpoint="${LOCAL_LLM_ENDPOINT}"
  log "Preflight: probing local LLM at $endpoint (mode=autonomous)"

  for attempt in $(seq 1 10); do
    if curl -sf -m 10 "$endpoint/health" >/dev/null 2>&1 \
       || curl -sf -m 10 "$endpoint/v1/models" >/dev/null 2>&1; then
      log "Local LLM reachable (attempt $attempt/10)"
      return 0
    fi
    log "Local LLM probe failed (attempt $attempt/10), sleeping 30s..."
    sleep 30
  done

  log "ACTIONABLE: Local LLM unreachable after 10 attempts (5 min)"
  log "ACTIONABLE: Autonomous mode aborting — local LLM unreachable."
  exit 1
}
"""

    return r"""
preflight_local_llm() {
  local endpoint="${LOCAL_LLM_ENDPOINT}"
  local bot_token="${TELEGRAM_BOT_TOKEN:-}"
  local user_id="${TELEGRAM_USER_ID:-}"
  local fallback="${LOCAL_LLM_FALLBACK:-closed}"
  local has_cloud_key="${HAS_CLOUD_KEY:-false}"
  local max_retries="${LOCAL_LLM_MAX_RETRIES:-5}"

  log "Preflight: probing local LLM at $endpoint (mode=assisted, fallback=$fallback)"

  for attempt in $(seq 1 10); do
    if curl -sf -m 10 "$endpoint/health" >/dev/null 2>&1 \
       || curl -sf -m 10 "$endpoint/v1/models" >/dev/null 2>&1; then
      log "Local LLM reachable (attempt $attempt/10)"
      return 0
    fi
    log "Local LLM probe failed (attempt $attempt/10), sleeping 30s..."
    sleep 30
  done

  log "ACTIONABLE: Local LLM unreachable after 10 attempts (5 min)"

  if [ -z "$bot_token" ] || [ -z "$user_id" ]; then
    log "ACTIONABLE: Telegram credentials missing; aborting."
    exit 1
  fi

  local reply_markup
  if [ "$fallback" = "cloud" ] && [ "$has_cloud_key" = "true" ]; then
    reply_markup='{"inline_keyboard":[[{"text":"Use cloud fallback","callback_data":"cloud"},{"text":"Retry","callback_data":"retry"},{"text":"Abort","callback_data":"abort"}]]}'
  else
    reply_markup='{"inline_keyboard":[[{"text":"Retry","callback_data":"retry"},{"text":"Abort","callback_data":"abort"}]]}'
  fi

  local text="Local LLM unreachable after 5 min. The agent cannot reach your local endpoint. Pick an action:"
  if ! curl -sf -X POST "https://api.telegram.org/bot${bot_token}/sendMessage" \
      -d "chat_id=${user_id}" \
      --data-urlencode "text=$text" \
      --data-urlencode "reply_markup=$reply_markup" >/dev/null; then
    log "ACTIONABLE: Failed to send Telegram prompt; aborting."
    exit 1
  fi

  log "Sent Telegram prompt; waiting up to 10 min for user reply..."

  local deadline=$(($(date +%s) + 600))
  local decision=""

  while [ "$(date +%s)" -lt "$deadline" ]; do
    sleep 5
    decision=$(curl -sf "https://api.telegram.org/bot${bot_token}/getUpdates" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for u in data.get('result', []):
        cb = u.get('callback_query') or {}
        if cb.get('data') in ('retry', 'cloud', 'abort'):
            print(cb['data'])
            sys.exit(0)
except Exception:
    pass
sys.exit(1)
" 2>/dev/null) || decision=""
    if [ -n "$decision" ]; then
      break
    fi
  done

  if [ -z "$decision" ]; then
    log "ACTIONABLE: No Telegram reply within 10 min; aborting."
    /usr/local/bin/assisted-shutdown.sh
    exit 1
  fi

  log "Telegram decision: $decision"

  case "$decision" in
    retry)
      if [ "$max_retries" -le 0 ]; then
        log "ACTIONABLE: max retries exhausted; aborting."
        /usr/local/bin/assisted-shutdown.sh
        exit 1
      fi
      log "User chose retry; re-running preflight"
      LOCAL_LLM_MAX_RETRIES=$((max_retries - 1)) preflight_local_llm
      ;;
    cloud)
      log "User chose cloud fallback; switching inference to cloud"
      switch_to_cloud_fallback
      ;;
    abort)
      log "User chose abort; shutting down"
      /usr/local/bin/assisted-shutdown.sh
      ;;
  esac
}
"""


def _switch_to_cloud_fallback_script() -> str:
    """Render the bash function `switch_to_cloud_fallback` that re-emits the
    cloud opencode config, exports OPENCODE_API_KEY from SSM, restarts
    opencode serve (assisted), and notifies the user on Telegram.

    Only emitted in assisted-mode user-data when local_llm is configured;
    autonomous mode aborts on unreachable local LLM and never needs this.
    """
    ssm_root = _ssm_root()
    return f"""
switch_to_cloud_fallback() {{
  log "Switching to cloud fallback..."

  if ! OPENCODE_API_KEY=$(aws ssm get-parameter \\
      --name "{ssm_root}/opencode/api-key" \\
      --with-decryption \\
      --query Parameter.Value \\
      --output text \\
      --region "$REGION" 2>/dev/null); then
    log "ERROR: cloud fallback requested but {ssm_root}/opencode/api-key is not configured in SSM."
    log "ERROR: refusing to switch; aborting run instead."
    curl -s -X POST "https://api.telegram.org/bot${{TELEGRAM_BOT_TOKEN}}/sendMessage" \\
      -d chat_id="${{TELEGRAM_USER_ID}}" \\
      -d text="Cloud fallback requested but no cloud API key is configured. Aborting." || true
    /usr/local/bin/assisted-shutdown.sh
    exit 1
  fi
  export OPENCODE_API_KEY

  mkdir -p /root/.config/opencode
  cat > /root/.config/opencode/opencode.json <<'OPENCODECFG'
{{
  "$schema": "https://opencode.ai/config.json",
  "model": "${{OPENCODE_MODEL}}",
  "default_agent": "build",
  "compaction": {{
    "auto": false
  }},
  "agent": {{
    "build": {{
      "steps": 75,
      "prompt": "You have a `shutdown` tool available. Use it when the user asks to shut down or terminate the instance."
    }}
  }},
  "provider": {{
    "minimax-coding-plan": {{
      "options": {{
        "apiKey": "{{env:OPENCODE_API_KEY}}"
      }}
    }}
  }}
}}
OPENCODECFG

  if [ -n "${{OPENCODE_PID:-}}" ] && kill -0 "$OPENCODE_PID" 2>/dev/null; then
    kill "$OPENCODE_PID" 2>/dev/null || true
    wait "$OPENCODE_PID" 2>/dev/null || true
  fi

  cd /workspace/repo 2>/dev/null || cd / || true
  OPENCODE_SERVER_USERNAME=agent
  OPENCODE_SERVER_PASSWORD="${{OPENCODE_SERVER_PASSWORD:-$(openssl rand -hex 16)}}"
  export OPENCODE_SERVER_USERNAME OPENCODE_SERVER_PASSWORD
  opencode serve --hostname 127.0.0.1 --port 4096 &
  OPENCODE_PID=$!

  curl -s -X POST "https://api.telegram.org/bot${{TELEGRAM_BOT_TOKEN}}/sendMessage" \\
    -d chat_id="${{TELEGRAM_USER_ID}}" \\
    -d text="Switched to cloud fallback (user request). Inference now going to ${{OPENCODE_MODEL}}." || true

  log "Cloud fallback active. OPENCODE_PID=$OPENCODE_PID"
}}
"""


def _tailscale_install_block(local_llm: dict | None) -> str:
    """Return the tailscale-install preamble + the `_install_tailscale_script`
    body, or "" if no local LLM is configured or no tailscale key was set.
    Only emitted when the user has configured tailscale for their LLM endpoint.
    """
    if not local_llm or not (local_llm.get("tailscale_auth_key") or ""):
        return ""
    return '\nlog "Installing and authenticating Tailscale..."\n' + _install_tailscale_script() + "\n"


def _preflight_definitions(mode: str) -> str:
    """Return the `preflight_local_llm()` definition(s) for the bootstrap.
    For assisted mode, the cloud-fallback function definition is also
    emitted so the preflight can call it on a `cloud` Telegram callback.
    """
    if mode == "assisted":
        return _preflight_local_llm_script("assisted") + _switch_to_cloud_fallback_script()
    return _preflight_local_llm_script("autonomous")


def _preflight_block(local_llm: dict | None, mode: str) -> str:
    """Return the runtime call to `preflight_local_llm` (and any env vars
    it needs at call time) for the bootstrap.
    """
    if not local_llm:
        return ""
    if mode == "assisted":
        return (
            '\nlog "Probing local LLM before installing system packages..."\n'
            'MODE=assisted HAS_CLOUD_KEY=$([ -n "$OPENCODE_API_KEY" ] && echo true || echo false) '
            "preflight_local_llm\n\n"
        )
    return (
        '\nlog "Probing local LLM before installing system packages..."\n'
        "MODE=autonomous "
        "HAS_CLOUD_KEY=false "
        "preflight_local_llm\n\n"
    )


def _local_llm_log_line(local_llm: dict | None) -> str:
    """Emit the 'Local LLM configured: ...' diagnostic, or "" if no
    local LLM is in play.
    """
    if not local_llm:
        return ""
    return 'log "Local LLM configured: endpoint=$LOCAL_LLM_ENDPOINT, model=$LOCAL_LLM_MODEL"\n'
