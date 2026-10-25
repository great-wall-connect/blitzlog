#!/bin/sh
# /usr/local/bin/install-opencode-plugins.sh — install the baked
# opencode components into the agent's HOME.
#
# Issue #164: the opencode plugins (idle_watchdog, periodic_autosave,
# session_archive, spot_watchdog), the tools (shutdown) and the shared
# lib are baked into /opt/blitzlog/opencode by the Dockerfile — NOT
# under $HOME. The container's rootfs is read-only and its HOME is a
# tmpfs, so anything baked under $HOME would be shadowed at runtime.
#
# This script copies the baked components into $HOME/.config/opencode
# so opencode finds them at startup. It is the single source of truth
# for that copy: the entrypoint calls it on every boot, and the
# image's smoke test calls it to assert the components land where
# opencode expects them — so the two can never disagree about what
# "installed" means.
#
# `cp -R`, not `cp -a`: `-a` preserves ownership, which an
# unprivileged user cannot do, and GNU cp exits non-zero when it fails
# to — which would abort the caller under `set -e`.
set -eu

BAKED_DIR="${BAKED_OPENCODE_DIR:-/opt/blitzlog/opencode}"
TARGET_DIR="${HOME:?HOME is not set}/.config/opencode"

for component in plugins tools lib; do
    if [ -d "$BAKED_DIR/$component" ]; then
        mkdir -p "$TARGET_DIR/$component"
        cp -R "$BAKED_DIR/$component/." "$TARGET_DIR/$component/"
    fi
done
