#!/bin/bash
# scripts-docker-ubuntu/04-publish.sh — Publish AMI id to SSM (Ubuntu variant).

set -euo pipefail

: "${BLITZLOG_ENV:?BLITZLOG_ENV must be set}"
: "${AWS_REGION:?AWS_REGION must be set}"

AMI_ID="${PACKER_AMI_ID:-}"
if [ -z "$AMI_ID" ]; then
    AMI_ID_VAR="PACKER_AMI_ID_${AWS_REGION//-/_}"
    AMI_ID="${!AMI_ID_VAR:-}"
fi

if [ -z "$AMI_ID" ]; then
    echo "ERROR: PACKER_AMI_ID not set by Packer"
    exit 1
fi

PARAM="/blitzlog/${BLITZLOG_ENV}/agent-ami-id-docker-ubuntu"

aws ssm put-parameter \
    --name "$PARAM" \
    --type String \
    --value "$AMI_ID" \
    --overwrite \
    --region "$AWS_REGION" \
    --description "Packer-built blitzlog agent AMI (Ubuntu 24.04 arm64) for env ${BLITZLOG_ENV}"

echo "Published $AMI_ID to $PARAM"
