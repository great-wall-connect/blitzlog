#!/bin/bash
# /usr/local/bin/watchdog.sh — HOST-SIDE lifecycle manager for the
# blitzlog-agent container.
#
# Started by the blitzlog-agent systemd unit on instance boot. The
# watchdog:
#   1. Loads the pre-baked agent image into dockerd
#   2. Starts the container with the right env vars + volume mounts
#   3. Waits for the container to exit (any reason: shutdown tool,
#      idle, opencode finished, spot interruption, etc.)
#   4. Uploads container + host logs to S3
#   5. Uploads session artifacts the container wrote to /workspace/.blitzlog/
#   6. Releases the bot-pool lock
#   7. Terminates the EC2 instance
#
# The container has NO AWS credentials — all S3 work happens here on the
# host, which has the EC2 instance role.

set -euo pipefail
# Packer-baked signature: identifies the single agent image loaded
# into dockerd. /root/blitzlog.env is written by the Packer build
# alongside the image save step, with chmod 400 root:root. We source
# it BEFORE /etc/blitzlog.env so AGENT_IMAGE_REPO / AGENT_IMAGE_TAG
# are available when we build the docker run command.
source /root/blitzlog.env
source /etc/blitzlog.env
# Issue #123: single source of truth for host-side S3 calls. Falls
# through to the caller's `log()` (defined below) for ERROR output.
source /usr/local/bin/blitzlog_uploader.sh
# Issue #145: 5-min background poll loop that re-reads
# /blitzlog/<env>/ephemeral/github-token-<N> and rewrites the
# bind-mounted credential files whenever the value changes (i.e. when
# the Lambda's refresh event has just fired). Sourced BEFORE the
# initial token write below so the same `_bl_tokenpoll_write_*`
# helpers are used at startup and on every subsequent refresh.
source /usr/local/bin/blitzlog_token_poll.sh
# Issue #164: the canonical container-hardening contract (dropped
# capabilities, no-new-privileges, pids-limit, init, and the one
# documented Tailscale capability exemption). Sourced from the AMI —
# the Packer `file` provisioner uploads it and 02-systemd.sh copies it
# to /usr/local/bin/. The contract file itself is POSIX (no arrays, no
# bashisms) so the mise dev tasks can source it under /bin/sh too.
#
# `set -euo pipefail` is already in effect: if the contract file is
# missing from the AMI, this `source` fails and the watchdog aborts
# BEFORE the container starts. Fail-closed and loud — a container that
# silently ran without its hardening flags would be far worse.
source /usr/local/bin/container-hardening.env
export HOME=/root

LOG_FILE="/var/log/backend-bootstrap.log"
log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" | tee -a "$LOG_FILE"; }

# Issue #164: expand the hardening contract into bash arrays we can
# splat into `docker run`. `read -r -a` is a bashism, which is fine —
# the watchdog is #!/bin/bash. (The contract FILE stays POSIX so the
# mise dev tasks can source it under /bin/sh.)
read -r -a HARDENING_ARGS <<<"$BLITZLOG_HARDENING_FLAGS"
read -r -a TAILSCALE_ARGS <<<"$BLITZLOG_TAILSCALE_FLAGS"
read -r -a TMPFS_ARGS <<<"$BLITZLOG_TMPFS_MOUNTS"

# Load the baked agent image
sudo docker load -i /opt/blitzlog/images/blitzlog-agent.tar.gz

# Pre-create .blitzlog (owned by host, so container's writes can't fail
# with permission denied) and .idle as a "container is alive" marker.
# We do NOT pre-create .shutdown — that's only created when the agent's
# shutdown tool runs assisted-shutdown.sh (as its LAST step, after
# exporting the session). This avoids a race against the watchdog's
# own exit-detection logic.
mkdir -p /workspace/.blitzlog
touch /workspace/.idle

