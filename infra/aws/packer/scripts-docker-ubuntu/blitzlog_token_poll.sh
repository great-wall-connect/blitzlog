#!/bin/bash
# infra/aws/packer/scripts-docker-ubuntu/blitzlog_token_poll.sh
#
# Issue #145 — host-side token-refresh poll loop.
#
# GitHub App installation tokens are capped to 1 h regardless of the
# requested `expires_at`. The Lambda schedules a chain of one-time
# EventBridge Scheduler events that re-mint the token every 50 min and
# overwrite the SecureString at /blitzlog/<env>/ephemeral/github-token-<N>.
# The chain keeps the token fresh, but the agent in the container is
# still using the *old* value in /root/.git-credentials.d/github and
# /root/.config/gh/hosts.yml (both bind-mounted from the host).
#
# This library is sourced by watchdog.sh. The poll loop:
#   * re-reads the SSM parameter every 5 min
#   * if the value differs from the last seen value, rewrites the
#     bind-mounted credential files in place
#   * logs the observed token's iat/exp, and says so LOUDLY when that
#     token is expired or about to expire — the symptom of a dead
#     issue-#145 refresh chain (issue #145's chain can die silently:
#     if the Lambda stops scheduling refresh events, the value in SSM
#     simply stops changing, and the only symptom was this loop's
#     log line. That made a dead chain indistinguishable from a
#     healthy one in the instance log.)
#   * exits when the watchdog SIGTERMs it (the watchdog's container
#     loop is the only consumer; once the container exits the watchdog
#     kills the poll subshell and proceeds to upload logs + terminate
#     the instance)
#
# Exit semantics: the loop never exits on its own. SIGTERM is the only
# exit path. `trap 'exit 0' TERM` is the only signal handler.
#
# Why not a separate process?  systemd is not on the path of the host
# at this stage (the watchdog is the lifecycle manager; systemd is
# reserved for the container), and a 5-min sleep + one SSM read is
# cheap enough to keep in-process.

# Public API — called by watchdog.sh:
#   blitzlog_start_token_poll_loop <param_name> <region> <git_creds_path> <gh_hosts_yml_path> [poll_interval_seconds]
#
# Sourcing: `source /usr/local/bin/blitzlog_token_poll.sh` from
# watchdog.sh. All other entry points are private.

# --- internals ---------------------------------------------------------

# _bl_tokenpoll_log <msg>
#   Forward to the caller's `log()` (defined in watchdog.sh). Falls
#   back to stderr if `log` is not defined (e.g. when this library is
#   exercised from a unit test).
#
#   NOTE: this writes to STDOUT (via the caller's `log()`, which tees
#   to the watchdog's log file and to stdout). That is deliberate — the
#   watchdog's log file is where an operator looks. It is also why no
#   caller may wrap a function that logs in a `$(...)` command
#   substitution: the captured stdout would swallow the log line into
#   the captured value. See _bl_tokenpoll_rewrite_if_changed below.
_bl_tokenpoll_log() {
    if declare -F log >/dev/null 2>&1; then
        log "$1"
    else
        printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >&2
    fi
}

# _bl_tokenpoll_token_times <token>
#   Echo "<iat> <exp>" (epoch seconds) for a GitHub App installation
#   token, or "unknown unknown" if the token is not a decodable JWT.
#   Purely for observability — the token's `exp` is what says whether
#   the chain actually refreshed it.
_bl_tokenpoll_token_times() {
    local token="${1:-}"
    if [ -z "$token" ]; then
        echo "unknown unknown"
        return 0
    fi
    printf '%s' "$token" | cut -d. -f2 2>/dev/null | tr '_-' '/+' \
        | base64 -d 2>/dev/null \
        | python3 -c 'import sys,json
try:
    d = json.load(sys.stdin)
    print(int(d.get("iat", 0)), int(d.get("exp", 0)))
except Exception:
    print("unknown unknown")' 2>/dev/null || echo "unknown unknown"
}

# _bl_tokenpoll_read_ssm <param_name> <region>
#   Read a SecureString from SSM. Echoes the value on stdout, empty
#   string on any error (so the loop doesn't trip `set -e` on a
#   transient SSM hiccup). The Lambda writes the value
#   `with type=SecureString` so `--with-decryption` is required.
_bl_tokenpoll_read_ssm() {
    local param_name="$1" region="$2"
    aws ssm get-parameter \
        --name "$param_name" \
        --with-decryption \
        --query Parameter.Value \
        --output text \
        --region "$region" 2>/dev/null || echo ""
}

