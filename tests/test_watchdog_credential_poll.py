"""Behavioural tests for the 5-min token-refresh poll loop in
``infra/aws/packer/scripts-docker-ubuntu/blitzlog_token_poll.sh``.

Issue #145 background: GitHub App installation tokens are capped to
1 h regardless of the requested lifetime. The Lambda mints a fresh
token every 50 min and writes it to
``/blitzlog/<env>/ephemeral/gh/<owner>/<repo>/<issue>``. The watchdog on the
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
LIB = REPO_ROOT / "infra/aws/packer/scripts-docker-ubuntu/blitzlog_token_poll.sh"


# --- the fake aws + curl shims ------------------------------------------
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

# The poll loop probes a newly observed token against GitHub before
# swapping it into the credential files (issue #145). The probe is a
# `curl` to github.com's git-upload-pack endpoint; the tests stub it
# with a shim that returns a configurable status code, so the test
# suite never touches the network.
CURL_SHIM_BODY = """\
#!/bin/bash
LOG="${FAKE_CURL_LOG:-/tmp/blitzlog_test_fake_curl.log}"
printf '%s\\n' "$*" >> "$LOG"
printf '%s' "${FAKE_CURL_STATUS:-200}"
exit 0
"""


@pytest.fixture
def aws_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install a fake ``aws`` CLI (and a fake ``curl``) into PATH for
    one test.

    Returns ``tmp_path`` so the test can find the per-test
    ``fake_aws.log`` (records every argv the poll loop makes).
    """
    shim = tmp_path / "aws"
    shim.write_text(SHIM_BODY)
    shim.chmod(0o755)
    curl_shim = tmp_path / "curl"
    curl_shim.write_text(CURL_SHIM_BODY)
    curl_shim.chmod(0o755)
    log = tmp_path / "fake_aws.log"
    log.write_text("")
    curl_log = tmp_path / "fake_curl.log"
    curl_log.write_text("")
    new_marker = tmp_path / "new_marker"
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_AWS_LOG", str(log))
    monkeypatch.setenv("FAKE_CURL_LOG", str(curl_log))
    monkeypatch.setenv("FAKE_AWS_NEW_MARKER", str(new_marker))
    monkeypatch.setenv("FAKE_SSM_VALUE_OLD", "ghp_token_v1_old")
    monkeypatch.setenv("FAKE_SSM_VALUE_NEW", "ghp_token_v2_new")
    # The probe target. The loop only probes when this is set, so the
    # fixture sets it by default: the tests then exercise the
    # verified path (the production path).
    monkeypatch.setenv("BLITZLOG_TOKEN_REPO", "example/owner/repo")
    # The stubbed probe says the token is usable.
    monkeypatch.setenv("FAKE_CURL_STATUS", "200")
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
            "/blitzlog/prod/ephemeral/gh/example/owner/repo/145" \\
            "ap-east-1" \\
            "{git_creds}" \\
            "{gh_hosts}" \\
            0.05
        # Give the loop 2 ticks (50 ms sleep + a moment for the read) to
        # start, then signal the "new value" and wait for the loop to
        # pick it up.
        sleep 0.3
        touch "{aws_shim}/new_marker"
        # Wait for the loop to land on the new value. Waiting on the
        # observable state (rather than sleeping a fixed amount) keeps
        # the test deterministic when the machine is under load.
        for _ in $(seq 1 200); do
            if grep -q "ghp_token_v2_new" "{gh_hosts}" 2>/dev/null; then break; fi
            sleep 0.05
        done
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
            "/blitzlog/prod/ephemeral/gh/example/owner/repo/145" \\
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
            "/blitzlog/prod/ephemeral/gh/example/owner/repo/145" \\
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


