"""Alicloud variant of the shared bootstrap helpers.

Issue #126: the AWS-side `scripts/aws/_common.py` generates a
user-data prologue that uses the S3 + SSM APIs from a Packer-baked
Ubuntu AMI. The Alicloud side mirrors those three helpers with:

  - `script_header(...)` — same shape, with `OSS_LOGS_BUCKET`
    instead of `S3_LOGS_BUCKET`, an inline `blitzlog_oss_*` bash
    library (parallel to the AWS-side `blitzlog_s3_*`), and a
    `source <(echo ...)` instead of `source /usr/local/bin/...`
    because the Packer image is still AWS-only and the OSS library
    doesn't live anywhere on the host.

  - `_read_secrets_from_kms_script(...)` — IMDSv2-equivalent (Aliyun
    metadata server at 100.100.100.200) + `aliyun kms GetSecretValue`
    instead of the SSM GetParameter API. Secret paths are byte-for-byte
    identical (`/blitzlog/<env>/<service>/<name>`), so the FC-side
    RAM grant in `infra/alicloud/modules/core/ram.tf` is the only
    ACL the host needs.

  - `_read_git_identity_kms_script(...)` — parallel to the AWS
    `_read_git_identity_ssm_script`. Same `2>/dev/null || echo ""`
    swallow for the unconfigured user-pool case.

The `aliyun` CLI is installed in the user-data prologue (one-time,
~10s on a fresh ECS instance) and the `aliyun configure` step
reaches out to the instance RAM role to get credentials — the
`alicloud_ram_role.lambda_role` policy in `infra/alicloud/modules/
core/ram.tf` grants `kms:GetSecretValue` on the
`/blitzlog/<env>/<service>/<name>` paths.

This file is the parallel of `scripts/aws/_common.py`. The
per-cloud `autonomous.py` / `assisted.py` import from this file
the same way their AWS counterparts import from
`scripts/aws/_common.py` — only the helper names + the secret-
read mechanism differ.
"""

from _env import (  # sys.path add via conftest; _env.py is at lambda/scripts/_env.py
    _blitzlog_env,
    _ssm_root,
)

