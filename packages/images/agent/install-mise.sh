#!/bin/sh
# /usr/local/bin/install-mise.sh — install mise and make sure it can
# actually be executed.
#
# Why this is a separate script (issue #164):
#
#   The container runs as an unprivileged uid, and there are two ways
#   the mise bootstrap can end with a mise that cannot be run:
#
#   1. Docker's `--tmpfs` mounts a tmpfs with `noexec` unless the
#      option string says otherwise, and mise lives on the HOME tmpfs
#      (`$HOME/.local/bin/mise`). With `noexec` in place:
#
#        /usr/local/bin/entrypoint.sh: 653: mise: Permission denied
#
#      (dash exits 126) — the container died before the agent started.
#      The tmpfs mounts now carry `exec` (see
#      packages/images/agent/container-hardening.env); this script
#      cannot fix that, but it can REPORT it.
#
#   2. mise's own install script only makes the binary executable when
#      it runs as ROOT:
#
#        if [ "$(id -u)" = "0" ]; then chown 0:0 mise/bin/mise; chmod 755 mise/bin/mise; fi
#
#      As the container's unprivileged uid the installed binary can
#      keep whatever mode the tarball carried. This script does the
#      chmod itself.
#
#   Either way the result is a mise that cannot be executed, so this
#   script VERIFIES that the binary runs: a broken toolchain must fail
#   loudly with a diagnosable message, not as an exec error three
#   lines later.
#
# Usage:
#   install-mise.sh [install-path]
#
#   install-path  where mise lives. Defaults to $MISE_INSTALL_PATH,
#                 else $HOME/.local/bin/mise (mise's own default).
#
#   Prints the path of the usable mise binary on stdout.
#   Exit code 0 = mise is installed and runnable, 1 = it is not.
#
# The image's smoke test calls this script with a deliberately
# non-executable fake binary, so the chmod path is covered by CI —
# the entrypoint and the smoke test can therefore never disagree about
# what "mise is installed" means.
set -eu

MISE_BIN="${1:-}"
if [ -z "$MISE_BIN" ]; then
    MISE_BIN="${MISE_INSTALL_PATH:-$HOME/.local/bin/mise}"
fi

# Only install when the binary is MISSING. A present-but-not-executable
# binary is fixed below — re-downloading mise would not fix a mode.
if [ ! -f "$MISE_BIN" ]; then
    # `|| true` so a failed download falls through to the checks below,
    # which report WHY mise is unusable; the installer's own error is
    # often just "command not found".
    wget -q -O - https://mise.run | sh || curl -fsSL https://mise.run | sh || true
fi

if [ -f "$MISE_BIN" ] && [ ! -x "$MISE_BIN" ]; then
    # mise's installer only chmods the binary when it runs as root.
    chmod +x "$MISE_BIN" 2>/dev/null || true
fi

if [ ! -f "$MISE_BIN" ]; then
    printf 'install-mise: mise could not be installed at %s\n' "$MISE_BIN" >&2
    exit 1
fi

if ! "$MISE_BIN" --version >/dev/null 2>&1; then
    printf 'install-mise: mise is installed at %s but cannot be executed (mode %s)\n' \
        "$MISE_BIN" "$(stat -c '%a' "$MISE_BIN" 2>/dev/null || echo '?')" >&2
    exit 1
fi

printf '%s\n' "$MISE_BIN"
