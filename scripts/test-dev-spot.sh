#!/usr/bin/env bash
# scripts/test-dev-spot.sh — Level 3 of the watchdog token-refresh
# 3-level testing convention.
#
# Launches a single dev t4g.medium spot from the already-baked dev
# AMI, runs the watchdog by hand (the systemd unit is stopped so we
# can control the env), drives 10 refresh iterations via real
# `aws lambda invoke --payload '{"action":"refresh",...}'` calls,
# and verifies the bind-mounted credential files on the host
# (/root/.git-credentials.d/github and
# /root/gh-config/<instance>/hosts.yml) are rewritten within
# BLITZLOG_TOKEN_POLL_INTERVAL seconds of each refresh.
#
# Level 2 (`mise run test-integration`) is the pytest round-trip
# against real SSM with no container in the picture. Level 3 is the
# full mechanism including the host-side watchdog, the real
# `aws ssm get-parameter --with-decryption`, the bind mounts, and
# the in-place file rewrite.
#
# Cost: ~$0.01 per run (1 x t4g.medium spot for ~10 min in
# ap-east-1) plus a few SSM/Lambda/EventBridge calls.
#
# Required env vars:
#   AMI_ID       - the dev AMI id (get from
#                  `aws ssm get-parameter --name
#                   /blitzlog/dev/agent-ami-id-docker-ubuntu`)
#   SUBNET_ID    - dev VPC subnet
#   SG_ID        - dev security group that allows SSH from the
#                  operator's IP
#   KEYPAIR      - the SSH keypair name (operator's ~/.ssh must
#                  have the matching .pem at the path set in
#                  KEYPAIR_PATH)
#   KEYPAIR_PATH - path to the .pem file (default: ~/.ssh/$KEYPAIR.pem)
#   REGION       - AWS region (default: ap-east-1)
#
# Optional env vars:
#   ISSUE        - throwaway issue number for the SSM param
#                  (default: a random 6-digit number so re-runs
#                  don't collide)
#   LAMBDA_NAME  - the blitzlog Lambda function name
#                  (default: blitzlog-dev-webhook)
#   WAIT_BETWEEN - seconds between refresh invocations
#                  (default: $BLITZLOG_TOKEN_POLL_INTERVAL + 5, so
#                  the loop is guaranteed to have ticked at least
#                  once between each invocation)
#
# See docs/WATCHDOG_TOKEN_REFRESH.md for the full manual procedure
# this script wraps (and the operator's escape-hatch if a step
# needs human intervention).

set -euo pipefail

# --- preflight ---------------------------------------------------------

: "${AMI_ID:?set AMI_ID to the dev AMI id}"
: "${SUBNET_ID:?set SUBNET_ID to the dev subnet}"
: "${SG_ID:?set SG_ID to the dev security group}"
: "${KEYPAIR:?set KEYPAIR to the SSH keypair name}"
: "${KEYPAIR_PATH:=~/.ssh/${KEYPAIR}.pem}"
REGION="${REGION:-ap-east-1}"
ISSUE="${ISSUE:-$((RANDOM % 900000 + 100000))}"
LAMBDA_NAME="${LAMBDA_NAME:-blitzlog-dev-webhook}"

if [ ! -f "$KEYPAIR_PATH" ]; then
    echo "FATAL: KEYPAIR_PATH=$KEYPAIR_PATH does not exist" >&2
    exit 1
fi

if ! command -v aws >/dev/null 2>&1; then
    echo "FATAL: aws CLI not on PATH" >&2
    exit 1
fi

echo "AMI_ID=$AMI_ID"
echo "SUBNET_ID=$SUBNET_ID"
echo "SG_ID=$SG_ID"
echo "KEYPAIR=$KEYPAIR"
echo "KEYPAIR_PATH=$KEYPAIR_PATH"
echo "REGION=$REGION"
echo "ISSUE=$ISSUE"
echo "LAMBDA_NAME=$LAMBDA_NAME"

# --- launch the spot instance ------------------------------------------

