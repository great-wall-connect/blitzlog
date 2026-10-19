#!/bin/bash
# scripts-scrummaster-ubuntu/02-publish-ami-id.sh — Publish scrummaster AMI id to SSM.
#
# Runs as a Packer shell-local post-processor on the LOCAL packer runner
# (NOT on the source EC2), reading the AMI id from the manifest.json
# written by the `manifest` post-processor. Same mechanism as
# scripts-docker-ubuntu/05-publish-ami-id.sh, but the scrummaster is
# env-independent operator hosting, so the parameter has no env segment.

set -euo pipefail

: "${AWS_REGION:?AWS_REGION must be set}"

MANIFEST="${PACKER_MANIFEST:-manifest.json}"
if [ ! -r "$MANIFEST" ]; then
    echo "ERROR: manifest file not found at $MANIFEST"
    echo "       (set PACKER_MANIFEST env var to override; the manifest post-processor writes it to the working dir by default)"
    exit 1
fi

# artifact_id is in the form "<region>:ami-<id>" — strip the region prefix.
AMI_ID=$(jq -r '.builds[-1].artifact_id' "$MANIFEST" | awk -F: '{print $2}')

if [ -z "$AMI_ID" ] || [ "$AMI_ID" = "null" ]; then
    echo "ERROR: could not extract AMI id from $MANIFEST"
    echo "       contents:"
    jq . "$MANIFEST" | sed 's/^/         /' >&2
    exit 1
fi

PARAM="/blitzlog/scrummaster/agent-ami-id-zeroclaw-ubuntu"

aws ssm put-parameter \
    --name "$PARAM" \
    --type String \
    --value "$AMI_ID" \
    --overwrite \
    --region "$AWS_REGION" \
    --description "Packer-built blitzlog scrummaster AMI (Ubuntu 26.04 arm64 + zeroclaw)"

echo "Published $AMI_ID to $PARAM"
