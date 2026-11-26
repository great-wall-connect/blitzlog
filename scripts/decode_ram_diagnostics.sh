#!/usr/bin/env bash
# Explain a RAM denial: which action was checked, against which
# resource.
#
# WHEN THIS HELPS
#
# Alicloud's API errors carry the resource the check ran against in
# `AccessDeniedDetail.EncodedDiagnosticMessage` — a base64 blob that
# no error message mentions. `ram DecodeDiagnosticMessage` decodes it,
# and the answer is the one that matters when a policy "looks right"
# and still denies:
#
#   AuthAction:   oos:ListSecretParameters
#   AuthResource: acs:oos:cn-hongkong:5313647991964730:secretparameter/*
#
# That resource is the whole namespace, not the name the list was
# filtered by — which is why a grant scoped to `blitzlog/*` does not
# satisfy it.
#
# USAGE
#
#   scripts/decode_ram_diagnostics.sh <terraform output file>
#   terraform plan 2>&1 | scripts/decode_ram_diagnostics.sh
#
# The input is searched for the encoded diagnostics; each one is
# decoded and printed. It is a no-op when there are none.

set -euo pipefail

ALICLOUD_CLI="${ALICLOUD_CLI:-aliyun}"

if ! command -v "$ALICLOUD_CLI" >/dev/null 2>&1; then
    echo "decode_ram_diagnostics: the aliyun CLI ('$ALICLOUD_CLI') is not on PATH." >&2
    exit 0
fi

input="${1:--}"
if [ "$input" = "-" ]; then
    text="$(cat)"
else
    text="$(cat "$input")"
fi

# The blob is a long base64 string in the JSON error. Pull them all
# out — the same diagnostic repeats for every resource the plan
# probed.
mapfile -t blobs < <(printf '%s' "$text" | grep -o '"EncodedDiagnosticMessage":"[^"]*"' | sed 's/.*":"//; s/"$//' | sort -u)

if [ "${#blobs[@]}" -eq 0 ]; then
    exit 0
fi

echo "RAM denials (the resource each check ran against):"
for blob in "${blobs[@]}"; do
    "$ALICLOUD_CLI" ram DecodeDiagnosticMessage \
        --EncodedDiagnosticMessage "$blob" \
        --region "${ALICLOUD_REGION:-cn-hongkong}" 2>/dev/null |
        python3 -c '
import json
import sys

try:
    doc = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
detail = doc.get("DecodedDiagnosticMessage") or {}
action = detail.get("AuthAction")
resource = detail.get("AuthResource")
if not action:
    sys.exit(0)
print("  %-34s %s" % (action, resource))
'
done

cat <<'EOF'

A grant on a narrower resource does not satisfy a check against a
broader one — and RAM evaluates a LIST action against the collection,
not against the name the list is filtered by.
EOF
