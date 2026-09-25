#!/bin/bash
# scripts-docker-ubuntu/05-publish-ami-id.sh — Publish AMI id to SSM.
#
# Runs as a Packer shell-local post-processor on the LOCAL packer runner
# (NOT on the source EC2). Packer does NOT set PACKER_AMI_ID in the
# post-processor context, so we read the AMI id from the manifest.json
# written by the `manifest` post-processor that runs immediately before
# this one.
#
# Uses the host user's AWS credentials (the same credentials used to
# invoke `packer build`).

set -euo pipefail

: "${BLITZLOG_ENV:?BLITZLOG_ENV must be set}"
: "${AWS_REGION:?AWS_REGION must be set}"

MANIFEST="${PACKER_MANIFEST:-manifest.json}"
if [ ! -r "$MANIFEST" ]; then
    echo "ERROR: manifest file not found at $MANIFEST"
    echo "       (set PACKER_MANIFEST env var to override; the manifest post-processor writes it to the working dir by default)"
    exit 1
fi

# artifact_id is in the form "<region>:ami-<id>" — strip the region prefix.
AMI_ID=$(jq -r '.builds[-1].artifact_id' "$MANIFEST" | awk -F: '{print $2}')

if [ -z "$AMI_ID" ] || [ "$AMI_ID" = "null" ] || [ "$AMI_ID" = "" ]; then
    echo "ERROR: could not extract AMI id from $MANIFEST"
    echo "       contents:"
    jq . "$MANIFEST" | sed 's/^/         /' >&2
    exit 1
fi

PARAM="/blitzlog/${BLITZLOG_ENV}/agent-ami-id-docker-ubuntu"

aws ssm put-parameter \
    --name "$PARAM" \
    --type String \
    --value "$AMI_ID" \
    --overwrite \
    --region "$AWS_REGION" \
    --description "Packer-built blitzlog agent AMI (Ubuntu 26.04 arm64) for env ${BLITZLOG_ENV}"

echo "Published $AMI_ID to $PARAM"
