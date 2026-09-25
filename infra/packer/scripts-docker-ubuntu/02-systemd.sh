#!/bin/bash
# scripts-docker-ubuntu/02-systemd.sh — Ubuntu 26.04 systemd setup.
#
# Enables docker.service, installs the host-side watchdog systemd unit,
# and writes the load-image helper. The watchdog (watchdog.sh) runs the
# blitzlog-agent container, waits for it to exit, and does all
# AWS-dependent cleanup (S3 upload + EC2 terminate + bot lock release).
# All commands run with sudo because Packer SSHs in as the unprivileged
# `ubuntu` user.

set -euo pipefail

sudo systemctl enable docker

sudo mkdir -p /opt/blitzlog/images /opt/whisper-stt/models

# load-image.sh — small helper for manual `docker load` of all baked images.
sudo tee /usr/local/bin/load-image.sh >/dev/null <<'LOAD_IMAGE_EOF'
#!/bin/bash
set -euo pipefail
for img in /opt/blitzlog/images/*.tar.gz; do
    [ -e "$img" ] || continue
    echo "[$(date '+%T')] docker load -i $img"
    sudo docker load -i "$img"
done
LOAD_IMAGE_EOF
sudo chmod +x /usr/local/bin/load-image.sh

# Install the watchdog (copied from infra/packer/scripts-docker-ubuntu/watchdog.sh
# at Packer build time).
sudo install -m 0755 \
    "${PACKER_DIR:-$(dirname "$0")}/watchdog.sh" \
    /usr/local/bin/watchdog.sh

# systemd unit that runs the watchdog on instance start. The watchdog
# runs the blitzlog-agent container, waits for it to exit, then does
# all AWS cleanup (S3 upload + EC2 terminate + bot-lock release).
sudo tee /etc/systemd/system/blitzlog-agent.service >/dev/null <<'UNIT_EOF'
[Unit]
Description=Blitzlog agent (runs blitzlog-agent container + handles cleanup)
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/blitzlog.env
ExecStart=/usr/local/bin/watchdog.sh
Restart=no
TimeoutStopSec=7200
StandardOutput=append:/var/log/backend-bootstrap.log
StandardError=append:/var/log/backend-bootstrap.log

[Install]
WantedBy=multi-user.target
UNIT_EOF
sudo systemctl enable blitzlog-agent.service

echo "Systemd setup complete"