# Aliyun OSS bucket helper names. Parallel to the `blitzlog_s3_*`
# names on the AWS side (see infra/aws/packer/scripts-docker-ubuntu/
# blitzlog_uploader.sh). The user-data inlines this library via
# `source <(echo "$BLITZLOG_OSS_HELPERS")` so the host doesn't need
# a Packer-baked copy.
_BLITZLOG_OSS_HELPERS = r"""
# Inlined blitzlog_oss_* bash library (parallel to the AWS-side
# blitzlog_s3_* in infra/aws/packer/scripts-docker-ubuntu/
# blitzlog_uploader.sh). Sourced from the user-data prologue on the
# Alicloud path because the Packer image is still AWS-only and
# doesn't have ossutil pre-installed OR a copy of this library.
#
# The function names mirror the AWS side so the autonomous/assisted
# user-data bodies stay parallel (issue #126's split-by-cloud goal).
# The internals wrap `ossutil` instead of `aws s3`. Each function
# returns the underlying ossutil exit code; callers opt in to
# `|| true` for best-effort semantics.

_bl_oss_log() {
    if declare -F log >/dev/null 2>&1; then
        log "$1"
    else
        printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" \
            | tee -a /var/log/backend-bootstrap.log >&2
    fi
}

# Resolve the region for ossutil --endpoint. Priority: explicit
# $1 > $REGION (set in the secrets block from IMDS) > $ALIBABA_CLOUD_REGION
# (set by `aliyun configure`) > "" (let ossutil fall back to its
# own configured default).
_bl_oss_region() {
    if [ -n "${1:-}" ]; then
        printf '%s' "$1"
    elif [ -n "${REGION:-}" ]; then
        printf '%s' "$REGION"
    elif [ -n "${ALIBABA_CLOUD_REGION:-}" ]; then
        printf '%s' "$ALIBABA_CLOUD_REGION"
    fi
}

# ossutil64's auth: when run on an ECS instance with a RAM role
# attached (alicloud_ram_role.ecs_agent_role), the SDK reads the
# credentials from the metadata server at 100.100.100.200 — the same
# one the bootstrap hits in the secrets block. So `ossutil` needs
# no explicit access-key flag. We just point at the right endpoint.

blitzlog_oss_get() {
    local bucket="$1" key="$2" dest="$3"
    if [ -z "$bucket" ] || [ -z "$key" ] || [ -z "$dest" ]; then
        _bl_oss_log "ERROR: blitzlog_oss_get: missing arg (bucket='$bucket' key='$key' dest='$dest')"
        return 64
    fi
    local ep
    ep=$(_bl_oss_region)
    if [ -n "$ep" ]; then
        ossutil cp -e "oss-${ep}.aliyuncs.com" "oss://${bucket}/${key}" "$dest"
    else
        ossutil cp "oss://${bucket}/${key}" "$dest"
    fi
    local rc=$?
    if [ $rc -ne 0 ]; then
        _bl_oss_log "ERROR: OSS get failed: bucket=$bucket key=$key dest=$dest exit=$rc"
    fi
    return $rc
}

blitzlog_oss_put() {
    local bucket="$1" key="$2" src="$3"
    if [ -z "$bucket" ] || [ -z "$key" ] || [ -z "$src" ]; then
        _bl_oss_log "ERROR: blitzlog_oss_put: missing arg (bucket='$bucket' key='$key' src='$src')"
        return 64
    fi
    local ep
    ep=$(_bl_oss_region)
    if [ -n "$ep" ]; then
        ossutil cp -e "oss-${ep}.aliyuncs.com" "$src" "oss://${bucket}/${key}"
    else
        ossutil cp "$src" "oss://${bucket}/${key}"
    fi
    local rc=$?
    if [ $rc -ne 0 ]; then
        _bl_oss_log "ERROR: OSS put failed: bucket=$bucket key=$key src=$src exit=$rc"
    fi
    return $rc
}

blitzlog_oss_ls() {
    local bucket="$1" key="$2"
    if [ -z "$bucket" ]; then
        _bl_oss_log "ERROR: blitzlog_oss_ls: missing arg (bucket='$bucket' key='$key')"
        return 64
    fi
    local ep
    ep=$(_bl_oss_region)
    if [ -n "$ep" ]; then
        ossutil ls -e "oss-${ep}.aliyuncs.com" "oss://${bucket}/${key}"
    else
        ossutil ls "oss://${bucket}/${key}"
    fi
    local rc=$?
    if [ $rc -ne 0 ]; then
        _bl_oss_log "ERROR: OSS ls failed: bucket=$bucket key=$key exit=$rc"
    fi
    return $rc
}

blitzlog_oss_rm() {
    local bucket="$1" key="$2"
    if [ -z "$bucket" ] || [ -z "$key" ]; then
        _bl_oss_log "ERROR: blitzlog_oss_rm: missing arg (bucket='$bucket' key='$key')"
        return 64
    fi
    local ep
    ep=$(_bl_oss_region)
    if [ -n "$ep" ]; then
        ossutil rm -e "oss-${ep}.aliyuncs.com" "oss://${bucket}/${key}"
    else
        ossutil rm "oss://${bucket}/${key}"
    fi
    local rc=$?
    if [ $rc -ne 0 ]; then
        _bl_oss_log "ERROR: OSS rm failed: bucket=$bucket key=$key exit=$rc"
    fi
    return $rc
}
"""


