#!/bin/bash
# scripts-docker/03-bake-images.sh — Amazon Linux 2023 image baking.
#
# Runs after host extras are installed. Starts dockerd, pulls the
# blitzlog-agent image, saves as a gzipped tarball to /opt/blitzlog/images/.
# Also pre-downloads the whisper model so the first launch doesn't pay
# the download cost.

set -euo pipefail

: "${AGENT_IMAGE_TAG:?AGENT_IMAGE_TAG must be set (Packer variable)}"
: "${STT_MODEL:?STT_MODEL must be set}"
: "${STT_MODELS_BUCKET:?STT_MODELS_BUCKET must be set}"
: "${AWS_REGION:?AWS_REGION must be set}"

mkdir -p /opt/blitzlog/images /opt/whisper-stt/models

# Start dockerd in the background. AL2023 ECS-optimized ships dockerd but
# it isn't running by default in the Packer build VM.
dockerd >/var/log/dockerd.log 2>&1 &
DOCKERD_PID=$!

# Wait for dockerd to be ready
for i in $(seq 1 30); do
    if docker info >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: dockerd did not become ready"
    cat /var/log/dockerd.log
    exit 1
fi

# Pull the agent image
echo "Pulling ghcr.io/great-wall-connect/blitzlog-agent:${AGENT_IMAGE_TAG}"
docker pull "ghcr.io/great-wall-connect/blitzlog-agent:${AGENT_IMAGE_TAG}"

# Save as a gzipped tarball
echo "Saving image to /opt/blitzlog/images/blitzlog-agent.tar.gz"
docker save "ghcr.io/great-wall-connect/blitzlog-agent:${AGENT_IMAGE_TAG}" \
    | gzip > /opt/blitzlog/images/blitzlog-agent.tar.gz

# Pre-download the whisper model
MODEL_FILE="ggml-${STT_MODEL}.bin"
echo "Downloading whisper model ${MODEL_FILE} from s3://${STT_MODELS_BUCKET}/models/"
aws s3 cp "s3://${STT_MODELS_BUCKET}/models/${MODEL_FILE}" \
    "/opt/whisper-stt/models/${MODEL_FILE}" --region "$AWS_REGION"

# Stop dockerd
kill "$DOCKERD_PID" 2>/dev/null || true
wait "$DOCKERD_PID" 2>/dev/null || true

ls -lh /opt/blitzlog/images/ /opt/whisper-stt/models/
echo "Image baking complete"
