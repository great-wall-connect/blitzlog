#!/bin/bash
# scripts-docker-ubuntu/01-system.sh — Ubuntu 24.04 host setup.
#
# Runs first in the Packer build on Canonical's Ubuntu 24.04 LTS arm64 base
# AMI. Installs dockerd (not preinstalled on the upstream image), awscli,
# git, jq.

set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg \
    awscli git jq openssl

# Docker's official Ubuntu repo (installs dockerd + CLI)
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

# Ubuntu 24.04 (noble)
echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu noble stable" \
    > /etc/apt/sources.list.d/docker.list

apt-get update
apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io

apt-get clean
rm -rf /var/lib/apt/lists/*

echo "Installed: $(aws --version 2>&1 | head -1), $(git --version), $(jq --version), $(docker --version)"