# Aliyun CLI install (one-time per launch, ~5-10s). The CLI is
# needed to call `kms:GetSecretValue` for the secret-fetch block.
# `aliyun` picks up credentials from the ECS instance's RAM role
# via the metadata server (same one used for ossutil auth).
_ALIYUN_CLI_INSTALL = r"""
# Install the aliyun CLI for KMS secret fetch. The host image is
# still AWS-only (issue #126 Phase 5 brings a per-cloud Packer
# image); the OSS + KMS CLIs are installed by the user-data on
# first launch. The Aliyun ECS instance RAM role grants
# kms:GetSecretValue on /blitzlog/<env>/<service>/<name> paths
# (see infra/alicloud/modules/core/ram.tf).
if ! command -v aliyun >/dev/null 2>&1; then
    log "Installing aliyun CLI for KMS secret fetch..."
    cd /tmp
    curl -fsSL -o aliyun-cli.tgz \
        https://aliyuncli.alicdn.com/aliyun-cli-linux-3.0.6-amd64.tgz
    tar -xzf aliyun-cli.tgz
    mv aliyun /usr/local/bin/aliyun
    chmod +x /usr/local/bin/aliyun
    rm -f aliyun-cli.tgz
fi
"""


# ossutil install (one-time per launch, ~5-10s). Same rationale as
# the aliyun CLI install above. Aliyun's `ossutil64` is the OSS
# analogue of `aws s3`; the `--endpoint` flag picks the right
# regional gateway.
_OSSUTIL_INSTALL = r"""
# Install ossutil for OSS operations (download whisper model,
# upload logs, restore session state, ...). The AWS-side
# `blitzlog_s3_*` is Packer-baked into the Ubuntu AMI; on the
# Alicloud path the equivalent `blitzlog_oss_*` helpers wrap
# ossutil, which is installed here on first launch.
if ! command -v ossutil >/dev/null 2>&1; then
    log "Installing ossutil for OSS operations..."
    cd /tmp
    curl -fsSL -o ossutil64 \
        https://gosspublic.alicdn.com/ossutil/2.0.5/ossutil64
    chmod +x ossutil64
    mv ossutil64 /usr/local/bin/ossutil
fi
"""


def _git_identity_ssm_block(owner_login: str) -> str:
    """Symmetric with the AWS-side `_git_identity_ssm_block` — returns
    "" so the byte layout of the pre-#119 prologue is preserved. The
    actual KMS fetch lives in `_read_git_identity_kms_script` so it
    runs AFTER `$REGION` is set by `_read_secrets_from_kms_script`.
    """
    return ""


def _git_identity_env_block(ssm_block: str) -> str:
    """Emit the GIT_AUTHOR_* export line for the consolidated export
    preamble. Mirrors the AWS-side helper.
    """
    if not ssm_block:
        return ""
    return "export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL\n"


def _local_llm_env_block(local_llm: dict | None) -> str:
    """Local-LLM env-export block — identical shape to the AWS-side.
    The Alicloud tailnet/Tailscale path uses the same variables; the
    Tailscale install script (in the watchdog) is provider-agnostic.
    """
    if not local_llm:
        return ""
    block = (
        f'LOCAL_LLM_ENDPOINT="{local_llm["endpoint"]}"\n'
        f'LOCAL_LLM_MODEL="{local_llm["model"]}"\n'
        f'LOCAL_LLM_API_KEY="{local_llm.get("api_key", "")}"\n'
        f'LOCAL_LLM_FALLBACK="{local_llm["fallback"]}"\n'
    )
    tailscale_key = local_llm.get("tailscale_auth_key") or ""
    if tailscale_key:
        block += f'TAILSCALE_AUTH_KEY="{tailscale_key}"\n'
    block += (
        "export LOCAL_LLM_ENDPOINT LOCAL_LLM_MODEL LOCAL_LLM_API_KEY "
        "LOCAL_LLM_FALLBACK"
    )
    if tailscale_key:
        block += " TAILSCALE_AUTH_KEY"
    return block + "\n"