# Issue #164: the container runs as a specific uid
# (BLITZLOG_CONTAINER_UID, see the hardening contract). A bind mount
# preserves the SOURCE's ownership, so the host paths the container
# writes to must be owned by that uid — otherwise the agent's first
# write fails with EACCES and the entrypoint aborts under `set -e`.
#
# The chown covers /workspace itself (the container clones the repo
# there) plus the pre-created markers above (the container re-touches
# /workspace/.idle on every start).
chown -R "$BLITZLOG_CONTAINER_UID:$BLITZLOG_CONTAINER_GID" /workspace

# Compute IMDSv2 token + region + instance id (host AWS creds via role)
TOKEN=$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/instance-id)
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/dynamic/instance-identity/document \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['region'])")
export AWS_REGION="$REGION"

# Write the env file the container reads (and that shutdown.sh sources).
# Tailscale + local LLM env vars are passed through so the container
# can join a Tailnet (if TAILSCALE_AUTH_KEY is set) and point opencode
# at a local-LLM endpoint (if LOCAL_LLM_ENDPOINT is set). The container's
# entrypoint handles the actual setup; the bootstrap is just plumbing.
cat > /etc/blitzlog.env <<ENVEOF
MODE="${MODE:-assisted}"
ISSUE_NUMBER="${ISSUE_NUMBER}"
REPO="${REPO}"
BLITZLOG_ENV="${BLITZLOG_ENV}"
S3_LOGS_BUCKET="${S3_LOGS_BUCKET}"
SESSION_ARCHIVE_BUCKET="${SESSION_ARCHIVE_BUCKET}"
SESSION_ARCHIVE_PREFIX="${SESSION_ARCHIVE_PREFIX}"
S3_LOG_PREFIX="${S3_LOG_PREFIX}"
OPENCODE_API_KEY="${OPENCODE_API_KEY}"
OPENCODE_MODEL="${OPENCODE_MODEL}"
OPENCODE_PROMPT="${OPENCODE_PROMPT:-}"
LOCAL_LLM_ENDPOINT="${LOCAL_LLM_ENDPOINT:-}"
LOCAL_LLM_MODEL="${LOCAL_LLM_MODEL:-}"
LOCAL_LLM_API_KEY="${LOCAL_LLM_API_KEY:-}"
LOCAL_LLM_FALLBACK="${LOCAL_LLM_FALLBACK:-closed}"
TAILSCALE_AUTH_KEY="${TAILSCALE_AUTH_KEY:-}"
OPENCODE_SERVER_USERNAME="${OPENCODE_SERVER_USERNAME:-agent}"
OPENCODE_SERVER_PASSWORD="${OPENCODE_SERVER_PASSWORD:-}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_USER_ID="${TELEGRAM_USER_ID:-}"
TELEGRAM_BOT_NAME="${TELEGRAM_BOT_NAME:-}"
TELEGRAM_SENDER_LOGIN="${TELEGRAM_SENDER_LOGIN:-}"
# Audit-trail identity (issue #119). The bootstrap fetched
# these from /blitzlog/users/<owner>/git-author/{name,email} and
# sourced them into scope; we re-emit them here so the
# rewrite of /etc/blitzlog.env (which overwrites the bootstrap's
# write) doesn't lose them. The container's entrypoint.sh
# reads GIT_AUTHOR_NAME / GIT_AUTHOR_EMAIL and runs
# `git config --global user.{name,email}` so agent commits
# are attributed to the operator, not root@<hostname>.
GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-}"
GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-}"
STT_MODEL="${STT_MODEL:-base.en}"
STT_LANGUAGE="${STT_LANGUAGE:-en}"
AWS_REGION="${REGION}"
ENVEOF

