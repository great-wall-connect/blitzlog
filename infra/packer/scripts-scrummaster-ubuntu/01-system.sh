#!/bin/bash
# scripts-scrummaster-ubuntu/01-system.sh — scrummaster host setup (issue #135).
#
# Runs on Canonical's Ubuntu 26.04 LTS arm64 base AMI. Installs dockerd,
# awscli, git, jq; creates the `zeroclaw-host` user (sole member of the
# `docker` group besides root); resolves the upstream zeroclaw image tag
# to a sha256 digest, pulls by digest, and saves it as a tarball under
# /opt/zeroclaw/images/; installs the systemd units uploaded to /tmp by
# the Packer `file` provisioners.
#
# Unlike scripts-docker-ubuntu/, there is no watchdog or IMDS poller:
# the scrummaster is a long-lived on-demand instance, not spot.

set -euo pipefail

: "${ZEROCLAW_IMAGE_TAG:?ZEROCLAW_IMAGE_TAG must be set}"

ZEROCLAW_REPO="ghcr.io/zeroclaw-labs/zeroclaw"
IMAGE_DIR="/opt/zeroclaw/images"

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg \
    awscli git jq

# Docker's official Ubuntu repo (installs dockerd + CLI + buildx plugin)
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Ubuntu 26.04 (resolute)
echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu resolute stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin

sudo apt-get clean
sudo rm -rf /var/lib/apt/lists/*

# zeroclaw-host: the only non-root identity allowed to talk to dockerd.
# System account, no login shell, no password.
if ! id zeroclaw-host >/dev/null 2>&1; then
    sudo useradd --system --create-home --shell /usr/sbin/nologin zeroclaw-host
fi
sudo usermod -aG docker zeroclaw-host

# Resolve the tag to a digest, pull by digest, save. The running
# scrummaster is always at the digest resolved here; re-pointing the
# upstream tag has no effect until the next Packer rebuild.
sudo systemctl start docker

DIGEST=$(sudo docker buildx imagetools inspect "${ZEROCLAW_REPO}:${ZEROCLAW_IMAGE_TAG}" \
    --format '{{json .Manifest}}' | jq -r '.digest')
if [[ ! "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "ERROR: could not resolve ${ZEROCLAW_REPO}:${ZEROCLAW_IMAGE_TAG} to a digest (got '${DIGEST}')"
    exit 1
fi
IMAGE_REF="${ZEROCLAW_REPO}@${DIGEST}"
echo "Resolved ${ZEROCLAW_REPO}:${ZEROCLAW_IMAGE_TAG} -> ${IMAGE_REF}"

sudo docker pull --platform linux/arm64 "$IMAGE_REF"

sudo mkdir -p "$IMAGE_DIR"
sudo sh -c "docker save '${IMAGE_REF}' | gzip > '${IMAGE_DIR}/zeroclaw.tar.gz'"
echo "$IMAGE_REF" | sudo tee "${IMAGE_DIR}/zeroclaw.ref" >/dev/null

# Install the systemd units. The template unit carries a
# @ZEROCLAW_IMAGE_DIGEST@ placeholder that is replaced with the digest
# resolved above, so the unit never references a mutable tag.
sudo sed "s|@ZEROCLAW_IMAGE_DIGEST@|${DIGEST}|g" /tmp/zeroclaw@.service \
    | sudo tee /etc/systemd/system/zeroclaw@.service >/dev/null
sudo cp /tmp/zeroclaw-image-pull.service /etc/systemd/system/zeroclaw-image-pull.service
sudo chmod 0644 /etc/systemd/system/zeroclaw@.service /etc/systemd/system/zeroclaw-image-pull.service
sudo systemctl daemon-reload

sudo ls -lh "$IMAGE_DIR"
echo "Installed: $(aws --version 2>&1 | head -1), $(git --version), $(jq --version), $(sudo docker --version)"
echo "zeroclaw-host groups: $(id -nG zeroclaw-host)"