def script_header(
    *,
    mode: str,
    repo: str,
    issue_number: int,
    opencode_model: str,
    opencode_max_steps: int,
    oss_bucket: str,
    oss_archive_prefix: str,
    oss_log_prefix: str,
    local_llm_env: str,
    git_identity_ssm: str = "",
    git_identity_env: str = "",
    local_llm_log_line: str = "",
    opencode_prompt: str | None = None,
) -> str:
    """Return the shebang + `set -euo pipefail` + defensive env scrub +
    CLI install + helper-source + `LOG_FILE`/`log()` + env-export
    preamble every Alicloud bootstrap needs.

    Mirrors `scripts/aws/_common.py:script_header` 1-to-1 with three
    cloud-specific differences:

    1. **CLI install block**: `_ALIYUN_CLI_INSTALL` + `_OSSUTIL_INSTALL`
       are emitted AFTER `log()` is defined and BEFORE the helper
       library is sourced. The host image is still AWS-only; the
       CLIs are installed here on first launch (~10s total).

    2. **OSS helper library**: `_BLITZLOG_OSS_HELPERS` is inlined
       into the script and sourced via `source <(echo "$BLITZLOG_OSS_HELPERS")`.
       The function names parallel the AWS `blitzlog_s3_*` so the
       autonomous/assisted body builders (the only consumers) are
       cloud-agnostic.

    3. **Bucket env var**: `OSS_LOGS_BUCKET` instead of
       `S3_LOGS_BUCKET`. The FC's environment_variables block
       (infra/alicloud/modules/core/function_compute.tf) sets the
       same name.
    """
    if opencode_prompt is not None:
        opencode_interactive_lines = (
            f"OPENCODE_NONINTERACTIVE=1\n" f'OPENCODE_PROMPT="{opencode_prompt}"\n'
        )
        interactive_export = " OPENCODE_NONINTERACTIVE OPENCODE_PROMPT"
    else:
        opencode_interactive_lines = ""
        interactive_export = ""

    git_identity_export = (
        " GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL" if git_identity_env else ""
    )

    return f"""#!/bin/bash
set -euo pipefail
export HOME=/root
export GIT_TERMINAL_PROMPT=0

# Defensive env scrubbing (always — cheap hygiene).
unset OPENCODE_API_KEY HTTPS_PROXY HTTP_PROXY https_proxy http_proxy

LOG_FILE="/var/log/backend-bootstrap.log"
log() {{
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}}

# Issue #126: the AWS-side bootstrap sources a Packer-baked bash
# library at /usr/local/bin/blitzlog_uploader.sh. On the Alicloud
# path the Packer image is still AWS-only, so the equivalent
# `blitzlog_oss_*` library is inlined below. Sourced AFTER `log()`
# is defined so library ERROR output uses the same channel.
{_ALIYUN_CLI_INSTALL}{_OSSUTIL_INSTALL}
export BLITZLOG_OSS_HELPERS=$(cat <<'OSS_LIB_EOF'
{_BLITZLOG_OSS_HELPERS}
OSS_LIB_EOF
)
unset BLITZLOG_OSS_HELPERS_NAME
BLITZLOG_OSS_HELPERS_NAME="blitzlog_oss"
# Source the inlined library. The heredoc + read-into-var pattern
# survives the f-string escaping in script_header.
eval "$BLITZLOG_OSS_HELPERS"

ISSUE_NUMBER={issue_number}
REPO="{repo}"
{opencode_interactive_lines}OPENCODE_MODEL="{opencode_model}"
OPENCODE_AGENT_MAX_STEPS="{opencode_max_steps}"
OSS_LOGS_BUCKET="{oss_bucket}"
SESSION_ARCHIVE_BUCKET="{oss_bucket}"
SESSION_ARCHIVE_PREFIX="{oss_archive_prefix}"
OSS_LOG_PREFIX="{oss_log_prefix}"
{local_llm_env}{git_identity_env}export ISSUE_NUMBER OPENCODE_MODEL OPENCODE_AGENT_MAX_STEPS OSS_LOGS_BUCKET{interactive_export} SESSION_ARCHIVE_BUCKET SESSION_ARCHIVE_PREFIX OSS_LOG_PREFIX{git_identity_export}

log "=== Cloud-coder bootstrap starting ({mode}) ==="
log "Repo: $REPO, Issue: $ISSUE_NUMBER"
{local_llm_log_line}

log "Reading secrets from KMS..."
"""


