#!/usr/bin/env bash
# packages/images/agent/smoke-test.sh — runs inside the built image to
# verify the toolchain is intact. Catches broken installs early without
# needing env vars or a real whisper model.
#
# One check needs the network: the package-install check proves that a
# project's `bootstrap` task can install its own build dependencies
# inside the container (issue #174). It is skipped — loudly — when the
# network is unavailable; in CI it always runs.
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
sh -n /usr/local/bin/blitzlog-credential-helper
sh -n /usr/local/bin/install-mise.sh

echo "==> checking mise can be installed AND executed"
# Issue #164: two things can leave mise installed but unusable, and
# BOTH have to be checked here:
#
#   1. Docker mounts every `--tmpfs` with `noexec` unless the option
#      string says otherwise. The tmpfs mounts carry `exec` explicitly
#      (see container-hardening.env), because mise's toolchain lives
#      on the HOME tmpfs and the agent has to run it. Without `exec`
#      the first `mise` call dies with "mise: Permission denied"
#      (exit 126) and the container never starts the agent.
#   2. mise's own install script only chmods the binary when it runs
#      as root.
#
# install-mise.sh does the chmod itself and then verifies that the
# binary runs. Assert that here with a deliberately non-executable
# fake binary — no network involved, and a `noexec` mount fails the
# same way the real mise did.
SMOKE_MISE="$(mktemp -d)/mise"
printf '#!/bin/sh\necho "mise 0.0.0"\n' > "$SMOKE_MISE"
chmod 0644 "$SMOKE_MISE"
test -x "$SMOKE_MISE" && { echo "FAIL: the fake mise should not be executable yet"; exit 1; }
SMOKE_MISE_PATH="$(/usr/local/bin/install-mise.sh "$SMOKE_MISE")" \
  || { echo "FAIL: install-mise.sh could not make the binary executable (is the tmpfs noexec?)"; exit 1; }
test -x "$SMOKE_MISE" \
  || { echo "FAIL: install-mise.sh left the binary non-executable"; exit 1; }
test "$SMOKE_MISE_PATH" = "$SMOKE_MISE" \
  || { echo "FAIL: install-mise.sh printed '$SMOKE_MISE_PATH', expected '$SMOKE_MISE'"; exit 1; }
rm -rf "$(dirname "$SMOKE_MISE")"
echo "install-mise.sh makes a non-executable mise runnable"

echo "==> checking the read-only GitHub credential helper"
# Issue #164: the credential file is owned by the host and mounted
# READ-ONLY into the container. `git credential-store` rewrites that
# file under a lock when git approves a credential after a successful
# auth, which fails on a read-only mount with:
#
#   fatal: unable to get credential storage lock in 1000 ms: Read-only file system
#
# The container therefore ships a get-only helper. Assert that it
# returns the credential AND that it never writes the file — the whole
# point of it is that it is safe to point at a read-only mount.
test -x /usr/local/bin/blitzlog-credential-helper \
  || { echo "FAIL: blitzlog-credential-helper is missing or not executable"; exit 1; }
SMOKE_CREDS="$(mktemp)"
printf 'https://x-access-token:smoke-test-token@github.com\n' > "$SMOKE_CREDS"
SMOKE_CREDS_BEFORE="$(cat "$SMOKE_CREDS")"
SMOKE_HELPER_OUT="$(BLITZLOG_GIT_CREDENTIAL_FILE="$SMOKE_CREDS" \
  /usr/local/bin/blitzlog-credential-helper get < /dev/null)"
case "$SMOKE_HELPER_OUT" in
  *"username=x-access-token"*"password=smoke-test-token"*) ;;
  *)
    echo "FAIL: credential helper returned '$SMOKE_HELPER_OUT'"
    rm -f "$SMOKE_CREDS"
    exit 1
    ;;
esac
# The store/erase paths must be no-ops: the file is read-only in
# production and must stay byte-identical.
BLITZLOG_GIT_CREDENTIAL_FILE="$SMOKE_CREDS" \
  /usr/local/bin/blitzlog-credential-helper store < /dev/null
BLITZLOG_GIT_CREDENTIAL_FILE="$SMOKE_CREDS" \
  /usr/local/bin/blitzlog-credential-helper erase < /dev/null
if [ "$(cat "$SMOKE_CREDS")" != "$SMOKE_CREDS_BEFORE" ]; then
  echo "FAIL: credential helper modified the credential file"
  rm -f "$SMOKE_CREDS"
  exit 1
fi
rm -f "$SMOKE_CREDS"
echo "credential helper returns the token and never writes the file"