# Pull the GitHub token from SSM (autonomous mode needs it for git push)
if [ -n "${GITHUB_TOKEN_SSM_PARAM:-}" ]; then
    GITHUB_TOKEN=$(aws ssm get-parameter \
        --name "${GITHUB_TOKEN_SSM_PARAM}" \
        --with-decryption \
        --query Parameter.Value \
        --output text \
        --region "$REGION" 2>/dev/null || echo "")
    if [ -n "$GITHUB_TOKEN" ]; then
        # Issue #145: pre-create the per-instance gh-config
        # bind-mount point so the path exists when docker starts
        # (the bind mount's host side must exist or docker creates
        # it as root and the container's `gh auth login` writes
        # cannot be made world-readable).
        #
        # Issue #164: the HOST is now the only writer of these two
        # files. They are mounted READ-ONLY into the container, so
        # the agent cannot tamper with the host's copy, and the
        # watchdog's poll loop overwrites them on every subsequent
        # refresh. The container-side `gh auth login` is therefore
        # only a local-dev fallback (see entrypoint.sh).
        mkdir -p "/root/gh-config/${INSTANCE_ID}"
        mkdir -p /root/.git-credentials.d
        # The library helpers are the single source of truth for
        # the credential-file shape. Using them here (and in the
        # poll loop) means a future format change ships in one
        # place, not two.
        _bl_tokenpoll_write_git_creds "$GITHUB_TOKEN" \
            /root/.git-credentials.d/github
        _bl_tokenpoll_write_gh_hosts_yml "$GITHUB_TOKEN" \
            "/root/gh-config/${INSTANCE_ID}/hosts.yml"
        git config --global credential.helper \
            'store --file /root/.git-credentials.d/github'
        # Issue #164: hand the credential files to the container's
        # unprivileged uid. A bind mount preserves the source's
        # ownership, so a root-owned 0600 file would be unreadable
        # from inside the container. The poll loop's rewrites
        # truncate these files in place, so the ownership survives
        # every refresh.
        chown -R "$BLITZLOG_CONTAINER_UID:$BLITZLOG_CONTAINER_GID" \
            /root/.git-credentials.d "/root/gh-config/${INSTANCE_ID}"
    fi
fi

# Random opencode server password — written to env, then read by container
OPENCODE_SERVER_PASSWORD=$(openssl rand -hex 16)
export OPENCODE_SERVER_PASSWORD
echo "OPENCODE_SERVER_PASSWORD=$OPENCODE_SERVER_PASSWORD" >> /etc/blitzlog.env

# Start the blitzlog-agent container. /var/log/blitzlog and /workspace
# are host-mounted so the container's logs and session artifacts are
# accessible to the watchdog after the container exits.
mkdir -p /var/log/blitzlog
mkdir -p /workspace

# Issue #164: the container runs as an unprivileged uid and its rootfs
# is read-only, so every host path it writes to has to be owned by
# that uid. /var/log/blitzlog holds the container's logs (the watchdog
# uploads them to S3 after the run). /workspace was already chowned
# above, right after its marker files were created.
chown -R "$BLITZLOG_CONTAINER_UID:$BLITZLOG_CONTAINER_GID" /var/log/blitzlog

# Common env vars (passed to every container launch).
COMMON_ARGS=(
    -e "MODE=$MODE"
    -e "ISSUE_NUMBER=$ISSUE_NUMBER"
    -e "REPO=$REPO"
    -e "OPENCODE_MODEL=$OPENCODE_MODEL"
    -e "OPENCODE_PROMPT=$OPENCODE_PROMPT"
    -e "OPENCODE_API_KEY=$OPENCODE_API_KEY"
    -e "LOCAL_LLM_ENDPOINT=$LOCAL_LLM_ENDPOINT"
    -e "LOCAL_LLM_MODEL=$LOCAL_LLM_MODEL"
    -e "LOCAL_LLM_API_KEY=$LOCAL_LLM_API_KEY"
    -e "LOCAL_LLM_FALLBACK=$LOCAL_LLM_FALLBACK"
    -e "TAILSCALE_AUTH_KEY=$TAILSCALE_AUTH_KEY"
    -e "OPENCODE_SERVER_USERNAME=$OPENCODE_SERVER_USERNAME"
    -e "OPENCODE_SERVER_PASSWORD=$OPENCODE_SERVER_PASSWORD"
    -e "BLITZLOG_ENV=$BLITZLOG_ENV"
    -e "S3_LOGS_BUCKET=$S3_LOGS_BUCKET"
    -e "SESSION_ARCHIVE_BUCKET=$SESSION_ARCHIVE_BUCKET"
    -e "SESSION_ARCHIVE_PREFIX=$SESSION_ARCHIVE_PREFIX"
    -e "S3_LOG_PREFIX=$S3_LOG_PREFIX"
    # Issue #164: GITHUB_TOKEN is deliberately NOT passed to the
    # container. The host owns the credential files and mounts them
    # READ-ONLY, so the agent gets its credentials without the token
    # ever appearing in the container's environment (where `docker
    # inspect` and every same-uid process could read it).
    -e "GIT_AUTHOR_NAME=$GIT_AUTHOR_NAME"
    -e "GIT_AUTHOR_EMAIL=$GIT_AUTHOR_EMAIL"
    # Issue #165: the container has no AWS credentials BY DESIGN. This
    # makes the AWS SDK refuse the instance metadata service as well,
    # on top of the host-side iptables rule that drops IMDS traffic
    # from the container's network.
    -e "AWS_EC2_METADATA_DISABLED=true"
)

