#!/bin/sh
# /usr/local/bin/entrypoint.sh — runs inside the blitzlog-agent container.
#
# Single bash entrypoint that branches on $MODE:
#   autonomous: git clone target repo, install project toolchain via mise,
#               exec opencode run --agent build.
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
# pipefail makes a pipeline's exit code reflect the first failing command,
# so the opencode exit code propagates through the tee below instead of
# tee always exiting 0.
set -o pipefail

# We do NOT globally redirect stdout/stderr — `docker logs` and the
# foreground terminal must see the entrypoint's progress. The watchdog's
# pickup file (/var/log/blitzlog/opencode.log) is written by log()
# below, which dual-writes to stderr and that file. Service processes
# (whisper-stt-shim, opencode serve, telegram-bot) have their own
# per-file redirects and are unaffected.
mkdir -p /var/log/blitzlog

# Pre-create .idle as a "container is alive" marker visible from the
# host. We do NOT pre-create .shutdown — that's only created when the
# agent's shutdown tool runs assisted-shutdown.sh (as its LAST step,
# after exporting the session). The host's watchdog/watcher script
# detects .shutdown by polling for its existence.
mkdir -p /workspace/.blitzlog
touch /workspace/.idle

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
    # Capture into a local var so we can unset the env var before
    # piping the token to `gh auth login --with-token`. gh refuses
    # --with-token while GITHUB_TOKEN is set in env. After this block,
    # git uses the credential helper (not env) and gh uses its own
    # stored credentials (not env), so leaving the env vars unset is
    # safe — no code downstream reads GITHUB_TOKEN from env.
    local_token="$GITHUB_TOKEN"
    mkdir -p /root/.git-credentials.d
    printf 'https://x-access-token:%s@github.com\n' "$local_token" \
        > /root/.git-credentials.d/github
    chmod 600 /root/.git-credentials.d/github
    git config --global credential.helper 'store --file /root/.git-credentials.d/github'
    # Authenticate gh CLI for `gh issue view` etc. gh does not support
    # unauthenticated access for any repo (public or private), so
    # without this it exits non-zero and aborts the entrypoint under
    # `set -e`. We unset GITHUB_TOKEN and GH_TOKEN first because gh
    # refuses --with-token while either is set in env. Token is piped
    # via stdin to keep it out of argv. We don't restore the env vars —
    # git uses the credential helper, gh reads from its stored
    # credentials.
    unset GITHUB_TOKEN GH_TOKEN
    printf '%s\n' "$local_token" | gh auth login --with-token --hostname github.com >/dev/null
fi
if [ -n "${GIT_USER_NAME:-}" ]; then
    git config --global user.name "${GIT_USER_NAME}"
    git config --global user.email "${GIT_USER_EMAIL:-}"
fi

# --- 2. (plugins/tools baked into the image by the Dockerfile) ---
# opencode plugins (idle_watchdog, periodic_autosave, session_archive,
# spot_watchdog) and tools (shutdown) are placed directly into
# /root/.config/opencode/{plugins,tools}/ by the Dockerfile. No runtime
# copy needed.

# --- 3. Derive model + provider config; write opencode.jsonc/json ---
# OPENCODE_MODEL is the slash-form "provider/model_id" string (e.g.
# "minimax-coding-plan/MiniMax-M3"). opencode parses this format
# directly. The provider prefix is extracted locally so we can register
# the API key for it. Local shell variables are used (no export) to
# avoid conflicts with opencode serve's environment.
#
# Local LLM support: when LOCAL_LLM_ENDPOINT is set, we emit a
# provider.local block (baseURL = the endpoint, model registered under
# provider.local.models) and skip the cloud provider entirely — mirrors
# the lambda's _write_opencode_config_script local_provider branch in
# lambda/scripts/_common.py:255-335. OPENCODE_API_KEY is intentionally
# not exported when a local LLM is configured so opencode has no cloud
# credentials to leak.
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

# Local LLM env (mirrors _local_llm_env_block in lambda/scripts/_common.py:42-65).
# When LOCAL_LLM_ENDPOINT is set, the opencode config below emits a
# provider.local block and skips the cloud provider.
local_llm_endpoint="${LOCAL_LLM_ENDPOINT:-}"
local_llm_model="${LOCAL_LLM_MODEL:-}"
local_llm_api_key="${LOCAL_LLM_API_KEY:-}"
local_llm_fallback="${LOCAL_LLM_FALLBACK:-closed}"

