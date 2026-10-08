#!/bin/bash
# /usr/local/bin/watchdog.sh — HOST-SIDE lifecycle manager for the
# blitzlog-agent container.
#
# Started by the blitzlog-agent systemd unit on instance boot. The
# watchdog:
#   1. Loads the pre-baked agent image into dockerd
#   2. Starts the container with the right env vars + volume mounts
#   3. Waits for the container to exit (any reason: shutdown tool,
#      idle, opencode finished, spot interruption, etc.)
#   4. Uploads container + host logs to S3
#   5. Uploads session artifacts the container wrote to /workspace/.blitzlog/
#   6. Releases the bot-pool lock
#   7. Terminates the EC2 instance
#
# The container has NO AWS credentials — all S3 work happens here on the
# host, which has the EC2 instance role.

set -euo pipefail
# Packer-baked signature: identifies the single agent image loaded
# into dockerd. /root/blitzlog.env is written by the Packer build
# alongside the image save step, with chmod 400 root:root. We source
# it BEFORE /etc/blitzlog.env so AGENT_IMAGE_REPO / AGENT_IMAGE_TAG
# are available when we build the docker run command.
source /root/blitzlog.env
source /etc/blitzlog.env
export HOME=/root

LOG_FILE="/var/log/backend-bootstrap.log"
log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" | tee -a "$LOG_FILE"; }

# Load the baked agent image
sudo docker load -i /opt/blitzlog/images/blitzlog-agent.tar.gz

# Pre-create .blitzlog (owned by host, so container's writes can't fail
# with permission denied) and .idle as a "container is alive" marker.
# We do NOT pre-create .shutdown — that's only created when the agent's
# shutdown tool runs assisted-shutdown.sh (as its LAST step, after
# exporting the session). This avoids a race against the watchdog's
# own exit-detection logic.
mkdir -p /workspace/.blitzlog
touch /workspace/.idle

# Compute IMDSv2 token + region + instance id (host AWS creds via role)
TOKEN=$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/instance-id)
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/dynamic/instance-identity/document \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['region'])")
export AWS_REGION="$REGION"

# Write the env file the container reads (and that shutdown.sh sources).
# Tailscale + local LLM env vars are passed through so the container
# can join a Tailnet (if TAILSCALE_AUTH_KEY is set) and point opencode
# at a local-LLM endpoint (if LOCAL_LLM_ENDPOINT is set). The container's
# entrypoint handles the actual setup; the bootstrap is just plumbing.
cat > /etc/blitzlog.env <<ENVEOF
MODE="${MODE:-assisted}"
ISSUE_NUMBER="${ISSUE_NUMBER}"
REPO="${REPO}"
BLITZLOG_ENV="${BLITZLOG_ENV}"
S3_LOGS_BUCKET="${S3_LOGS_BUCKET}"
SESSION_ARCHIVE_BUCKET="${SESSION_ARCHIVE_BUCKET}"
SESSION_ARCHIVE_PREFIX="${SESSION_ARCHIVE_PREFIX}"
S3_LOG_PREFIX="${S3_LOG_PREFIX}"
OPENCODE_API_KEY="${OPENCODE_API_KEY}"
OPENCODE_MODEL="${OPENCODE_MODEL}"
OPENCODE_PROMPT="${OPENCODE_PROMPT:-}"
LOCAL_LLM_ENDPOINT="${LOCAL_LLM_ENDPOINT:-}"
LOCAL_LLM_MODEL="${LOCAL_LLM_MODEL:-}"
LOCAL_LLM_API_KEY="${LOCAL_LLM_API_KEY:-}"
LOCAL_LLM_FALLBACK="${LOCAL_LLM_FALLBACK:-closed}"
TAILSCALE_AUTH_KEY="${TAILSCALE_AUTH_KEY:-}"
OPENCODE_SERVER_USERNAME="${OPENCODE_SERVER_USERNAME:-agent}"
OPENCODE_SERVER_PASSWORD="${OPENCODE_SERVER_PASSWORD:-}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_USER_ID="${TELEGRAM_USER_ID:-}"
TELEGRAM_BOT_NAME="${TELEGRAM_BOT_NAME:-}"
TELEGRAM_SENDER_LOGIN="${TELEGRAM_SENDER_LOGIN:-}"
STT_MODEL="${STT_MODEL:-base.en}"
STT_LANGUAGE="${STT_LANGUAGE:-en}"
AWS_REGION="${REGION}"
ENVEOF

# Pull the GitHub token from SSM (autonomous mode needs it for git push)
if [ -n "${GITHUB_TOKEN_SSM_PARAM:-}" ]; then
    GITHUB_TOKEN=$(aws ssm get-parameter \
        --name "${GITHUB_TOKEN_SSM_PARAM}" \
        --with-decryption \
        --query Parameter.Value \
        --output text \
        --region "$REGION" 2>/dev/null || echo "")
    if [ -n "$GITHUB_TOKEN" ]; then
        mkdir -p /root/.git-credentials.d
        printf 'https://x-access-token:%s@github.com\n' "$GITHUB_TOKEN" \
            > /root/.git-credentials.d/github
        chmod 600 /root/.git-credentials.d/github
        git config --global credential.helper \
            'store --file /root/.git-credentials.d/github'
    fi