# Mode-specific env vars. We don't reference these outside the
# matching branch — `set -u` would otherwise kill the script when a
# var that isn't declared in /etc/blitzlog.env for the current mode
# is read. This is intentional: a future contributor adding a var the
# watchdog should pass gets a loud failure, not a silent empty value.
if [ "$MODE" = "assisted" ]; then
    COMMON_ARGS+=(
        -e "STT_MODEL=$STT_MODEL"
        -e "STT_LANGUAGE=$STT_LANGUAGE"
        -e "TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN"
        -e "TELEGRAM_BOT_NAME=$TELEGRAM_BOT_NAME"
        -e "TELEGRAM_USER_ID=$TELEGRAM_USER_ID"
    )
fi

# Tailscale needs CAP_NET_ADMIN (to create and configure the WireGuard
# interface), CAP_NET_RAW (for `tailscale ping`'s ICMP probes) and the
# /dev/net/tun character device (for the tunnel itself).
#
# This is the ONE and ONLY relaxation of --cap-drop=ALL in the whole
# project, and it is granted ONLY when TAILSCALE_AUTH_KEY is set —
# i.e. only on runs that actually enrol in a Tailnet. Every other run
# gets --cap-drop=ALL and nothing else.
#
# The flags come from the canonical contract
# (packages/images/agent/container-hardening.env) so the dev tasks in
# mise.toml grant exactly the same exemption.
#
# Passing --device /dev/net/tun is what makes the in-container join
# actually work. Before issue #164 the capability flags were passed
# WITHOUT the device and `tailscaled` was started without
# --tun=userspace-networking, so the join could never establish a
# tunnel — it logged "WARNING: tailscale up failed" and the local-LLM
# preflight failed over to cloud. The host-side counterpart is
# 01-system.sh, which loads the `tun` module and creates
# /dev/net/tun on the AMI.
TUNNEL_FLAGS=()
if [ -n "${TAILSCALE_AUTH_KEY:-}" ]; then
    TUNNEL_FLAGS=("${TAILSCALE_ARGS[@]}")
fi

# Host-side .shutdown watcher. Mirrors the local mise.toml
# image-telegram-live-assisted watcher. Polls for the agent's shutdown
# signal (touched by assisted-shutdown.sh, both for the user-invoked
# shutdown tool and for spot-interrupt emergency save) and SIGTERMs the
# container — otherwise the entrypoint's TERM trap is never triggered and
# the `docker run` below blocks until AWS reclaims it.
(
    while [ ! -f /workspace/.shutdown ]; do
            sleep 1
    done
    log ".shutdown detected; sending SIGTERM to blitzlog-agent"
    sudo docker kill --signal TERM blitzlog-agent 2>/dev/null || true
) &
WATCHER_PID=$!