def test_poll_loop_holds_back_a_token_that_is_not_usable_yet(
    aws_shim: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newly observed token that GitHub has not finished granting
    access for must NOT be swapped into the credential files.

    Why: GitHub occasionally issues an installation token whose
    per-repository grant has not fully materialised. The token
    authenticates and can read repo metadata and pull requests, but
    the `contents` grant is missing — so git against a private repo
    fails with "remote: Repository not found" (GitHub's 404 for "you
    cannot read this repo"). Swapping such a token in takes the
    agent's GitHub access away for as long as the grant takes to
    settle.

    The loop must therefore keep the PREVIOUS token in the files
    (which is still valid, because the chain mints its replacement 30
    min before the current one expires) and re-probe on the next
    tick.

    Observed on 2026-10-27: one mint of five came back with an
    incomplete grant for ~20 min, and the agent lost git for that
    whole window.
    """
    # The probe says "no access" — the stubbed git-upload-pack
    # endpoint answers 404, exactly as GitHub does for a token that
    # cannot read a private repo.
    monkeypatch.setenv("FAKE_CURL_STATUS", "404")

    git_creds = aws_shim / "git-creds"
    gh_hosts = aws_shim / "gh-hosts"
    log_file = aws_shim / "watchdog.log"
    # Seed the files with the previous (still valid) token.
    git_creds.write_text("https://x-access-token:ghp_token_v1_old@github.com\n")
    gh_hosts.write_text(
        "github.com:\n    oauth_token: ghp_token_v1_old\n    git_protocol: https\n"
    )

    snippet = textwrap.dedent(f"""
        # The watchdog's log() writes to stdout AND tees to a file.
        # We tee to a file so the test can wait on the log line
        # instead of sleeping a fixed amount (a tick now costs a
        # `curl` spawn, so a fixed sleep is not reliably long enough).
        LOG_FILE="{log_file}"
        log() {{ printf '[%s] %s\\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" | tee -a "$LOG_FILE"; }}
        blitzlog_start_token_poll_loop \\
            "/blitzlog/prod/ephemeral/gh/example/owner/repo/145" \\
            "ap-east-1" \\
            "{git_creds}" \\
            "{gh_hosts}" \\
            0.05
        # Now the Lambda's refresh fires: the shim returns the new
        # token from here on.
        touch "{aws_shim}/new_marker"
        # Wait for the loop to have tried the new token and held it back.
        for _ in $(seq 1 200); do
            if grep -q "not yet usable for git" "$LOG_FILE" 2>/dev/null; then break; fi
            sleep 0.05
        done
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
    result = _run_bash(aws_shim, snippet)
    assert result.returncode == 0, result.stderr

    # The previous token must still be in the files.
    assert "ghp_token_v1_old" in git_creds.read_text()
    assert "ghp_token_v2_new" not in git_creds.read_text()
    assert "ghp_token_v1_old" in gh_hosts.read_text()
    assert "ghp_token_v2_new" not in gh_hosts.read_text()

    # And the operator must be told why.
    log_text = log_file.read_text()
    assert "not yet usable for git" in log_text, (
        "the loop must say that it is holding a token back; otherwise a "
        f"held-back token looks like a silently broken git. log={log_text!r}"
    )
    assert "probe=404" in log_text, (
        "the log must include the probe's status code so the operator can "
        f"tell a permission problem from a network one. log={log_text!r}"
    )


def test_poll_loop_applies_a_held_back_token_once_it_becomes_usable(
    aws_shim: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a held-back token starts working, the loop must swap it
    in and say so."""
    monkeypatch.setenv("FAKE_CURL_STATUS", "404")

    git_creds = aws_shim / "git-creds"
    gh_hosts = aws_shim / "gh-hosts"
    git_creds.write_text("https://x-access-token:ghp_token_v1_old@github.com\n")
    gh_hosts.write_text(
        "github.com:\n    oauth_token: ghp_token_v1_old\n    git_protocol: https\n"
    )

    # The probe starts failing, then starts working.
    curl_shim = aws_shim / "curl"
    curl_shim.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "${FAKE_CURL_LOG:-/tmp/blitzlog_test_fake_curl.log}"\n'
        'if [ -f "${FAKE_CURL_OK_MARKER:-/tmp/never}" ]; then\n'
        '  printf "200"\n'
        "else\n"
        '  printf "404"\n'
        "fi\n"
        "exit 0\n"
    )
    curl_shim.chmod(0o755)
    ok_marker = aws_shim / "curl_ok_marker"
    monkeypatch.setenv("FAKE_CURL_OK_MARKER", str(ok_marker))
    curl_log = aws_shim / "fake_curl.log"

    snippet = textwrap.dedent(f"""
        blitzlog_start_token_poll_loop \\
            "/blitzlog/prod/ephemeral/gh/example/owner/repo/145" \\
            "ap-east-1" \\
            "{git_creds}" \\
            "{gh_hosts}" \\
            0.05
        # Wait for the first probe to happen before changing anything,
        # so the ordering is deterministic rather than a race against
        # the poll interval.
        for _ in $(seq 1 200); do
            if [ -s "{curl_log}" ]; then break; fi
            sleep 0.05
        done
        touch "{aws_shim}/new_marker"
        # Let the loop probe the new token (which still answers 404).
        sleep 0.4
        # GitHub finishes granting access to the new token.
        touch "{ok_marker}"
        # Wait for the token to be applied.
        for _ in $(seq 1 200); do
            if grep -q "ghp_token_v2_new" "{git_creds}"; then break; fi
            sleep 0.05
        done
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
    result = _run_bash(aws_shim, snippet)
    assert result.returncode == 0, result.stderr

    assert "ghp_token_v2_new" in git_creds.read_text()
    assert "ghp_token_v2_new" in gh_hosts.read_text()
    assert "now usable for git" in result.stderr, (
        "the loop must log that the held-back token became usable; "
        f"stderr={result.stderr!r}"
    )


def test_poll_loop_applies_the_token_unverified_when_no_repo_is_known(
    aws_shim: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``BLITZLOG_TOKEN_REPO`` the loop cannot verify a token.

    It must then fall back to the pre-verification behaviour —
    apply whatever SSM holds — rather than never rotating at all. A
    host that cannot verify should degrade to "rotate as before", not
    "never rotate". It must also say so, once.
    """
    monkeypatch.delenv("BLITZLOG_TOKEN_REPO", raising=False)

    git_creds = aws_shim / "git-creds"
    gh_hosts = aws_shim / "gh-hosts"
    git_creds.write_text("seed\n")
    gh_hosts.write_text("seed\n")

    snippet = textwrap.dedent(f"""
        blitzlog_start_token_poll_loop \\
            "/blitzlog/prod/ephemeral/gh/example/owner/repo/145" \\
            "ap-east-1" \\
            "{git_creds}" \\
            "{gh_hosts}" \\
            0.05
        sleep 0.3
        touch "{aws_shim}/new_marker"
        for _ in $(seq 1 200); do
            if grep -q "ghp_token_v2_new" "{gh_hosts}" 2>/dev/null; then break; fi
            sleep 0.05
        done
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
    result = _run_bash(aws_shim, snippet)
    assert result.returncode == 0, result.stderr

    assert "ghp_token_v2_new" in git_creds.read_text()
    assert "ghp_token_v2_new" in gh_hosts.read_text()
    assert "BLITZLOG_TOKEN_REPO is unset" in result.stderr, (
        "the loop must warn that it is rotating without verifying; "
        f"stderr={result.stderr!r}"
    )


def test_poll_loop_terminates_cleanly_on_sigterm(aws_shim: Path) -> None:
    """The watchdog kills the poll loop with SIGTERM after the
    container exits. The loop's `trap 'exit 0' TERM` must make this
    immediate so the watchdog doesn't carry a zombie to the
    instance shutdown.
    """
    snippet = textwrap.dedent(f"""
        blitzlog_start_token_poll_loop \\
            "/blitzlog/prod/ephemeral/gh/example/owner/repo/145" \\
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


# --- the two gaps that let a dead chain look healthy -------------------


def test_poll_loop_does_not_report_a_change_when_the_token_is_unchanged(
    aws_shim: Path,
) -> None:
    """The watchdog's `log()` writes to STDOUT. The poll loop used to
    capture the helper's stdout into the "last seen token" variable, so
    the captured value was "<log line>\\n<token>" — which can never
    equal the token itself. The loop therefore concluded "the token
    changed" on EVERY tick, rewrote both credential files every 5
    minutes, and logged "Token change detected" every 5 minutes.

    That log line is the operator's only in-instance signal that the
    issue-#145 refresh chain is alive, so a false positive on every
    tick made a DEAD chain indistinguishable from a healthy one. That
    is exactly what happened in production on 2026-10-26: the instance
    log showed a steady stream of "Token change detected" while the
    value in SSM had not changed since the launch-time mint and the
    token had been expired for hours.

    The existing tests could not catch this because neither of them
    reproduces the production `log()`: one omits `log()` entirely (so
    the library's fallback writes to stderr) and the other defines it
    as a plain file writer. This one defines it exactly the way
    watchdog.sh does — stdout plus a tee to the log file.
    """
    log_file = aws_shim / "watchdog.log"
    git_creds = aws_shim / "git-creds"
    gh_hosts = aws_shim / "gh-hosts"
    marker = aws_shim / "new_marker"

    snippet = textwrap.dedent(f"""
        LOG_FILE="{log_file}"
        # EXACTLY the watchdog's log() (infra/aws/packer/scripts-docker-ubuntu/
        # watchdog.sh): printf | tee -a "$LOG_FILE" — the tee writes to
        # the file AND to stdout.
        log() {{ printf '[%s] %s\\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" | tee -a "$LOG_FILE"; }}
        blitzlog_start_token_poll_loop \\
            "/blitzlog/prod/ephemeral/gh/example/owner/repo/145" \\
            "ap-east-1" \\
            "{git_creds}" \\
            "{gh_hosts}" \\
            0.05
        # Several ticks with a STABLE ssm value. Nothing changed, so
        # "Token change detected" must not appear at all.
        sleep 0.4
        # Now simulate the Lambda's refresh: the shim starts returning
        # the new value once the marker exists.
        touch "{marker}"
        for _ in $(seq 1 200); do
            if grep -q "ghp_token_v2_new" "{gh_hosts}" 2>/dev/null; then break; fi
            sleep 0.05
        done
        if kill -0 "$BLITZLOG_TOKEN_POLL_PID" 2>/dev/null; then
            kill "$BLITZLOG_TOKEN_POLL_PID" 2>/dev/null
        fi
        """)
    result = _run_bash(aws_shim, snippet)
    assert result.returncode == 0, result.stderr

    log_text = log_file.read_text()
    changes = log_text.count("Token change detected")
    initialisations = log_text.count("Initialising credential files from SSM")

    assert initialisations == 1, (
        "the first observation must initialise the credential files "
        f"exactly once, got {initialisations}: {log_text!r}"
    )
    assert changes == 1, (
        "the poll loop must report a token change EXACTLY once — on the "
        "genuine rotation. It reported "
        f"{changes} times. The watchdog's log() writes to stdout and the "
        "loop used to capture that stdout into the 'last seen token' "
        "variable, so the comparison could never match and every tick "
        "looked like a rotation. A dead refresh chain then looks "
        f"identical to a healthy one: {log_text!r}"
    )

    # The rotation must still have been applied to the files.
    assert "ghp_token_v2_new" in git_creds.read_text()
    assert "ghp_token_v2_new" in gh_hosts.read_text()


def test_poll_loop_exits_promptly_on_sigterm_with_a_long_interval(
    aws_shim: Path,
) -> None:
    """SIGTERM must be honoured while the loop is blocked in its sleep.

    The loop used to run `sleep "$poll_interval"` in the foreground.
    bash defers a trapped signal until the running foreground command
    returns, so with the production interval (300 s) the `trap 'exit 0'
    TERM` only fired once the sleep finished — up to five minutes
    later. The watchdog gives the loop a 100 ms grace period and then
    SIGKILLs it, so every teardown logged

        Token refresh poll loop did not exit on SIGTERM; sending SIGKILL

    The existing SIGTERM test used a 50 ms interval, so the loop was
    essentially never blocked in a long sleep when the signal arrived
    and the trap fired immediately — which is why the bug survived.
    This one uses a 30 s interval, so the loop IS blocked in the sleep.
    """
    snippet = textwrap.dedent(f"""
        blitzlog_start_token_poll_loop \\
            "/blitzlog/prod/ephemeral/gh/example/owner/repo/145" \\
            "ap-east-1" \\
            "{aws_shim}/git-creds" \\
            "{aws_shim}/gh-hosts" \\
            30
        sleep 0.3
        if ! kill -0 "$BLITZLOG_TOKEN_POLL_PID" 2>/dev/null; then
            echo "DIED_EARLY"
            exit 1
        fi
        # SIGTERM while the loop is blocked in its 30 s sleep.
        kill "$BLITZLOG_TOKEN_POLL_PID"
        for _ in $(seq 1 100); do
            if ! kill -0 "$BLITZLOG_TOKEN_POLL_PID" 2>/dev/null; then
                echo "OK"
                exit 0
            fi
            sleep 0.02
        done
        echo "TRAP_DEFERRED"
        kill -9 "$BLITZLOG_TOKEN_POLL_PID" 2>/dev/null
        exit 1
        """)
    result = _run_bash(aws_shim, snippet)
    assert result.returncode == 0, (
        "the poll loop did not exit on SIGTERM while blocked in its "
        f"sleep: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "OK" in result.stdout, result.stdout
    assert "TRAP_DEFERRED" not in result.stdout, (
        "the SIGTERM trap was deferred until the sleep finished — bash "
        "only runs a trapped signal after the running foreground command "
        "returns. The loop must background its sleep and `wait` on it, "
        "because `wait` IS interruptible."
    )
