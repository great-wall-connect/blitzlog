#!/usr/bin/env bash
# Copy the deploy-time OOS parameters into the per-env set, or back.
#
# The store holds the same nine secrets twice:
#
#   blitzlog/<env>/<leaf>           the deploy-time set — the
#                                    bootstrap stack owns it
#                                    (infra/alicloud/bootstrap/secrets.tf)
#                                    and the CI reads it to render the
#                                    TF variables
#
#   blitzlog/<env>/<per-env path>   the per-env set — the core module
#                                    owns it and the runtime reads it
#
# Both hold the same values, so a rotation has to reach both: the
# deploy-time set is what the next deploy reads, the per-env set is
# what the running agents read.
#
# The copy happens in OOS, not in terraform: the terraform provider
# reads an OOS parameter back WITHOUT decryption, so its copy of the
# value is always empty, and an apply could not carry a value across
# without risking wiping it.
#
# USAGE
#
#   scripts/sync_oos_parameters.sh --to-runtime dev   # deploy-time -> per-env
#   scripts/sync_oos_parameters.sh --to-deploy dev    # per-env -> deploy-time
#   scripts/sync_oos_parameters.sh --to-runtime dev --apply
#
# The default is a dry run: it prints what it would copy.

set -euo pipefail

REGION="${ALICLOUD_REGION:-cn-hongkong}"
ALICLOUD_CLI="${ALICLOUD_CLI:-aliyun}"
DIRECTION=""
ENVIRONMENT=""
APPLY=0

# The deploy-time leaf and the per-env path it corresponds to. The
# per-env paths are the ones in
# infra/alicloud/modules/core/locals.tf.
DEPLOY_LEAVES=(
    "github-app-id"
    "github-app-private-key"
    "github-app-installation-id"
    "github-webhook-secret"
    "opencode-api-key"
    "stt-api-key"
    "stt-model"
    "stt-language"
    "stt-models-bucket-name"
)
PER_ENV_PATHS=(
    "github-app/id"
    "github-app/private-key"
    "github-app/installation-id"
    "github-webhook/secret"
    "opencode/api-key"
    "stt/api-key"
    "stt/model"
    "stt/language"
    "stt/models-bucket"
)

usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
    case "$1" in
    --to-runtime | --to-deploy)
        DIRECTION="$1"
        shift
        ;;
    --apply)
        APPLY=1
        shift
        ;;
    --region)
        REGION="$2"
        shift 2
        ;;
    -h | --help)
        usage
        exit 0
        ;;
    *)
        ENVIRONMENT="$1"
        shift
        ;;
    esac
done

if [ -z "$DIRECTION" ] || [ -z "$ENVIRONMENT" ]; then
    echo "FATAL: pass a direction (--to-runtime | --to-deploy) and an environment." >&2
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
raw = sys.stdin.read()
if not raw.strip():
    # The CLI answers an API error on stderr and exits non-zero, so
    # there is nothing on stdout to parse — its own error is already
    # in the log.
    sys.exit("the aliyun CLI returned no output — its error is above")
try:
    doc = json.loads(raw)
except ValueError as exc:
    sys.exit("the aliyun CLI did not return JSON: %s" % exc)
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

# Create the parameter if it is missing, update it otherwise. The
# encryption key is fixed at creation — an existing parameter keeps
# the key it has (use scripts/migrate_oos_parameters_to_cmk.sh to move
# one onto the CMK).
write_parameter() {
    # $1 = name, $2 = value file, $3 = key id, $4 = description
    local name="$1" value_file="$2" key_id="$3" description="$4"
    local value
    value="$(cat "$value_file")"

    # The CLI answers a missing parameter with an error document and a
    # ZERO exit code, so the answer is parsed rather than trusted.
    local exists
    exists="$("$ALICLOUD_CLI" oos GetSecretParameter \
        --Name "$name" \
        --region "$REGION" |
        python3 -c '
import json
import sys

try:
    doc = json.load(sys.stdin)
except ValueError:
    print("no")
    sys.exit(0)
print("yes" if doc.get("Parameter") is not None else "no")
')" || echo "no"

    if [ "$exists" = "yes" ]; then
        "$ALICLOUD_CLI" oos UpdateSecretParameter \
            --Name "$name" \
            --Value "$value" \
            --region "$REGION" >/dev/null
        return 0
    fi

    local create_args=(oos CreateSecretParameter
        --Name "$name"
        --Type Secret
        --Value "$value"
        --region "$REGION")
    if [ -n "$key_id" ]; then
        create_args+=(--KeyId "$key_id")
    fi
    if [ -n "$description" ]; then
        create_args+=(--Description "$description")
    fi
    "$ALICLOUD_CLI" "${create_args[@]}" >/dev/null
}

if [ "$DIRECTION" = "--to-runtime" ]; then
    SOURCE_LEAVES=("${DEPLOY_LEAVES[@]}")
    TARGET_PATHS=("${PER_ENV_PATHS[@]}")
    SOURCE_LABEL="the deploy-time set"
    TARGET_LABEL="the per-env set"
else
    SOURCE_LEAVES=("${PER_ENV_PATHS[@]}")
    TARGET_PATHS=("${DEPLOY_LEAVES[@]}")
    SOURCE_LABEL="the per-env set"
    TARGET_LABEL="the deploy-time set"
fi

echo "Environment: $ENVIRONMENT"
echo "Copying: $SOURCE_LABEL -> $TARGET_LABEL"
if [ "$APPLY" -eq 0 ]; then
    echo "DRY RUN — pass --apply to copy the values."
fi

for index in "${!SOURCE_LEAVES[@]}"; do
    source_name="blitzlog/${ENVIRONMENT}/${SOURCE_LEAVES[$index]}"
    target_name="blitzlog/${ENVIRONMENT}/${TARGET_PATHS[$index]}"
    value_file="$WORK_DIR/value"

    if ! meta="$(read_parameter "$source_name" "$value_file")"; then
        echo "  ${source_name}: cannot read the source — skipped."
        continue
    fi
    key_id="${meta%%$'\t'*}"
    description="${meta#*$'\t'}"

    echo "  ${source_name} -> ${target_name}"
    if [ "$APPLY" -eq 0 ]; then
        continue
    fi
    write_parameter "$target_name" "$value_file" "$key_id" "$description"
    echo "    copied"
done

if [ "$APPLY" -eq 0 ]; then
    echo
    echo "Nothing was changed. Re-run with --apply to copy the values."
fi