# Issue #145: 5-min token-refresh poll loop. Reads the SSM
# /blitzlog/<env>/ephemeral/github-token-<N> parameter and rewrites
# the bind-mounted credential files whenever the value changes
# (i.e. when the Lambda's refresh event has just fired). The loop
# runs for the full lifetime of the container; it has no per-run
# state of its own. The chain of 10 scheduled refresh events
# covers ~8.3 h — well over the longest realistic assisted run —
# so the loop cannot run forever even if the schedule is buggy.
# Poll interval is 300s (5 min) — same cadence as the existing
# host-side SESSION_ARCHIVE_BUCKET and S3_LOG_PREFIX exports, so
# operators only need to remember one number.
if [ -n "${GITHUB_TOKEN_SSM_PARAM:-}" ]; then
    # `blitzlog_start_token_poll_loop` writes the subshell's PID to
    # the global `BLITZLOG_TOKEN_POLL_PID`. We use a global instead
    # of `$(...)` capture because bash's `wait` for a non-direct
    # child (the subshell is a grandchild of the watchdog once it
    # runs inside a function called via command substitution) is
    # unreliable; killing by PID sidesteps the issue. See the
    # comment in blitzlog_token_poll.sh for the rationale.
    blitzlog_start_token_poll_loop \
        "${GITHUB_TOKEN_SSM_PARAM}" \
        "$REGION" \
        /root/.git-credentials.d/github \
        "/root/gh-config/${INSTANCE_ID}/hosts.yml" \
        "${BLITZLOG_TOKEN_POLL_INTERVAL:-300}"
    log "Started token refresh poll loop (pid=${BLITZLOG_TOKEN_POLL_PID}, interval=${BLITZLOG_TOKEN_POLL_INTERVAL:-300}s)"
fi

# Defense in depth: clear any stale .shutdown left over from a crashed
# previous run. watchdog.sh no longer pre-creates it, so on a fresh
# instance .shutdown only ever appears because the agent's shutdown
# tool or the spot_watchdog plugin just ran assisted-shutdown.sh.
rm -f /workspace/.shutdown

# Issue #164: the credential files are owned by the HOST and mounted
# READ-ONLY (see the comment on the chown above). The container's uid
# can read them but cannot write them, so the agent cannot tamper with
# the host's copy of the token. The container's HOME is a tmpfs (see
# TMPFS_ARGS above), so the two credential paths are bind-mounted
# INSIDE it.
#
# `set -e` is active, so a non-zero `docker run` aborted the script
# right here — before the cleanup below (log upload, bot-lock release,
# SSM/schedule teardown, instance terminate) could run. A failed agent
# run therefore left the instance alive, the bot-pool lock held and the
# logs on the instance instead of in S3. Capture the exit code instead
# of letting errexit kill the watchdog; the cleanup path runs either
# way and the script exits with the container's code at the very end.
CONTAINER_EXIT=0

# Defense in depth (issue #175): block the instance metadata service
# from the CONTAINER's network.
#
# The container runs as root on the docker bridge, so it can reach
# 169.254.169.254 and assume the EC2 instance role. That role can
# read SSM parameters the container must never see — the GitHub App's
# private key and the webhook secret among them (issue #175) — so
# "the container has no AWS credentials" is only true while it cannot
# reach IMDS.
#
# DOCKER-USER is only traversed by FORWARDED container traffic, so the
# HOST's own IMDS access (the watchdog's token + region fetches above)
# is unaffected.
#
# This is FAIL-CLOSED. The first version of this rule was best-effort:
# it logged a warning and started the agent anyway, so a boundary that
# was not actually in force looked exactly like one that was — and the
# gap went unnoticed for a release cycle (issue #175). If the rule
# cannot be installed, the agent does not start; the cleanup path below
# still runs, so the instance is not stranded.
block_imds_from_container() {
    if ! command -v iptables >/dev/null 2>&1; then
        log "ERROR: iptables is not available — cannot block IMDS from the container network"
        return 1
    fi
    if iptables -C DOCKER-USER -d 169.254.169.254 -j DROP >/dev/null 2>&1; then
        return 0
    fi
    if ! iptables -I DOCKER-USER -d 169.254.169.254 -j DROP >/dev/null 2>&1; then
        log "ERROR: could not install the DOCKER-USER rule that blocks IMDS from the container network"
        return 1
    fi
    return 0
}