# Agent block for the build agent. In assisted mode we add a
# comma + prompt field hinting at the shutdown tool (so the bot
# knows it can ask for shutdown). In autonomous mode the prompt
# is omitted to keep the config minimal. Mirrors the lambda's
# _write_opencode_config_script in lambda/scripts/_common.py:455.
if [ "${MODE:-autonomous}" = "assisted" ]; then
    agent_prompt=',
      "prompt": "You have a shutdown tool available. Use it when the user asks to shut down or terminate the instance."'
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
    # compaction, agent.build.steps, provider).
    if [ -n "$local_llm_endpoint" ]; then
        # Local LLM path — emit provider.local, omit the cloud
        # provider entirely. The model id may contain a slash
        # (e.g. "qwen/qwen3.8-27b"); opencode's model parser uses
        # the FIRST slash as provider/model separator, so a model
        # like that parses as (provider=local, model=qwen/qwen3.8-27b)
        # and we must register it under the exact same key in
        # provider.models for opencode to find it. We shell-escape
        # the model id by wrapping in double quotes (the heredoc
        # already escapes the value).
        log "Configuring opencode for local LLM: $local_llm_endpoint (model=$local_llm_model)"
        if [ -n "$local_llm_api_key" ]; then
            cat > /root/.config/opencode/opencode.jsonc <<OPENCODE_CFG
{
  "\$schema": "https://opencode.ai/config.json",
  "model": "${local_llm_model}",
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
    "local": {
      "options": {
        "baseURL": "${local_llm_endpoint}",
        "apiKey": "${local_llm_api_key}"
      },
      "models": {
        "${local_llm_model}": {}
      }
    }
  }
}
OPENCODE_CFG
        else
            cat > /root/.config/opencode/opencode.jsonc <<OPENCODE_CFG
{
  "\$schema": "https://opencode.ai/config.json",
  "model": "${local_llm_model}",
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
    "local": {
      "options": {
        "baseURL": "${local_llm_endpoint}"
      },
      "models": {
        "${local_llm_model}": {}
      }
    }
  }
}
OPENCODE_CFG
        fi
        cp /root/.config/opencode/opencode.jsonc /root/.config/opencode/opencode.json
    elif [ -n "$opencode_provider" ] && [ -n "$opencode_api_key" ]; then
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

# --- 3.5 (optional): join Tailscale Tailnet if TAILSCALE_AUTH_KEY is set ---
# Lets the container reach endpoints on a private Tailnet (e.g., a
# local LLM at 100.x.y.z). The tailscale binary is in the image
# unconditionally (see Dockerfile); the connection is opt-in via env.
# Mirrors the lambda's _install_tailscale_script bootstrap snippet in
# lambda/scripts/_common.py:210-251, but inlined into the entrypoint so
# local testing gets the same behavior.
if [ -n "${TAILSCALE_AUTH_KEY:-}" ]; then
    log "TAILSCALE_AUTH_KEY set; starting tailscaled and joining Tailnet..."
    mkdir -p /var/lib/tailscale
    tailscaled --state=/var/lib/tailscale/tailscaled.state >>/var/log/blitzlog/tailscaled.log 2>&1 &
    TAILSCALED_PID=$!
    # tailscale up is what authenticates and flips the daemon from
    # NeedsLogin to Running. Call it FIRST, then wait for the state to
    # settle. (Earlier we had this inverted and the loop timed out
    # before we ever called tailscale up — the daemon stayed at
    # NeedsLogin and never authenticated.)
    TSC_HOSTNAME="blitzlog-agent-${ISSUE_NUMBER:-local}-$(date +%s)"
    if tailscale up --authkey="$TAILSCALE_AUTH_KEY" \
            --hostname="$TSC_HOSTNAME" \
            --accept-routes=false >>/var/log/blitzlog/tailscaled.log 2>&1; then
        for i in $(seq 1 30); do
            STATE=$(tailscale status --json 2>/dev/null \
                | python3 -c "import sys,json; print(json.load(sys.stdin).get('BackendState',''))" 2>/dev/null || echo "")
            if [ "$STATE" = "Running" ]; then break; fi
            sleep 2
        done
        TAILSCALE_IP=$(tailscale ip -4 2>/dev/null | head -1 || echo "")
        log "Tailscale connected: hostname=$TSC_HOSTNAME, ip=$TAILSCALE_IP"
    else
        log "WARNING: tailscale up failed; local-LLM preflight will likely fail if endpoint is on the Tailnet"
    fi