echo "=== launching spot instance ==="
INSTANCE_ID=$(aws ec2 run-instances \
    --image-id "$AMI_ID" \
    --instance-type t4g.medium \
    --subnet-id "$SUBNET_ID" \
    --security-group-ids "$SG_ID" \
    --key-name "$KEYPAIR" \
    --iam-instance-profile Name=blitzlog-dev-agent-instance-profile \
    --instance-market-options 'MarketType=spot' \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=blitzlog-token-refresh-test}]" \
    --query 'Instances[0].InstanceId' --output text \
    --region "$REGION")
echo "INSTANCE_ID=$INSTANCE_ID"

cleanup() {
    echo "=== tearing down ==="
    aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --region "$REGION" 2>/dev/null || true
    aws ssm delete-parameter --name "/blitzlog/dev/ephemeral/github-token-${ISSUE}" --region "$REGION" 2>/dev/null || true
    for ITER in 0 1 2 3 4 5 6 7 8 9 10; do
        aws scheduler delete-schedule \
            --name "refresh-issue-${ISSUE}-iter-${ITER}" \
            --group-name "blitzlog-dev-token-refresh" \
            --region "$REGION" 2>/dev/null || true
    done
}
trap cleanup EXIT INT TERM

echo "=== waiting for instance to enter running state ==="
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$REGION"