run_agent_container() {
    sudo docker run --name blitzlog-agent \
        "${HARDENING_ARGS[@]}" \
        "${TMPFS_ARGS[@]}" \
        "${TUNNEL_FLAGS[@]}" \
        "${COMMON_ARGS[@]}" \
        -v /opt/whisper-stt/models:/opt/whisper-stt/models:ro \
        -v "/root/.git-credentials.d:${BLITZLOG_CONTAINER_HOME}/.git-credentials.d:ro" \
        -v "/root/gh-config/${INSTANCE_ID}:${BLITZLOG_CONTAINER_HOME}/.config/gh:ro" \
        -v /workspace:/workspace \
        -v /var/log/blitzlog:/var/log/blitzlog \
        "${AGENT_IMAGE_REPO}:${AGENT_IMAGE_TAG}"
}

if block_imds_from_container; then
    run_agent_container || CONTAINER_EXIT=$?
else
    log "ERROR: refusing to start the agent container — without the IMDS block it could assume the instance role and read SSM parameters it must not see (issue #175)"
    CONTAINER_EXIT=1
fi
# The watcher auto-exits once it sends SIGTERM (the while loop ends
# when .shutdown appears). If .shutdown was never touched (autonomous
# mode finished, agent ran to completion), the watcher is still
# polling — kill it explicitly.
kill "$WATCHER_PID" 2>/dev/null || true
wait "$WATCHER_PID" 2>/dev/null || true

# Kill the token refresh poll loop now that the container has
# exited. The poll loop's `trap 'exit 0' TERM` makes the SIGTERM
# immediate. We don't `wait` — bash's `wait` for a non-direct
# child is unreliable when the subshell was spawned from a function
# (see blitzlog_token_poll.sh's comment on why we use a global PID
# instead of `$(...)` capture). A 100 ms grace period lets the trap
# fire and the subshell exit cleanly; if it's still alive after
# that, we send SIGKILL so it doesn't become a zombie carried to
# the EC2 instance shutdown.
if [ -n "${BLITZLOG_TOKEN_POLL_PID:-}" ]; then
    log "Stopping token refresh poll loop (pid=${BLITZLOG_TOKEN_POLL_PID})"
    kill "${BLITZLOG_TOKEN_POLL_PID}" 2>/dev/null || true
    # 100 ms grace period. 5 ticks of 20 ms each is enough for the
    # `trap 'exit 0' TERM` to fire even on a busy host; if the
    # process is still alive after that, SIGTERM is being ignored
    # and we escalate.
    for _ in 1 2 3 4 5; do
        if ! kill -0 "${BLITZLOG_TOKEN_POLL_PID}" 2>/dev/null; then
            break
        fi
        sleep 0.02
    done
    if kill -0 "${BLITZLOG_TOKEN_POLL_PID}" 2>/dev/null; then
        log "Token refresh poll loop did not exit on SIGTERM; sending SIGKILL"
        kill -9 "${BLITZLOG_TOKEN_POLL_PID}" 2>/dev/null || true
    fi
fi

log "Container exited with code $CONTAINER_EXIT"

# Decode known LLM-provider API errors into actionable log lines.
if [ -f /var/log/blitzlog/opencode.log ] && grep -qE "insufficient_balance|insufficient balance|\(1008\)" /var/log/blitzlog/opencode.log; then
    log "ACTIONABLE: LLM provider returned insufficient balance / HTTP 1008."
    log "ACTIONABLE: The token-plan account for $OPENCODE_MODEL has zero credits."
    log "ACTIONABLE: Top up at the provider console before retrying."
fi
if [ -f /var/log/blitzlog/opencode.log ] && grep -qE "Unauthorized|invalid_api_key|\(401\)" /var/log/blitzlog/opencode.log; then
    log "ACTIONABLE: LLM provider rejected the API key (HTTP 401)."
    log "ACTIONABLE: Rotate /blitzlog/$BLITZLOG_ENV/opencode/api-key in SSM and re-run terraform apply."
