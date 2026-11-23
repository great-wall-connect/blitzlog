#!/bin/bash
# infra/alicloud/packer/scripts-ecs-ubuntu/blitzlog_oss_uploader.sh
#
# Single source of truth for every OSS-shaped operation blitzlog
# performs on the ECS instance and at Packer bake time. Sourced, not
# executed.
#
# The Alicloud analogue of
# infra/aws/packer/scripts-docker-ubuntu/blitzlog_uploader.sh (issue
# #123). Same four verbs, same call-site contract, same logging
# discipline — only the backend changes (`ossutil` instead of
# `aws s3`).
#
# WHY A SEPARATE FILE PER CLOUD. The host image is single-cloud by
# construction: the AMI bakes the backend in, so there is no runtime
# dispatch to do (the AWS-side header says the same thing — the
# cross-cloud dispatch used by the Lambda via BLITZLOG_PROVIDER does
# NOT apply here). The function *names* are provider-neutral so the
# watchdog bodies stay parallel between the two trees.
#
# Logging discipline (mirrors issue #123's acceptance criteria):
#   * Every function returns the underlying `ossutil ...` exit code.
#   * On non-zero exit, emits `ERROR: OSS <op> failed: bucket=...
#     key=... exit=...` via the caller's `log()` if defined, otherwise
#     via an internal append-to-/var/log/backend-bootstrap.log helper.
#   * Never swallows errors itself. Callers opt in to swallow via
#     `|| true` at the call site — best-effort semantics live with
#     the caller, not the library.

# --- internal logger (falls through to caller's log() if defined) ----
_bl_oss_uploader_log() {
    if declare -F log >/dev/null 2>&1; then
        log "$1"
    else
        printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" \
            | tee -a /var/log/backend-bootstrap.log >&2
    fi
}

# --- internal region resolver ------------------------------------------
# Resolution order: explicit $1 > $REGION > $ALIBABA_CLOUD_REGION >
# $ALICLOUD_REGION > "" (let `ossutil` fall back to its own
# configured default).
#
# $REGION is set on the ECS host by watchdog.sh from the Aliyun
# metadata service. $ALIBABA_CLOUD_REGION is set by `aliyun configure`
# and by the Lambda-rendered user-data bootstrap env. $ALICLOUD_REGION
# is the name the Packer bake passes — at bake time neither of the
# other two exists, and the region has to come from somewhere.
_bl_oss_uploader_region() {
    if [ -n "${1:-}" ]; then
        printf '%s' "$1"
    elif [ -n "${REGION:-}" ]; then
        printf '%s' "$REGION"
    elif [ -n "${ALIBABA_CLOUD_REGION:-}" ]; then
        printf '%s' "$ALIBABA_CLOUD_REGION"
    elif [ -n "${ALICLOUD_REGION:-}" ]; then
        printf '%s' "$ALICLOUD_REGION"
    fi
}

# --- internal ossutil invocation ----------------------------------------
# ossutil v2 signs with SigV4, which needs the region as its own
# parameter — the `-e` endpoint alone is not enough ("region must be
# set in sign version 4"). It also does NOT read the ALIBABA_CLOUD_*
# environment variables the Alicloud SDKs use; it wants the region
# plus, on an ECS instance with a RAM role attached, the EcsRamRole
# mode so it fetches the role's temporary credentials from the
# metadata service at 100.100.100.200.
#
# The region is an ARGUMENT, not something re-derived from the
# environment: the caller has already resolved it (possibly from its
# own argument), and re-deriving it here silently dropped it whenever
# the caller knew the region but the environment did not — which is
# exactly the bake, where the region travels as a Packer
# environment variable and $REGION / $ALIBABA_CLOUD_REGION are unset.
#
# Every caller of this library runs on an ECS instance that has the
# blitzlog RAM role attached (the agent role at runtime, the packer
# build role at bake time), so the role mode is always the right one.
_bl_oss_uploader_ossutil() {
    local region="$1"
    shift
    if [ -n "$region" ]; then
        OSS_REGION="$region" ossutil --region "$region" --mode EcsRamRole "$@"
    else
        ossutil --mode EcsRamRole "$@"
    fi
}

# --- internal endpoint resolver ----------------------------------------
# ossutil needs the regional gateway endpoint for every operation
# against a bucket that is not in the CLI's own default region. The
# endpoint shape is `oss-<region>.aliyuncs.com` — the same shape the
# user-data downloader in
# lambda/provider/alicloud.py::build_user_data_downloader_script uses.
#
# An empty region yields an empty endpoint, and the caller falls back
# to the endpoint-less form of the command (ossutil then uses its own
# configured default). This mirrors the AWS-side library's "let the
# CLI decide" fallback for the region.
_bl_oss_uploader_endpoint() {
    local region
    region="$(_bl_oss_uploader_region "${1:-}")"
    if [ -z "$region" ]; then
        printf ''
        return 0
    fi
    printf 'oss-%s.aliyuncs.com' "$region"
}

