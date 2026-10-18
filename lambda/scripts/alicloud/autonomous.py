"""Alicloud variant of the autonomous-mode user-data builder.

Issue #126: parallel to `scripts/aws/autonomous.py`. Generates a
bash bootstrap that ECS runs at first boot for an autonomous
("fire-and-forget") agent run on Alicloud. The script:

  - installs `ossutil` + `aliyun` CLI (one-time, ~10s) — the host
    image is still AWS-only (Phase 5 brings a per-cloud Packer image)
  - sources an inline `blitzlog_oss_*` library (parallel to the
    AWS-side `blitzlog_s3_*`)
  - reads secrets from KMS via the instance metadata service at
    100.100.100.200 (Aliyun's IMDSv2-equivalent)
  - downloads the whisper model from OSS (if not cached locally)
    via the inlined `blitzlog_oss_get` helper
  - writes /etc/blitzlog.env (the host's watchdog reads this and
    passes selected vars to the container)
  - boots the host's Packer-baked watchdog.sh via systemd

The `/etc/blitzlog.env` heredoc keeps the same shape as the AWS
side; the only difference is the `OSS_LOGS_BUCKET` / `OSS_LOG_PREFIX`
env var names (vs the AWS `S3_*` prefix). The watchdog's log-upload
helper is still AWS-only (issue #126 Phase 5 follow-up); on
Alicloud, the watchdog's S3 API calls fail silently. The
MNS DLQ + FC dead-letter config catch the failure.
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


def build_autonomous_user_data(
    repo: str,
    issue_number: int,
    sender_login: str = "",
    sender_id: str = "",
    local_llm: dict | None = None,
) -> str:
    """Generate the autonomous-mode user-data script for an Alicloud ECS instance.

    Parallel to scripts.aws.autonomous.build_autonomous_user_data. The
    difference is the prologue (CLI install + OSS helpers + KMS read,
    in alicloud/_common.py) and the bucket env-var names
    (OSS_LOGS_BUCKET vs the AWS-side S3_LOGS_BUCKET).

    The function signature mirrors
    scripts.aws.autonomous.build_autonomous_user_data 1-to-1 so
    lambda/handler.py can dispatch via the scripts package's
    __init__.py without per-cloud branching.
    """
    prompt = (
        f"Work on GitHub issue #{issue_number}. Follow AGENTS.md for branch naming, "
        f"implementation standards, testing, linting and commit conventions. "
        f"Create a PR when done."
    )

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
    # emitted by `script_header` (so the watchdog/container see the
    # values the fetch below assigns); the actual KMS read lives in
    # `_read_git_identity_kms_script` and runs AFTER `$REGION` is set
    # so the IMDS dance completes first.
    git_identity_env = (
        "export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL\n" if sender_login else ""
    )

    opencode_max_steps = int(os.environ.get("OPENCODE_AGENT_MAX_STEPS", "500"))

    header = script_header(
        mode="autonomous",
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
        opencode_prompt=prompt,
    )

    return f"""{header}{_read_secrets_from_kms_script(issue_number, local_llm=bool(local_llm))}{_read_git_identity_kms_script(sender_login)}

log "Downloading whisper model..."
mkdir -p /opt/whisper-stt/models
if [ ! -f "/opt/whisper-stt/models/ggml-${{STT_MODEL:-base.en}}.bin" ]; then
    blitzlog_oss_get "${{STT_MODELS_BUCKET}}" \\
        "models/ggml-${{STT_MODEL:-base.en}}.bin" \\
        "/opt/whisper-stt/models/ggml-${{STT_MODEL:-base.en}}.bin"
fi

log "Effective opencode config: model=$OPENCODE_MODEL"

# opencode plugins (session_archive, spot_watchdog) and tools are baked
# into the container image at /root/.config/opencode/{{plugins,tools}}/
# by the Dockerfile (packages/images/agent/Dockerfile). Autonomous mode
# only needs session_archive + spot_watchdog; idle_watchdog and
# periodic_autosave are assisted-mode-only and not loaded in autonomous.

