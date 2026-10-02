#!/bin/bash
# /usr/local/bin/assisted-shutdown.sh — CONTAINER-SIDE shutdown helper.
#
# Runs INSIDE the blitzlog-agent container when the user (via Telegram
# bot / shutdown plugin) or the host (via watchdog detecting idle) asks
# the agent to stop. This script:
#   1. Exports the current opencode session to /workspace/.blitzlog/
#   2. Writes a metadata.json with session id / branch / commit / shutdown
#      reason
#   3. Touches /workspace/.shutdown — the host's watchdog polls for this
#      to do AWS-dependent cleanup (S3 upload, lock release, EC2 terminate)
#
# The container has NO AWS credentials — S3 uploads happen on the host
# after the container exits.

set -euo pipefail

SHUTDOWN_REASON="${_SHUTDOWN_REASON:-unknown}"
mkdir -p /workspace/.blitzlog

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Container assisted-shutdown initiated: $SHUTDOWN_REASON"

# Export the current opencode session to the shared host-readable dir.
SESSION_ID=$(opencode session list --format json -n 1 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])" 2>/dev/null || echo "")
if [ -n "$SESSION_ID" ]; then
    opencode export "$SESSION_ID" > "/workspace/.blitzlog/session-archive-${SESSION_ID}.json" 2>/dev/null || true
    BRANCH=$(git -C /workspace/repo branch --show-current 2>/dev/null || echo "")
    COMMIT=$(git -C /workspace/repo rev-parse HEAD 2>/dev/null || echo "")
    python3 -c "import json; print(json.dumps({'sessionId': '$SESSION_ID', 'branch': '$BRANCH', 'commit': '$COMMIT', 'timestamp': $(date +%s000), 'shutdownReason': '$SHUTDOWN_REASON'}))" > "/workspace/.blitzlog/metadata.json" 2>/dev/null || true
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Session $SESSION_ID exported to /workspace/.blitzlog/"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARNING: no active session found to export"
fi

# Notify the user via Telegram (container has telegram bot creds).
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_USER_ID:-}" ]; then
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_USER_ID}" \
        -d text="Assisted agent shutting down (reason: ${SHUTDOWN_REASON}). Session archived; host will finalize." \
        >/dev/null 2>&1 || true
fi

# Signal the host's watchdog: when this script exits, the container's
# opencode process will also exit, the host's `docker wait` returns,
# and the watchdog uploads /var/log/blitzlog/ + /workspace/.blitzlog/ to
# S3 and terminates the EC2 instance.
touch /workspace/.shutdown
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Container exit pending; host will upload artifacts and terminate."