def _read_secrets_from_kms_script(issue_number: int, local_llm: bool = False) -> str:
    """Render the bootstrap snippet that fetches the GitHub token and (when a
    cloud run is intended) the OPENCODE_API_KEY from Aliyun KMS via the
    instance metadata server (Aliyun's IMDSv2-equivalent at 100.100.100.200).

    Parallel to `scripts/aws/_common.py:_read_secrets_from_ssm_script`. The
    secret paths are byte-for-byte identical to the AWS-side SSM paths
    (`/blitzlog/<env>/<service>/<name>`), so the FC-side RAM grant in
    `infra/alicloud/modules/core/ram.tf` is the only ACL the host needs.

    `aliyun kms GetSecretValue` returns the secret value in JSON; the
    `--output json` flag + `python3 -c '... json.load(sys.stdin)["SecretData"]'`
    is the canonical way to extract the value. We do the same dance the
    AWS-side does (IMDS token → region → KMS read), so the byte layout
    of the pre-#126 prologue stays the same except for the API name.
    """
    ssm_root = _ssm_root()

    api_key_block = ""
    if not local_llm:
        api_key_block = (
            "\n"
            f"OPENCODE_API_KEY=$(aliyun kms GetSecretValue "
            f'--SecretName "{ssm_root}/opencode/api-key" '
            f'--region "$REGION" '
            f"--output json 2>/dev/null | python3 -c "
            f"'import sys, json; print(json.load(sys.stdin)[\"SecretData\"])')\n"
            "export OPENCODE_API_KEY\n"
        )

    return f"""
# Aliyun ECS instance metadata service (IMDSv2-equivalent). The
# endpoint is at 100.100.100.200 and uses a different token-header
# name (`X-aliyun-ecs-metadata-token`) than AWS (`X-aws-ec2-...`).
TOKEN=$(curl -s -X PUT 'http://100.100.100.200/latest/api/token' -H 'X-aliyun-ecs-metadata-token-ttl-seconds: 60')
INSTANCE_ID=$(curl -s -H "X-aliyun-ecs-metadata-token: $TOKEN" http://100.100.100.200/latest/meta-data/instance-id)
REGION=$(curl -s -H "X-aliyun-ecs-metadata-token: $TOKEN" http://100.100.100.200/latest/meta-data/region-id)
export ALIBABA_CLOUD_REGION=$REGION
export BLITZLOG_ENV={_blitzlog_env()}

_CCSECRET=/tmp/cc_secret.json
get_kms() {{
    local name="$1" out="$2"
    if [ -z "$name" ]; then
        return 64
    fi
    aliyun kms GetSecretValue \\
        --SecretName "$name" \\
        --region "$REGION" \\
        --output json 2>/dev/null > "$out"
    python3 -c "import json,sys; print(json.load(open('$out'))['SecretData'])" 2>/dev/null
}}

_CCSECRET_OUT=/tmp/cc_secret_value.txt
_CCSECRET_PATH="{ssm_root}/ephemeral/gh/$REPO/{issue_number}"
get_kms "$_CCSECRET_PATH" "$_CCSECRET"
_CC_GITHUB_TOKEN=$(cat "$_CCSECRET_OUT" 2>/dev/null || cat "$_CCSECRET" 2>/dev/null)
# Re-extract from the JSON dump for the simple case (small files).
if [ -f "$_CCSECRET" ]; then
    _CC_GITHUB_TOKEN=$(python3 -c "import json; print(json.load(open('$_CCSECRET'))['SecretData'])" 2>/dev/null || echo "")
fi
export _CC_GITHUB_TOKEN{api_key_block}

STT_API_URL=$(get_kms "{ssm_root}/stt/api-url" /tmp/stt_api_url.json)
export STT_API_URL
STT_API_KEY=$(get_kms "{ssm_root}/stt/api-key" /tmp/stt_api_key.json)
export STT_API_KEY
STT_MODEL=$(get_kms "{ssm_root}/stt/model" /tmp/stt_model.json)
export STT_MODEL
STT_LANGUAGE=$(get_kms "{ssm_root}/stt/language" /tmp/stt_language.json)
export STT_LANGUAGE
STT_MODELS_BUCKET=$(get_kms "{ssm_root}/stt/models-bucket" /tmp/stt_models_bucket.json)
export STT_MODELS_BUCKET
"""


