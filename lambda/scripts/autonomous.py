"""Autonomous-mode user-data bootstrap builder.

Builds the bash script that EC2 runs at first boot for an autonomous
("fire-and-forget") agent run. The script:
  - sets up the env (defensive scrub, LOG_FILE/log(), exports)
  - fetches secrets from SSM (GitHub token, optionally the cloud API key)
  - optionally installs/authenticates Tailscale (for local-LLM transport)
  - probes the local LLM endpoint if configured (abort on 5 min unreachable)
  - downloads the whisper model from S3 (if not cached locally) for
    the container's whisper-stt-shim
  - writes the opencode config + session-archive plugin
  - writes the spot-watchdog and periodic-autosave plugins
  - writes /etc/blitzlog.env (the host's watchdog reads this and
    passes selected vars to the container)
  - boots the host's Packer-baked watchdog.sh via systemd, which loads
    the Packer-baked container image and `docker run`s it; the
    container's entrypoint handles the rest (git clone, mise install,
    `opencode run --agent build`)

Module-local helpers are kept here because they're only used by this
script. Helpers shared with assisted mode (`_decode_api_errors_script`,
`_read_secrets_from_ssm_script`, ...) live in `_common.py`.
"""

import os

from _common import (
    _local_llm_env_block,
    _local_llm_log_line,
    _read_secrets_from_ssm_script,
    script_header,
)


def build_autonomous_user_data(
    repo: str,
    issue_number: int,
    sender_login: str = "",
    sender_id: str = "",
    local_llm: dict | None = None,
) -> str:
    prompt = (
        f"Work on GitHub issue #{issue_number}. Follow AGENTS.md for branch naming, "
        f"implementation standards, testing, linting and commit conventions. "
        f"Create a PR when done."
    )

    s3_bucket = os.environ.get("S3_LOGS_BUCKET", "<your-agent-logs-bucket>")
    base_opencode_model = os.environ.get(
        "OPENCODE_MODEL", "minimax-coding-plan/MiniMax-M3"
    )
    opencode_model = local_llm["model"] if local_llm else base_opencode_model
    s3_archive_prefix = f"{repo}/issue/{issue_number}"

    local_llm_env = _local_llm_env_block(local_llm)
    local_llm_log = _local_llm_log_line(local_llm)

    opencode_max_steps = int(os.environ.get("OPENCODE_AGENT_MAX_STEPS", "500"))

    header = script_header(
        mode="autonomous",
        repo=repo,
        issue_number=issue_number,
        opencode_model=opencode_model,
        opencode_max_steps=opencode_max_steps,
        s3_bucket=s3_bucket,
        s3_archive_prefix=s3_archive_prefix,
        local_llm_env=local_llm_env,
        local_llm_log_line=local_llm_log,
        opencode_prompt=prompt,
    )

    return f"""{header}{_read_secrets_from_ssm_script(issue_number, local_llm=bool(local_llm))}

log "Downloading whisper model..."
mkdir -p /opt/whisper-stt/models
if [ ! -f "/opt/whisper-stt/models/ggml-${{STT_MODEL:-base.en}}.bin" ]; then
    aws s3 cp "s3://${{STT_MODELS_BUCKET}}/models/ggml-${{STT_MODEL:-base.en}}.bin" \\
        "/opt/whisper-stt/models/ggml-${{STT_MODEL:-base.en}}.bin" \\
        --region "$REGION"
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
# session artifacts to S3, releases the bot pool lock, and terminates
# the EC2 instance.
#
# Write the env file the watchdog reads. Tailscale + local LLM env
# vars are passed through so the container can join a Tailnet and
# point opencode at a local-LLM endpoint. The container's entrypoint
# already runs tailscale up, writes opencode.json, and runs the local
# LLM preflight — we don't repeat any of that here.
mkdir -p /workspace/.blitzlog
cat > /etc/blitzlog.env <<ENVEOF
MODE=autonomous
ISSUE_NUMBER={issue_number}
REPO={repo}
BLITZLOG_ENV=${{BLITZLOG_ENV}}
S3_LOGS_BUCKET={s3_bucket}
SESSION_ARCHIVE_BUCKET={s3_bucket}
SESSION_ARCHIVE_PREFIX={s3_archive_prefix}
OPENCODE_API_KEY=${{OPENCODE_API_KEY}}
OPENCODE_MODEL=${{OPENCODE_MODEL}}
OPENCODE_PROMPT=${{OPENCODE_PROMPT:-}}
LOCAL_LLM_ENDPOINT=${{LOCAL_LLM_ENDPOINT:-}}
LOCAL_LLM_MODEL=${{LOCAL_LLM_MODEL:-}}
LOCAL_LLM_API_KEY=${{LOCAL_LLM_API_KEY:-}}
LOCAL_LLM_FALLBACK=${{LOCAL_LLM_FALLBACK:-closed}}
TAILSCALE_AUTH_KEY=${{TAILSCALE_AUTH_KEY:-}}
OPENCODE_SERVER_USERNAME=agent
GITHUB_TOKEN_SSM_PARAM=/blitzlog/${{BLITZLOG_ENV}}/ephemeral/github-token-${{ISSUE_NUMBER}}
ENVEOF

# Unit is already enabled (Packer 02-systemd.sh). Start it; watchdog
# runs the agent container, waits for exit, then does AWS cleanup.
sudo systemctl start blitzlog-agent.service
"""
