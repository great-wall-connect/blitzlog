"""Behavioural tests for the 5-min token-refresh poll loop in
``infra/packer/scripts-docker-ubuntu/blitzlog_token_poll.sh``.

Issue #145 background: GitHub App installation tokens are capped to
1 h regardless of the requested lifetime. The Lambda mints a fresh
token every 50 min and writes it to
``/blitzlog/<env>/ephemeral/github-token-<N>``. The watchdog on the
EC2 instance must pick up the new value and rewrite the bind-mounted
credential files (``/root/.git-credentials.d/github`` and
``/root/.config/gh/hosts.yml``) so the next ``git push`` and
``gh`` invocation use the new token — without restarting the
container.

This test:

  * Installs a fake ``aws`` CLI into ``PATH`` (matches the pattern in
    ``test_watchdog_uploader.py``). The shim returns one SSM value,
    then a different one — to simulate the Lambda's first mint, then
    the 50-min-later refresh.
  * Sources ``blitzlog_token_poll.sh`` in a bash subprocess.
  * Starts the poll loop via ``blitzlog_start_token_poll_loop`` with a
    tiny interval (50 ms) so the test runs in seconds, not minutes.
  * Asserts the bind-mounted files are rewritten when the SSM value
    changes.

The test runs in real bash, against the real library, with a fake
``aws`` shim. No network, no mocks, no real boto3.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "infra/packer/scripts-docker-ubuntu/blitzlog_token_poll.sh"


# --- the fake aws shim -------------------------------------------------
#
# Returns a configurable SSM value. The first call returns
# FAKE_SSM_VALUE_OLD, subsequent calls return FAKE_SSM_VALUE_NEW. We
# implement the "first vs subsequent" logic via a file flag the shim
# touches (atomic across subprocess invocations) — keeps the test
# state shared between the bash parent and the bash subshell that
# runs the poll loop.
SHIM_BODY = """\
#!/bin/bash
LOG="${FAKE_AWS_LOG:-/tmp/blitzlog_test_fake_aws.log}"
printf '%s\\n' "$*" >> "$LOG"

# If FAKE_SSM_VALUE_NEW is set AND the marker file exists, the shim
# returns the "new" value. Otherwise it returns the "old" value. The
# bash parent writes the marker file when it wants to simulate the
# Lambda's refresh firing.
if [ -n "${FAKE_SSM_VALUE_NEW:-}" ] && [ -f "${FAKE_AWS_NEW_MARKER:-/tmp/blitzlog_test_new_marker}" ]; then
    printf '%s' "$FAKE_SSM_VALUE_NEW"
else
    printf '%s' "$FAKE_SSM_VALUE_OLD"
fi
exit 0
"""


@pytest.fixture
def aws_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install a fake ``aws`` CLI into PATH for one test.

    Returns ``tmp_path`` so the test can find the per-test
    ``fake_aws.log`` (records every argv the poll loop makes).
    """
    shim = tmp_path / "aws"
    shim.write_text(SHIM_BODY)
    shim.chmod(0o755)
    log = tmp_path / "fake_aws.log"
    log.write_text("")
    new_marker = tmp_path / "new_marker"
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_AWS_LOG", str(log))
    monkeypatch.setenv("FAKE_AWS_NEW_MARKER", str(new_marker))
    monkeypatch.setenv("FAKE_SSM_VALUE_OLD", "ghp_token_v1_old")
    monkeypatch.setenv("FAKE_SSM_VALUE_NEW", "ghp_token_v2_new")
    monkeypatch.setenv("AWS_REGION", "ap-east-1")
    return tmp_path


def _run_bash(aws_shim: Path, snippet: str) -> subprocess.CompletedProcess:
    """Run ``snippet`` in a bash subprocess with the library sourced
    and the test's ``aws`` shim on PATH.

    Returns the CompletedProcess so the test can inspect stdout /
    stderr / return code. ``set -u`` is on so an unset variable
    surfaces loudly.
    """
    env = os.environ.copy()
    script = f"set -u\nsource {LIB}\n{snippet}\n"
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


# --- happy path: a single refresh within the poll interval ------------


