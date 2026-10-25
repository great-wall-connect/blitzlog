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

# Install the watchdog (uploaded to /tmp/blitzlog-watchdog.sh by the
# preceding `file` provisioner; Packer only uploads the shell script
# itself, not sibling files). Use cp + chmod rather than `install` —
# the `install` binary isn't reliably present on the Ubuntu 26.04
# minimal arm64 base.
sudo cp /tmp/blitzlog-watchdog.sh /usr/local/bin/watchdog.sh
sudo chmod 0755 /usr/local/bin/watchdog.sh

# Install the host-side S3 abstraction (issue #123). Sourced by
# watchdog.sh at boot and by the Lambda-bootstrap user-data heredoc
# (cf. lambda/scripts/_common.py::script_header) at first boot. One
# canonical call site per S3 verb across watchdog.sh, 03-bake-images.sh,
# lambda/scripts/assisted.py, and lambda/scripts/autonomous.py.
sudo cp /tmp/blitzlog-uploader.sh /usr/local/bin/blitzlog_uploader.sh
sudo chmod 0755 /usr/local/bin/blitzlog_uploader.sh

# Install the host-side token-refresh poll loop (issue #145). Sourced
# by watchdog.sh at boot to keep the bind-mounted GitHub credential
# files in sync with the Lambda's 50-min refresh chain.
sudo cp /tmp/blitzlog-token-poll.sh /usr/local/bin/blitzlog_token_poll.sh
sudo chmod 0755 /usr/local/bin/blitzlog_token_poll.sh

# Install the canonical container-hardening contract (issue #164).
# Sourced by watchdog.sh at boot so the container it launches carries
# the hardening flags (--security-opt=no-new-privileges:true,
# --cap-drop=ALL, --pids-limit, --init) and the one documented
# Tailscale capability exemption. The file is plain KEY=value data, so
# 0644 is enough — the watchdog runs as root and reads it.
sudo cp /tmp/blitzlog-container-hardening.env /usr/local/bin/container-hardening.env
sudo chmod 0644 /usr/local/bin/container-hardening.env

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
