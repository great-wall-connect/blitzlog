#!/bin/bash
# infra/alicloud/packer/scripts-ecs-ubuntu/02-systemd.sh — Ubuntu systemd setup.
#
# The Alicloud analogue of
# infra/aws/packer/scripts-docker-ubuntu/02-systemd.sh.
#
# Enables docker.service, installs the host-side watchdog systemd unit,
# and writes the load-image helper. The watchdog (watchdog.sh) runs the
# blitzlog-agent container, waits for it to exit, and does all
# Alicloud-dependent cleanup (OSS upload + ECS terminate + bot lock
# release).
#
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
# the `install` binary isn't reliably present on the Ubuntu minimal
# arm64 base.
sudo cp /tmp/blitzlog-watchdog.sh /usr/local/bin/watchdog.sh
sudo chmod 0755 /usr/local/bin/watchdog.sh

# Install the host-side OSS abstraction (issue #125). Sourced by
# watchdog.sh at boot and — once the AMI ships it — by the
# Lambda-bootstrap user-data heredoc
# (lambda/scripts/alicloud/_common.py::script_header) at first boot.
# One canonical call site per OSS verb across watchdog.sh,
# 03-bake-images.sh, lambda/scripts/alicloud/assisted.py, and
# lambda/scripts/alicloud/autonomous.py.
sudo cp /tmp/blitzlog-oss-uploader.sh /usr/local/bin/blitzlog_oss_uploader.sh
sudo chmod 0755 /usr/local/bin/blitzlog_oss_uploader.sh

# Install the canonical container-hardening contract (issue #164).
# Sourced by watchdog.sh at boot so the container it launches carries
# the hardening flags (--security-opt=no-new-privileges:true,
# --cap-drop=ALL, --pids-limit, --init). The contract lives next to
# the image it describes (the agent package) so the dev tasks in
# mise.toml can source the very same file from the repo. The final
# copy to /usr/local/bin/ is done here.
sudo cp /tmp/blitzlog-container-hardening.env /usr/local/bin/container-hardening.env
sudo chmod 0644 /usr/local/bin/container-hardening.env

# systemd unit that runs the watchdog on instance start. The watchdog
# runs the blitzlog-agent container, waits for it to exit, then does
# all Alicloud cleanup (OSS upload + ECS terminate + bot-lock release).
#
# NOTE: the Alicloud tree has NO token-refresh poll loop
# (blitzlog_token_poll.sh). The AWS side has one because the AWS
# Lambda schedules an EventBridge Scheduler chain that re-mints the
# GitHub App installation token every 30 min (issue #145); Aliyun has
# no equivalent wired up yet (see
# infra/alicloud/modules/core/function_compute.tf), so the token is
# minted once at launch and the 1 h GitHub cap is operationally
# visible. The GITHUB_TOKEN_SSM_PARAM path is therefore not set on
# this path and the watchdog's token branch stays inert.
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
# stdout goes to the journal, NOT to the log file: the watchdog's own
# `log()` already appends every message to /var/log/backend-bootstrap.log
# (`printf ... | tee -a "$LOG_FILE"`). With stdout ALSO appended to the
# same file, every single line landed there twice — the instance log
# showed each message in duplicate. stderr still appends to the file,
# so anything the watchdog writes outside `log()` is still captured.
# The journal keeps the full stdout stream either way.
StandardOutput=journal
StandardError=append:/var/log/backend-bootstrap.log

[Install]
WantedBy=multi-user.target
UNIT_EOF
sudo systemctl enable blitzlog-agent.service

echo "Systemd setup complete"
