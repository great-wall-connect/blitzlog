#!/bin/bash
# scripts-docker-ubuntu/02-systemd.sh — Ubuntu 24.04 systemd setup.
#
# Enables docker.service, writes the host-side watchdog + load-image scripts.
# Watchdog is identical to the AL2023 one (the same `docker stop --time=120`
# flow works on either OS family).

set -euo pipefail

systemctl enable docker

mkdir -p /opt/blitzlog/images /opt/whisper-stt/models

cat > /usr/local/bin/load-image.sh <<'LOAD_IMAGE_EOF'
#!/bin/bash
set -euo pipefail
for img in /opt/blitzlog/images/*.tar.gz; do
    [ -e "$img" ] || continue
    echo "[$(date '+%T')] docker load -i $img"
    docker load -i "$img"
done
LOAD_IMAGE_EOF
chmod +x /usr/local/bin/load-image.sh

cat > /usr/local/bin/watchdog.sh <<'WATCHDOG_EOF'
#!/bin/bash
set -euo pipefail
source /etc/blitzlog.env
export HOME=/root

LOG_FILE="/var/log/backend-bootstrap.log"
CONTAINER_NAME=blitzlog-agent
TIMEOUT=7200

for img in /opt/blitzlog/images/*.tar.gz; do
    [ -e "$img" ] || continue
    docker load -i "$img"
done

AGENT_TAG="${AGENT_TAG:-2.0.0}"

timeout "$TIMEOUT" docker run --rm \
    --name "$CONTAINER_NAME" \
    -e MODE=autonomous \
    -e ISSUE_NUMBER="${ISSUE_NUMBER:-}" \
    -e REPO="${REPO:-}" \
    -e OPENCODE_MODEL="${OPENCODE_MODEL:-}" \
    -e OPENCODE_PROMPT="${OPENCODE_PROMPT:-}" \
    -e OPENCODE_CONFIG_JSON="${OPENCODE_CONFIG_JSON:-}" \
    -e AWS_REGION="${REGION:-ap-east-1}" \
    -e BLITZLOG_ENV="${BLITZLOG_ENV:-prod}" \
    -e STT_MODEL="${STT_MODEL:-base.en}" \
    -e STT_LANGUAGE="${STT_LANGUAGE:-en}" \
    -e GITHUB_TOKEN="${_CC_GITHUB_TOKEN:-}" \
    -e GIT_USER_NAME="${GIT_USER_NAME:-}" \
    -e GIT_USER_EMAIL="${GIT_USER_EMAIL:-}" \
    -v /opt/whisper-stt/models:/opt/whisper-stt/models:ro \
    -v /root/.git-credentials.d:/root/.git-credentials.d \
    -v /workspace:/workspace \
    -v /root/.config/opencode:/root/.config/opencode \
    "ghcr.io/great-wall-connect/blitzlog-agent:${AGENT_TAG}" \
    > "$LOG_FILE" 2>&1 &
DOCKER_PID=$!

TOKEN=$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
while kill -0 "$DOCKER_PID" 2>/dev/null; do
    SPOT_ACTION=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" \
        http://169.254.169.254/latest/meta-data/spot/instance-action 2>/dev/null || echo "")
    if [ -n "$SPOT_ACTION" ]; then
        echo "[$(date '+%T')] Spot interruption detected: $SPOT_ACTION"
        echo "[$(date '+%T')] Stopping container with 120s grace period"
        docker stop --time=120 "$CONTAINER_NAME" || true
        break
    fi
    sleep 5
done

wait "$DOCKER_PID" 2>/dev/null || true
EXIT=$?

LOG_KEY="${REPO}/issue/${ISSUE_NUMBER}/logs/$(hostname)-$(date +%Y%m%d-%H%M%S).log"
aws s3 cp "$LOG_FILE" "s3://${S3_LOGS_BUCKET}/${LOG_KEY}" --region "${REGION:-ap-east-1}" 2>/dev/null || true

if [ -f /workspace/session-export.json ]; then
    SESSION_ID=$(python3 -c "import json; print(json.load(open('/workspace/session-export.json')).get('id',''))" 2>/dev/null || echo "")
    if [ -n "$SESSION_ID" ]; then
        aws s3 cp /workspace/session-export.json \
            "s3://${S3_LOGS_BUCKET}/${SESSION_ARCHIVE_PREFIX:-${REPO}/issue/${ISSUE_NUMBER}}/sessions/${SESSION_ID}.json" \
            --region "${REGION:-ap-east-1}" 2>/dev/null || true
    fi
fi

INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/instance-id)
aws ec2 terminate-instances --instance-id "$INSTANCE_ID" \
    --region "${REGION:-ap-east-1}" 2>/dev/null || true

exit "$EXIT"
WATCHDOG_EOF
chmod +x /usr/local/bin/watchdog.sh

echo "Systemd setup complete"
