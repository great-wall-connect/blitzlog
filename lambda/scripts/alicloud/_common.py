"""Alicloud variant of the shared bootstrap helpers.

Issue #126: the AWS-side `scripts/aws/_common.py` generates a
user-data prologue that uses the S3 + SSM APIs from a Packer-baked
Ubuntu AMI. The Alicloud side mirrors those three helpers with:

  - `script_header(...)` — same shape, with `OSS_LOGS_BUCKET`
    instead of `S3_LOGS_BUCKET`, and the host-side `blitzlog_oss_*`
    bash library (parallel to the AWS-side `blitzlog_s3_*`).

Issue #125: the Alicloud AMI
(`infra/alicloud/packer/agent-ecs-ubuntu.pkr.hcl`) now bakes the OSS
library at `/usr/local/bin/blitzlog_oss_uploader.sh`, exactly as the
AWS AMI bakes `blitzlog_uploader.sh` (issue #123). `script_header`
sources the baked copy when it is present and falls back to an inline
copy of the same library otherwise — the fallback is what lets a run
finish against a host that is still on the pre-#125 public Ubuntu
image.

  - `_read_secrets_from_oos_script(...)` — IMDSv2-equivalent (Aliyun
    metadata server at 100.100.100.200) + `aliyun oos
    GetSecretParameter` instead of the SSM GetParameter API. The
    Alicloud secret store is the OOS Parameter Store (KMS Secrets
    Manager needs a billable KMS instance on KMS 3.0). The paths
    mirror the AWS-side SSM paths (`/blitzlog/<env>/<service>/<name>`)
    minus the leading slash, which is how OOS names its parameters
    (`blitzlog/<env>/<service>/<name>`), so the FC-side RAM grant
    in `infra/alicloud/modules/core/ram.tf` is the only ACL the host
    needs.

  - `_read_git_identity_oos_script(...)` — parallel to the AWS
    `_read_git_identity_ssm_script`. Same `2>/dev/null || echo ""`
    swallow for the unconfigured user-pool case.

The `aliyun` CLI is installed in the user-data prologue (one-time,
~10s on a fresh ECS instance) and the `aliyun configure` step
reaches out to the instance RAM role to get credentials — the
`alicloud_ram_role.ecs_agent_role` policy in
`infra/alicloud/modules/core/ram.tf` grants `oos:GetSecretParameter`
on the `blitzlog/<env>/<service>/<name>` parameters.

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
# (set by `aliyun configure`) > $ALICLOUD_REGION > "" (let ossutil
# fall back to its own configured default).
_bl_oss_region() {
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

# ossutil's auth: it signs with SigV4, which needs the region as its
# own parameter (the `-e` endpoint alone is not enough), and it does
# not read the ALIBABA_CLOUD_* variables the Alicloud SDKs use. On an
# ECS instance the credentials come from the RAM role attached to the
# instance (alicloud_ram_role.ecs_agent_role), fetched from the
# metadata server at 100.100.100.200 — the same one the bootstrap
# hits in the secrets block. The EcsRamRole mode is what selects that
# source.
# The region arrives as an argument: the caller has already
# resolved it, and re-deriving it from the environment here would
# drop it whenever the caller knew the region but the environment
# did not.
_bl_oss_run() {
    local region="$1"
    shift
    if [ -n "$region" ]; then
        OSS_REGION="$region" ossutil --region "$region" --mode EcsRamRole "$@"
    else
        ossutil --mode EcsRamRole "$@"
    fi
}

blitzlog_oss_get() {
    local bucket="$1" key="$2" dest="$3"
    if [ -z "$bucket" ] || [ -z "$key" ] || [ -z "$dest" ]; then
        _bl_oss_log "ERROR: blitzlog_oss_get: missing arg (bucket='$bucket' key='$key' dest='$dest')"
        return 64
    fi
    local region
    region=$(_bl_oss_region)
    if [ -n "$region" ]; then
        _bl_oss_run "$region" cp -e "oss-${region}.aliyuncs.com" "oss://${bucket}/${key}" "$dest"
    else
        _bl_oss_run "" cp "oss://${bucket}/${key}" "$dest"
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
    local region
    region=$(_bl_oss_region)
    if [ -n "$region" ]; then
        _bl_oss_run "$region" cp -e "oss-${region}.aliyuncs.com" "$src" "oss://${bucket}/${key}"
    else
        _bl_oss_run "" cp "$src" "oss://${bucket}/${key}"
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
    local region
    region=$(_bl_oss_region)
    if [ -n "$region" ]; then
        _bl_oss_run "$region" ls -e "oss-${region}.aliyuncs.com" "oss://${bucket}/${key}"
    else
        _bl_oss_run "" ls "oss://${bucket}/${key}"
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
    local region
    region=$(_bl_oss_region)
    if [ -n "$region" ]; then
        _bl_oss_run "$region" rm -e "oss-${region}.aliyuncs.com" "oss://${bucket}/${key}"
    else
        _bl_oss_run "" rm "oss://${bucket}/${key}"
    fi
    local rc=$?
    if [ $rc -ne 0 ]; then
        _bl_oss_log "ERROR: OSS rm failed: bucket=$bucket key=$key exit=$rc"
    fi
    return $rc
}
"""