fi

# --- 3.6 (optional): local LLM preflight ---
# If LOCAL_LLM_ENDPOINT is set, probe the endpoint before launching
# opencode. Mirrors the lambda's _preflight_local_llm_script in
# lambda/scripts/_common.py:340-475. Behavior differs by mode:
#   autonomous: 10x retry @ 30s; exit 1 on failure (no Telegram, no
#              fallback — the run is unrecoverable).
#   assisted:   5x retry @ 30s; send Telegram prompt with retry/cloud/
#              abort options. If LOCAL_LLM_FALLBACK=cloud and a cloud
#              key is set, the cloud option rewrites opencode.json to
#              use the cloud provider and restarts opencode serve.
preflight_local_llm() {
    local endpoint="${LOCAL_LLM_ENDPOINT}"
    local max_retries="${LOCAL_LLM_MAX_RETRIES:-10}"
    log "Preflight: probing local LLM at $endpoint (mode=${MODE})"

    local attempt
    for attempt in $(seq 1 "$max_retries"); do
        # Probe /health and /v1/models in turn. Auth header is only
        # added when LOCAL_LLM_API_KEY is non-empty (the array form
        # `local auth_args=(...)` is bash-only — POSIX sh doesn't allow
        # empty array assignments, so we duplicate the curl calls
        # below instead).
        if [ -n "${LOCAL_LLM_API_KEY:-}" ]; then
            if curl -sf -m 10 -H "Authorization: Bearer ${LOCAL_LLM_API_KEY}" "$endpoint/health" >/dev/null 2>&1 \
            || curl -sf -m 10 -H "Authorization: Bearer ${LOCAL_LLM_API_KEY}" "$endpoint/v1/models" >/dev/null 2>&1; then
                log "Local LLM reachable (attempt $attempt/$max_retries)"
                return 0
            fi
        else
            if curl -sf -m 10 "$endpoint/health" >/dev/null 2>&1 \
            || curl -sf -m 10 "$endpoint/v1/models" >/dev/null 2>&1; then
                log "Local LLM reachable (attempt $attempt/$max_retries)"
                return 0
            fi
        fi
        log "Local LLM probe failed (attempt $attempt/$max_retries), sleeping 30s..."
        sleep 30
    done

    log "ACTIONABLE: Local LLM unreachable after $max_retries attempts (~$((max_retries / 2)) min)"

    if [ "${MODE}" = "autonomous" ]; then
        log "ACTIONABLE: Autonomous mode aborting — local LLM unreachable."
        exit 1
    fi

    # Assisted: notify user via Telegram, optionally fall back to cloud.
    local bot_token="${TELEGRAM_BOT_TOKEN:-}"
    local user_id="${TELEGRAM_USER_ID:-}"
    if [ -z "$bot_token" ] || [ -z "$user_id" ]; then
        log "ACTIONABLE: Telegram credentials missing; aborting."
        exit 1
    fi

    local reply_markup
    local has_cloud_key="false"
    if [ -n "${OPENCODE_API_KEY:-}" ]; then has_cloud_key="true"; fi
    if [ "${LOCAL_LLM_FALLBACK:-closed}" = "cloud" ] && [ "$has_cloud_key" = "true" ]; then
        reply_markup='{"inline_keyboard":[[{"text":"Use cloud fallback","callback_data":"cloud"},{"text":"Retry","callback_data":"retry"},{"text":"Abort","callback_data":"abort"}]]}'
    else
        reply_markup='{"inline_keyboard":[[{"text":"Retry","callback_data":"retry"},{"text":"Abort","callback_data":"abort"}]]}'
    fi

    local text="Local LLM unreachable after $((max_retries / 2)) min. The agent cannot reach your local endpoint. Pick an action:"
    if ! curl -sf -X POST "https://api.telegram.org/bot${bot_token}/sendMessage" \
        -d "chat_id=${user_id}" \
        --data-urlencode "text=$text" \
        --data-urlencode "reply_markup=$reply_markup" >/dev/null; then
        log "ACTIONABLE: Failed to send Telegram prompt; aborting."
        exit 1
    fi

    log "Sent Telegram prompt; waiting up to 10 min for user reply..."
    local callback=""
    local waited=0
    while [ -z "$callback" ] && [ "$waited" -lt 600 ]; do
        callback=$(curl -sf "https://api.telegram.org/bot${bot_token}/getUpdates?offset=-1&timeout=30" 2>/dev/null \
            | python3 -c "import sys,json
data=json.load(sys.stdin).get('result', [])
for u in data:
    cb=u.get('callback_query',{})
    if cb.get('data') in ('cloud','retry','abort'): print(cb['data']); break" 2>/dev/null || echo "")
        if [ -z "$callback" ]; then
            waited=$((waited + 30))
        fi
    done

    case "$callback" in
        cloud)
            if [ "$has_cloud_key" = "true" ]; then
                log "User chose cloud fallback; switching inference to cloud"
                # Re-emit the cloud opencode.json and restart opencode serve
                cat > /root/.config/opencode/opencode.jsonc <<OPENCODE_CFG
{
  "\$schema": "https://opencode.ai/config.json",
  "model": "${opencode_model}",
  "default_agent": "build",
  "compaction": { "auto": false },
  "agent": {
    "build": {
      "steps": ${opencode_max_steps}${agent_prompt}
    }
  },
  "provider": {
    "${opencode_provider}": {
      "options": { "apiKey": "${OPENCODE_API_KEY}" }
    }
  }
}
OPENCODE_CFG
                cp /root/.config/opencode/opencode.jsonc /root/.config/opencode/opencode.json
                # Restart opencode serve (assisted mode only)
                if [ -n "${SERVE_PID:-}" ] && kill -0 "$SERVE_PID" 2>/dev/null; then
                    kill "$SERVE_PID" 2>/dev/null || true
                    wait "$SERVE_PID" 2>/dev/null || true
                fi
                OPENCODE_SERVER_USERNAME=agent
                OPENCODE_SERVER_PASSWORD="${OPENCODE_SERVER_PASSWORD:-$(openssl rand -hex 16)}"
                export OPENCODE_SERVER_USERNAME OPENCODE_SERVER_PASSWORD
                setsid opencode serve --hostname 127.0.0.1 --port 4096 >>/var/log/blitzlog/opencode-serve.log 2>&1 &
                SERVE_PID=$!
                curl -s -X POST "https://api.telegram.org/bot${bot_token}/sendMessage" \
                    -d chat_id="${user_id}" \
                    -d text="Switched to cloud fallback (user request). Inference now going to ${opencode_model}." || true
                log "Cloud fallback active. SERVE_PID=$SERVE_PID"
            else
                log "User chose cloud but OPENCODE_API_KEY is unset; aborting"
                /usr/local/bin/assisted-shutdown.sh
                exit 1
            fi
            ;;
        retry)
            log "User chose retry; re-probing local LLM"
            return 1  # signal caller to retry
            ;;
        abort|*)
            log "User chose abort (or no response); shutting down"
            /usr/local/bin/assisted-shutdown.sh
            exit 1
            ;;
    esac
    return 0
}

