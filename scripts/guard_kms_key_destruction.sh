#!/usr/bin/env bash
# Refuse a plan that would destroy a KMS key.
#
# Every OOS parameter is encrypted with the account's CMK. Destroying
# the key SCHEDULES its deletion (a seven-day window), and when it
# goes the parameters it encrypts can no longer be read — a week
# later, when nobody is looking at the plan that caused it.
#
# The key is adopted, not created: it lives in the bootstrap stack's
# state. A plan that destroys one means a stack that still manages it
# should stop managing it instead:
#
#   terraform state rm <address>
#
# USAGE
#
#   scripts/guard_kms_key_destruction.sh <tfplan file>

set -euo pipefail

PLAN="${1:?usage: guard_kms_key_destruction.sh <tfplan file>}"

if [ ! -f "$PLAN" ]; then
    echo "guard_kms_key_destruction: ${PLAN} does not exist." >&2
    exit 1
fi

# `terraform show -json` loads the provider's schema, which it finds
# next to the plan — so the check runs where the stack was
# initialised, whatever directory the caller is in.
PLAN_DIR="$(cd "$(dirname "$PLAN")" && pwd)"
cd "$PLAN_DIR"

if ! terraform show -json "$(basename "$PLAN")" > /tmp/blitzlog-plan.json; then
    echo "guard_kms_key_destruction: could not read ${PLAN}." >&2
    echo "  The plan has to be read where the stack was initialised —" >&2
    echo "  terraform show loads the provider's schema." >&2
    exit 1
fi

destroyed="$(jq -r '
  [ .resource_changes[]?
    | select(.type == "alicloud_kms_key")
    | select(.change.actions | index("delete"))
    | .address
  ] | join("\n")
' /tmp/blitzlog-plan.json)"

rm -f /tmp/blitzlog-plan.json

if [ -n "$destroyed" ]; then
    echo "FATAL: this plan would destroy a KMS key:" >&2
    echo "$destroyed" >&2
    echo "  The OOS parameters are encrypted with it. When the key is" >&2
    echo "  gone they cannot be read any more." >&2
    echo "  The key belongs to the bootstrap stack — the stack that" >&2
    echo "  manages it should stop managing it instead:" >&2
    echo "    terraform state rm <address>" >&2
    exit 1
fi
