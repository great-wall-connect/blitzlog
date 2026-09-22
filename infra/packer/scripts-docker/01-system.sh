#!/bin/bash
# scripts-docker/01-system.sh — Amazon Linux 2023 host setup.
#
# Runs first in the Packer build on the ECS-optimized AL2023 arm64 base AMI.
# dockerd is already installed; we add awscli, git, jq for the host-side
# user-data and watchdog.

set -euo pipefail

dnf install -y awscli git jq
echo "Installed: $(aws --version 2>&1 | head -1), $(git --version), $(jq --version)"