# Aliyun CLI install (one-time per launch, ~5-10s). The CLI is
# needed to call `oos:GetSecretParameter` for the secret-fetch block.
# `aliyun` picks up credentials from the ECS instance's RAM role
# via the metadata server (same one used for ossutil auth).
#
# Issue #125: the Alicloud AMI bakes the `aliyun` CLI
# (infra/alicloud/packer/scripts-ecs-ubuntu/01-system.sh), so on a
# baked host the `command -v` guard below short-circuits and this
# block costs nothing. The install stays as a fallback for hosts
# still on the pre-#125 public Ubuntu image.
_ALIYUN_CLI_INSTALL = r"""
# Install the aliyun CLI for the OOS parameter fetch. The Alicloud
# AMI (issue #125) ships this CLI, so the guard normally
# short-circuits; the install is the fallback for hosts still on the
# pre-#125 public Ubuntu image. The Aliyun ECS instance RAM role
# grants oos:GetSecretParameter on the
# blitzlog/<env>/<service>/<name> parameters (see
# infra/alicloud/modules/core/ram.tf).
if ! command -v aliyun >/dev/null 2>&1; then
    log "Installing aliyun CLI for OOS parameter fetch..."
    cd /tmp
    case "$(uname -m)" in
        x86_64) CLI_TGZ="aliyun-cli-linux-3.0.6-amd64.tgz" ;;
        aarch64 | arm64) CLI_TGZ="aliyun-cli-linux-latest-arm64.tgz" ;;
        *)
            log "ERROR: unsupported architecture $(uname -m)"
            exit 1
            ;;
    esac
    curl -fsSL -o aliyun-cli.tgz "https://aliyuncli.alicdn.com/${CLI_TGZ}"
    tar -xzf aliyun-cli.tgz
    mv aliyun /usr/local/bin/aliyun
    chmod +x /usr/local/bin/aliyun
    rm -f aliyun-cli.tgz
fi
"""