if [ -n "${LOCAL_LLM_ENDPOINT:-}" ]; then
    # Retry the preflight + Telegram prompt loop indefinitely in assisted
    # mode (user can retry via Telegram); abort in autonomous on the
    # first failure.
    if [ "${MODE}" = "autonomous" ]; then
        preflight_local_llm || exit 1
    else
        # Assisted: retry on user "retry" callback
        until preflight_local_llm; do :; done
    fi
fi

# --- 4 + 5 (assisted mode only): verify whisper model + start shim ---
# The whisper-stt-shim is only used by the telegram bot for voice-message
# transcription. Autonomous mode runs opencode directly and never talks
# to the shim, so we skip both the model-mount check and the shim
# startup. The shim's binary and the bundled ggml models stay in the
# image (the Dockerfile always installs them) — they're just not run.
if [ "$MODE" = "assisted" ]; then
    MODEL_FILE="ggml-${STT_MODEL:-base.en}.bin"
    if [ ! -f "/opt/whisper-stt/models/$MODEL_FILE" ]; then
        log "ERROR: whisper model $MODEL_FILE not mounted from host"
        exit 1
    fi
    export WHISPER_MODEL="/opt/whisper-stt/models/$MODEL_FILE"
    export WHISPER_LANGUAGE="${STT_LANGUAGE:-en}"

    log "Starting whisper-stt-shim..."
    mkdir -p /var/log
    python3 /opt/whisper-stt/server.py >>/var/log/blitzlog/whisper-stt-shim.log 2>&1 &
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
fi

