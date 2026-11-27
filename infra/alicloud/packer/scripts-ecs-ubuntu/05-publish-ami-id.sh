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
# `SecretStore` implementation is the Aliyun OOS Parameter Store
# (lambda/provider/alicloud.py::AlicloudSecretStore), so the image
# id is published as an OOS parameter at the parallel path
# blitzlog/<env>/agent-ami-id-ecs-ubuntu (no leading slash — that
# is how OOS names its parameters).
#
# That matters for the consumer: `lambda/ec2.py::get_agent_ami` reads
# the id through `get_secrets()`, which dispatches on
# BLITZLOG_PROVIDER. Writing to SSM would be invisible to the
# Alicloud path — it would always fall back to the public Ubuntu
# image, and the baked agent image would never be used.
#
# The parameter is NOT encrypted with our customer CMK (no
# `--KeyId`): the image id is not a secret — it is an identifier
# that the FC function reads on every launch. It is published
# through the SecretStore purely because that is the cross-cloud
# read/write abstraction the Lambda already has. OOS encrypts
# every parameter it holds regardless; omitting `--KeyId` leaves
# the choice to OOS's own key.
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

# artifact_id is in the form "<region>:<image-id>" — strip the region
# prefix. The Alicloud packer builder names the artifact
# "<region>:<image-id>" (the ECS image id, e.g. "m-xxxx").
IMAGE_ID=$(jq -r '.builds[-1].artifact_id' "$MANIFEST" | awk -F: '{print $2}')

if [ -z "$IMAGE_ID" ] || [ "$IMAGE_ID" = "null" ] || [ "$IMAGE_ID" = "" ]; then
    echo "ERROR: could not extract image id from $MANIFEST"
    echo "       contents:"
    jq . "$MANIFEST" | sed 's/^/         /' >&2
    exit 1
fi

# No leading slash: OOS parameter names are `blitzlog/<env>/...`
# (see infra/alicloud/modules/core/locals.tf).
PARAM="blitzlog/${BLITZLOG_ENV}/agent-ami-id-ecs-ubuntu"

# The parameter is encrypted with the account's CMK — the one the
# bootstrap stack creates and publishes its id for. Without it the
# parameter would sit under OOS's default service key, and the
# readers would need a grant on that key instead of the CMK they
# already have.
#
# The id is a PLAIN OOS parameter (`blitzlog/shared/kms-key-id`),
# so it is read with `oos:GetParameter`.
KMS_KEY_ID=$(aliyun oos GetParameter \
    --Name "blitzlog/shared/kms-key-id" \
    --region "$ALICLOUD_REGION" 2>/dev/null |
    python3 -c 'import sys,json; print(json.load(sys.stdin)["Parameter"]["Value"], end="")')
if [ -z "$KMS_KEY_ID" ]; then
    echo "ERROR: could not read the CMK id from blitzlog/shared/kms-key-id" >&2
    echo "       The bootstrap stack publishes it — apply it first:" >&2
    echo "         cd infra/alicloud/bootstrap && terraform apply" >&2
    exit 1
fi

# The key is fixed at creation, so an existing parameter created
# under a different key has to be re-created rather than updated —
# the update API has no key. The value is the image id (not a
# secret), and the parameter is read only when an agent launches, so
# the window is harmless.
existing_key=$(aliyun oos GetSecretParameter \
    --Name "$PARAM" \
    --region "$ALICLOUD_REGION" 2>/dev/null |
    python3 -c '
import json
import sys

raw = sys.stdin.read()
if not raw.strip():
    # The CLI answers a missing parameter on stderr and exits
    # non-zero, so there is nothing on stdout to parse.
    sys.exit(0)
try:
    doc = json.loads(raw)
except ValueError:
    sys.exit(0)
param = doc.get("Parameter") or {}
sys.stdout.write(param.get("KeyId") or "")
')
if [ -n "$existing_key" ] && [ "$existing_key" != "$KMS_KEY_ID" ]; then
    echo "Re-creating $PARAM under the current CMK (it was created under another key)"
    aliyun oos DeleteSecretParameter \
        --Name "$PARAM" \
        --region "$ALICLOUD_REGION" >/dev/null
fi

# `aliyun oos CreateSecretParameter` fails when the name is taken,
# so try CreateSecretParameter first and fall through to
# UpdateSecretParameter (which adds a new version) — the same shape
# as lambda/provider/alicloud.py::AlicloudSecretStore.put.
#
# The update cannot change the key (the API has no KeyId on it), so
# an existing parameter keeps the key it was created with —
# scripts/migrate_oos_parameters_to_cmk.sh moves it.
#
# No `--output json`: aliyun CLI 3.x's `--output` is the TABLE
# formatter (`--output cols=Field1,Field2`) and rejects `json` with
# "bad flag format --output with field cols= required" — the CLI's
# default output is already JSON.
if aliyun oos CreateSecretParameter \
    --Name "$PARAM" \
    --Value "$IMAGE_ID" \
    --Type "Secret" \
    --KeyId "$KMS_KEY_ID" \
    --region "$ALICLOUD_REGION" >/dev/null 2>&1; then
    echo "Created $PARAM"
else
    aliyun oos UpdateSecretParameter \
        --Name "$PARAM" \
        --Value "$IMAGE_ID" \
        --region "$ALICLOUD_REGION" >/dev/null
    echo "Updated $PARAM"
fi

echo "Published $IMAGE_ID to $PARAM"
