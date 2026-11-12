#!/bin/bash
# infra/alicloud/packer/scripts-ecs-ubuntu/05-publish-ami-id.sh
#
# Publish the baked ECS image id so the Function Compute function can
# find it at launch time.
#
# The Alicloud analogue of
# infra/aws/packer/scripts-docker-ubuntu/05-publish-ami-id.sh.
#
# WHERE IT IS PUBLISHED. On AWS this lands in SSM Parameter Store
# (/blitzlog/<env>/agent-ami-id-docker-ubuntu) because SSM is the
# AWS-side `SecretStore` implementation. On Alicloud the
# `SecretStore` implementation is Aliyun KMS Secrets Manager
# (lambda/provider/alicloud.py::AlicloudSecretStore), so the image id
# is published as a KMS secret at the parallel path
# /blitzlog/<env>/agent-ami-id-ecs-ubuntu.
#
# That matters for the consumer: `lambda/ec2.py::get_agent_ami` reads
# the id through `get_secrets()`, which dispatches on
# BLITZLOG_PROVIDER. Writing to SSM would be invisible to the
# Alicloud path — it would always fall back to the public Ubuntu
# image, and the baked agent image would never be used.
#
# The secret is NOT encrypted-at-rest with a customer CMK (no
# `encryption_key_id`): the image id is not a secret — it is an
# identifier that the FC function reads on every launch. It is
# published through the SecretStore purely because that is the
# cross-cloud read/write abstraction the Lambda already has.
#
# Runs as a Packer shell-local post-processor on the LOCAL packer
# runner (NOT on the source ECS instance). Packer does NOT set
# PACKER_IMAGE_ID in the post-processor context, so we read the image
# id from the manifest.json written by the `manifest` post-processor
# that runs immediately before this one.
#
# Uses the host user's Alicloud credentials (the same credentials
# used to invoke `packer build`).

set -euo pipefail

: "${BLITZLOG_ENV:?BLITZLOG_ENV must be set}"
: "${ALICLOUD_REGION:?ALICLOUD_REGION must be set}"

MANIFEST="${PACKER_MANIFEST:-manifest.json}"
if [ ! -r "$MANIFEST" ]; then
    echo "ERROR: manifest file not found at $MANIFEST"
    echo "       (set PACKER_MANIFEST env var to override; the manifest post-processor writes it to the working dir by default)"
    exit 1
fi

# artifact_id is in the form "<region>:image-<id>" — strip the region
# prefix. The Alicloud packer builder names the artifact
# "<region>:image-<image-id>".
IMAGE_ID=$(jq -r '.builds[-1].artifact_id' "$MANIFEST" | awk -F: '{print $2}')

if [ -z "$IMAGE_ID" ] || [ "$IMAGE_ID" = "null" ] || [ "$IMAGE_ID" = "" ]; then
    echo "ERROR: could not extract image id from $MANIFEST"
    echo "       contents:"
    jq . "$MANIFEST" | sed 's/^/         /' >&2
    exit 1
fi

PARAM="/blitzlog/${BLITZLOG_ENV}/agent-ami-id-ecs-ubuntu"

# `aliyun kms CreateSecret` fails when the name is taken, so try
# CreateSecret first and fall through to PutSecretValue (which adds a
# new version) — the same shape as
# lambda/provider/alicloud.py::AlicloudSecretStore.put.
if aliyun kms CreateSecret \
    --SecretName "$PARAM" \
    --SecretData "$IMAGE_ID" \
    --SecretDataType "text" \
    --region "$ALICLOUD_REGION" \
    --output json >/dev/null 2>&1; then
    echo "Created $PARAM"
else
    aliyun kms PutSecretValue \
        --SecretName "$PARAM" \
        --SecretData "$IMAGE_ID" \
        --SecretDataType "text" \
        --region "$ALICLOUD_REGION" \
        --output json >/dev/null
    echo "Updated $PARAM"
fi

echo "Published $IMAGE_ID to $PARAM"
