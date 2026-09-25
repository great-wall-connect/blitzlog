#!/bin/bash
# scripts-docker-ubuntu/03-bake-images.sh — Ubuntu 26.04 image baking.
#
# Starts dockerd, authenticates to ghcr.io, pulls the blitzlog-agent
# container image, saves it as a gzipped tarball at
# /opt/blitzlog/images/, and pre-downloads the whisper model from S3 so
# the first agent launch doesn't pay the download cost. All commands
# run with sudo because Packer SSHs in as the unprivileged `ubuntu`
# user. Logs go to /tmp (writable by ubuntu) — using /var/log would
# fail the redirect because the shell creates the file before sudo.

set -euo pipefail

: "${AGENT_IMAGE_TAG:?AGENT_IMAGE_TAG must be set}"
: "${STT_MODEL:?STT_MODEL must be set}"
: "${STT_MODELS_BUCKET:?STT_MODELS_BUCKET must be set}"
: "${AWS_REGION:?AWS_REGION must be set}"

sudo mkdir -p /opt/blitzlog/images /opt/whisper-stt/models

sudo dockerd </dev/null >/tmp/dockerd.log 2>&1 &
DOCKERD_PID=$!

for i in $(seq 1 30); do
    if sudo docker info >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

if ! sudo docker info >/dev/null 2>&1; then
    echo "ERROR: dockerd did not become ready"
    cat /tmp/dockerd.log
    exit 1
fi

# Authenticate to ghcr.io before pulling. Packer passes GITHUB_TOKEN via
# environment_variables on the source block; the token must be a
# GitHub PAT with `read:packages` scope. `sudo docker login` so the
# credentials land in /root/.docker/config.json (where sudo docker
# reads from), not /home/ubuntu/.docker/config.json.
if [ -n "${GITHUB_TOKEN:-}" ]; then
    echo "Logging in to ghcr.io..."
    echo "${GITHUB_TOKEN}" | sudo docker login ghcr.io -u daniel-sarosi-gwc --password-stdin
else
    echo "WARNING: GITHUB_TOKEN not set; pull will likely fail with 'unauthorized'"
fi

echo "Pulling ghcr.io/great-wall-connect/blitzlog-agent:${AGENT_IMAGE_TAG}"
sudo docker pull "ghcr.io/great-wall-connect/blitzlog-agent:${AGENT_IMAGE_TAG}"

# Logout to leave no credentials in the AMI
sudo docker logout ghcr.io >/dev/null 2>&1 || true

echo "Saving image to /opt/blitzlog/images/blitzlog-agent.tar.gz"
sudo sh -c "docker save 'ghcr.io/great-wall-connect/blitzlog-agent:${AGENT_IMAGE_TAG}' | gzip > /opt/blitzlog/images/blitzlog-agent.tar.gz"

MODEL_FILE="ggml-${STT_MODEL}.bin"
echo "Downloading whisper model ${MODEL_FILE}"
sudo aws s3 cp "s3://${STT_MODELS_BUCKET}/models/${MODEL_FILE}" \
    "/opt/whisper-stt/models/${MODEL_FILE}" --region "$AWS_REGION"

sudo kill "$DOCKERD_PID" 2>/dev/null || true
sudo wait "$DOCKERD_PID" 2>/dev/null || true

sudo ls -lh /opt/blitzlog/images/ /opt/whisper-stt/models/
echo "Image baking complete"
