"""Alicloud variant of the assisted-mode user-data builder.

Issue #126: parallel to `scripts/aws/assisted.py`. Generates a
bash bootstrap that ECS runs at first boot for an assisted
("interactive via Telegram") agent run on Alicloud. The script
shape mirrors the AWS side 1-to-1 with two cloud-specific
differences:

  - **OSS + KMS instead of S3 + SSM.** The OSS bucket helpers
    (`blitzlog_oss_*`) are inlined; the KMS read uses `aliyun
    kms GetSecretValue` via the instance metadata service at
    100.100.100.200.
  - **Bucket env vars renamed.** `OSS_LOGS_BUCKET`,
    `SESSION_ARCHIVE_BUCKET`, `OSS_LOG_PREFIX` (vs the AWS
    `S3_*` prefix). The /etc/blitzlog.env heredoc reflects the
    new names; the watchdog reads them by name.

The function signature mirrors
`scripts.aws.assisted.build_assisted_user_data` 1-to-1 so
`lambda/handler.py` can dispatch via the `scripts` package's
`__init__.py` without per-cloud branching.
"""

import os

from _env import _blitzlog_env

from ._common import (  # sibling subpackage import
    _local_llm_env_block,
    _local_llm_log_line,
    _read_git_identity_kms_script,
    _read_secrets_from_kms_script,
    script_header,
)


def _session_restore_script(
    repo: str, issue_number: int, oss_bucket: str, env: str
) -> str:
    """Parallel to `scripts/aws/assisted.py:_session_restore_script`,
    with `blitzlog_s3_*` → `blitzlog_oss_*` everywhere. The session
    metadata file is fetched from OSS instead of S3, and the
    `S3_RESTORE_*` env-var names become `OSS_RESTORE_*`.
    """
    oss_prefix = f"{env}/{repo}/issue/{issue_number}"
    return f"""
OSS_RESTORE_PREFIX="{oss_prefix}"
OSS_RESTORE_BUCKET="{oss_bucket}"
RESUMED=false

log "Checking OSS for existing session state..."
if blitzlog_oss_ls "${{OSS_RESTORE_BUCKET}}" "${{OSS_RESTORE_PREFIX}}/metadata.json" >/dev/null 2>&1; then
    log "Found previous session metadata, downloading..."
    blitzlog_oss_get "${{OSS_RESTORE_BUCKET}}" \\
        "${{OSS_RESTORE_PREFIX}}/metadata.json" \\
        /tmp/session-metadata.json 2>/dev/null || true

    if [ -f /tmp/session-metadata.json ]; then
        RESTORE_BRANCH=$(python3 -c "import json; print(json.load(open('/tmp/session-metadata.json')).get('branch',''))" 2>/dev/null || echo "")
        RESTORE_SESSION_ID=$(python3 -c "import json; print(json.load(open('/tmp/session-metadata.json')).get('sessionId',''))" 2>/dev/null || echo "")

        if [ -n "$RESTORE_BRANCH" ]; then
            log "Checking out branch: $RESTORE_BRANCH"
            git -C /workspace/repo fetch origin "$RESTORE_BRANCH" 2>/dev/null || true
            git -C /workspace/repo checkout "$RESTORE_BRANCH" 2>/dev/null || log "WARNING: Could not checkout branch $RESTORE_BRANCH"
        fi

        if [ -n "$RESTORE_SESSION_ID" ]; then
            log "Downloading session $RESTORE_SESSION_ID..."
            blitzlog_oss_get "${{OSS_RESTORE_BUCKET}}" \\
                "${{OSS_RESTORE_PREFIX}}/sessions/${{RESTORE_SESSION_ID}}.json" \\
                /tmp/session-import.json 2>/dev/null || true
        fi

        if [ -f /tmp/session-import.json ]; then
            RESUMED=true
            log "Session state downloaded for restore"
        fi
    fi
else
    log "No previous session state found"
fi

# Export resume state for the watchdog/entrypoint. The container
# entrypoint reads these from /etc/blitzlog.env (written by the bootstrap
# block below) to construct the "Resumed session: <title>" line in the
# Telegram ready notification — matching the AWS-side byte layout
# except for the OSS_RESTORE_* env-var names.
export OPENCODE_RESUMED="$RESUMED"
if [ -f /tmp/session-import.json ]; then
    export OPENCODE_RESUMED_TITLE=$(python3 -c "import json; print(json.load(open('/tmp/session-import.json')).get('title',''))" 2>/dev/null || echo "")
else
    export OPENCODE_RESUMED_TITLE=""
fi
"""