fi
if [ -f /var/log/blitzlog/opencode.log ] && grep -qE "rate.?limit|quota.?exceeded|too.?many.?requests|\(429\)" /var/log/blitzlog/opencode.log; then
    log "ACTIONABLE: LLM provider returned a rate-limit error (HTTP 429)."
    log "ACTIONABLE: Wait for the quota window to reset, then re-trigger."
fi

# Stamp every object from this run with the same timestamp+instance prefix,
# so all files from one EC2 run sort together and never overwrite each
# other (or a previous run's files on the same issue).
RUN_TS=$(date -u +%Y%m%d_%H%M)
RUN_TAG="${RUN_TS}_i${INSTANCE_ID}"

# Defensive default for AMIs baked before S3_LOG_PREFIX was added to the
# bootstrap env file. Falls back to the legacy flat env/logs/ path so a
# mid-deploy window (new lambda, old AMI) doesn't produce double-slash
# keys.
: "${S3_LOG_PREFIX:=${BLITZLOG_ENV}/logs}"

# Upload container + host logs to S3 (BOTH per user request).
for log in /var/log/blitzlog/*.log /var/log/backend-bootstrap.log; do
    [ -f "$log" ] || continue
    log_name=$(basename "$log")
    log "Uploading $log_name to S3"
    blitzlog_s3_put "${S3_LOGS_BUCKET}" \
        "${S3_LOG_PREFIX}/${RUN_TAG}_${log_name}" \
        "$log" || true
done

# Upload session artifacts the container wrote to /workspace/.blitzlog/
for f in /workspace/.blitzlog/*; do
    [ -f "$f" ] || continue
    fname=$(basename "$f")
    log "Uploading $fname to S3"
    blitzlog_s3_put "${SESSION_ARCHIVE_BUCKET:-${S3_LOGS_BUCKET}}" \
        "${SESSION_ARCHIVE_PREFIX}/${RUN_TAG}_${fname}" \
        "$f" || true
done

# Release the bot pool lock (S3 delete) — required so the same bot can
# be picked up by the next agent run.
if [ -n "${TELEGRAM_BOT_NAME:-}" ] && [ -n "${TELEGRAM_SENDER_LOGIN:-}" ]; then
    log "Releasing bot pool lock for ${TELEGRAM_SENDER_LOGIN}/${TELEGRAM_BOT_NAME}"
    blitzlog_s3_rm "${S3_LOGS_BUCKET}" \
        "bot-pool-locks/${TELEGRAM_SENDER_LOGIN}/${TELEGRAM_BOT_NAME}.json" \
        2>/dev/null || true
fi

# Clean up container
sudo docker rm -f blitzlog-agent || true

# Clean up instance-owned AWS state BEFORE terminating this instance.
# Once terminate-instances runs, the instance is shutting down and
# subsequent AWS calls from this script may fail. Both cleanups are
# idempotent (the SSM delete is per-issue; the schedule group delete
# cascade-deletes every refresh-event schedule for this instance).
log "Tearing down instance-owned AWS state"
if [ -n "${GITHUB_TOKEN_SSM_PARAM:-}" ]; then
    log "Deleting SSM token at ${GITHUB_TOKEN_SSM_PARAM}"
    aws ssm delete-parameter \
        --name "${GITHUB_TOKEN_SSM_PARAM}" \
        --region "$REGION" 2>/dev/null || true
fi
if [ -n "${GITHUB_TOKEN_SCHED_GROUP:-}" ]; then
    log "Deleting schedule group ${GITHUB_TOKEN_SCHED_GROUP} (cascade-deletes all chain events)"
    aws scheduler delete-schedule-group \
        --name "${GITHUB_TOKEN_SCHED_GROUP}" \
        --region "$REGION" 2>/dev/null || true
fi

# Terminate the EC2 instance
log "Terminating instance ${INSTANCE_ID}"
aws ec2 terminate-instances \
    --instance-id "$INSTANCE_ID" \
    --region "$REGION" || true

exit "$CONTAINER_EXIT"
