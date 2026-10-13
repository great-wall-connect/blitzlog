#!/bin/bash
# tests/smoketest.sh — post-apply sanity check for the scrummaster host.
#
# Verifies the three acceptance criteria that don't need a real Telegram
# channel / GitHub App to be wired up (issue #135 §Acceptance criteria):
#
#   1. `ssh ubuntu@<ip> 'which zeroclaw && zeroclaw --version'` returns a version
#   2. `ssh ubuntu@<ip> 'systemctl list-units --type=service | grep zeroclaw'` shows nothing
#   3. /etc/zeroclaw, /var/lib/zeroclaw, /var/log/zeroclaw all exist
#
# Also runs `terraform plan` and asserts the output is empty (the
# "no diff on re-apply" invariant from the issue's test plan).
#
# Usage:
#
#   cd infra/scrummaster
#   terraform apply -auto-approve
#   ./tests/smoketest.sh
#
# Optional env vars:
#
#   SMOKETEST_SSH_KEY=/path/to/key.pem    SSH private key (else agent / default)
#   SMOKETEST_SSH_USER=ubuntu             SSH user (default ubuntu)
#   SMOKETEST_SKIP_SSH=1                  Skip the SSH checks (CI without network)
#
# The script is deliberately idempotent — running it twice in a row
# should succeed. Exits non-zero on the first failed assertion with
# a clear one-line error so the operator can grep the CI log.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${STACK_DIR}"

SSH_USER="${SMOKETEST_SSH_USER:-ubuntu}"
SSH_KEY_OPTS=()
if [ -n "${SMOKETEST_SSH_KEY:-}" ]; then
    SSH_KEY_OPTS=(-i "${SMOKETEST_SSH_KEY}")
fi

SSH_OPTS=(
    -o StrictHostKeyChecking=accept-new
    -o UserKnownHostsFile=/dev/null
    -o LogLevel=ERROR
    -o ConnectTimeout=10
)

# Resolve outputs from the current stack state.
INSTANCE_ID="$(terraform output -raw instance_id 2>/dev/null || true)"
PUBLIC_IP="$(terraform output -raw public_ip 2>/dev/null || true)"

if [ -z "$INSTANCE_ID" ] || [ -z "$PUBLIC_IP" ]; then
    echo "FATAL: terraform output is empty — did you run \`terraform apply\` first?" >&2
    exit 1
fi
echo "Instance: ${INSTANCE_ID} @ ${PUBLIC_IP}"

if [ "${SMOKETEST_SKIP_SSH:-0}" = "1" ]; then
    echo "SMOKETEST_SKIP_SSH=1 — skipping SSH checks."
    exit 0
fi

# ssh availability check — fail fast with a clear message when the
# runner doesn't have an ssh client (rare on Linux, common on
# macOS dev sandboxes without Xcode CLT).
if ! command -v ssh >/dev/null 2>&1; then
    echo "FATAL: ssh not on PATH — install OpenSSH client or set SMOKETEST_SKIP_SSH=1" >&2
    exit 1
fi

ssh_run() {
    ssh "${SSH_OPTS[@]}" "${SSH_KEY_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}" "$@"
}

# Wait for SSH to come up. The user-data script installs ZeroClaw
# (apt-get update + curl + tar extract), so first boot can take 30-60s.
echo "Waiting for SSH to become reachable on ${PUBLIC_IP}…"
for attempt in $(seq 1 30); do
    if ssh_run "true" 2>/dev/null; then
        echo "  reachable after ${attempt} attempt(s)"
        break
    fi
    if [ "$attempt" = "30" ]; then
        echo "FATAL: SSH never came up after 5 min — check /var/log/zeroclaw-bootstrap.log in the SSM session" >&2
        exit 1
    fi
    sleep 10
done

echo "Checking zeroclaw binary…"
if ! ssh_run "command -v zeroclaw >/dev/null"; then
    echo "FATAL: zeroclaw not on PATH — user-data install failed" >&2
    exit 1
fi
ssh_run "zeroclaw --version"

echo "Checking three directories…"
for dir in /etc/zeroclaw /var/lib/zeroclaw /var/log/zeroclaw; do
    if ! ssh_run "test -d '$dir'"; then
        echo "FATAL: directory missing: $dir" >&2
        exit 1
    fi
done

echo "Checking no zeroclaw daemon is running (by design, pre-#136)…"
if ssh_run "systemctl list-units --type=service --all 2>/dev/null | grep -E '^zeroclaw' || true" | grep -q zeroclaw; then
    echo "FATAL: a zeroclaw systemd unit is present — issue #135 ships no daemon; #136 wires the unit" >&2
    exit 1
fi

echo "Checking terraform plan is clean (no diff on re-apply)…"
# -detailed-exitcode: 0 = no diff, 1 = error, 2 = changes pending
PLAN_EXIT=0
terraform plan -detailed-exitcode -input=false -no-color >/tmp/scrummaster-plan.out 2>&1 || PLAN_EXIT=$?
if [ "$PLAN_EXIT" = "2" ]; then
    echo "FATAL: terraform plan is non-empty on re-apply — see /tmp/scrummaster-plan.out" >&2
    tail -n 40 /tmp/scrummaster-plan.out >&2
    exit 1
fi
if [ "$PLAN_EXIT" != "0" ]; then
    echo "FATAL: terraform plan failed (exit $PLAN_EXIT) — see /tmp/scrummaster-plan.out" >&2
    tail -n 40 /tmp/scrummaster-plan.out >&2
    exit 1
fi

echo "All scrummaster smoke checks passed."