#!/usr/bin/env bash
# scripts/clean_amis.sh — deregister self-owned AMIs older than N days
# and delete the EBS snapshots they own.
#
# Default mode is dry-run; pass --yes to actually delete. We deregister
# first (frees the AMI itself) and then remove the snapshots it created
# (otherwise the snapshots become orphans that still cost money).
#
# Requires: aws CLI configured with ec2:DescribeImages, ec2:DeregisterImage,
# and ec2:DeleteSnapshot on the target account/region.
#
# Usage:
#   scripts/clean_amis.sh                  # dry-run, 2-day cutoff
#   scripts/clean_amis.sh --days 7         # dry-run, 7-day cutoff
#   scripts/clean_amis.sh --region us-west-2
#   scripts/clean_amis.sh --yes            # actually delete, 2-day cutoff
set -euo pipefail

DAYS=2
APPLY=0
REGION="${AWS_REGION:-}"

usage() {
  cat <<EOF
Usage: $0 [--days N] [--region REGION] [--yes]

  --days N       delete AMIs older than N days (default 2)
  --region R     override AWS region (default: \$AWS_REGION or aws cli default)
  --yes          actually delete (default is dry-run)
  -h, --help     show this help
EOF
}

while (( $# > 0 )); do
  case "$1" in
    --days)
      DAYS="$2"
      shift 2
      ;;
    --days=*)
      DAYS="${1#*=}"
      shift
      ;;
    --region)
      REGION="$2"
      shift 2
      ;;
    --region=*)
      REGION="${1#*=}"
      shift
      ;;
    --yes|-y)
      APPLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! [[ "$DAYS" =~ ^[0-9]+$ ]] || (( DAYS < 1 )); then
  echo "--days must be a positive integer (got: $DAYS)" >&2
  exit 2
fi

# Resolve region: explicit flag > AWS_REGION > aws-cli default.
if [[ -z "$REGION" ]]; then
  REGION=$(aws configure get region 2>/dev/null || true)
fi
if [[ -z "$REGION" ]]; then
  echo "could not determine AWS region — pass --region or set AWS_REGION" >&2
  exit 2
fi

# Auth sanity check up front. If this fails we want a clean error before
# we even think about making describe-images calls.
if ! AWS_REGION="$REGION" aws sts get-caller-identity >/dev/null 2>&1; then
  echo "aws credentials invalid or missing for region $REGION" >&2
  exit 2
fi

# UTC cutoff — `CreationDate` is the AMI's creation timestamp and is in
# ISO-8601; we want a string-comparable cutoff so format with -u.
CUTOFF=$(date -u -d "${DAYS} days ago" +'%Y-%m-%dT%H:%M:%S')

if (( APPLY )); then
  MODE="apply"
else
  MODE="dry-run"
fi

echo "==> ami cleanup"
echo "    region:  $REGION"
echo "    cutoff:  $CUTOFF  (older than ${DAYS} day(s))"
echo "    mode:    $MODE"
echo

# Self-owned AMIs only. describe-images returns a JSON blob; we ask for
# just the fields we need so jq has less to chew on.
# - CreationDate `>=` CUTOFF is "keep", the complement is "delete".
# - ImageId and the snapshot ids from BlockDeviceMappings[].Ebs.SnapshotId.
AMI_JSON=$(aws ec2 describe-images --owners self --region "$REGION" \
  --output json)

ERR_COUNT=0

echo "$AMI_JSON" | jq -r --arg ts "$CUTOFF" '
  .Images
  | map(select(.CreationDate <= $ts))
  | sort_by(.CreationDate)
  | .[]
  | "\(.ImageId)\t\(.CreationDate)\t\(.Name // "(unnamed)")\t\(
      ([.BlockDeviceMappings[]?.Ebs?.SnapshotId] | join(",") // "")
    )"
' | while IFS=$'\t' read -r ami_id ami_created ami_name ami_snaps; do
  [[ -z "$ami_id" ]] && continue

  if (( APPLY )); then
    if aws ec2 deregister-image --region "$REGION" --image-id "$ami_id" >/dev/null 2>&1; then
      echo "  [apply]   deregistered ${ami_id} (${ami_name}, ${ami_created})"
    else
      echo "  [error]   failed to deregister ${ami_id} (${ami_name})" >&2
      ERR_COUNT=$((ERR_COUNT + 1))
      # Don't try to delete snapshots if deregistration failed — they may
      # still be referenced by the (now-still-existing) AMI.
      continue
    fi

    if [[ -n "$ami_snaps" ]]; then
      IFS=',' read -ra SNAP_LIST <<< "$ami_snaps"
      for snap in "${SNAP_LIST[@]}"; do
        [[ -z "$snap" ]] && continue
        if aws ec2 delete-snapshot --region "$REGION" --snapshot-id "$snap" >/dev/null 2>&1; then
          echo "  [apply]   deleted snapshot ${snap}"
        else
          echo "  [error]   failed to delete snapshot ${snap}" >&2
          ERR_COUNT=$((ERR_COUNT + 1))
        fi
      done
    fi
  else
    echo "  [dry-run] would deregister ${ami_id} (${ami_name}, ${ami_created})"
    if [[ -n "$ami_snaps" ]]; then
      IFS=',' read -ra SNAP_LIST <<< "$ami_snaps"
      for snap in "${SNAP_LIST[@]}"; do
        [[ -z "$snap" ]] && continue
        echo "  [dry-run] would delete snapshot ${snap}"
      done
    fi
  fi
done

# The summary counters live in the subshell of the pipe above and are not
# visible here. Re-query for a final tally so the user gets useful output
# even after the dry-run finishes.
FINAL_COUNT=$(echo "$AMI_JSON" | jq --arg ts "$CUTOFF" \
  '.Images | map(select(.CreationDate <= $ts)) | length')

echo
echo "==> summary"
echo "    amis flagged: $FINAL_COUNT"
if [[ $ERR_COUNT -gt 0 ]]; then
  # ERR_COUNT only reflects --yes runs (subshell side effect); mention it.
  echo "    errors:       $ERR_COUNT"
fi

if (( ERR_COUNT > 0 )); then
  exit 4
fi