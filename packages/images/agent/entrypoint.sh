#!/bin/sh
# /usr/local/bin/entrypoint.sh — runs inside the blitzlog-agent container.
#
# Single bash entrypoint that branches on $MODE:
#   autonomous: start whisper-stt-shim, git clone target repo, install project
#               toolchain via mise, exec opencode run --agent build.
#   assisted:   start whisper-stt-shim + opencode serve + telegram-bot, idle
#               until SIGTERM.
#
# The container has NO AWS credentials. The host user-data reads SSM and
# passes every secret / config as an env var or mounted file. The
# /opt/whisper-stt/models/ directory is mounted read-only from the host.
#
# opencode CLI handles SIGTERM gracefully on its own (commits pending work,
# closes session). The container's only responsibility is to forward
# signals and exit cleanly.
set -eu

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }

MODE="${MODE:-autonomous}"

shutdown() {
    log "Caught signal; graceful shutdown"
    if [ -n "${SHIM_PID:-}" ]; then
        kill "$SHIM_PID" 2>/dev/null || true
    fi
    if [ -n "${SERVE_PID:-}" ]; then
        kill "$SERVE_PID" 2>/dev/null || true
    fi
    if [ -n "${BOT_PID:-}" ]; then
        kill "$BOT_PID" 2>/dev/null || true
    fi
    exit 0
}
trap shutdown TERM INT

# --- 1. Configure git credentials (mounted from host) ---
if [ -n "${GITHUB_TOKEN:-}" ]; then
    mkdir -p /root/.git-credentials.d
    printf 'https://x-access-token:%s@github.com\n' "$GITHUB_TOKEN" \
        > /root/.git-credentials.d/github
    chmod 600 /root/.git-credentials.d/github
    git config --global credential.helper 'store --file /root/.git-credentials.d/github'
fi
if [ -n "${GIT_USER_NAME:-}" ]; then
    git config --global user.name "${GIT_USER_NAME}"
    git config --global user.email "${GIT_USER_EMAIL:-}"
fi

# --- 2. Copy baked-in opencode plugins into the config dir ---
mkdir -p /root/.config/opencode/plugins
if [ -d /opt/blitzlog/plugins ]; then
    cp /opt/blitzlog/plugins/*.js /root/.config/opencode/plugins/ 2>/dev/null || true
fi

# --- 3. Write opencode.json from env vars (the host rendered this) ---
if [ -n "${OPENCODE_CONFIG_JSON:-}" ]; then
    printf '%s' "$OPENCODE_CONFIG_JSON" > /root/.config/opencode/opencode.json
fi

# --- 4. Verify whisper model is mounted (host pre-fetches from S3) ---
MODEL_FILE="ggml-${STT_MODEL:-base.en}.bin"
if [ ! -f "/opt/whisper-stt/models/$MODEL_FILE" ]; then
    log "ERROR: whisper model $MODEL_FILE not mounted from host"
    exit 1
fi
export WHISPER_MODEL="/opt/whisper-stt/models/$MODEL_FILE"
export WHISPER_LANGUAGE="${STT_LANGUAGE:-en}"

# --- 5. Start whisper-stt-shim (always) ---
log "Starting whisper-stt-shim..."
mkdir -p /var/log
python3 /opt/whisper-stt/server.py >>/var/log/whisper-stt-shim.log 2>&1 &
SHIM_PID=$!
i=0
while [ "$i" -lt 30 ]; do
    if wget -q -O - http://127.0.0.1:7878/healthz >/dev/null 2>&1 \
        || curl -fs http://127.0.0.1:7878/healthz >/dev/null 2>&1; then
        break
    fi
    i=$((i + 1))
    sleep 1
done
log "whisper-stt-shim ready (after ${i}s)"

if [ "$MODE" = "autonomous" ]; then
    # --- Autonomous: clone, install toolchain, run agent ---
    log "Autonomous mode: cloning ${REPO:-<unknown-repo>}"
    mkdir -p /workspace
    cd /workspace
    git clone "https://github.com/${REPO}.git" repo
    cd repo

    # Project-side bootstrap (mirrors the current _install_toolchain_script)
    if [ -f mise.toml ] || [ -f .tool-versions ]; then
        wget -q -O - https://mise.run | sh || curl -fsSL https://mise.run | sh
        export PATH="/root/.local/bin:$PATH"
        mise trust 2>/dev/null || true
        mise install -y
        if mise tasks --name-only 2>/dev/null | grep -qx bootstrap; then
            mise run bootstrap
        fi
    fi

    log "Launching opencode run --agent build"
    OPENCODE_NONINTERACTIVE=1 opencode run --agent build "${OPENCODE_PROMPT:-}" || EXIT=$?
    EXIT="${EXIT:-0}"
    log "opencode exited with $EXIT"
    kill "$SHIM_PID" 2>/dev/null || true
    exit "$EXIT"
else
    # --- Assisted: long-running opencode serve + telegram-bot ---
    log "Assisted mode: starting opencode serve"

    OPENCODE_SERVER_USERNAME=agent
    OPENCODE_SERVER_PASSWORD="${OPENCODE_SERVER_PASSWORD:-$(openssl rand -hex 16)}"
    export OPENCODE_SERVER_USERNAME OPENCODE_SERVER_PASSWORD

    opencode serve --hostname 127.0.0.1 --port 4096 >>/var/log/opencode-serve.log 2>&1 &
    SERVE_PID=$!
    i=0
    while [ "$i" -lt 30 ]; do
        if wget -q -O - http://127.0.0.1:4096/health >/dev/null 2>&1 \
            || curl -fs http://127.0.0.1:4096/health >/dev/null 2>&1; then
            break
        fi
        i=$((i + 1))
        sleep 1
    done
    log "opencode serve ready (after ${i}s)"

    # --- Telegram bot ---
    log "Starting telegram bot"
    # The bot is fetched by npx from the registry at first start. The host
    # passes all env vars (TELEGRAM_BOT_TOKEN, etc.). See packages/opencode-
    # telegram-bot upstream for the full env list.
    OPENCODE_API_URL=http://127.0.0.1:4096 \
    OPENCODE_SERVER_USERNAME=agent \
    OPENCODE_SERVER_PASSWORD="$OPENCODE_SERVER_PASSWORD" \
    STT_API_URL="http://127.0.0.1:7878/v1" \
    npx -y @grinev/opencode-telegram-bot@latest start >>/var/log/telegram-bot.log 2>&1 &
    BOT_PID=$!

    log "All services running; idle until SIGTERM"
    wait "$SHIM_PID" "$SERVE_PID" "$BOT_PID"
fi
