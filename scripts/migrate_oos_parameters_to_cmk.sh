#!/usr/bin/env bash
# Re-key the OOS parameters onto the customer CMK, preserving their
# values.
#
# WHY A SCRIPT AND NOT A TERRAFORM APPLY
#
# A parameter's encryption key is fixed at creation, so moving one
# onto the CMK means recreating it. The terraform provider reads an
# OOS secret parameter back WITHOUT decryption, so the value in the
# terraform state is ALWAYS EMPTY — and `ignore_changes = [value]`
# keeps that empty prior value, which means a replacement re-creates
# the parameter with an EMPTY value and silently wipes the secret.
#
# The values only exist in OOS, so the migration has to happen in
# OOS: read the value, delete the parameter, create it again with the
# CMK and the same value.
#
# USAGE
#
#   scripts/migrate_oos_parameters_to_cmk.sh <parameter> [<parameter>...]
#   scripts/migrate_oos_parameters_to_cmk.sh --apply <parameter> ...
#
# The default is a dry run: it prints what it would do and changes
# nothing. `--apply` performs the migration.
#
# The names are the OOS names, e.g.
#
#   blitzlog/dev/github-app/id
#   blitzlog/dev/stt/model
#
# The CMK id is read from the OOS parameter the bootstrap stack
# publishes (`blitzlog/shared/kms-key-id`).
#
# The script uses the aliyun CLI's default profile, so it runs with
# the operator's own credentials.

set -euo pipefail

REGION="${ALICLOUD_REGION:-cn-hongkong}"
KEY_ID_PARAMETER="blitzlog/shared/kms-key-id"
ALICLOUD_CLI="${ALICLOUD_CLI:-aliyun}"
APPLY=0
NAMES=()

usage() {
    sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
    case "$1" in
    --apply)
        APPLY=1
        shift
        ;;
    --region)
        REGION="$2"
        shift 2
        ;;
    --key-id-parameter)
        KEY_ID_PARAMETER="$2"
        shift 2
        ;;
    -h | --help)
        usage
        exit 0
        ;;
    *)
        NAMES+=("$1")
        shift
        ;;
    esac
done

if [ "${#NAMES[@]}" -eq 0 ]; then
    echo "FATAL: name at least one OOS parameter to migrate." >&2
    usage >&2
    exit 1
fi

if ! command -v "$ALICLOUD_CLI" >/dev/null 2>&1; then
    echo "FATAL: the aliyun CLI ('$ALICLOUD_CLI') is not on PATH." >&2
    exit 1
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# The value of a parameter can be a multi-line PEM, so it is written
# to a file rather than carried in a shell variable.
read_parameter() {
    # $1 = parameter name, $2 = file to write the value into.
    # stdout: "<key id>\t<description>"
    "$ALICLOUD_CLI" oos GetSecretParameter \
        --Name "$1" \
        --WithDecryption true \
        --region "$REGION" |
        python3 -c '
import json
import sys

path = sys.argv[1]
doc = json.load(sys.stdin)
param = doc.get("Parameter")
if param is None:
    code = doc.get("error_code") or doc.get("ErrorCode") or "unknown"
    message = doc.get("message") or doc.get("Message") or ""
    sys.exit("OOS refused the read (%s): %s" % (code, message))

with open(path, "w", encoding="utf-8") as fh:
    fh.write(param.get("Value") or "")
sys.stdout.write("%s\t%s\n" % (param.get("KeyId") or "", param.get("Description") or ""))
' "$2"
}

echo "Region: $REGION"
echo "CMK parameter: $KEY_ID_PARAMETER"

CMK_ID="$("$ALICLOUD_CLI" oos GetParameter \
    --Name "$KEY_ID_PARAMETER" \
    --region "$REGION" |
    python3 -c '
import json
import sys

doc = json.load(sys.stdin)
param = doc.get("Parameter")
if param is None:
    code = doc.get("error_code") or doc.get("ErrorCode") or "unknown"
    message = doc.get("message") or doc.get("Message") or ""
    sys.exit("OOS refused the read (%s): %s" % (code, message))
sys.stdout.write(param.get("Value") or "")
')" || {
    echo "FATAL: cannot read the CMK id from ${KEY_ID_PARAMETER}." >&2
    echo "  Run 'cd infra/alicloud/bootstrap && terraform apply' first." >&2
    exit 1
}

if [ -z "$CMK_ID" ]; then
    echo "FATAL: ${KEY_ID_PARAMETER} is empty." >&2
    exit 1
fi
echo "CMK id: $CMK_ID"
if [ "$APPLY" -eq 0 ]; then
    echo "DRY RUN — pass --apply to perform the migration."
fi

for name in "${NAMES[@]}"; do
    value_file="$WORK_DIR/value"
    meta="$(read_parameter "$name" "$value_file")" || {
        echo "FATAL: cannot read ${name}." >&2
        exit 1
    }
    current_key="${meta%%$'\t'*}"
    description="${meta#*$'\t'}"

    if [ "$current_key" = "$CMK_ID" ]; then
        echo "  ${name}: already on the CMK"
        continue
    fi

    echo "  ${name}: ${current_key:-<the OOS service CMK>} -> ${CMK_ID}"
    if [ "$APPLY" -eq 0 ]; then
        continue
    fi

    # The value goes through the shell so it can be multi-line; a
    # trailing newline is dropped (it carries no meaning for a PEM
    # or an API key).
    value="$(cat "$value_file")"

    "$ALICLOUD_CLI" oos DeleteSecretParameter --Name "$name" --region "$REGION" >/dev/null

    create_args=(oos CreateSecretParameter
        --Name "$name"
        --Type Secret
        --KeyId "$CMK_ID"
        --Value "$value"
        --region "$REGION")
    if [ -n "$description" ]; then
        create_args+=(--Description "$description")
    fi
    "$ALICLOUD_CLI" "${create_args[@]}" >/dev/null

    # Verify: the parameter must read back with the same value.
    after_file="$WORK_DIR/after"
    read_parameter "$name" "$after_file" >/dev/null
    if [ "$(cat "$after_file")" != "$value" ]; then
        echo "FATAL: ${name} does not read back the same value after the migration." >&2
        exit 1
    fi
    echo "    migrated and verified"
done

if [ "$APPLY" -eq 0 ]; then
    echo
    echo "Nothing was changed. Re-run with --apply to perform the migration."
fi
