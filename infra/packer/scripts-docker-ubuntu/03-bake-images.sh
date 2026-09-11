#!/bin/bash
# scripts-docker-ubuntu/03-bake-images.sh — Ubuntu 24.04 image baking.
# Identical to the AL2023 script.

set -euo pipefail

: "${AGENT_IMAGE_TAG:?AGENT_IMAGE_TAG must be set}"
: "${STT_MODEL:?STT_MODEL must be set}"
: "${STT_MODELS_BUCKET:?STT_MODELS_BUCKET must be set}"
: "${AWS_REGION:?AWS_REGION must be set}"

mkdir -p /opt/blitzlog/images /opt/whisper-stt/models

dockerd >/var/log/dockerd.log 2>&1 &
DOCKERD_PID=$!

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

echo "Pulling ghcr.io/great-wall-connect/blitzlog-agent:${AGENT_IMAGE_TAG}"
docker pull "ghcr.io/great-wall-connect/blitzlog-agent:${AGENT_IMAGE_TAG}"

echo "Saving image to /opt/blitzlog/images/blitzlog-agent.tar.gz"
docker save "ghcr.io/great-wall-connect/blitzlog-agent:${AGENT_IMAGE_TAG}" \
    | gzip > /opt/blitzlog/images/blitzlog-agent.tar.gz

MODEL_FILE="ggml-${STT_MODEL}.bin"
echo "Downloading whisper model ${MODEL_FILE}"
aws s3 cp "s3://${STT_MODELS_BUCKET}/models/${MODEL_FILE}" \
    "/opt/whisper-stt/models/${MODEL_FILE}" --region "$AWS_REGION"

kill "$DOCKERD_PID" 2>/dev/null || true
wait "$DOCKERD_PID" 2>/dev/null || true

ls -lh /opt/blitzlog/images/ /opt/whisper-stt/models/
echo "Image baking complete"