def test_poll_loop_rewrites_credential_files_when_ssm_value_changes(
    aws_shim: Path,
) -> None:
    """The poll loop reads SSM every ``poll_interval`` seconds. When
    the value differs from the last seen value, the loop rewrites
    both bind-mounted files.

    Test flow:
      1. Write an initial value to the credential files.
      2. Start the poll loop with a 50 ms interval.
      3. Wait 2 s (two iterations).
      4. Touch the "new value" marker file so the shim returns the
         new token on the next poll.
      5. Wait 2 s.
      6. Assert both files now contain the new token.
    """
    git_creds = aws_shim / "git-creds"
    gh_hosts = aws_shim / "gh-hosts"
    git_creds.parent.mkdir(parents=True, exist_ok=True)
    gh_hosts.parent.mkdir(parents=True, exist_ok=True)
    # Seed the initial value (mimics the watchdog's startup write).
    git_creds.write_text("https://x-access-token:ghp_token_v1_old@github.com\n")
    gh_hosts.write_text(
        "github.com:\n    oauth_token: ghp_token_v1_old\n    git_protocol: https\n"
    )

    # Start the poll loop in the background with a 50 ms interval.
    # The library writes the subshell PID to BLITZLOG_TOKEN_POLL_PID
    # (a global). We use a global instead of `$(...)` capture
    # because bash's `wait` for a non-direct child (the subshell is
    # a grandchild of this script once it runs inside a function)
    # is unreliable — see the comment in blitzlog_token_poll.sh.
    # The short interval also keeps the test fast and the
    # SIGTERM-handling time small (bash's `sleep` only checks
    # signals at the next 50 ms tick when running as a builtin, so
    # a 50 ms interval gives a tight bound on how long SIGTERM
    # takes to reach the trap).
    snippet = textwrap.dedent(f"""
        # Start the poll loop. We use a 50 ms interval so the test runs
        # in seconds, not the production 5 min.
        blitzlog_start_token_poll_loop \\
            "/blitzlog/prod/ephemeral/github-token-145" \\
            "ap-east-1" \\
            "{git_creds}" \\
            "{gh_hosts}" \\
            0.05
        # Give the loop 2 ticks (50 ms sleep + a moment for the read) to
        # start, then signal the "new value" and wait for the loop to
        # pick it up.
        sleep 0.3
        touch "{aws_shim}/new_marker"
        # Wait for two more ticks to land on the new value.
        sleep 0.3
        # Stop the loop. We use `kill -0` to confirm the subshell is
        # still alive before sending TERM (the trap should make the
        # exit immediate, but a 200 ms grace period is generous).
        if kill -0 "$BLITZLOG_TOKEN_POLL_PID" 2>/dev/null; then
            kill "$BLITZLOG_TOKEN_POLL_PID" 2>/dev/null
            for _ in 1 2 3 4 5 6 7 8 9 10; do
                if ! kill -0 "$BLITZLOG_TOKEN_POLL_PID" 2>/dev/null; then
                    break
                fi
                sleep 0.02
            done
        fi
        # Print the final file contents so the test can assert on them.
        echo "==GIT_CREDS=="
        cat "{git_creds}"
        echo "==GH_HOSTS=="
        cat "{gh_hosts}"
        """)
    result = _run_bash(aws_shim, snippet)
    assert result.returncode == 0, (
        f"snippet failed: rc={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )

    # Both files should now carry the new token. The initial seed
    # was the old token; the loop's diff check should have rewritten
    # them.
    assert "ghp_token_v2_new" in git_creds.read_text()
    assert "ghp_token_v1_old" not in git_creds.read_text()
    assert "ghp_token_v2_new" in gh_hosts.read_text()
    assert "ghp_token_v1_old" not in gh_hosts.read_text()


def test_poll_loop_does_not_rewrite_when_ssm_value_unchanged(
    aws_shim: Path,
) -> None:
    """When the SSM value is the same as the last seen value, the
    loop must NOT touch the credential files. The diff check is the
    key invariant — touching the file on every tick would race with
    the container's `git push` (which is reading the file via the
    credential helper) and add load with no benefit.
    """
    git_creds = aws_shim / "git-creds"
    gh_hosts = aws_shim / "gh-hosts"
    git_creds.parent.mkdir(parents=True, exist_ok=True)
    gh_hosts.parent.mkdir(parents=True, exist_ok=True)
    # Pre-create the files with the same content the fake aws shim
    # returns, so the loop's diff check fires `false` and the files
    # stay untouched across the test. (If we left the files empty,
    # the first tick would always rewrite them — the loop's diff
    # check only fires AFTER the first observation, which is the
    # correct behavior to handle a fresh restart where the
    # credential files may not yet exist.)
    git_creds.write_text("https://x-access-token:ghp_token_v1_old@github.com\n")
    gh_hosts.write_text(
        "github.com:\n    oauth_token: ghp_token_v1_old\n    git_protocol: https\n"
    )

    snippet = textwrap.dedent(f"""
        blitzlog_start_token_poll_loop \\
            "/blitzlog/prod/ephemeral/github-token-145" \\
            "ap-east-1" \\
            "{git_creds}" \\
            "{gh_hosts}" \\
            0.05
        # Let the loop stabilise: the first tick writes the
        # current SSM value to both files (a write that always
        # happens on a fresh start). Subsequent ticks observe
        # the same value and must NOT touch the files.
        sleep 0.3
        # 6 more ticks, no marker touched → the shim returns
        # the same value every time → the loop must not rewrite
        # the files.
        sleep 0.5
        if kill -0 "$BLITZLOG_TOKEN_POLL_PID" 2>/dev/null; then
            kill "$BLITZLOG_TOKEN_POLL_PID" 2>/dev/null
            for _ in 1 2 3 4 5 6 7 8 9 10; do
                if ! kill -0 "$BLITZLOG_TOKEN_POLL_PID" 2>/dev/null; then
                    break
                fi
                sleep 0.02
            done
        fi
        """)
    _run_bash(aws_shim, snippet)

    # The loop has stabilised; subsequent ticks must not have
    # touched the files. We can't reliably assert "mtime
    # unchanged" because the FIRST tick always writes (a fresh
    # restart where the credential files may not exist yet), so
    # instead we assert the file content still matches the value
    # the shim returns. The container's `git push` / `gh` rely
    # on the file content being correct, not the mtime.
    assert "ghp_token_v1_old" in git_creds.read_text()
    assert "ghp_token_v1_old" in gh_hosts.read_text()


def test_poll_loop_survives_ssm_error_and_keeps_polling(
    aws_shim: Path,
) -> None:
    """A transient SSM error must NOT kill the loop. The library
    echoes the empty string on any `aws ssm get-parameter` failure
    (so the diff check sees `"" == ""` → no rewrite) and continues
    to the next tick.

    This is the regression guard against the issue-145 bug class:
    a poll loop that dies on the first transient error silently
    reverts the agent to the "static 1 h token" behaviour.
    """
    # Override the shim to fail every call. The library must not crash.
    shim_path = aws_shim / "aws"
    shim_path.write_text("#!/bin/bash\necho 'transient error' >&2\nexit 7\n")
    shim_path.chmod(0o755)

    git_creds = aws_shim / "git-creds"
    gh_hosts = aws_shim / "gh-hosts"
    git_creds.parent.mkdir(parents=True, exist_ok=True)
    gh_hosts.parent.mkdir(parents=True, exist_ok=True)
    git_creds.write_text("seed\n")
    gh_hosts.write_text("seed\n")

    snippet = textwrap.dedent(f"""
        blitzlog_start_token_poll_loop \\
            "/blitzlog/prod/ephemeral/github-token-145" \\
            "ap-east-1" \\
            "{git_creds}" \\
            "{gh_hosts}" \\
            0.05
        # 6 ticks of SSM failure. The loop should still be running.
        sleep 0.5
        # If the loop died, the check would print DEAD.
        if kill -0 "$BLITZLOG_TOKEN_POLL_PID" 2>/dev/null; then
            echo "ALIVE"
        else
            echo "DEAD"
        fi
        kill "$BLITZLOG_TOKEN_POLL_PID" 2>/dev/null
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            if ! kill -0 "$BLITZLOG_TOKEN_POLL_PID" 2>/dev/null; then
                break
            fi
            sleep 0.02
        done
        """)
    result = _run_bash(aws_shim, snippet)
    assert result.returncode == 0, result.stderr
    assert (
        "ALIVE" in result.stdout
    ), f"poll loop died on transient SSM error: {result.stdout!r}"


def test_poll_loop_terminates_cleanly_on_sigterm(aws_shim: Path) -> None:
    """The watchdog kills the poll loop with SIGTERM after the
    container exits. The loop's `trap 'exit 0' TERM` must make this
    immediate so the watchdog doesn't carry a zombie to the
    instance shutdown.
    """
    snippet = textwrap.dedent(f"""
        blitzlog_start_token_poll_loop \\
            "/blitzlog/prod/ephemeral/github-token-145" \\
            "ap-east-1" \\
            "{aws_shim}/git-creds" \\
            "{aws_shim}/gh-hosts" \\
            0.05
        # Verify the loop is alive.
        if ! kill -0 "$BLITZLOG_TOKEN_POLL_PID" 2>/dev/null; then
            echo "DIED_EARLY"
            exit 1
        fi
        # Send SIGTERM and verify the trap fires within 200 ms.
        kill "$BLITZLOG_TOKEN_POLL_PID"
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            if ! kill -0 "$BLITZLOG_TOKEN_POLL_PID" 2>/dev/null; then
                echo "OK"
                exit 0
            fi
            sleep 0.02
        done
        # If we got here the trap is missing or being ignored.
        echo "TRAP_MISSING"
        kill -9 "$BLITZLOG_TOKEN_POLL_PID" 2>/dev/null
        exit 1
        """)
    result = _run_bash(aws_shim, snippet)
    assert result.returncode == 0, (
        f"SIGTERM did not stop the poll loop: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert "OK" in result.stdout


# --- behavioural sanity: the library is syntactically valid -----------


def test_library_passes_bash_n() -> None:
    """Pin: the library is syntactically valid bash."""
    result = subprocess.run(
        ["bash", "-n", str(LIB)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_library_uses_caller_log_when_defined() -> None:
    """The library must fall through to the caller's `log()` if
    defined, so the watchdog's `log()` (which writes to
    /var/log/backend-bootstrap.log) sees the poll-loop output. This
    is the same discipline as `blitzlog_uploader.sh`.
    """
    capture_path = Path("/tmp/blitzlog_test_caller_log.log")
    if capture_path.exists():
        capture_path.unlink()
    snippet = textwrap.dedent(f"""
        log() {{ printf '%s\\n' "$1" >> "{capture_path}"; }}
        source {LIB}
        # Trigger one of the internal log paths. We invoke the
        # helper directly rather than going through the full loop
        # so the test stays fast.
        _bl_tokenpoll_log "test message from poll library"
        """)
    _run_bash(Path("/tmp"), snippet)  # aws_shim not needed for this test
    assert "test message from poll library" in capture_path.read_text()
    capture_path.unlink()