# --- public API ------------------------------------------------------
#
# Implementation note: each function uses `cmd ... || rc=$?` rather
# than `if ! cmd; then ... fi` because Bash resets `$?` to 0 at the
# end of an `if` block that has no `else` clause. The `||` short
# circuit preserves the original exit code so we can surface it to
# the caller and to the ERROR log line.

# blitzlog_oss_put <bucket> <key> <local_path> [region]
#   Upload <local_path> to oss://<bucket>/<key>.
#   Returns the `ossutil cp` exit code (0 on success).
blitzlog_oss_put() {
    local bucket="$1" key="$2" local_path="$3" region="${4:-}"
    region="$(_bl_oss_uploader_region "$region")"
    local endpoint
    endpoint="$(_bl_oss_uploader_endpoint "$region")"
    local rc=0
    if [ -n "$endpoint" ]; then
        _bl_oss_uploader_ossutil "$region" cp -e "$endpoint" "$local_path" "oss://${bucket}/${key}" || rc=$?
    else
        _bl_oss_uploader_ossutil "$region" cp "$local_path" "oss://${bucket}/${key}" || rc=$?
    fi
    if [ "$rc" -ne 0 ]; then
        _bl_oss_uploader_log "ERROR: OSS put failed: bucket=${bucket} key=${key} local=${local_path} exit=${rc}"
        return "$rc"
    fi
    return 0
}

# blitzlog_oss_get <bucket> <key> <local_path> [region]
#   Download oss://<bucket>/<key> to <local_path>.
#   Returns the `ossutil cp` exit code (0 on success).
blitzlog_oss_get() {
    local bucket="$1" key="$2" local_path="$3" region="${4:-}"
    region="$(_bl_oss_uploader_region "$region")"
    local endpoint
    endpoint="$(_bl_oss_uploader_endpoint "$region")"
    local rc=0
    if [ -n "$endpoint" ]; then
        _bl_oss_uploader_ossutil "$region" cp -e "$endpoint" "oss://${bucket}/${key}" "$local_path" || rc=$?
    else
        _bl_oss_uploader_ossutil "$region" cp "oss://${bucket}/${key}" "$local_path" || rc=$?
    fi
    if [ "$rc" -ne 0 ]; then
        _bl_oss_uploader_log "ERROR: OSS get failed: bucket=${bucket} key=${key} exit=${rc}"
        return "$rc"
    fi
    return 0
}

# blitzlog_oss_rm <bucket> <key> [region]
#   Delete oss://<bucket>/<key>. Used by the watchdog to release the
#   bot-pool lock at instance shutdown.
#   Returns the `ossutil rm` exit code (0 on success).
blitzlog_oss_rm() {
    local bucket="$1" key="$2" region="${3:-}"
    region="$(_bl_oss_uploader_region "$region")"
    local endpoint
    endpoint="$(_bl_oss_uploader_endpoint "$region")"
    local rc=0
    if [ -n "$endpoint" ]; then
        _bl_oss_uploader_ossutil "$region" rm -e "$endpoint" "oss://${bucket}/${key}" || rc=$?
    else
        _bl_oss_uploader_ossutil "$region" rm "oss://${bucket}/${key}" || rc=$?
    fi
    if [ "$rc" -ne 0 ]; then
        _bl_oss_uploader_log "ERROR: OSS rm failed: bucket=${bucket} key=${key} exit=${rc}"
        return "$rc"
    fi
    return 0
}

# blitzlog_oss_ls <bucket> <key_or_prefix> [region]
#   List oss://<bucket>/<key_or_prefix>. Used by the assisted-mode
#   bootstrap to probe for a previous session's metadata.json.
#   Returns the `ossutil ls` exit code (0 if the key exists, 1 if
#   not — matches the pre-refactor inline-call semantics).
blitzlog_oss_ls() {
    local bucket="$1" key="$2" region="${3:-}"
    region="$(_bl_oss_uploader_region "$region")"
    local endpoint
    endpoint="$(_bl_oss_uploader_endpoint "$region")"
    local rc=0
    if [ -n "$endpoint" ]; then
        _bl_oss_uploader_ossutil "$region" ls -e "$endpoint" "oss://${bucket}/${key}" || rc=$?
    else
        _bl_oss_uploader_ossutil "$region" ls "oss://${bucket}/${key}" || rc=$?
    fi
    if [ "$rc" -ne 0 ]; then
        _bl_oss_uploader_log "ERROR: OSS ls failed: bucket=${bucket} key=${key} exit=${rc}"
        return "$rc"
    fi
    return 0
}
