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

# We do NOT globally redirect stdout/stderr — `docker logs` and the
# foreground terminal must see the entrypoint's progress. The watchdog's
# pickup file (/var/log/blitzlog/opencode.log) is written by log()
# below, which dual-writes to stderr and that file. Service processes
# (whisper-stt-shim, opencode serve, telegram-bot) have their own
# per-file redirects and are unaffected.
mkdir -p /var/log/blitzlog

# Pre-create the signal files so the container's touch won't fail with
# permission denied (the host owns /workspace, not the container user).
mkdir -p /workspace/.blitzlog
touch /workspace/.idle /workspace/.shutdown

log() {
    local msg
    msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    printf '%s\n' "$msg" >&2
    printf '%s\n' "$msg" >> /var/log/blitzlog/opencode.log
}

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

# --- 3. Derive model + provider config; write opencode.jsonc/json ---
# OPENCODE_MODEL is the slash-form "provider/model_id" string (e.g.
# "minimax-coding-plan/MiniMax-M3"). opencode parses this format
# directly. The provider prefix is extracted locally so we can register
# the API key for it. Local shell variables are used (no export) to
# avoid conflicts with opencode serve's environment.
#
# We write BOTH opencode.jsonc AND opencode.json. opencode's loader
# checks opencode.jsonc first when picking its primary config path, and
# loads .jsonc LAST in its merge order — so .jsonc wins if both exist.
# Writing both is defensive: if opencode ever changes its preference,
# our config is still picked up.
opencode_model="${OPENCODE_MODEL:-}"
opencode_api_key="${OPENCODE_API_KEY:-}"
opencode_provider=""
opencode_model_id=""
opencode_max_steps="${OPENCODE_AGENT_MAX_STEPS:-500}"
if [ -n "$opencode_model" ] && [ "${opencode_model#*/}" != "$opencode_model" ]; then
    opencode_provider="${opencode_model%%/*}"
    opencode_model_id="${opencode_model#*/}"
fi

# Agent block for the build agent. In assisted mode we add a
# comma + prompt field hinting at the shutdown tool (so the bot
# knows it can ask for shutdown). In autonomous mode the prompt
# is omitted to keep the config minimal. Mirrors the lambda's
# _write_opencode_config_script in lambda/scripts/_common.py:455.
if [ "${MODE:-autonomous}" = "assisted" ]; then
    agent_prompt=',
      "prompt": "You have a `shutdown` tool available. Use it when the user asks to shut down or terminate the instance."'
else
    agent_prompt=""
fi

mkdir -p /root/.config/opencode
if [ -n "${OPENCODE_CONFIG_JSON:-}" ]; then
    # Lambda pre-rendered the config (production path) — use verbatim
    printf '%s' "$OPENCODE_CONFIG_JSON" > /root/.config/opencode/opencode.jsonc