# _bl_tokenpoll_write_git_creds <token> <git_creds_path>
#   Overwrite the git-credential-store file with the new token. The
#   git credential helper re-reads the file on every `git push`, so
#   this single in-place rewrite is enough — no in-container code
#   changes, no inotify, no signals.
_bl_tokenpoll_write_git_creds() {
    local token="$1" path="$2"
    mkdir -p "$(dirname "$path")"
    printf 'https://x-access-token:%s@github.com\n' "$token" > "$path"
    chmod 600 "$path"
}

# _bl_tokenpoll_write_gh_hosts_yml <token> <gh_hosts_yml_path>
#   Overwrite the gh CLI hosts file with the new token. The format
#   mirrors what `gh auth login --with-token --hostname github.com`
#   produces. `gh` re-reads on every invocation, so the next `gh`
#   command picks up the new token without a process restart.
#
#   We deliberately write only `oauth_token` (not `user` /
#   `git_protocol` / etc.); `gh` re-fetches the user lazily on the
#   first API call, and the only field the agent ever consults is
#   the token. If a future `gh` upgrade starts demanding a static
#   `user` field, this is the line to extend.
_bl_tokenpoll_write_gh_hosts_yml() {
    local token="$1" path="$2"
    mkdir -p "$(dirname "$path")"
    cat > "$path" <<EOF
github.com:
    oauth_token: ${token}
    git_protocol: https
EOF
}

# _bl_tokenpoll_rewrite_if_changed <current> <last> <git_creds_path> <gh_hosts_yml_path>
#   Side-effect: when current != last (and current is non-empty),
#   rewrite both files. The new value is returned in the GLOBAL
#   BLITZLOG_TOKEN_LAST (not on stdout).
#
#   WHY a global instead of echoing the value on stdout: the previous
#   shape was
#
#       last="$(_bl_tokenpoll_rewrite_if_changed "$current" "$last" ...)"
#
#   and the caller's `log()` writes to STDOUT, so the command
#   substitution captured the log line TOO. `last` became
#
#       "[2026-10-26 12:03:04] Token change detected; ...\n<token>"
#
#   which can never equal `current` — so the loop concluded "the token
#   changed" on EVERY tick, rewrote both credential files every 5
#   minutes, and logged "Token change detected" every 5 minutes. The
#   log was pure noise: it looked like a stream of rotations while the
#   token in SSM never actually changed. The comment in the watchdog
#   and in docs/WATCHDOG_TOKEN_REFRESH.md both promise that this line
#   fires "exactly once (on the genuine rotation), not on every 5-min
#   tick" — it did not, and no test caught it because the unit tests
#   either omit `log()` (so the fallback writes to stderr) or define it
#   as a file writer, while the real watchdog's `log()` writes to
#   stdout.
#
#   A global has no such coupling: the log line goes to stdout, the
#   remembered value goes to a variable.
_bl_tokenpoll_rewrite_if_changed() {
    local current="$1" last="$2" git_creds="$3" gh_hosts="$4"
    if [ -n "$current" ] && [ "$current" != "$last" ]; then
        if [ -z "$last" ]; then
            # First observation of this run: the credential files may
            # not exist yet (fresh instance), so this is an
            # initialisation, not a rotation.
            _bl_tokenpoll_log "Initialising credential files from SSM"
        else
            _bl_tokenpoll_log "Token change detected; rewriting credential files"
        fi
        _bl_tokenpoll_write_git_creds "$current" "$git_creds"
        _bl_tokenpoll_write_gh_hosts_yml "$current" "$gh_hosts"
        BLITZLOG_TOKEN_LAST="$current"
    else
        # Either no change, OR the SSM read returned an empty string
        # (transient SSM hiccup, IAM role churn, etc.). In the empty-read
        # case, keep the last known good value so the caller's
        # `last` doesn't reset to "". Without this, a single empty read
        # would log "Token change detected; rewriting credential files"
        # on the *next* non-empty read, even though the value is the
        # same — a false positive visible in the bootstrap log on every
        # empty read. The bind mount is untouched in both branches.
        BLITZLOG_TOKEN_LAST="$last"
    fi
}