# ossutil install (one-time per launch, ~5-10s). Same rationale as
# the aliyun CLI install above. Aliyun's `ossutil` is the OSS
# analogue of `aws s3`; the `--endpoint` flag picks the right
# regional gateway.
#
# Issue #125: baked into the Alicloud AMI like the `aliyun` CLI, so
# the guard short-circuits on a baked host.
_OSSUTIL_INSTALL = r"""
# Install ossutil for OSS operations (download whisper model,
# upload logs, restore session state, ...). The AWS-side
# `blitzlog_s3_*` is Packer-baked into the Ubuntu AMI; the Alicloud
# AMI (issue #125) bakes the equivalent `blitzlog_oss_*` helpers and
# ossutil itself, so this install is the fallback for hosts still on
# the pre-#125 public Ubuntu image.
if ! command -v ossutil >/dev/null 2>&1; then
    log "Installing ossutil for OSS operations..."
    cd /tmp
    case "$(uname -m)" in
        x86_64) OSSUTIL_ARCH="amd64" ;;
        aarch64 | arm64) OSSUTIL_ARCH="arm64" ;;
        *)
            log "ERROR: unsupported architecture $(uname -m)"
            exit 1
            ;;
    esac
    OSSUTIL_VERSION=$(curl -fsSL https://gosspublic.alicdn.com/ossutil/v2/version.txt \
        | sed -n 's/^ossutil version: //p')
    if [ -z "$OSSUTIL_VERSION" ]; then
        log "ERROR: could not resolve the ossutil version"
        exit 1
    fi
    curl -fsSL -o ossutil.zip \
        "https://gosspublic.alicdn.com/ossutil/v2/${OSSUTIL_VERSION}/ossutil-linux-${OSSUTIL_ARCH}.zip"
    rm -rf ossutil-unpack
    unzip -q ossutil.zip -d ossutil-unpack
    mv "ossutil-unpack/ossutil-linux-${OSSUTIL_ARCH}/ossutil" /usr/local/bin/ossutil
    chmod +x /usr/local/bin/ossutil
    rm -rf ossutil.zip ossutil-unpack
fi
"""


def _git_identity_ssm_block(owner_login: str) -> str:
    """Symmetric with the AWS-side `_git_identity_ssm_block` — returns
    "" so the byte layout of the pre-#119 prologue is preserved. The
    actual OOS fetch lives in `_read_git_identity_oos_script` so it
    runs AFTER `$REGION` is set by `_read_secrets_from_oos_script`.
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

# Issue #125: the Alicloud AMI (infra/alicloud/packer/agent-ecs-ubuntu.pkr.hcl)
# bakes the host-side OSS abstraction at /usr/local/bin/blitzlog_oss_uploader.sh,
# exactly as the AWS AMI bakes blitzlog_uploader.sh (issue #123). Source it
# when it is present — that is the canonical copy.
#
# The inline fallback below is kept for hosts still running the PUBLIC
# Ubuntu image (the pre-#125 Alicloud path, which had no baked agent
# image at all). It is the same library, byte-for-byte the same four
# verbs; the fallback is what lets an in-flight run finish against an
# old image while the baked image rolls out.
#
# Sourced AFTER `log()` is defined so library ERROR output uses the
# same channel.
{_ALIYUN_CLI_INSTALL}{_OSSUTIL_INSTALL}
if [ -r /usr/local/bin/blitzlog_oss_uploader.sh ]; then
    source /usr/local/bin/blitzlog_oss_uploader.sh
else
    export BLITZLOG_OSS_HELPERS=$(cat <<'OSS_LIB_EOF'
{_BLITZLOG_OSS_HELPERS}
OSS_LIB_EOF
)
    unset BLITZLOG_OSS_HELPERS_NAME
    BLITZLOG_OSS_HELPERS_NAME="blitzlog_oss"
    # Source the inlined library. The heredoc + read-into-var pattern
    # survives the f-string escaping in script_header.
    eval "$BLITZLOG_OSS_HELPERS"
fi

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

log "Reading secrets from the OOS Parameter Store..."
"""


