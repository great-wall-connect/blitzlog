#!/bin/bash
# scripts/aliyun-env.sh — export Alicloud credentials for tools that
# cannot read a CloudSSO profile on their own (the `oss` terraform
# backend).
#
# WHY THIS EXISTS
#
# The `aliyun` CLI understands a CloudSSO profile in
# ~/.aliyun/config.json: it attaches the `sts_token` to every
# request. The `oss` terraform backend does NOT — it signs with the
# access_key_id + access_key_secret but leaves the security token
# out, so OSS rejects the signature with:
#
#   ErrorCode=InvalidArgument, ErrorMessage="Authorization header is invalid."
#
# The credential itself is fine (the CLI works with the very same
# profile); only the backend's signing is incomplete. Exporting the
# three values explicitly completes the signature and the backend
# works.
#
# USAGE
#
#   source scripts/aliyun-env.sh      # export into the current shell
#   . scripts/aliyun-env.sh
#   scripts/aliyun-env.sh --print     # print the exports, apply nothing
#
# Then, for example:
#
#   cd infra/alicloud/bootstrap
#   source ../../scripts/aliyun-env.sh
#   terraform init -reconfigure -backend-config=bootstrap-backend.hcl
#   terraform plan
#
# It must be SOURCED, not executed: a script cannot export into its
# parent shell.
#
# EXPIRY
#
# A CloudSSO session is a short-lived STS credential (typically ~4 h).
# When it lapses, `terraform` starts failing with the auth error
# again. Fix:
#
#   aliyun configure --profile default   # re-login (browser)
#   source scripts/aliyun-env.sh
#
# The script warns on stderr when the token is expired or close to
# expiry, so you get told to re-login instead of a generic
# "Authorization header is invalid" from whatever tool failed.

# This script is normally SOURCED, so `set -euo pipefail` below would
# leave errexit and nounset switched on in the caller's shell. zsh keeps
# errexit off in interactive shells precisely so a failing command does
# not kill the session; a sourced script can turn it back on, and the
# first non-zero command then takes the whole shell down. Snapshot the
# caller's options and restore them on exit so only this script's body
# runs under strict mode.

__aliyun_env_errexit=off
__aliyun_env_nounset=off
__aliyun_env_pipefail=off
case $- in *e*) __aliyun_env_errexit=on ;; esac
case $- in *u*) __aliyun_env_nounset=on ;; esac
if (set -o | grep -q '^pipefail .*on$'); then __aliyun_env_pipefail=on; fi

# Restore the caller's options. The failure path calls this BEFORE
# bailing out: an EXIT trap would run too late, once the shell has
# already decided to exit.
__aliyun_env_restore_opts() {
    case "$__aliyun_env_errexit" in
        on) set -e ;;
        off) set +e ;;
    esac
    case "$__aliyun_env_nounset" in
        on) set -u ;;
        off) set +u ;;
    esac
    case "$__aliyun_env_pipefail" in
        on) set -o pipefail ;;
        off) set +o pipefail ;;
    esac
}

# Failure path. `return` rather than `exit` because a sourced script's
# `exit` terminates the calling shell; `return` only ends the script.
__aliyun_env_fail() {
    __aliyun_env_restore_opts
    return 1
}

set -euo pipefail

CONFIG_FILE="${ALICLOUD_CONFIG_FILE:-$HOME/.aliyun/config.json}"

if [ ! -r "$CONFIG_FILE" ]; then
    echo "aliyun-env: cannot read $CONFIG_FILE" >&2
    echo "  Run 'aliyun configure --profile default' first." >&2
    __aliyun_env_fail
    return 1 2>/dev/null || exit 1
fi

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [ -z "$PYTHON_BIN" ]; then
    echo "aliyun-env: python3 not found on PATH" >&2
    __aliyun_env_fail
    return 1 2>/dev/null || exit 1
fi

# Read the profile, emit the exports, and report the expiry.
#
# The profile is chosen by:
#   1. $ALICLOUD_PROFILE, if set
#   2. the "current" key in config.json
#   3. the first profile that carries credentials
#
# A stale "current" pointer should not leave the user with nothing,
# so the fallbacks exist — but the named profile wins when it is
# usable.
read_profile() {
    "$PYTHON_BIN" - "$CONFIG_FILE" <<'PY'
import json
import os
import sys
import time

config_path = sys.argv[1]
with open(config_path, encoding="utf-8") as fh:
    config = json.load(fh)

profiles = config.get("profiles") or []
if not profiles:
    sys.exit("aliyun-env: no profiles in the config file")

wanted = os.environ.get("ALICLOUD_PROFILE") or config.get("current")


def usable(profile):
    return bool(profile.get("access_key_id") and profile.get("access_key_secret"))


by_name = {p.get("name"): p for p in profiles if isinstance(p, dict)}

chosen = None
candidate = by_name.get(wanted)
if candidate and usable(candidate):
    chosen = candidate

if chosen is None:
    for profile in profiles:
        if isinstance(profile, dict) and usable(profile):
            chosen = profile
            break

if chosen is None:
    sys.exit("aliyun-env: no profile with an access_key_id/secret found")

ak = chosen["access_key_id"]
sk = chosen["access_key_secret"]
token = chosen.get("sts_token") or ""
region = chosen.get("region_id") or "cn-hongkong"


def q(value):
    return "'" + value.replace("'", "'\\''") + "'"


out = []
out.append(f"export ALICLOUD_ACCESS_KEY={q(ak)}")
out.append(f"export ALICLOUD_SECRET_KEY={q(sk)}")
if token:
    out.append(f"export ALICLOUD_SECURITY_TOKEN={q(token)}")
# The Alibaba Cloud provider's own chain uses the ALIBABA_CLOUD_*
# names, so export those too — that way `terraform plan` (the
# provider) works with the same sourced environment as `terraform
# init` (the backend).
out.append(f"export ALIBABA_CLOUD_ACCESS_KEY_ID={q(ak)}")
out.append(f"export ALIBABA_CLOUD_ACCESS_KEY_SECRET={q(sk)}")
if token:
    out.append(f"export ALIBABA_CLOUD_SECURITY_TOKEN={q(token)}")
out.append(f"export ALIBABA_CLOUD_REGION={q(region)}")
out.append(f"export ALICLOUD_REGION={q(region)}")

print("\n".join(out))

# Expiry check — advisory. It must not stop the exports from being
# usable, but the user should know to re-login.
expiry = chosen.get("sts_expiration")
if expiry:
    remaining = int(expiry) - int(time.time())
    if remaining <= 0:
        sys.stderr.write(
            "aliyun-env: WARNING: the CloudSSO token is EXPIRED. "
            "Run 'aliyun configure --profile default' and re-source.\n"
        )
    elif remaining < 900:
        sys.stderr.write(
            f"aliyun-env: WARNING: the CloudSSO token expires in "
            f"{remaining}s. Run 'aliyun configure --profile default' "
            f"soon.\n"
        )
PY
}

EXPORTS="$(read_profile)" || {
    __aliyun_env_fail
    return 1 2>/dev/null || exit 1
}

if [ "${1:-}" = "--print" ]; then
    printf '%s\n' "$EXPORTS"
    __aliyun_env_restore_opts
    exit 0
fi

# Apply to the CURRENT shell. A script cannot export into its
# parent, so `source` is required — hence the usage line above.
eval "$EXPORTS"

# Tell the user it worked, without echoing any secret material.
echo "aliyun-env: exported Alicloud credentials (region ${ALIBABA_CLOUD_REGION:-unknown})" >&2

__aliyun_env_restore_opts