log "Starting autonomous opencode agent via systemd watchdog..."
# The watchdog (Packer-baked to /usr/local/bin/watchdog.sh, registered
# via 02-systemd.sh as the blitzlog-agent.service unit) is the host's
# lifecycle manager. It loads the baked container image, runs the agent
# (the container's entrypoint invokes opencode run for the autonomous
# mode), waits for it to exit, uploads host + container logs and
# session artifacts to OSS, releases the bot pool lock, and terminates
# the ECS instance.
#
# KNOWN LIMITATION (issue #126 Phase 5): the watchdog's log upload
# uses the AWS-side `blitzlog_s3_*` library
# (infra/aws/packer/scripts-docker-ubuntu/watchdog.sh). On the
# Alicloud path the upload will fail silently (`|| true` swallow) —
# the MNS DLQ + FC dead-letter config catch the eventual session-
# export failure.
#
# Write the env file the watchdog reads. Tailscale + local LLM env
# vars are passed through so the container can join a Tailnet and
# point opencode at a local-LLM endpoint. The container's entrypoint
# already runs tailscale up, writes opencode.json, and runs the local
# LLM preflight — we don't repeat any of that here.
mkdir -p /workspace/.blitzlog
cat > /etc/blitzlog.env <<ENVEOF
MODE="autonomous"
ISSUE_NUMBER="{issue_number}"
REPO="{repo}"
BLITZLOG_ENV="${{BLITZLOG_ENV}}"
OSS_LOGS_BUCKET="${{OSS_LOGS_BUCKET}}"
SESSION_ARCHIVE_BUCKET="${{OSS_LOGS_BUCKET}}"
SESSION_ARCHIVE_PREFIX="{oss_archive_prefix}"
OSS_LOG_PREFIX="{oss_log_prefix}"
OPENCODE_API_KEY="${{OPENCODE_API_KEY}}"
OPENCODE_MODEL="${{OPENCODE_MODEL}}"
OPENCODE_PROMPT="${{OPENCODE_PROMPT:-}}"
LOCAL_LLM_ENDPOINT="${{LOCAL_LLM_ENDPOINT:-}}"
LOCAL_LLM_MODEL="${{LOCAL_LLM_MODEL:-}}"
LOCAL_LLM_API_KEY="${{LOCAL_LLM_API_KEY:-}}"
LOCAL_LLM_FALLBACK="${{LOCAL_LLM_FALLBACK:-closed}}"
TAILSCALE_AUTH_KEY="${{TAILSCALE_AUTH_KEY:-}}"
# Audit-trail identity (issue #119). The bootstrap above fetched
# these from /blitzlog/users/{{owner}}/git-author/{{name,email}};
# the watchdog forwards them to the container via `-e` so the
# container's `git config --global user.{{name,email}}` step
# (entrypoint.sh) attributes commits to the operator.
GIT_AUTHOR_NAME="${{GIT_AUTHOR_NAME:-}}"
GIT_AUTHOR_EMAIL="${{GIT_AUTHOR_EMAIL:-}}"
OPENCODE_SERVER_USERNAME="agent"
GITHUB_TOKEN_SSM_PARAM="/blitzlog/${{BLITZLOG_ENV}}/ephemeral/gh/${{REPO}}/${{ISSUE_NUMBER}}"
# Issue #145: the Alicloud path has no EventBridge Scheduler
# equivalent in this PR (no token-refresh chain — the 1h GitHub
# token cap is operationally visible). The watchdog's refresh
# branch is a no-op on Alicloud until the Aliyun scheduler
# equivalent lands (Phase 5 follow-up).
GITHUB_TOKEN_SCHED_GROUP="blitzlog-${{BLITZLOG_ENV}}-token-refresh-$(printf '%s|%s' "${{REPO}}" "${{ISSUE_NUMBER}}" | sha256sum | cut -c1-16)"
ENVEOF

# Unit is already enabled (Packer 02-systemd.sh). Start it; watchdog
# runs the agent container, waits for exit, then does AWS cleanup.
# (Cleanup is still AWS-shaped on the Alicloud path — Phase 5
# follow-up adds the Alicloud equivalents.)
sudo systemctl start blitzlog-agent.service
"""