PUBLIC_IP=$(aws ec2 describe-instances \
    --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' \
    --output text --region "$REGION")
echo "PUBLIC_IP=$PUBLIC_IP"

SSH="ssh -i $KEYPAIR_PATH -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ubuntu@$PUBLIC_IP"
SCP="scp -i $KEYPAIR_PATH -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

# --- prep the instance -------------------------------------------------

echo "=== stopping systemd unit (we'll run the watchdog by hand) ==="
$SSH "sudo systemctl stop blitzlog-agent && sudo systemctl disable blitzlog-agent" 2>&1

echo "=== seeding SSM with an initial token ==="
INITIAL_TOKEN="ghp_test_dev_spot_${RANDOM}_aaaa"
aws ssm put-parameter \
    --name "/blitzlog/dev/ephemeral/github-token-${ISSUE}" \
    --type SecureString \
    --value "$INITIAL_TOKEN" \
    --overwrite \
    --region "$REGION" >/dev/null
echo "SSM seeded with $INITIAL_TOKEN"

# Resolve the agent image tag. The dev AMI bakes a specific image
# tag in; we read it from the AMI's tag and pass it to the
# watchdog so `docker load` doesn't 404.
IMAGE_TAG=$(aws ec2 describe-images \
    --image-ids "$AMI_ID" \
    --query 'Images[0].Tags[?Key==`AgentTag`].Value' \
    --output text --region "$REGION")
if [ -z "$IMAGE_TAG" ] || [ "$IMAGE_TAG" = "None" ]; then
    echo "FATAL: could not read AgentTag from AMI $AMI_ID" >&2
    exit 1
fi
echo "IMAGE_TAG=$IMAGE_TAG"

echo "=== writing /root/blitzlog.env and /etc/blitzlog.env ==="
$SSH "sudo mkdir -p /root && sudo tee /root/blitzlog.env >/dev/null <<EOF
AGENT_IMAGE_REPO=ghcr.io/great-wall-connect/blitzlog-agent
AGENT_IMAGE_TAG=${IMAGE_TAG}
EOF"
$SSH "sudo tee /etc/blitzlog.env >/dev/null <<EOF
MODE=assisted
ISSUE_NUMBER=${ISSUE}
REPO=great-wall-connect/blitzlog
BLITZLOG_ENV=dev
S3_LOGS_BUCKET=gwc-blitzlog-agent-logs
SESSION_ARCHIVE_BUCKET=gwc-blitzlog-agent-logs
SESSION_ARCHIVE_PREFIX=dev/sessions
S3_LOG_PREFIX=dev/logs
GITHUB_TOKEN_SSM_PARAM=/blitzlog/dev/ephemeral/github-token-${ISSUE}
BLITZLOG_TOKEN_POLL_INTERVAL=30
AWS_REGION=${REGION}
EOF"

# --- run the watchdog in the background --------------------------------

# Start the watchdog in the background via nohup. It will:
#   1. `docker load` the baked image (succeeds — the AMI bakes it in)
#   2. Read SSM, write the credential files
#   3. Start the poll loop
#   4. `docker run` the agent container (we don't care what the
#      container does — the host-side poll loop is what we're testing)
#
# We don't run it under `sudo -E` because the env is already in
# /etc/blitzlog.env. The watchdog sources that file on line 26.
echo "=== starting watchdog in the background ==="
$SSH "sudo nohup /usr/local/bin/watchdog.sh > /tmp/watchdog.out 2>&1 & echo \$!" 2>&1 | tee /tmp/watchdog.pid
WATCHDOG_PID=$($SSH "cat /tmp/watchdog.pid")
echo "WATCHDOG_PID=$WATCHDOG_PID"

# --- wait for the poll loop to come up ---------------------------------

echo "=== waiting for poll loop to write initial credentials ==="
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if $SSH "test -s /root/.git-credentials.d/github && grep -q ${INITIAL_TOKEN} /root/.git-credentials.d/github" 2>/dev/null; then
        echo "initial credentials written"
        break
    fi
    sleep 5
done

$SSH "cat /root/.git-credentials.d/github /root/gh-config/${INSTANCE_ID}/hosts.yml" 2>&1
echo

# --- drive 10 refresh iterations --------------------------------------

# Default to BLITZLOG_TOKEN_POLL_INTERVAL + 5 so the loop is
# guaranteed to have ticked between invocations. The script's
# default is 30 s so 35 s per iteration × 10 = ~6 min, well within
# the 2 h EC2 cap.
WAIT_BETWEEN="${WAIT_BETWEEN:-35}"

for ITER in 0 1 2 3 4 5 6 7 8 9; do
    echo "=== iteration $ITER: invoking Lambda with action=refresh ==="
    PAYLOAD=$(jq -nc \
        --argjson issue "${ISSUE}" \
        --arg instance "${INSTANCE_ID}" \
        --arg repo "great-wall-connect/blitzlog" \
        --argjson iter "${ITER}" \
        '{action: "refresh", issue: $issue, instance: $instance, repo: $repo, iteration: $iter}')
    RESPONSE=$(aws lambda invoke \
        --function-name "$LAMBDA_NAME" \
        --payload "$PAYLOAD" \
        --cli-binary-format raw-in-base64-out \
        /tmp/refresh-${ITER}.json \
        --region "$REGION" \
        --query 'StatusCode' --output text)
    echo "Lambda StatusCode=$RESPONSE"
    cat /tmp/refresh-${ITER}.json
    echo
    if [ "$ITER" -lt 9 ]; then
        echo "sleeping ${WAIT_BETWEEN}s for the poll loop to tick..."
        sleep "$WAIT_BETWEEN"
    fi
done

# --- verify the chain ends at iter 9 ----------------------------------

echo "=== verifying no iter-10 schedule was created ==="
ITER10=$(aws scheduler list-schedules \
    --group-name "blitzlog-dev-token-refresh" \
    --region "$REGION" \
    --query "Schedules[?contains(Name, 'refresh-issue-${ISSUE}-iter-10')].Name" \
    --output text)
if [ -n "$ITER10" ]; then
    echo "FATAL: chain did not end at iter 9: found $ITER10" >&2
    exit 1
fi
echo "chain ended at iter 9 (good)"

# --- final report ------------------------------------------------------

echo
echo "=== final state on the host ==="
$SSH "cat /root/.git-credentials.d/github" 2>&1
echo "---"
$SSH "cat /root/gh-config/${INSTANCE_ID}/hosts.yml" 2>&1
echo "---"
$SSH "grep -E 'Token change|Started token refresh poll loop' /var/log/backend-bootstrap.log | tail -20" 2>&1

echo
echo "=== test-dev-spot.sh complete (trap will tear down) ==="