def build_assisted_user_data(
    repo: str,
    issue_number: int,
    sender_login: str = "",
    sender_id: str = "",
    bot_name: str = "",
    bot_token: str = "",
    telegram_user_id: str = "",
    local_llm: dict | None = None,
    opencode_model: str | None = None,
    opencode_base_url: str = "",
    opencode_api_key_ssm_param: str = "",
) -> str:
    """Generate the assisted-mode user-data script for an Alicloud ECS instance.

    Parallel to `scripts.aws.assisted.build_assisted_user_data`. The
    prologue (CLI install + OSS helpers + KMS read) lives in
    `alicloud/_common.py`; the body of this function only differs
    from the AWS side in (a) the `OSS_*` env-var names and (b) the
    `blitzlog_oss_*` helper calls in the session-restore block.

    `opencode_model`, `opencode_base_url` and
    `opencode_api_key_ssm_param` are the resolved model + credential
    path of issue #165. They are ACCEPTED for signature parity but
    IGNORED here: the three-level model selection ships on the AWS
    path only (the Alicloud tree is still under development, and it
    has no per-user stack to read a per-user model or credential
    from). The Alicloud run keeps using the org default model and
    the org KMS secret.
    """
    oss_bucket = os.environ.get("OSS_LOGS_BUCKET", "<your-agent-logs-bucket>")
    base_opencode_model = os.environ.get(
        "OPENCODE_MODEL", "minimax-coding-plan/MiniMax-M3"
    )
    opencode_model = local_llm["model"] if local_llm else base_opencode_model
    env = _blitzlog_env()
    oss_archive_prefix = f"{env}/{repo}/issue/{issue_number}"
    oss_log_prefix = oss_archive_prefix

    local_llm_env = _local_llm_env_block(local_llm)
    local_llm_log = _local_llm_log_line(local_llm)
    # Symmetric with the AWS-side: the `export GIT_AUTHOR_*` line is
    # emitted by `script_header`; the actual KMS read lives in
    # `_read_git_identity_kms_script` and runs AFTER `$REGION` is set.
    git_identity_env = (
        "export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL\n" if sender_login else ""
    )

    opencode_max_steps = int(os.environ.get("OPENCODE_AGENT_MAX_STEPS", "500"))

    header = script_header(
        mode="assisted",
        repo=repo,
        issue_number=issue_number,
        opencode_model=opencode_model,
        opencode_max_steps=opencode_max_steps,
        oss_bucket=oss_bucket,
        oss_archive_prefix=oss_archive_prefix,
        oss_log_prefix=oss_log_prefix,
        local_llm_env=local_llm_env,
        git_identity_env=git_identity_env,
        local_llm_log_line=local_llm_log,
        opencode_prompt=None,
    )

    return f"""{header}{_read_secrets_from_kms_script(issue_number, local_llm=bool(local_llm))}{_read_git_identity_kms_script(sender_login)}
TELEGRAM_USER_ID="{telegram_user_id}"
TELEGRAM_BOT_TOKEN="{bot_token}"
export TELEGRAM_BOT_TOKEN TELEGRAM_USER_ID

log "Downloading whisper model..."
mkdir -p /opt/whisper-stt/models
if [ ! -f "/opt/whisper-stt/models/ggml-${{STT_MODEL:-base.en}}.bin" ]; then
    blitzlog_oss_get "${{STT_MODELS_BUCKET}}" \\
        "models/ggml-${{STT_MODEL:-base.en}}.bin" \\
        "/opt/whisper-stt/models/ggml-${{STT_MODEL:-base.en}}.bin"
fi

log "Restoring previous session state..."
{_session_restore_script(repo, issue_number, oss_bucket, env)}

log "Effective opencode config: model=$OPENCODE_MODEL, provider=${{OPENCODE_MODEL%%/*}}, api_key_prefix=${{OPENCODE_API_KEY:0:8}}..."

# opencode plugins (idle_watchdog, periodic_autosave, session_archive,
# spot_watchdog) and tools (shutdown) are baked into the container
# image at /root/.config/opencode/{{plugins,tools}}/ by the Dockerfile
# (packages/images/agent/Dockerfile). The Lambda bootstrap no longer
# writes them via heredoc — that path was removed when the container
# took over plugin/tool installation.

log "Starting blitzlog-agent via systemd watchdog..."
# The watchdog (Packer-baked to /usr/local/bin/watchdog.sh and registered
# via 02-systemd.sh as the blitzlog-agent.service unit) is the host's
# lifecycle manager. It loads the baked container image, runs it with
# the right env vars + mounts, waits for it to exit, uploads host +
# container logs and session artifacts to OSS, releases the bot pool
# lock, and terminates the ECS instance.
#
# KNOWN LIMITATION (issue #126 Phase 5): the watchdog's log upload
# helper is still the AWS-side `blitzlog_s3_*` library (Packer-baked
# at /usr/local/bin/blitzlog_uploader.sh). On the Alicloud path the
# upload will fail silently (`|| true` swallow) — the MNS DLQ + FC
# dead-letter config catch the eventual session-export failure.
#
# Write the env file the watchdog reads. Tailscale + local LLM env
# vars are passed through so the container can join a Tailnet and
# point opencode at a local-LLM endpoint. The container's entrypoint
# already runs tailscale up, writes opencode.json, and runs the local
# LLM preflight — we don't repeat any of that here.
mkdir -p /workspace/.blitzlog
cat > /etc/blitzlog.env <<ENVEOF
MODE="assisted"
ISSUE_NUMBER="{issue_number}"
REPO="{repo}"
BLITZLOG_ENV="${{BLITZLOG_ENV}}"
OSS_LOGS_BUCKET="${{OSS_LOGS_BUCKET}}"
SESSION_ARCHIVE_BUCKET="${{OSS_LOGS_BUCKET}}"
SESSION_ARCHIVE_PREFIX="{oss_archive_prefix}"
OSS_LOG_PREFIX="{oss_log_prefix}"
OPENCODE_API_KEY="${{OPENCODE_API_KEY}}"
OPENCODE_MODEL="{opencode_model}"
OPENCODE_PROMPT=""
LOCAL_LLM_ENDPOINT="${{LOCAL_LLM_ENDPOINT:-}}"
LOCAL_LLM_MODEL="${{LOCAL_LLM_MODEL:-}}"
LOCAL_LLM_API_KEY="${{LOCAL_LLM_API_KEY:-}}"
LOCAL_LLM_FALLBACK="${{LOCAL_LLM_FALLBACK:-closed}}"
TAILSCALE_AUTH_KEY="${{TAILSCALE_AUTH_KEY:-}}"
TELEGRAM_BOT_TOKEN="${{TELEGRAM_BOT_TOKEN}}"
TELEGRAM_USER_ID="${{TELEGRAM_USER_ID}}"
TELEGRAM_BOT_NAME="{bot_name}"
TELEGRAM_SENDER_LOGIN="{sender_login}"
# Audit-trail identity (issue #119). Fetched from
# /blitzlog/users/{{owner}}/git-author/{{name,email}} above; the
# watchdog forwards to the container so entrypoint.sh can
# git config --global user.{{name,email}}.
GIT_AUTHOR_NAME="${{GIT_AUTHOR_NAME:-}}"
GIT_AUTHOR_EMAIL="${{GIT_AUTHOR_EMAIL:-}}"
STT_API_URL="${{STT_API_URL}}"
STT_API_KEY="${{STT_API_KEY}}"
STT_MODEL="${{STT_MODEL}}"
STT_LANGUAGE="${{STT_LANGUAGE}}"
OPENCODE_SERVER_USERNAME="agent"
OPENCODE_RESUMED="${{OPENCODE_RESUMED:-}}"
OPENCODE_RESUMED_TITLE="${{OPENCODE_RESUMED_TITLE:-}}"
GITHUB_TOKEN_SSM_PARAM="/blitzlog/${{BLITZLOG_ENV}}/ephemeral/gh/${{REPO}}/${{ISSUE_NUMBER}}"
# Issue #145: the Alicloud path has no EventBridge Scheduler
# equivalent in this PR. GITHUB_TOKEN_SCHED_GROUP is set so the
# watchdog's refresh branch has the right group name to look up;
# the actual schedule creation is a no-op until the Aliyun
# scheduler equivalent lands (Phase 5 follow-up).
GITHUB_TOKEN_SCHED_GROUP="blitzlog-${{BLITZLOG_ENV}}-token-refresh-$(printf '%s|%s' "${{REPO}}" "${{ISSUE_NUMBER}}" | sha256sum | cut -c1-16)"
ENVEOF

# The unit is already enabled (Packer 02-systemd.sh). Just start it; the
# watchdog will `docker run` the agent, wait for it to exit, then do all
# AWS-dependent cleanup + terminate the instance.
# (Cleanup is still AWS-shaped on the Alicloud path — Phase 5
# follow-up adds the Alicloud equivalents.)
sudo systemctl start blitzlog-agent.service
"""