fi

# Random opencode server password — written to env, then read by container
OPENCODE_SERVER_PASSWORD=$(openssl rand -hex 16)
export OPENCODE_SERVER_PASSWORD
echo "OPENCODE_SERVER_PASSWORD=$OPENCODE_SERVER_PASSWORD" >> /etc/blitzlog.env

# Start the blitzlog-agent container. /var/log/blitzlog and /workspace
# are host-mounted so the container's logs and session artifacts are
# accessible to the watchdog after the container exits.
mkdir -p /var/log/blitzlog
mkdir -p /workspace

# Common env vars (passed to every container launch).
COMMON_ARGS=(
    -e "MODE=$MODE"
    -e "ISSUE_NUMBER=$ISSUE_NUMBER"
    -e "REPO=$REPO"
    -e "OPENCODE_MODEL=$OPENCODE_MODEL"
    -e "OPENCODE_PROMPT=$OPENCODE_PROMPT"
    -e "OPENCODE_API_KEY=$OPENCODE_API_KEY"
    -e "LOCAL_LLM_ENDPOINT=$LOCAL_LLM_ENDPOINT"
    -e "LOCAL_LLM_MODEL=$LOCAL_LLM_MODEL"
    -e "LOCAL_LLM_API_KEY=$LOCAL_LLM_API_KEY"
    -e "LOCAL_LLM_FALLBACK=$LOCAL_LLM_FALLBACK"
    -e "TAILSCALE_AUTH_KEY=$TAILSCALE_AUTH_KEY"
    -e "OPENCODE_SERVER_USERNAME=$OPENCODE_SERVER_USERNAME"
    -e "OPENCODE_SERVER_PASSWORD=$OPENCODE_SERVER_PASSWORD"
    -e "BLITZLOG_ENV=$BLITZLOG_ENV"
    -e "S3_LOGS_BUCKET=$S3_LOGS_BUCKET"
    -e "SESSION_ARCHIVE_BUCKET=$SESSION_ARCHIVE_BUCKET"
    -e "SESSION_ARCHIVE_PREFIX=$SESSION_ARCHIVE_PREFIX"
    -e "S3_LOG_PREFIX=$S3_LOG_PREFIX"
    -e "GITHUB_TOKEN=$GITHUB_TOKEN"
)

# Mode-specific env vars. We don't reference these outside the
# matching branch — `set -u` would otherwise kill the script when a
# var that isn't declared in /etc/blitzlog.env for the current mode
# is read. This is intentional: a future contributor adding a var the
# watchdog should pass gets a loud failure, not a silent empty value.
if [ "$MODE" = "assisted" ]; then
    COMMON_ARGS+=(
        -e "STT_MODEL=$STT_MODEL"
        -e "STT_LANGUAGE=$STT_LANGUAGE"
        -e "TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN"
        -e "TELEGRAM_BOT_NAME=$TELEGRAM_BOT_NAME"
        -e "TELEGRAM_USER_ID=$TELEGRAM_USER_ID"
    )
fi

# Tailscale needs CAP_NET_ADMIN (to write iptables rules) and a TUN
# device (for the wireguard tunnel). Both are only needed when
# TAILSCALE_AUTH_KEY is set, since tailscaled only runs in that case.
# Build a conditional array — least privilege means we don't grant
# capabilities the container doesn't use.
TUNNEL_FLAGS=()
if [ -n "${TAILSCALE_AUTH_KEY:-}" ]; then
    TUNNEL_FLAGS=(--cap-add=NET_ADMIN --cap-add=NET_RAW)
fi

# Host-side .shutdown watcher. Mirrors the local mise.toml
# image-telegram-live-assisted watcher. Polls for the agent's shutdown
# signal (touched by assisted-shutdown.sh, both for the user-invoked
# shutdown tool and for spot-interrupt emergency save) and SIGTERMs the
# container — otherwise the entrypoint's TERM trap is never triggered and
# the `docker run` below blocks until AWS reclaims it.
(
    while [ ! -f /workspace/.shutdown ]; do
            sleep 1
    done
    log ".shutdown detected; sending SIGTERM to blitzlog-agent"
    sudo docker kill --signal TERM blitzlog-agent 2>/dev/null || true
) &
WATCHER_PID=$!

# Defense in depth: clear any stale .shutdown left over from a crashed
# previous run. watchdog.sh no longer pre-creates it, so on a fresh
# instance .shutdown only ever appears because the agent's shutdown
# tool or the spot_watchdog plugin just ran assisted-shutdown.sh.
rm -f /workspace/.shutdown

