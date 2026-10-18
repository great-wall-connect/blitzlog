#!/bin/bash
# infra/aws/packer/scripts-docker-ubuntu/blitzlog_uploader.sh
#
# Single source of truth for every S3-shaped operation blitzlog
# performs on the EC2 instance and at Packer bake time. Sourced, not
# executed.
#
# Issue #123: replaces the inline `aws s3 cp/rm/ls` calls that were
# previously scattered across watchdog.sh, 03-bake-images.sh, and the
# Lambda-bootstrap user-data heredocs rendered by
# lambda/scripts/{assisted,autonomous}.py.
#
# AWS today; the function *names* are provider-neutral so a future
# Alicloud port (Phase 5) can swap `aws s3` → `ossutil` in the four
# bodies without touching the four call sites. The cross-cloud
# dispatch used by the Lambda (BLITZLOG_PROVIDER env var, see
# lambda/provider/__init__.py) does NOT apply here — the host AMI is
# single-cloud, so the backend is baked in.
#
# Logging discipline (issue #123 acceptance criteria):
#   * Every function returns the underlying `aws s3 ...` exit code.
#   * On non-zero exit, emits `ERROR: S3 <op> failed: bucket=... key=...
#     exit=...` via the caller's `log()` if defined, otherwise via an
#     internal append-to-/var/log/backend-bootstrap.log helper.
#   * Never swallows errors itself. Callers opt in to swallow via
#     `|| true` at the call site — best-effort semantics live with
#     the caller, not the library.

# --- internal logger (falls through to caller's log() if defined) ----
_bl_uploader_log() {
    if declare -F log >/dev/null 2>&1; then
        log "$1"
    else
        printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" \
            | tee -a /var/log/backend-bootstrap.log >&2
    fi
}

# --- internal AWS-region resolver -------------------------------------
# Resolution order: explicit $1 > $REGION > $AWS_REGION > "" (let
# `aws s3` fall back to its own configured default).
#
# $REGION is set on the EC2 host by watchdog.sh from IMDS at line 49.
# $AWS_REGION is set by Packer at bake time (03-bake-images.sh) and
# by the Lambda-rendered user-data bootstrap env. Centralising the
# fallback here means callers don't have to know which one applies.
_bl_uploader_region() {
    if [ -n "${1:-}" ]; then
        printf '%s' "$1"
    elif [ -n "${REGION:-}" ]; then
        printf '%s' "$REGION"
    else
        printf '%s' "${AWS_REGION:-}"
    fi
}

# --- public API ------------------------------------------------------
#
# Implementation note: each function uses `cmd ... || rc=$?` rather
# than `if ! cmd; then ... fi` because Bash resets `$?` to 0 at the
# end of an `if` block that has no `else` clause. The `||` short
# circuit preserves the original exit code so we can surface it to
# the caller and to the ERROR log line.

# blitzlog_s3_put <bucket> <key> <local_path> [region]
#   Upload <local_path> to s3://<bucket>/<key>.
#   Returns the `aws s3 cp` exit code (0 on success).
blitzlog_s3_put() {
    local bucket="$1" key="$2" local_path="$3" region="${4:-}"
    region="$(_bl_uploader_region "$region")"
    local rc=0
    aws s3 cp "$local_path" "s3://${bucket}/${key}" --region "$region" || rc=$?
    if [ "$rc" -ne 0 ]; then
        _bl_uploader_log "ERROR: S3 put failed: bucket=${bucket} key=${key} local=${local_path} exit=${rc}"
        return "$rc"
    fi
    return 0
}

# blitzlog_s3_get <bucket> <key> <local_path> [region]
#   Download s3://<bucket>/<key> to <local_path>.
#   Returns the `aws s3 cp` exit code (0 on success).
blitzlog_s3_get() {
    local bucket="$1" key="$2" local_path="$3" region="${4:-}"
    region="$(_bl_uploader_region "$region")"
    local rc=0
    aws s3 cp "s3://${bucket}/${key}" "$local_path" --region "$region" || rc=$?
    if [ "$rc" -ne 0 ]; then
        _bl_uploader_log "ERROR: S3 get failed: bucket=${bucket} key=${key} local=${local_path} exit=${rc}"
        return "$rc"
    fi
    return 0
}

# blitzlog_s3_rm <bucket> <key> [region]
#   Delete s3://<bucket>/<key>. Used by the watchdog to release the
#   bot-pool lock at instance shutdown.
#   Returns the `aws s3 rm` exit code (0 on success).
blitzlog_s3_rm() {
    local bucket="$1" key="$2" region="${3:-}"
    region="$(_bl_uploader_region "$region")"
    local rc=0
    aws s3 rm "s3://${bucket}/${key}" --region "$region" || rc=$?
    if [ "$rc" -ne 0 ]; then
        _bl_uploader_log "ERROR: S3 rm failed: bucket=${bucket} key=${key} exit=${rc}"
        return "$rc"
    fi
    return 0
}

# blitzlog_s3_ls <bucket> <key_or_prefix> [region]
#   List s3://<bucket>/<key_or_prefix>. Used by the assisted-mode
#   bootstrap to probe for a previous session's metadata.json.
#   Returns the `aws s3 ls` exit code (0 if the key exists, 1 if
#   not — matches the pre-refactor inline-call semantics).
blitzlog_s3_ls() {
    local bucket="$1" key="$2" region="${3:-}"
    region="$(_bl_uploader_region "$region")"
    local rc=0
    aws s3 ls "s3://${bucket}/${key}" --region "$region" || rc=$?
    if [ "$rc" -ne 0 ]; then
        _bl_uploader_log "ERROR: S3 ls failed: bucket=${bucket} key=${key} exit=${rc}"
        return "$rc"
    fi
    return 0
}