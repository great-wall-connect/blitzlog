#!/bin/bash
# scripts-docker/04-publish.sh — Publish the resulting AMI id to SSM.
#
# Packer calls this last. The Lambda's get_agent_ami() reads from
# /blitzlog/<env>/agent-ami-id-docker-al2023 with the canonical fallback
# to the ECS-optimized AMI's SSM source.

set -euo pipefail

: "${BLITZLOG_ENV:?BLITZLOG_ENV must be set}"
: "${AWS_REGION:?AWS_REGION must be set}"

# Packer exports the AMI id as $PACKER_AMI_ID (and per-region variants)
# when it runs this provisioner on the source build. We pick the id for
# the configured region.
AMI_ID="${PACKER_AMI_ID:-}"

if [ -z "$AMI_ID" ]; then
    # Fallback: read from the per-region env var Packer also sets.
    AMI_ID_VAR="PACKER_AMI_ID_${AWS_REGION//-/_}"
    AMI_ID="${!AMI_ID_VAR:-}"
fi

if [ -z "$AMI_ID" ]; then
    echo "ERROR: PACKER_AMI_ID not set by Packer"
    exit 1
fi

PARAM="/blitzlog/${BLITZLOG_ENV}/agent-ami-id-docker-al2023"

# Use the instance role to write to SSM (Packer uses the temporary instance
# profile granted via iam:PassRole).
aws ssm put-parameter \
    --name "$PARAM" \
    --type String \
    --value "$AMI_ID" \
    --overwrite \
    --region "$AWS_REGION" \
    --description "Packer-built blitzlog agent AMI (AL2023 arm64) for env ${BLITZLOG_ENV}"

echo "Published $AMI_ID to $PARAM"