sudo docker run --name blitzlog-agent \
    "${TUNNEL_FLAGS[@]}" \
    "${COMMON_ARGS[@]}" \
    -v /opt/whisper-stt/models:/opt/whisper-stt/models:ro \
    -v /root/.git-credentials.d:/root/.git-credentials.d \
    -v /workspace:/workspace \
    -v /var/log/blitzlog:/var/log/blitzlog \
    "${AGENT_IMAGE_REPO}:${AGENT_IMAGE_TAG}"

CONTAINER_EXIT=$?

# The watcher auto-exits once it sends SIGTERM (the while loop ends
# when .shutdown appears). If .shutdown was never touched (autonomous
# mode finished, agent ran to completion), the watcher is still
# polling — kill it explicitly.
kill "$WATCHER_PID" 2>/dev/null || true
wait "$WATCHER_PID" 2>/dev/null || true

log "Container exited with code $CONTAINER_EXIT"

# Decode known LLM-provider API errors into actionable log lines.
if [ -f /var/log/blitzlog/opencode.log ] && grep -qE "insufficient_balance|insufficient balance|\(1008\)" /var/log/blitzlog/opencode.log; then
    log "ACTIONABLE: LLM provider returned insufficient balance / HTTP 1008."
    log "ACTIONABLE: The token-plan account for $OPENCODE_MODEL has zero credits."
    log "ACTIONABLE: Top up at the provider console before retrying."
fi
if [ -f /var/log/blitzlog/opencode.log ] && grep -qE "Unauthorized|invalid_api_key|\(401\)" /var/log/blitzlog/opencode.log; then
    log "ACTIONABLE: LLM provider rejected the API key (HTTP 401)."
    log "ACTIONABLE: Rotate /blitzlog/$BLITZLOG_ENV/opencode/api-key in SSM and re-run terraform apply."
fi
if [ -f /var/log/blitzlog/opencode.log ] && grep -qE "rate.?limit|quota.?exceeded|too.?many.?requests|\(429\)" /var/log/blitzlog/opencode.log; then
    log "ACTIONABLE: LLM provider returned a rate-limit error (HTTP 429)."
    log "ACTIONABLE: Wait for the quota window to reset, then re-trigger."
fi

# Stamp every object from this run with the same timestamp+instance prefix,
# so all files from one EC2 run sort together and never overwrite each
# other (or a previous run's files on the same issue).
RUN_TS=$(date -u +%Y%m%d_%H%M)
RUN_TAG="${RUN_TS}_i${INSTANCE_ID}"

# Defensive default for AMIs baked before S3_LOG_PREFIX was added to the
# bootstrap env file. Falls back to the legacy flat env/logs/ path so a
# mid-deploy window (new lambda, old AMI) doesn't produce double-slash
# keys.
: "${S3_LOG_PREFIX:=${BLITZLOG_ENV}/logs}"

# Upload container + host logs to S3 (BOTH per user request).
for log in /var/log/blitzlog/*.log /var/log/backend-bootstrap.log; do
    [ -f "$log" ] || continue
    log_name=$(basename "$log")
    log "Uploading $log_name to S3"
    aws s3 cp "$log" \
        "s3://${S3_LOGS_BUCKET}/${S3_LOG_PREFIX}/${RUN_TAG}_${log_name}" \
        --region "$REGION" || true
done

# Upload session artifacts the container wrote to /workspace/.blitzlog/
for f in /workspace/.blitzlog/*; do
    [ -f "$f" ] || continue
    fname=$(basename "$f")
    log "Uploading $fname to S3"
    aws s3 cp "$f" \
        "s3://${SESSION_ARCHIVE_BUCKET:-${S3_LOGS_BUCKET}}/${SESSION_ARCHIVE_PREFIX}/${RUN_TAG}_${fname}" \
        --region "$REGION" || true
done

# Release the bot pool lock (S3 delete) — required so the same bot can
# be picked up by the next agent run.
if [ -n "${TELEGRAM_BOT_NAME:-}" ] && [ -n "${TELEGRAM_SENDER_LOGIN:-}" ]; then
    log "Releasing bot pool lock for ${TELEGRAM_SENDER_LOGIN}/${TELEGRAM_BOT_NAME}"
    aws s3 rm \
        "s3://${S3_LOGS_BUCKET}/bot-pool-locks/${TELEGRAM_SENDER_LOGIN}/${TELEGRAM_BOT_NAME}.json" \
        --region "$REGION" 2>/dev/null || true
fi

# Clean up container
sudo docker rm -f blitzlog-agent || true

# Terminate the EC2 instance
log "Terminating instance ${INSTANCE_ID}"
aws ec2 terminate-instances \
    --instance-id "$INSTANCE_ID" \
    --region "$REGION" || true

exit "$CONTAINER_EXIT"