def _read_git_identity_kms_script(owner_login: str) -> str:
    """Render the bootstrap snippet that fetches the operator-configured
    git author identity from KMS. Parallel to
    `scripts/aws/_common.py:_read_git_identity_ssm_script`.

    The `2>/dev/null || echo ""` swallow keeps the bootstrap working
    when the operator hasn't provisioned the user-pool yet. The
    container's `entrypoint.sh` already gates the `git config
    --global` step on `GIT_AUTHOR_NAME` being non-empty, so the
    fallback is behaviour-preserving for unconfigured pools.

    `owner_login` empty → return "" (same convention as the AWS side).
    """
    if not owner_login:
        return ""
    root = f"/blitzlog/users/{owner_login}/git-author"
    return (
        f'\nGIT_AUTHOR_NAME=$(get_kms "{root}/name" /tmp/git_author_name.json '
        f'2>/dev/null || echo "")\n'
        f'GIT_AUTHOR_EMAIL=$(get_kms "{root}/email" /tmp/git_author_email.json '
        f'2>/dev/null || echo "")\n'
    )


def _decode_api_errors_script() -> str:
    """Decode known LLM-provider API errors into actionable log lines.
    Identical to the AWS-side helper — the error patterns are
    provider-agnostic, the log line content is too. Runs after the
    opencode process exits and before session export.
    """
    ssm_root = _ssm_root()
    return f"""
# Decode known LLM-provider API errors into actionable log lines.
# Run after the opencode process exits and before session export.
if [ -f "$LOG_FILE" ] && grep -qE "insufficient_balance|insufficient balance|\\(1008\\)" "$LOG_FILE"; then
    log "ACTIONABLE: LLM provider returned insufficient balance / HTTP 1008."
    log "ACTIONABLE: The token-plan account for the configured provider (${{OPENCODE_MODEL:-unknown}}) has zero credits."
    log "ACTIONABLE: Top up at the provider console (e.g. https://platform.minimax.io) before retrying."
    log "ACTIONABLE: Verify the API key at KMS secret {ssm_root}/opencode/api-key belongs to a funded account."
fi
if [ -f "$LOG_FILE" ] && grep -qE "Unauthorized|invalid_api_key|\\(401\\)|Authentication" "$LOG_FILE"; then
    log "ACTIONABLE: LLM provider rejected the API key as unauthorized (HTTP 401)."
    log "ACTIONABLE: Rotate {ssm_root}/opencode/api-key in KMS and re-run terraform apply."
fi
if [ -f "$LOG_FILE" ] && grep -qE "rate.?limit|quota.?exceeded|too.?many.?requests|\\(429\\)" "$LOG_FILE"; then
    log "ACTIONABLE: LLM provider returned a rate-limit / quota error (HTTP 429)."
    log "ACTIONABLE: Wait for the quota window to reset or upgrade the plan, then re-trigger."
fi
"""


def _local_llm_log_line(local_llm: dict | None) -> str:
    """Identical to the AWS-side helper. The local-LLM diagnostics
    line is provider-agnostic.
    """
    if not local_llm:
        return ""
    return 'log "Local LLM configured: endpoint=$LOCAL_LLM_ENDPOINT, model=$LOCAL_LLM_MODEL"'
