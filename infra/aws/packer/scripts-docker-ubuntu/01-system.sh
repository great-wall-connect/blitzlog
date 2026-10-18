#!/bin/bash
# scripts-docker-ubuntu/01-system.sh — Ubuntu 26.04 host setup.
#
# Runs first in the Packer build on Canonical's Ubuntu 26.04 LTS arm64 base
# AMI. Installs dockerd (not preinstalled on the upstream image), awscli,
# git, jq. All commands run with sudo because Packer SSHs in as the
# unprivileged `ubuntu` user.

set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg \
    awscli jq openssl

# Docker's official Ubuntu repo (installs dockerd + CLI)
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Ubuntu 26.04 (resolute)
echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu resolute stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

sudo apt-get update
sudo apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io

sudo apt-get clean
sudo rm -rf /var/lib/apt/lists/*

echo "Installed: $(aws --version 2>&1 | head -1), $(git --version), $(jq --version), $(sudo docker --version)"
