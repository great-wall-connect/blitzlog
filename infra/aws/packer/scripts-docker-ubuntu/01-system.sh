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
# `iptables` is installed explicitly (issue #175): the watchdog installs a
# DOCKER-USER rule that blocks the agent container from reaching the
# instance metadata service, and it REFUSES to start the agent if the
# rule cannot be installed. It must not depend on iptables arriving as a
# transitive dependency of docker-ce.
sudo apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg \
    awscli jq openssl \
    kmod iptables

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

# Issue #164: the agent container joins the Tailnet from INSIDE the
# container, and the Tailscale daemon needs the /dev/net/tun character
# device to build the WireGuard tunnel. A minimal Ubuntu AMI does not
# have that device until the `tun` module is loaded.
#
# Load the module and create the device node explicitly — we do not
# rely on udev to do it, because udev is not guaranteed to be running
# on the minimal image at bake time. The device node is 0666 so the
# container can open it when the run enables the Tailnet path
# (`--device /dev/net/tun`).
sudo modprobe tun || true
if [ ! -e /dev/net/tun ]; then
    sudo mkdir -p /dev/net
    sudo mknod /dev/net/tun c 10 200
    sudo chmod 0666 /dev/net/tun
fi

echo "Installed: $(aws --version 2>&1 | head -1), $(git --version), $(jq --version), $(sudo docker --version)"