if [ "$MODE" = "autonomous" ]; then
    # --- Autonomous: clone, install toolchain, run agent ---
    log "Autonomous mode: cloning ${REPO:-<unknown-repo>}"
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
    # Tee opencode's stdout+stderr to a host-visible log file
    # (/var/log/blitzlog/opencode-run.log → /tmp/blitzlog-logs/ on the
    # host) while still streaming live to the user's terminal. PIPESTATUS
    # gives opencode's exit code (tee's exit is ignored), and pipefail
    # propagates a non-zero opencode exit through the || below.
    OPENCODE_NONINTERACTIVE=1 opencode run --agent build "${OPENCODE_PROMPT:-}" \
        2>&1 | tee /var/log/blitzlog/opencode-run.log || EXIT="${PIPESTATUS[0]}"
    EXIT="${EXIT:-0}"
    log "opencode exited with $EXIT"
    exit "$EXIT"
else
    # --- Assisted: long-running opencode serve + telegram-bot ---
    log "Assisted mode: cloning ${REPO:-<unknown-repo>}"
    # Bot config dir holds settings.json (auto-select below) and .env
    # (bot startup below). Both write paths need the dir to exist.
    mkdir -p /root/.config/opencode-telegram-bot
    cd /workspace
    if [ ! -d repo/.git ]; then
        git clone "https://github.com/${REPO}.git" repo
    else
        log "Workspace already cloned; skipping"
        cd repo
        git fetch origin 2>/dev/null || true
    fi
    cd repo

    # Project-side bootstrap (mirrors autonomous mode and main's pre-docker
    # bootstrap). mise installs the toolchain pinned in mise.toml so
    # /workspace/repo can be developed with the same tooling locally.
    if [ -f mise.toml ] || [ -f .tool-versions ]; then
        wget -q -O - https://mise.run | sh || curl -fsSL https://mise.run | sh
        export PATH="/root/.local/bin:$PATH"
        mise trust 2>/dev/null || true
        mise install -y
        if mise tasks --name-only 2>/dev/null | grep -qx bootstrap; then
            mise run bootstrap
        fi
    fi

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
    # Run from /workspace/repo so opencode serve can auto-discover the
    # project at the /project endpoint (matches main's pre-docker flow).
    cd /workspace/repo
    setsid opencode serve --hostname 127.0.0.1 --port 4096 >>/var/log/blitzlog/opencode-serve.log 2>&1 &
    SERVE_PID=$!
    # Wait for opencode serve to be ready. Matches the pre-Docker lambda
    # bootstrap's 30 × 2 s = 60 s budget — gives project discovery time
    # to populate /project before we curl it; otherwise the bot starts
    # without a pre-selected project and the user has to run /projects
    # manually. Pre-Docker flow had this same 60 s budget (lambda/handler.py
    # at commit 0a8edcb); it was inadvertently tightened to 5 s when the
    # logic moved into the container entrypoint (commit c7d914a).
    i=0
    while [ "$i" -lt 30 ]; do
        if curl -fs -u "agent:${OPENCODE_SERVER_PASSWORD}" \
               -o /dev/null \
               http://127.0.0.1:4096/ 2>/dev/null; then
            break
        fi
        i=$((i + 1))
        sleep 2
    done
    log "opencode serve ready (after ${i}s)"

    # Auto-select project (and session if resuming) in the bot's settings.
    # Matches main's pre-docker-refactor flow: the bootstrap queried
    # opencode serve at /project for a project whose worktree matches
    # /workspace, and wrote /root/.config/opencode-telegram-bot/settings.json
    # so the bot pre-selects the project when it starts (no /projects
    # prompt required from the user).
    log "Auto-selecting project and session in bot settings..."
    PROJECT_JSON=$(curl -sf -u "agent:${OPENCODE_SERVER_PASSWORD}" http://127.0.0.1:4096/project 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
for p in data if isinstance(data, list) else [data]:
    if p.get('worktree','').startswith('/workspace'):
        print(json.dumps({'id': p['id'], 'worktree': p['worktree'], 'name': p.get('name', p['worktree'])}))
        break
" 2>/dev/null || echo "")

    if [ -n "$PROJECT_JSON" ]; then
        if [ "${OPENCODE_RESUMED:-}" = "true" ] && [ -f /tmp/session-import.json ]; then
            # Resume requires an active session ID; the host bootstrap
            # downloaded the session JSON to /tmp before this container
            # started (this file is on the host's /tmp, NOT mounted — see
            # the follow-up to also mount /tmp or move the file).
            RESTORE_SESSION_ID=$(python3 -c "import json; print(json.load(open('/tmp/session-import.json')).get('id',''))" 2>/dev/null || echo "")
            if [ -n "$RESTORE_SESSION_ID" ]; then
                SESSION_TITLE=$(curl -sf -u "agent:${OPENCODE_SERVER_PASSWORD}" "http://127.0.0.1:4096/session/${RESTORE_SESSION_ID}" 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(json.dumps({'id': d['id'], 'title': d.get('title',''), 'directory': d.get('directory','')}))
" 2>/dev/null || echo "")
                if [ -n "$SESSION_TITLE" ]; then
                    cat > /root/.config/opencode-telegram-bot/settings.json <<SETTINGS_EOF
{"currentProject": $PROJECT_JSON, "currentSession": $SESSION_TITLE}
SETTINGS_EOF
                    log "Project and session pre-selected (resumed)"
                else
                    cat > /root/.config/opencode-telegram-bot/settings.json <<SETTINGS_EOF
{"currentProject": $PROJECT_JSON}
SETTINGS_EOF
                    log "Project pre-selected (new session); session resume unavailable"
                fi
            else
                cat > /root/.config/opencode-telegram-bot/settings.json <<SETTINGS_EOF
{"currentProject": $PROJECT_JSON}
SETTINGS_EOF
                log "Project pre-selected (new session); resume session ID missing"
            fi
        else
            cat > /root/.config/opencode-telegram-bot/settings.json <<SETTINGS_EOF
{"currentProject": $PROJECT_JSON}
SETTINGS_EOF
            log "Project pre-selected (new session)"
        fi
    else
        log "WARNING: Could not auto-select project, user will need /projects"
    fi

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
    # When LOCAL_LLM_ENDPOINT is set, the bot should ask opencode for the
    # local provider/model — not whatever OPENCODE_MODEL says (which is
    # typically a cloud model). Override provider/model_id accordingly.
    if [ -n "${LOCAL_LLM_ENDPOINT:-}" ]; then
        bot_provider="local"
        bot_model_id="${LOCAL_LLM_MODEL:-}"
    else
        bot_provider="${opencode_provider:-}"
        bot_model_id="${opencode_model_id:-}"
    fi
    {
        printf 'TELEGRAM_BOT_TOKEN=%s\n' "${TELEGRAM_BOT_TOKEN:-}"
        printf 'TELEGRAM_ALLOWED_USER_ID=%s\n' "${TELEGRAM_USER_ID:-}"
        printf 'OPENCODE_API_URL=http://127.0.0.1:4096\n'
        printf 'OPENCODE_SERVER_USERNAME=agent\n'
        printf 'OPENCODE_SERVER_PASSWORD=%s\n' "${OPENCODE_SERVER_PASSWORD:-}"
        printf 'OPENCODE_MODEL_PROVIDER=%s\n' "${bot_provider}"
        printf 'OPENCODE_MODEL_ID=%s\n' "${bot_model_id}"
        printf 'STT_API_URL=http://127.0.0.1:7878/v1\n'
        printf 'STT_API_KEY=%s\n' "${STT_API_KEY:-local-dev-not-validated}"
        printf 'STT_MODEL=%s\n' "${STT_MODEL:-tiny.en}"
        printf 'STT_LANGUAGE=%s\n' "${STT_LANGUAGE:-en}"
        printf 'BOT_LOCALE=%s\n' "${BOT_LOCALE:-en}"
    } > "$bot_env_dir/.env"
    chmod 600 "$bot_env_dir/.env"
    log "Wrote bot config to $bot_env_dir/.env"

    # The Dockerfile installs @grinev/opencode-telegram-bot globally via
    # `npm install -g`, so the binary is already on PATH. The old pre-warm
    # called `npx ... status` to populate the npx cache, but `status` runs
    # an interactive / long-poll health check that never returns during
    # pre-launch (it reports "Service status: stopped" and then waits
    # indefinitely). Just verify the binary is on PATH instead — instant
    # and matches the actual contract.
    log "Checking for opencode-telegram-bot binary..."
    PRE_WARM_EXIT=0
    if ! command -v opencode-telegram >/dev/null 2>&1; then
        PRE_WARM_EXIT=1
        log "WARNING: opencode-telegram-bot binary not on PATH; bot start will likely fail"
    fi

    # Fetch the issue title for the Telegram notification body. gh is in
    # the container runtime image (added in commit c7bfcc4). The
    # OPENCODE_ISSUE_TITLE env override is provided for local testing
    # (when `gh` may be rate-limited or unauthenticated) and any other
    # deployment that already knows the title upstream.
    ISSUE_TITLE="${OPENCODE_ISSUE_TITLE:-}"
    if [ -z "$ISSUE_TITLE" ]; then
        if ISSUE_TITLE=$(gh issue view "$ISSUE_NUMBER" --repo "$REPO" --json title --jq .title 2>/dev/null); then
            :
        else
            log "WARNING: gh issue view failed (gh may need 'gh auth login' or GH_TOKEN); using 'unknown'"
            ISSUE_TITLE="unknown"
        fi
    fi

    # Build the resume-status suffix for the notification (matches main).
    RESUME_STATUS=""
    if [ "${OPENCODE_RESUMED:-}" = "true" ] && [ -n "${OPENCODE_RESUMED_TITLE:-}" ]; then
        RESUME_STATUS="\n\nResumed session: ${OPENCODE_RESUMED_TITLE}"
    fi

    # Send a proactive "agent ready" notification to the user's Telegram
    # chat. Matches the pre-docker-refactor behavior from main: the
    # bootstrap issued this curl right before starting the bot. Moving it
    # to the entrypoint keeps the behavior identical because the
    # container has the same env vars (TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID,
    # TELEGRAM_BOT_NAME, OPENCODE_RESUMED_*) the bootstrap used to have.
    log "Sending Telegram ready notification"
    if [ "${PRE_WARM_EXIT:-0}" -ne 0 ]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="${TELEGRAM_USER_ID}" \
            -d parse_mode="Markdown" \
            -d text="Assisted agent cannot be started [Bot: ${TELEGRAM_BOT_NAME}]

Repo: ${REPO}
[Issue #${ISSUE_NUMBER}: ${ISSUE_TITLE}](https://github.com/${REPO}/issues/${ISSUE_NUMBER})
Mode: Assisted (interactive via Telegram)${RESUME_STATUS}" \
            || true
    else
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="${TELEGRAM_USER_ID}" \
            -d parse_mode="Markdown" \
            -d text="Assisted agent ready [Bot: ${TELEGRAM_BOT_NAME}]

Repo: ${REPO}
[Issue #${ISSUE_NUMBER}: ${ISSUE_TITLE}](https://github.com/${REPO}/issues/${ISSUE_NUMBER})
Mode: Assisted (interactive via Telegram)${RESUME_STATUS}

Connect to this bot to start working on the task." \
            || true
    fi

    # setsid gives the bot its own process group (independent of the
    # entrypoint shell's controlling terminal). The bot inherits the
    # parent shell's env (TELEGRAM_BOT_TOKEN, STT_*, etc. from
    # --env-file) and reads everything else from $bot_env_dir/.env.
    setsid npx -y @grinev/opencode-telegram-bot@latest start >>/var/log/blitzlog/telegram-bot.log 2>&1 &
    BOT_PID=$!

    log "All services running; idle until SIGTERM"
    wait "$SHIM_PID" "$SERVE_PID" "$BOT_PID"
fi