# _bl_tokenpoll_log_token_state <token>
#   Log the observed token's `iat` / `exp` and — when the token is
#   already expired, or close to it — say so loudly.
#
#   The chain that refreshes the token (issue #145) can die silently:
#   if the Lambda stops scheduling its refresh events, the value in
#   SSM simply stops changing, and the only symptom was the poll
#   loop's (always-firing) "Token change detected" line. That made a
#   dead chain indistinguishable from a healthy one in the instance
#   log. Logging the token's own expiry turns "the chain is dead" into
#   something an operator can see in the instance log.
_bl_tokenpoll_log_token_state() {
    local token="${1:-}"
    local now="" exp="" iat=""
    now=$(date +%s)
    read -r iat exp < <(_bl_tokenpoll_token_times "$token") || true
    if [ -z "$exp" ] || [ "$exp" = "unknown" ] || [ "$exp" = "0" ]; then
        _bl_tokenpoll_log "Observed SSM token: expiry unknown (not a decodable JWT)"
        return 0
    fi
    if [ "$exp" -le "$now" ]; then
        _bl_tokenpoll_log "ACTIONABLE: the token in SSM EXPIRED $(($now - exp))s ago (exp=$(date -u -d "@$exp" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo "$exp")). The issue-#145 token-refresh chain is not refreshing it — the agent will lose GitHub access."
    elif [ "$exp" -le "$((now + 900))" ]; then
        _bl_tokenpoll_log "ACTIONABLE: the token in SSM expires in $(($exp - now))s (exp=$(date -u -d "@$exp" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo "$exp")). The issue-#145 token-refresh chain is not refreshing it."
    else
        _bl_tokenpoll_log "Observed SSM token: iat=$(date -u -d "@$iat" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo "$iat") exp=$(date -u -d "@$exp" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo "$exp") (expires in $(($exp - now))s)"
    fi
}

# --- public API --------------------------------------------------------

# blitzlog_start_token_poll_loop <param_name> <region> <git_creds_path> <gh_hosts_yml_path> [poll_interval_seconds]
#   Spawn the background poll loop. Returns immediately; the loop
#   runs in a subshell with `trap 'exit 0' TERM` so the watchdog can
#   kill it cleanly. The subshell's PID is written to the global
#   `BLITZLOG_TOKEN_POLL_PID` so the caller can `kill` and `wait` it
#   from its own process tree.
#
#   **Why a global instead of `printf`?**
#   `$!` is the PID of the most-recent backgrounded process in the
#   *current* shell. The caller wants to `wait` for the subshell
#   from the outer script, but `wait` from a shell for a non-direct
#   child is unreliable when the subshell was spawned from a
#   function called via `$(...)` — bash's `wait` only reaps direct
#   children, so the backgrounded subshell becomes a "grandchild"
#   that the outer shell cannot reap. Storing the PID in a global
#   variable the outer shell can see, and `kill`ing that PID
#   directly, sidesteps the `wait` problem: the subshell's
#   `trap 'exit 0' TERM` fires, the subshell exits, and the outer
#   shell's `wait` is no longer needed. (If the caller does want to
#   reap the subshell, it can do so via `kill -0 $PID` until
#   non-zero, then `wait` for ANY background process — but the
#   watchdog's typical flow is "kill and proceed".)
blitzlog_start_token_poll_loop() {
    local param_name="$1" region="$2" git_creds="$3" gh_hosts="$4"
    local poll_interval="${5:-300}"

    # The global must be declared with `declare -g` so the assignment
    # propagates from the function's local scope to the caller's.
    declare -g BLITZLOG_TOKEN_POLL_PID

    (
        # SIGTERM must be honoured PROMPTLY. The previous shape was
        # `sleep "$poll_interval"` in the foreground: bash defers a
        # trapped signal until the running foreground command returns,
        # so the trap only fired once the 300 s sleep finished. The
        # watchdog gives the loop a 100 ms grace period and then
        # SIGKILLs it, so every teardown logged
        #
        #   Token refresh poll loop did not exit on SIGTERM; sending SIGKILL
        #
        # `sleep` in the background + `wait` fixes it: `wait` IS
        # interruptible by a trapped signal, so the trap runs
        # immediately. The trap also kills the sleep child, so the
        # loop leaves no orphaned `sleep` behind.
        local sleep_pid=""
        trap 'if [ -n "$sleep_pid" ]; then kill "$sleep_pid" 2>/dev/null || true; fi; exit 0' TERM

        local last=""
        while true; do
            sleep "$poll_interval" &
            sleep_pid=$!
            wait "$sleep_pid" 2>/dev/null || true
            sleep_pid=""

            local current
            current="$(_bl_tokenpoll_read_ssm "$param_name" "$region")"
            if [ -n "$current" ]; then
                _bl_tokenpoll_log_token_state "$current"
            fi
            _bl_tokenpoll_rewrite_if_changed "$current" "$last" "$git_creds" "$gh_hosts"
            last="$BLITZLOG_TOKEN_LAST"
        done
    ) &
    BLITZLOG_TOKEN_POLL_PID=$!
}