def _read_secrets_from_oos_script(issue_number: int, local_llm: bool = False) -> str:
    """Render the bootstrap snippet that fetches the GitHub token and (when a
    cloud run is intended) the OPENCODE_API_KEY from the Aliyun OOS
    Parameter Store via the instance metadata server (Aliyun's
    IMDSv2-equivalent at 100.100.100.200).

    Parallel to `scripts/aws/_common.py:_read_secrets_from_ssm_script`. The
    Alicloud secret store is the OOS Parameter Store, so the read is
    `aliyun oos GetSecretParameter --WithDecryption true`. The
    parameter names mirror the AWS-side SSM paths
    (`/blitzlog/<env>/<service>/<name>`) minus the leading slash —
    OOS names its parameters `blitzlog/<env>/<service>/<name>` — so
    the FC-side RAM grant in `infra/alicloud/modules/core/ram.tf` is
    the only ACL the host needs.

    `aliyun oos GetSecretParameter` returns the parameter in JSON —
    the CLI's default output — so
    `python3 -c '... json.load(sys.stdin) ["Parameter"]["Value"]'` is
    the canonical way to extract the value. (The CLI's `--output`
    flag is the TABLE formatter — `--output cols=Field1,Field2` — and
    rejects `json` with "bad flag format --output with field cols=
    required".)
    We do the same dance the AWS-side does (IMDS token → region →
    store read), so the byte layout of the pre-#126 prologue stays the
    same except for the API name.
    """
    oos_root = _ssm_root().lstrip("/")

    api_key_block = ""
    if not local_llm:
        api_key_block = (
            "\n"
            f"OPENCODE_API_KEY=$(aliyun oos GetSecretParameter "
            f'--Name "{oos_root}/opencode/api-key" '
            f"--WithDecryption true "
            f'--region "$REGION" '
            f"2>/dev/null | python3 -c "
            f'\'import sys, json; print(json.load(sys.stdin)["Parameter"]["Value"])\')\n'
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
get_oos_param() {{
    local name="$1" out="$2"
    if [ -z "$name" ]; then
        return 64
    fi
    aliyun oos GetSecretParameter \\
        --Name "$name" \\
        --WithDecryption true \\
        --region "$REGION" \\
        2>/dev/null > "$out"
    python3 -c "import json,sys; print(json.load(open('$out'))['Parameter']['Value'])" 2>/dev/null
}}

_CCSECRET_OUT=/tmp/cc_secret_value.txt
_CCSECRET_PATH="{oos_root}/ephemeral/gh/$REPO/{issue_number}"
get_oos_param "$_CCSECRET_PATH" "$_CCSECRET"
_CC_GITHUB_TOKEN=$(cat "$_CCSECRET_OUT" 2>/dev/null || cat "$_CCSECRET" 2>/dev/null)
# Re-extract from the JSON dump for the simple case (small files).
if [ -f "$_CCSECRET" ]; then
    _CC_GITHUB_TOKEN=$(python3 -c "import json; print(json.load(open('$_CCSECRET'))['Parameter']['Value'])" 2>/dev/null || echo "")
fi
export _CC_GITHUB_TOKEN{api_key_block}

STT_API_URL=$(get_oos_param "{oos_root}/stt/api-url" /tmp/stt_api_url.json)
export STT_API_URL
STT_API_KEY=$(get_oos_param "{oos_root}/stt/api-key" /tmp/stt_api_key.json)
export STT_API_KEY
STT_MODEL=$(get_oos_param "{oos_root}/stt/model" /tmp/stt_model.json)
export STT_MODEL
STT_LANGUAGE=$(get_oos_param "{oos_root}/stt/language" /tmp/stt_language.json)
export STT_LANGUAGE
STT_MODELS_BUCKET=$(get_oos_param "{oos_root}/stt/models-bucket" /tmp/stt_models_bucket.json)
export STT_MODELS_BUCKET
"""


def _read_git_identity_oos_script(owner_login: str) -> str:
    """Render the bootstrap snippet that fetches the operator-configured
    git author identity from the OOS Parameter Store. Parallel to
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
    root = f"blitzlog/users/{owner_login}/git-author"
    return (
        f'\nGIT_AUTHOR_NAME=$(get_oos_param "{root}/name" /tmp/git_author_name.json '
        f'2>/dev/null || echo "")\n'
        f'GIT_AUTHOR_EMAIL=$(get_oos_param "{root}/email" /tmp/git_author_email.json '
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
    log "ACTIONABLE: Verify the API key in the OOS parameter {ssm_root}/opencode/api-key belongs to a funded account."
fi
if [ -f "$LOG_FILE" ] && grep -qE "Unauthorized|invalid_api_key|\\(401\\)|Authentication" "$LOG_FILE"; then
    log "ACTIONABLE: LLM provider rejected the API key as unauthorized (HTTP 401)."
    log "ACTIONABLE: Rotate {ssm_root}/opencode/api-key in the OOS Parameter Store and re-run terraform apply."
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
