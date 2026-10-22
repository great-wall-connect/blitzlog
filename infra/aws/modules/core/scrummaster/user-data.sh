#!/bin/bash
# infra/aws/modules/core/scrummaster/user-data.sh — scrummaster first-boot setup (issue #135).
#
# Deliberately minimal. Docker, the zeroclaw-host user, the baked image
# tarball and both systemd units already live in the AMI (Packer,
# scripts-scrummaster-ubuntu/01-system.sh). This script only:
#   - creates the three root-owned parent dirs for per-user bind mounts
#   - enables the one-shot zeroclaw-image-pull.service (runs on every
#     subsequent boot; the image is already in dockerd's store from the
#     Packer bake, so the first boot does not need it)
#
# Out of scope here: ZeroClaw binaries (it only ever runs in Docker),
# per-user accounts and dirs (#136), config.toml (#137).

set -euo pipefail

install -d -o root -g root -m 0755 /var/lib/zeroclaw /etc/zeroclaw /var/log/zeroclaw

systemctl enable zeroclaw-image-pull.service