echo "==> checking the container has NO capabilities"
# Issue #174: the container runs as uid 0, because dpkg needs root to
# unpack a package and write its own database (a project's `bootstrap`
# task installs its build dependencies — see docs/BOOTSTRAP.md). What
# keeps that bounded is the capability set, not the uid: the caller
# passes --cap-drop=ALL, so the agent cannot mount anything, cannot
# bypass file permissions, cannot make raw sockets and cannot escalate.
#
# CapEff (the effective capability mask) is the runtime proof: a run
# that loses --cap-drop=ALL fails here instead of silently handing the
# agent the default capability set.
AGENT_CAPS=$(awk '/^CapEff:/ {print $2}' /proc/self/status)
if [ "$AGENT_CAPS" != "0000000000000000" ]; then
  echo "FAIL: the container has effective capabilities ($AGENT_CAPS) — --cap-drop=ALL is not in effect"
  exit 1
fi
echo "no effective capabilities (CapEff=$AGENT_CAPS)"

echo "==> checking the image ships no compiler"
# The build toolchain belongs to the PROJECT, not to the agent image:
# every repo's `bootstrap` task declares what it needs and installs it
# (docs/BOOTSTRAP.md). The image therefore stays lean, and the
# container's package manager is what makes the project's own toolchain
# available.
for tool in gcc g++ cc make; do
  if command -v "$tool" >/dev/null 2>&1; then
    echo "FAIL: the agent image ships '$tool' — the project installs its own toolchain"
    exit 1
  fi
done
echo "the image ships no compiler"

echo "==> checking the container can install packages"
# This is the regression that issue #174 is about: with a read-only
# rootfs and a non-root uid, `apt-get install` died with
#
#   E: List directory /var/lib/apt/lists/partial is missing.
#      - Acquire (30: Read-only file system)
#
# and the agent never started. A project's `bootstrap` task MUST be
# able to install its build dependencies, so prove it here — under the
# production hardening flags, in the real image.
#
# `gcc` is the package the target repos actually ask for (the Rust
# recipe in docs/BOOTSTRAP.md installs it), and it is the one with the
# interesting bits: its postinst registers the `cc` alternative, which
# is the driver Rust's `cc` crate looks for. A smaller package would
# not exercise that path.
#
# It needs the network (apt has to fetch the index), so the check is
# skipped — loudly — when there is none.
if ! apt-get update >/dev/null 2>&1; then
  echo "SKIP: apt-get update failed (no network?) — the package-install path was not exercised"
elif ! apt-get install -y --no-install-recommends gcc >/dev/null 2>&1; then
  echo "FAIL: apt-get install failed — a project's bootstrap task cannot install its toolchain"
  exit 1
elif ! command -v gcc >/dev/null 2>&1; then
  echo "FAIL: apt-get install reported success but gcc is not on PATH"
  exit 1
elif ! cc --version >/dev/null 2>&1; then
  echo "FAIL: cc does not work — the alternatives were not set up"
  exit 1
else
  echo "apt-get update + apt-get install work inside the container (gcc, cc)"
fi

echo "==> checking the agent's HOME is writable"
# HOME is a tmpfs, so the agent's global git config, mise's toolchain,
# the npm cache and opencode's config + state stay out of the
# container's writable layer.
test -w "$HOME" \
  || { echo "FAIL: HOME ($HOME) is not writable"; exit 1; }
echo "HOME=$HOME is writable"

echo "==> checking the agent's config dir is writable"
# $HOME/.config needs its own tmpfs, not just the $HOME one. Docker
# creates the missing parent of a bind mount as root:root 0755, so the
# gh config's mount point ($HOME/.config/gh) makes Docker create
# $HOME/.config itself. The contract mounts a uid-owned tmpfs there so
# the mount point has a known owner — the container has no
# CAP_DAC_OVERRIDE, so it cannot fall back on "root sees everything".
# The CI step mounts a gh config at the same path production does, so
# this assertion sees the real topology.
test -w "$HOME/.config" \
  || { echo "FAIL: $HOME/.config is not writable by uid $(id -u) — it is probably a Docker-created bind-mount parent (root:root); it needs its own tmpfs (see container-hardening.env)"; exit 1; }
echo "$HOME/.config is writable"

echo "==> checking the baked opencode components land in HOME"
# The components are baked into /opt/blitzlog/opencode (NOT under
# $HOME — HOME is a tmpfs and would shadow them) and copied into
# $HOME/.config/opencode by install-opencode-plugins.sh, which the
# entrypoint also calls. Running it here proves the copy works.
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
# Assert existence — do NOT mkdir. A missing directory is the bug we
# want to report, not paper over.
for d in /opt/whisper-stt/models /opt/blitzlog/opencode /workspace /var/log; do
  test -d "$d" || { echo "missing dir: $d"; exit 1; }
done

echo "smoke-test: all checks passed"
