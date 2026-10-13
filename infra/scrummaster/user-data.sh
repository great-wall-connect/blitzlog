#!/bin/bash
# user-data.sh — first-boot installer for the blitzlog scrummaster host.
#
# Runs as root (cloud-init user-data is always root). Installs the
# ZeroClaw binary from https://github.com/zeroclaw-labs/zeroclaw/releases,
# lays down the three required directories, and writes a placeholder
# /etc/zeroclaw/config.toml that fails closed (no providers, no
# channels) so a half-provisioned daemon cannot accidentally talk to a
# provider or post to Telegram. The provider / channel wiring ships in
# #137 / #138 / #139; this script deliberately stops short of writing
# a service unit so the host stays inert until then.
#
# Failure semantics: every step uses `set -euo pipefail` and logs to
# /var/log/zeroclaim-bootstrap.log (the path mirrors the agent host's
# /var/log/backend-bootstrap.log convention) so an operator reading the
# SSM session manager transcript or the journalctl can see exactly
# where the install failed.

set -euo pipefail

LOG=/var/log/zeroclaw-bootstrap.log
exec > >(tee -a "$LOG") 2>&1
echo "=== zeroclaw bootstrap started at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# Pin the ZeroClaw release. `latest` is the issue #135 default (resolves
# to /releases/latest at apply-time via Terraform templating in main.tf);
# operators who want reproducibility override var.zeroclaw_release_tag.
# A real tag (e.g. v0.1.3) maps to
# https://github.com/zeroclaw-labs/zeroclaw/releases/download/${tag}/...
ZEROCLAW_RELEASE="${zeroclaw_release}"
ZEROCLAW_VERSION="${zeroclaw_version}"
echo "Installing ZeroClaw release=${ZEROCLAW_RELEASE} version=${ZEROCLAW_VERSION}"

# apt-get update is cheap on a fresh Ubuntu minimal image (the cache
# is empty). Install jq so future ZeroClaw version probes / log
# scrapes can grep structured JSON without a separate install.
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl jq

ARCH="$(uname -m)"
case "$ARCH" in
    aarch64) ZEROCLAW_ARCH=arm64 ;;
    x86_64)  ZEROCLAW_ARCH=amd64 ;;
    *)
        echo "FATAL: unsupported architecture: $ARCH" >&2
        exit 1
        ;;
esac

# `/releases/latest` redirects to the most recent tag. `-L` follows
# the redirect; `-f` fails fast on HTTP errors; `-o` writes the body
# to disk. `--silent --show-error` keeps the journalctl output quiet
# unless something goes wrong.
RELEASE_URL="https://github.com/zeroclaw-labs/zeroclaw/releases/download/${ZEROCLAW_RELEASE}/zeroclaw-linux-${ZEROCLAW_ARCH}.tar.gz"
TMP_TARBALL="$(mktemp --suffix=.tar.gz)"
echo "Downloading $RELEASE_URL"
curl --location --fail --silent --show-error \
    --output "$TMP_TARBALL" \
    "$RELEASE_URL"

# Lay down the three directories before extracting the binary so the
# installer can drop its own config skeleton into /etc/zeroclaw/
# alongside the placeholder config.toml below.
install -d -m 0755 /etc/zeroclaw
install -d -m 0750 /var/lib/zeroclaw
install -d -m 0755 /var/log/zeroclaw

# /usr/local/bin is on PATH for both root and the default `ubuntu`
# user (when SSH'd in via Session Manager). strip-components=1 lets
# the archive's internal layout stay canonical without nesting a
# zeroclaw-linux-arm64/ directory on disk.
tar -xzf "$TMP_TARBALL" -C /usr/local/bin --strip-components=1
chmod 0755 /usr/local/bin/zeroclaw
rm -f "$TMP_TARBALL"

# Placeholder config that fails closed. No [providers.*] entries
# means ZeroClaw refuses to start an LLM round-trip; no
# [channels.*] entries means no Telegram token is required and no
# outbound message can be sent. Operators wire up real config in
# #137 / #138 / #139 by templating this file per user-pool user
# (one ZeroClaw daemon per user, isolated by --config-dir).
cat > /etc/zeroclaw/config.toml <<'CONFIG_EOF'
# Placeholder config — fails closed (issue #135).
#
# Providers, channels, and tools are intentionally absent. Until
# they are populated, ZeroClaw refuses to make outbound calls or
# accept inbound messages. The [meta] block is the minimum a
# `zeroclaw --check-config` run needs to parse without erroring.
[meta]
version     = "0"
description = "Scrummaster placeholder (issue #135) — providers / channels are wired by #137 / #138 / #139"

# Intentionally empty: no [providers.openai_compat.*], no
# [channels.telegram.*], no [[tools]]. `zeroclaw --check-config`
# should report this file as "valid but inert".

[security]
workspace_only = true
CONFIG_EOF
chmod 0640 /etc/zeroclaw/config.toml

echo "ZeroClaw installed: $(command -v zeroclaw)"
echo "Version probe:"
zeroclaw --version || echo "(zeroclaw --version failed; daemon not yet wired up — see #136)"

echo "Directories:"
ls -ld /etc/zeroclaw /var/lib/zeroclaw /var/log/zeroclaw

echo "=== zeroclaw bootstrap finished at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="