#!/bin/bash
# infra/alicloud/packer/scripts-ecs-ubuntu/01-system.sh — Ubuntu host setup.
#
# The Alicloud analogue of
# infra/aws/packer/scripts-docker-ubuntu/01-system.sh.
#
# Runs first in the Packer build on a Canonical-published Ubuntu base
# image. Installs dockerd (not preinstalled on the upstream image),
# the `aliyun` CLI, `ossutil`, git, jq.
#
# Why the Aliyun CLIs are installed HERE rather than by the user-data
# prologue: on the AWS side the same split exists (awscli is baked
# into the AMI), and the user-data install in
# lambda/scripts/alicloud/_common.py was a workaround for the missing
# Alicloud AMI. Now that the Alicloud AMI ships these CLIs, the
# user-data prologue installs them only as a fallback (see
# _ALIYUN_CLI_INSTALL / _OSSUTIL_INSTALL — both are `command -v`
# guarded, so a baked image skips the download).
#
# All commands run with sudo because Packer SSHs in as the
# unprivileged `ubuntu` user.

set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update
# `iptables` is installed explicitly (issue #175): the watchdog installs
# a DOCKER-USER rule that blocks the agent container from reaching the
# Aliyun instance metadata service, and it REFUSES to start the agent
# if the rule cannot be installed. It must not depend on iptables
# arriving as a transitive dependency of docker-ce.
sudo apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg \
    jq openssl \
    kmod iptables

# Docker's official Ubuntu repo (installs dockerd + CLI)
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Ubuntu 26.04 (resolute) — the same base the AWS AMI bakes on, so the
# two trees stay comparable.
echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu resolute stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

sudo apt-get update
sudo apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io

# The `aliyun` CLI — the Alicloud analogue of awscli. Used for:
#   * `aliyun kms GetSecretValue` (host-side secret reads in the
#     user-data prologue)
#   * `aliyun ecs DeleteInstances` (the watchdog's instance teardown)
#   * `aliyun configure` (RAM role → credential chain)
#
# The version is pinned the same way the AWS side pins nothing (awscli
# comes from apt). The tarball is the official one from Aliyun's CDN.
if ! command -v aliyun >/dev/null 2>&1; then
    echo "Installing aliyun CLI..."
    cd /tmp
    curl -fsSL -o aliyun-cli.tgz \
        https://aliyuncli.alicdn.com/aliyun-cli-linux-3.0.6-amd64.tgz
    tar -xzf aliyun-cli.tgz
    sudo mv aliyun /usr/local/bin/aliyun
    sudo chmod +x /usr/local/bin/aliyun
    rm -f aliyun-cli.tgz
fi

# `ossutil` — the OSS analogue of `aws s3`. The AWS-side host library
# (blitzlog_uploader.sh) wraps `aws s3 cp/rm/ls`; the Alicloud-side
# library (blitzlog_oss_uploader.sh) wraps these ossutil verbs.
if ! command -v ossutil >/dev/null 2>&1; then
    echo "Installing ossutil..."
    cd /tmp
    curl -fsSL -o ossutil64 \
        https://gosspublic.alicdn.com/ossutil/2.0.5/ossutil64
    sudo chmod +x ossutil64
    sudo mv ossutil64 /usr/local/bin/ossutil
    rm -f ossutil64
fi

sudo apt-get clean
sudo rm -rf /var/lib/apt/lists/*

# Issue #164: the agent container joins the Tailnet from INSIDE the
# container, and the Tailscale daemon needs the /dev/net/tun character
# device to build the WireGuard tunnel. A minimal Ubuntu image does
# not have that device until the `tun` module is loaded.
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

echo "Installed: $(aliyun version 2>&1 | head -1), $(ossutil version 2>&1 | head -1), $(git --version), $(jq --version), $(sudo docker --version)"
