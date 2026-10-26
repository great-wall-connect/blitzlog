#!/usr/bin/env bash
# packages/images/agent/smoke-test.sh — runs inside the built image to
# verify the toolchain is intact. Catches broken installs early without
# needing env vars, network, or a real whisper model.
#
# Pair with the entrypoint smoke test (Tier 2 in docs/DOCKER.md) for
# full coverage: the entrypoint actually tries to git clone and call
# opencode, which requires network and real LLM auth.
set -euo pipefail

echo "==> checking whisper-cli"
/usr/local/bin/whisper-cli --help >/dev/null

echo "==> checking node + npm"
/usr/local/bin/node --version
/usr/local/bin/npm --version

echo "==> checking opencode CLI"
/usr/local/bin/opencode --version

echo "==> checking telegram bot binary"
test -x /usr/local/bin/opencode-telegram \
  || { echo "missing /usr/local/bin/opencode-telegram"; exit 1; }

echo "==> checking Python deps"
python3 -c "import pywhispercpp, multipart, imageio_ffmpeg; print('python deps ok')"

echo "==> checking whisper-stt-shim"
test -f /opt/whisper-stt/server.py

echo "==> checking entrypoint syntax"
# Both checks: the container's runtime shell is /bin/sh -> dash on
# python:3.12-slim, so `sh -n` matches what actually runs. `bash -n` is
# kept for wider compatibility — bash accepts a superset of POSIX, so a
# script that passes `sh -n` will also pass `bash -n`. Running both in
# this order would surface bash-only constructs (e.g., ${var:0:8},
# ${PIPESTATUS[…]}) that would crash under /bin/sh at container boot —
# the regression caught in PR #89.
sh -n /usr/local/bin/entrypoint.sh
bash -n /usr/local/bin/entrypoint.sh
sh -n /usr/local/bin/install-opencode-plugins.sh

echo "==> checking the container does NOT run as root"
# Issue #164: the agent container runs as an unprivileged uid. The
# image's USER directive sets it to `blitzlog` (uid 1000); this
# assertion is the runtime proof, so a Dockerfile that loses its USER
# directive fails here rather than silently running the agent as root.
AGENT_UID=$(id -u)
if [ "$AGENT_UID" = "0" ]; then
  echo "FAIL: the container is running as root (uid 0)"
  exit 1
fi
echo "running as uid $AGENT_UID ($(id -un))"

echo "==> checking the agent's HOME is writable"
# HOME is a tmpfs (the rootfs is read-only), so the agent can write
# its global git config, mise's toolchain, the npm cache and opencode's
# config + state — and nothing else.
test -w "$HOME" \
  || { echo "FAIL: HOME ($HOME) is not writable"; exit 1; }
echo "HOME=$HOME is writable"

echo "==> checking the agent's config dir is writable"
# $HOME/.config needs its own uid-owned tmpfs, not just the $HOME one.
# Docker creates the missing parent of a bind mount as root:root 0755,
# so the gh config's mount point ($HOME/.config/gh) makes Docker create
# $HOME/.config as root — and the agent (uid 1000) then cannot create
# $HOME/.config/opencode under it:
#
#   mkdir: cannot create directory '/home/blitzlog/.config/opencode':
#   Permission denied
#
# which aborted the entrypoint before the agent ever started. The CI
# step mounts a gh config at the same path production does, so this
# assertion sees the real topology.
test -w "$HOME/.config" \
  || { echo "FAIL: $HOME/.config is not writable by uid $(id -u) — it is probably a Docker-created bind-mount parent (root:root); it needs its own uid-owned tmpfs (see container-hardening.env)"; exit 1; }
echo "$HOME/.config is writable"

echo "==> checking the baked opencode components land in HOME"
# The components are baked into /opt/blitzlog/opencode (NOT under
# $HOME — HOME is a tmpfs and would shadow them) and copied into
# $HOME/.config/opencode by install-opencode-plugins.sh, which the
# entrypoint also calls. Running it here proves the copy works for an
# unprivileged user.
for d in plugins tools lib; do
  test -d "/opt/blitzlog/opencode/$d" \
    || { echo "missing baked component /opt/blitzlog/opencode/$d"; exit 1; }
done
/usr/local/bin/install-opencode-plugins.sh
for d in plugins tools lib; do
  test -d "$HOME/.config/opencode/$d" \
    || { echo "missing $HOME/.config/opencode/$d"; exit 1; }
done

echo "==> checking expected dirs"
# Assert existence — do NOT mkdir. The container's rootfs is read-only,
# so creating a missing directory here would fail with EROFS; a missing
# directory is the bug we want to report, not paper over.
for d in /opt/whisper-stt/models /opt/blitzlog/opencode /workspace /var/log; do
  test -d "$d" || { echo "missing dir: $d"; exit 1; }
done

echo "smoke-test: all checks passed"