else
    # Fallback: render from env vars (local test path). Mirrors the
    # lambda's _write_opencode_config_script structure (default_agent,
    # compaction, agent.build.steps, provider). No baseURL needed —
    # minimax-coding-plan's endpoint is hardcoded in the opencode
    # binary.
    if [ -n "$opencode_provider" ] && [ -n "$opencode_api_key" ]; then
        cat > /root/.config/opencode/opencode.jsonc <<OPENCODE_CFG
{
  "\$schema": "https://opencode.ai/config.json",
  "model": "${opencode_model}",
  "default_agent": "build",
  "compaction": {
    "auto": false
  },
  "agent": {
    "build": {
      "steps": ${opencode_max_steps}${agent_prompt}
    }
  },
  "provider": {
    "${opencode_provider}": {
      "options": {
        "apiKey": "${opencode_api_key}"
      }
    }
  }
}
OPENCODE_CFG
        cp /root/.config/opencode/opencode.jsonc /root/.config/opencode/opencode.json
    elif [ -n "$opencode_model" ]; then
        cat > /root/.config/opencode/opencode.jsonc <<OPENCODE_CFG
{
  "\$schema": "https://opencode.ai/config.json",
  "model": "${opencode_model}",
  "default_agent": "build",
  "compaction": {
    "auto": false
  },
  "agent": {
    "build": {
      "steps": ${opencode_max_steps}${agent_prompt}
    }
  }
}
OPENCODE_CFG
        cp /root/.config/opencode/opencode.jsonc /root/.config/opencode/opencode.json
    fi
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

    # Generate server-side auth credentials. The bot uses these to
    # authenticate against opencode serve's /v1/* endpoints. Note the
    # ordering: export FIRST, then run opencode serve (which inherits
    # the env). The env vars do NOT need to be inlined on the opencode
    # serve command line because the shell already has them exported.
    OPENCODE_SERVER_USERNAME=agent
    OPENCODE_SERVER_PASSWORD="${OPENCODE_SERVER_PASSWORD:-$(openssl rand -hex 16)}"
    export OPENCODE_SERVER_USERNAME OPENCODE_SERVER_PASSWORD

    # Model config for the bot is derived locally from OPENCODE_MODEL
    # earlier (see block 3). The bot receives OPENCODE_MODEL_ID and
    # OPENCODE_MODEL_PROVIDER via the `setsid env ...` line below —
    # we deliberately do NOT export them globally so opencode serve
    # (running in this same shell) does not pick them up.

    # Start opencode serve via setsid so it gets its own process group
    # (independent of the entrypoint shell's controlling terminal).
    # This is the systemd-style daemonization the test was missing.
    setsid opencode serve --hostname 127.0.0.1 --port 4096 >>/var/log/opencode-serve.log 2>&1 &
    SERVE_PID=$!
    # Wait only for the port to be listening, not for /health (which
    # can take 30s+ if opencode is doing a real LLM-API connectivity
    # check). 5s is plenty for the Go binary to bind to port 4096.
    i=0
    while [ "$i" -lt 5 ]; do
        if curl -fs -u "agent:${OPENCODE_SERVER_PASSWORD}" \
               -o /dev/null \
               http://127.0.0.1:4096/ 2>/dev/null; then
            break
        fi
        i=$((i + 1))
        sleep 1
    done
    log "opencode serve ready (after ${i}s)"

    # --- Telegram bot (setsid + uses the env vars set above) ---
    log "Starting telegram bot"

    # --- Write bot's persistent .env at the default location ---
    # The bot's getInstalledAppHome() returns ~/.config/opencode-telegram-bot
    # on Linux (no APPDATA / XDG_CONFIG_HOME override). Writing here lets
    # the bot find its config without OPENCODE_TELEGRAM_HOME. The bot's
    # startup validation reads this file directly via dotenv.parse(), so
    # we must seed it before launching — setsid env vars alone are NOT
    # enough to skip the setup wizard.
    bot_env_dir="/root/.config/opencode-telegram-bot"
    mkdir -p "$bot_env_dir"
    {
        printf 'TELEGRAM_BOT_TOKEN=%s\n' "${TELEGRAM_BOT_TOKEN:-}"
        printf 'TELEGRAM_ALLOWED_USER_ID=%s\n' "${TELEGRAM_USER_ID:-}"
        printf 'OPENCODE_API_URL=http://127.0.0.1:4096\n'
        printf 'OPENCODE_SERVER_USERNAME=agent\n'
        printf 'OPENCODE_SERVER_PASSWORD=%s\n' "${OPENCODE_SERVER_PASSWORD:-}"
        printf 'OPENCODE_MODEL_PROVIDER=%s\n' "${opencode_provider:-}"
        printf 'OPENCODE_MODEL_ID=%s\n' "${opencode_model_id:-}"
        printf 'STT_API_URL=http://127.0.0.1:7878/v1\n'
        printf 'STT_API_KEY=%s\n' "${STT_API_KEY:-local-dev-not-validated}"
        printf 'STT_MODEL=%s\n' "${STT_MODEL:-tiny.en}"
        printf 'STT_LANGUAGE=%s\n' "${STT_LANGUAGE:-en}"
        printf 'BOT_LOCALE=%s\n' "${BOT_LOCALE:-en}"
    } > "$bot_env_dir/.env"
    chmod 600 "$bot_env_dir/.env"
    log "Wrote bot config to $bot_env_dir/.env"

    # setsid gives the bot its own process group (independent of the
    # entrypoint shell's controlling terminal). The bot inherits the
    # parent shell's env (TELEGRAM_BOT_TOKEN, STT_*, etc. from
    # --env-file) and reads everything else from $bot_env_dir/.env.
    setsid npx -y @grinev/opencode-telegram-bot@latest start >>/var/log/telegram-bot.log 2>&1 &
    BOT_PID=$!

    log "All services running; idle until SIGTERM"
    wait "$SHIM_PID" "$SERVE_PID" "$BOT_PID"
fi
