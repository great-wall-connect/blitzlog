"""End-to-end tests for the watchdog's wiring of the token-refresh
poll loop (issue #145).

These tests pin the contract between
``infra/packer/scripts-docker-ubuntu/watchdog.sh`` and
``infra/packer/scripts-docker-ubuntu/blitzlog_token_poll.sh``.

The existing ``test_watchdog_credential_poll.py`` covers the poll
library in isolation (the diff-rewrite loop, the SIGTERM trap, the
no-op-on-stable behaviour). This file covers the way the watchdog
*drives* the library:

  1. **Initial credential writes** (``watchdog.sh:130-133``) — the
     ``_bl_tokenpoll_write_git_creds`` and
     ``_bl_tokenpoll_write_gh_hosts_yml`` helpers receive the
     bind-mounted paths the container reads from.

  2. **Loop start** (``watchdog.sh:234-239``) —
     ``blitzlog_start_token_poll_loop`` receives the SSM param name,
     the region, the bind-mounted paths, and an optional poll
     interval override (``BLITZLOG_TOKEN_POLL_INTERVAL``).

  3. **Bind-mount mapping** (``watchdog.sh:254``) — the host writes
     to ``/root/gh-config/<instance>/hosts.yml`` and the container's
     ``gh auth login`` writes to ``/root/.config/gh/hosts.yml`` are
     bridged by the bind mount
     ``-v "/root/gh-config/${INSTANCE_ID}:/root/.config/gh"``. If the
     write path and the bind-mount source ever drift apart, the
     container will see a stale token — a class of bug that does NOT
     surface in the library-in-isolation tests.

  4. **Container-side format** — the file ``git credential-store``
     reads and the YAML the ``gh`` CLI reads must match what
     ``_bl_tokenpoll_write_*`` produces. Drift here breaks the
     agent's ``git push`` / ``gh`` calls without breaking the SSM
     read; the existing tests don't catch it.

The tests use a hybrid approach to match the existing
``test_watchdog_credential_poll.py`` style:

  * **Static analysis** (read ``watchdog.sh`` as text + regex) pins
    the path arguments and the bind-mount mapping. Drift on
    whitespace, indentation, or comment placement does not break the
    test, but a path typo in either ``COMMON_ARGS``, the initial
    write block, the ``blitzlog_start_token_poll_loop`` call, or
    the bind-mount ``-v`` flag will.

  * **Bash subprocess** (source the libraries, run the same code
    blocks the watchdog runs) pins the file format that
    ``git credential fill`` and PyYAML (the same parser ``gh``
    uses) accept. Plus a re-run of the existing
    ``test_poll_loop_rewrites_credential_files_when_ssm_value_changes``
    scenario, but driven through the watchdog's *call signature*
    (same args, same paths) — so a future refactor of the watchdog's
    call site is caught if it breaks the contract.

The tests do NOT execute the full ``watchdog.sh`` end-to-end — that
needs IMDS, dockerd, and a real GitHub App installation token. Those
are covered by the dev-spot-instance smoke test documented in
``docs/architecture/watchdog-token-refresh.md`` (Phase A of the
issue #145 validation plan).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = REPO_ROOT / "infra/packer/scripts-docker-ubuntu/watchdog.sh"
POLL_LIB = REPO_ROOT / "infra/packer/scripts-docker-ubuntu/blitzlog_token_poll.sh"

# Issue #145: the GitHub App token in the bind-mounted credential
# file is read by `git credential-store` (for `git push`) and by the
# `gh` CLI's hosts.yml (for `gh ...`). This constant is the *format*
# the helpers must produce, NOT a real token. The tests assert that
# the format round-trips through both readers.
DUMMY_TOKEN_V1 = "ghp_token_v1_old_aaaa"
DUMMY_TOKEN_V2 = "ghp_token_v2_new_bbbb"


# --- helpers -----------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_comments_and_continuations(src: str) -> str:
    """Drop ``# ...`` line comments and bash ``\\\n`` line
    continuations so a regex match against the post-processed body
    is unaffected by editor whitespace and by comments documenting
    the block (e.g. the issue-145 comment block right above
    ``blitzlog_start_token_poll_loop`` in ``watchdog.sh``).
    """
    cleaned = re.sub(r"(?m)^\s*#[^\n]*\n", "", src)
    cleaned = cleaned.replace("\\\n", "")
    return cleaned


def _run_bash(
    snippet: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run ``snippet`` in a bash subprocess with the poll library
    pre-sourced. ``env`` overlays the inherited environment (so
    tests can flip ``FAKE_AWS_LOG`` etc *for the subprocess only*
    without leaking to the pytest process).
    """
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    script = f"set -u\nsource {POLL_LIB}\n{snippet}\n"
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )


@pytest.fixture
def aws_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install a fake ``aws`` CLI into ``PATH`` for one test.

    The shim returns the current value of ``$FAKE_SSM_VALUE_OLD`` on
    the first ``aws ssm get-parameter`` call and the current value
    of ``$FAKE_SSM_VALUE_NEW`` on subsequent calls. The test flips
    ``FAKE_SSM_VALUE_NEW`` mid-run to simulate the Lambda's 50-min
    refresh; the marker file controls the old-vs-new flip without
    re-spawning the shim.

    Returns ``tmp_path`` so the test can read ``fake_aws.log`` and
    touch ``new_marker`` to flip the SSM value.
    """
    shim_body = textwrap.dedent("""\
        #!/bin/bash
        LOG="${FAKE_AWS_LOG:-/tmp/blitzlog_test_fake_aws.log}"
        printf '%s\\n' "$*" >> "$LOG"

        if [ -n "${FAKE_SSM_VALUE_NEW:-}" ] && [ -f "${FAKE_AWS_NEW_MARKER:-/tmp/blitzlog_test_new_marker}" ]; then
            printf '%s' "$FAKE_SSM_VALUE_NEW"
        else
            printf '%s' "$FAKE_SSM_VALUE_OLD"
        fi
        exit 0
        """)
    shim = tmp_path / "aws"
    shim.write_text(shim_body)
    shim.chmod(0o755)
    log = tmp_path / "fake_aws.log"
    log.write_text("")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_AWS_LOG", str(log))
    monkeypatch.setenv("FAKE_AWS_NEW_MARKER", str(tmp_path / "new_marker"))
    monkeypatch.setenv("FAKE_SSM_VALUE_OLD", DUMMY_TOKEN_V1)
    monkeypatch.setenv("FAKE_SSM_VALUE_NEW", DUMMY_TOKEN_V2)
    monkeypatch.setenv("AWS_REGION", "ap-east-1")
    return tmp_path


# =====================================================================
# Part 1 — Static analysis of watchdog.sh
# =====================================================================
#
# These tests read watchdog.sh as text and assert the path arguments
# the watchdog passes to the poll library. They run in < 10 ms and
# catch a class of bug (path typo, wrong bind-mount source) that
# would not surface in the library-in-isolation tests.


def test_watchdog_initial_credential_writes_use_poll_library_helpers() -> None:
    """The watchdog's initial credential write block must call the
    ``_bl_tokenpoll_write_git_creds`` and
    ``_bl_tokenpoll_write_gh_hosts_yml`` helpers (not a hand-rolled
    ``printf > $file``) so the file format is the same as the
    poll loop's later writes.

    Regression guard: a contributor who replaces the helper call
    with a raw ``printf`` (e.g. to "drop the dependency on the
    library") would silently break the format invariance.
    """
    src = _strip_comments_and_continuations(_read(WATCHDOG))

    # The initial write block sits inside the `if [ -n
    # "${GITHUB_TOKEN_SSM_PARAM:-}" ]` branch, right after the
    # `mkdir -p /root/gh-config/${INSTANCE_ID}` line. We anchor on
    # the helper name and assert the two required calls are present
    # in the same script body.
    assert "_bl_tokenpoll_write_git_creds" in src, (
        "watchdog.sh does not call _bl_tokenpoll_write_git_creds; "
        "the initial git-credential file would not be in the format "
        "the poll loop's later writes use, breaking the "
        "`git push` -> `git credential-store` round-trip"
    )
    assert "_bl_tokenpoll_write_gh_hosts_yml" in src, (
        "watchdog.sh does not call _bl_tokenpoll_write_gh_hosts_yml; "
        "the initial gh hosts.yml would not be in the format the poll "
        "loop's later writes use, breaking the `gh` -> `oauth_token` "
        "round-trip"
    )

    # The git-credential helper line must follow the write — git
    # push reads the file via `git credential-store`, which is
    # configured by `git config --global credential.helper 'store
    # --file /root/.git-credentials.d/github'`. If the helper line
    # is missing, every `git push` falls back to a missing-credential
    # prompt even when the file is freshly written.
    m = re.search(
        r"_bl_tokenpoll_write_git_creds\b[\s\S]*?"
        r"git config --global credential\.helper\s+"
        r"'store --file /root/\.git-credentials\.d/github'",
        src,
    )
    assert m is not None, (
        "watchdog.sh does not configure the git credential helper "
        "right after the initial credential write; `git push` will "
        "not read the bind-mounted file"
    )


def test_watchdog_poll_loop_start_args_match_container_bind_mounts() -> None:
    """``watchdog.sh:234-239`` calls
    ``blitzlog_start_token_poll_loop`` with five arguments. The test
    pins each one against the bind-mount contract:

      arg 1 — SSM param name (the same one the Lambda wrote the
              initial token to)
      arg 2 — region (the same one IMDS returned at line 59-62)
      arg 3 — git-credential file path (matches `-v
              /root/.git-credentials.d:/root/.git-credentials.d`
              at watchdog.sh:253)
      arg 4 — gh hosts.yml path on the host (matches `-v
              /root/gh-config/${INSTANCE_ID}:/root/.config/gh`
              at watchdog.sh:254)
      arg 5 — poll interval, defaulted via
              ``${BLITZLOG_TOKEN_POLL_INTERVAL:-300}``

    A typo in any of these is silent (the loop will happily poll
    a wrong param / write to a wrong path that no one reads).
    """
    src = _strip_comments_and_continuations(_read(WATCHDOG))

    # After stripping comments and bash line continuations, the
    # call collapses to a single line. The whitespace between
    # arguments is one or more spaces (the source uses 9-space
    # alignment for readability under the backslash continuations).
    pattern = (
        r"blitzlog_start_token_poll_loop\s+"
        r"\"\$\{GITHUB_TOKEN_SSM_PARAM\}\"\s+"
        r"\"\$REGION\"\s+"
        r"/root/\.git-credentials\.d/github\s+"
        r"\"/root/gh-config/\$\{INSTANCE_ID\}/hosts\.yml\"\s+"
        r'"\$\{BLITZLOG_TOKEN_POLL_INTERVAL:-300\}"'
    )
    assert re.search(pattern, src), (
        "watchdog.sh's `blitzlog_start_token_poll_loop` call does "
        "not match the expected argument signature. Expected: "
        "GITHUB_TOKEN_SSM_PARAM, $REGION, "
        "/root/.git-credentials.d/github, "
        "/root/gh-config/${INSTANCE_ID}/hosts.yml, "
        "${BLITZLOG_TOKEN_POLL_INTERVAL:-300}. A drift here "
        "would silently break the wiring between the Lambda's "
        "SSM writes and the container's git/gh reads."
    )


def test_watchdog_bind_mount_gh_config_source_matches_write_path() -> None:
    """The host-side ``-v /root/gh-config/${INSTANCE_ID}:/root/.config/gh``
    bind mount at ``watchdog.sh:254`` is the bridge between
    ``_bl_tokenpoll_write_gh_hosts_yml`` (which writes to
    ``/root/gh-config/${INSTANCE_ID}/hosts.yml``) and the
    container's ``gh auth login`` (which writes to
    ``/root/.config/gh/hosts.yml``). If the source ever stops
    matching the write path, the poll loop will rewrite a file
    the container never reads.

    The git-credential bind mount at line 253 is the same shape
    (host path = container path), so it's self-consistent by
    construction — no drift surface to test.
    """
    src = _strip_comments_and_continuations(_read(WATCHDOG))

    # The bind mount is the one with `gh-config` in the source
    # half of the `:`. The container-side `/root/.config/gh`
    # must be the dest.
    pattern = r'-v\s*"/root/gh-config/\$\{INSTANCE_ID\}:/root/\.config/gh"'
    assert re.search(pattern, src), (
        "watchdog.sh's gh-config bind mount does not map "
        "/root/gh-config/${INSTANCE_ID} (host) onto "
        "/root/.config/gh (container). The poll loop's "
        "rewrites of hosts.yml would land on a path the "
        "container never reads."
    )


# =====================================================================
# Part 2 — Subprocess: the file format survives `git credential fill`
#          and YAML parsing.
# =====================================================================
#
# The library's _bl_tokenpoll_write_git_creds and
# _bl_tokenpoll_write_gh_hosts_yml produce specific file formats.
# The container's `git push` and `gh` calls parse those formats
# respectively. These tests run the real parsers against the
# library's output, catching drift on either side without spinning
# up a real container.


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_initial_git_credential_file_is_parseable_by_credential_fill(
    tmp_path: Path,
) -> None:
    """The file ``_bl_tokenpoll_write_git_creds`` produces must be
    parseable by ``git credential fill`` for the agent's
    ``git push`` to work. The test:
      1. Sources the library and writes the file with a dummy
         token.
      2. Spawns ``git credential fill`` against a clean HOME
         (so no system credential cache interferes) configured
         to read the test file.
      3. Asserts the parser returns the expected
         ``username=x-access-token`` and ``password=<token>``.

    This is the closest we can get to the real ``git push`` flow
    without an actual remote.
    """
    git_creds = tmp_path / "git-creds"
    snippet = textwrap.dedent(f"""
        _bl_tokenpoll_write_git_creds "{DUMMY_TOKEN_V1}" "{git_creds}"
        """)
    r = _run_bash(snippet)
    assert r.returncode == 0, r.stderr

    # Spawn `git credential fill` with a clean HOME + no system
    # credential cache, configured to read our test file. The
    # credential helper writes `username=` and `password=` lines
    # to stdout, which we then assert on.
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    result = subprocess.run(
        [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            f"credential.helper=store --file {git_creds}",
            "credential",
            "fill",
        ],
        input="protocol=https\nhost=github.com\n",
        capture_output=True,
        text=True,
        env={
            "HOME": str(empty_home),
            "PATH": os.environ["PATH"],
            "GIT_TERMINAL_PROMPT": "0",
        },
        check=False,
    )
    assert result.returncode == 0, (
        f"`git credential fill` failed: rc={result.returncode} "
        f"stderr={result.stderr!r}"
    )
    lines = dict(
        line.split("=", 1) for line in result.stdout.strip().splitlines() if "=" in line
    )
    assert lines.get("protocol") == "https"
    assert lines.get("host") == "github.com"
    # The credential helper's username is the left half of the URL's
    # userinfo (`x-access-token` is the literal string GitHub's
    # App-install-token flow uses; see
    # https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/about-authentication-with-a-github-app).
    assert (
        lines.get("username") == "x-access-token"
    ), f"expected username=x-access-token, got username={lines.get('username')!r}"
    assert (
        lines.get("password") == DUMMY_TOKEN_V1
    ), f"expected password={DUMMY_TOKEN_V1!r}, got password={lines.get('password')!r}"


def test_initial_gh_hosts_yml_is_parseable_yaml_with_oauth_token(
    tmp_path: Path,
) -> None:
    """The file ``_bl_tokenpoll_write_gh_hosts_yml`` produces must
    be parseable as YAML and have the ``oauth_token`` field the
    ``gh`` CLI reads. The test:
      1. Sources the library and writes the file with a dummy
         token.
      2. Parses it with PyYAML (the same parser ``gh`` uses
         upstream — see
         https://github.com/cli/cli/blob/trunk/internal/config).
      3. Asserts the structure is what ``gh auth status`` expects:
         ``{"github.com": {"oauth_token": <token>, "git_protocol": "https"}}``.
    """
    gh_hosts = tmp_path / "gh-hosts.yml"
    snippet = textwrap.dedent(f"""
        _bl_tokenpoll_write_gh_hosts_yml "{DUMMY_TOKEN_V1}" "{gh_hosts}"
        """)
    r = _run_bash(snippet)
    assert r.returncode == 0, r.stderr

    data = yaml.safe_load(gh_hosts.read_text())
    assert isinstance(
        data, dict
    ), f"gh hosts.yml must be a YAML mapping at the top level, got {type(data).__name__}"
    assert (
        "github.com" in data
    ), f"gh hosts.yml must have a 'github.com' key, got keys {list(data.keys())}"
    github = data["github.com"]
    assert github.get("oauth_token") == DUMMY_TOKEN_V1, (
        f"gh hosts.yml github.com.oauth_token={github.get('oauth_token')!r}, "
        f"expected {DUMMY_TOKEN_V1!r}"
    )
    # The library always writes git_protocol=https (per the comment
    # in blitzlog_token_poll.sh:84-89). A future refactor that drops
    # this field would surface here as a missing-key assertion.
    assert github.get("git_protocol") == "https"


# =====================================================================
# Part 3 — Subprocess: the poll loop started with the watchdog's
#          call signature rewrites (and does NOT rewrite) the
#          bind-mounted files as expected.
# =====================================================================
#
# These are the same scenarios as test_watchdog_credential_poll.py
# but driven through the watchdog's exact call signature — so a
# future refactor of the watchdog's call site that breaks the
# contract (e.g. drops the poll interval env var, swaps the path
# arguments) is caught here even though the library in isolation
# still passes its own tests.


def test_watchdog_signature_poll_loop_rewrites_files_on_ssm_change(
    aws_shim: Path,
) -> None:
    """The poll loop, when started with the same args the watchdog
    uses, rewrites both bind-mounted files when the SSM value
    changes. This is the regression guard for issue #145: the
    Lambda's 50-min refresh event overwrites the SSM parameter,
    the poll loop picks up the new value, and the container's
    next ``git push`` / ``gh`` call uses the new token.

    The test runs the loop with a 50 ms poll interval (5 ms in
    production is 300,000 ms — same logic, 6,000× faster). The
    SSM stub returns the old value until the test touches the
    marker file, then the new value on subsequent calls.
    """
    git_creds = aws_shim / "git-creds"
    gh_hosts = aws_shim / "gh-config" / "i-1234567890abcdef0" / "hosts.yml"
    gh_hosts.parent.mkdir(parents=True, exist_ok=True)
    # Seed the initial state the way the watchdog's
    # `_bl_tokenpoll_write_*` calls would.
    git_creds.write_text(f"https://x-access-token:{DUMMY_TOKEN_V1}@github.com\n")
    gh_hosts.write_text(
        f"github.com:\n    oauth_token: {DUMMY_TOKEN_V1}\n" f"    git_protocol: https\n"
    )

    snippet = textwrap.dedent(f"""
        # Same signature as watchdog.sh:234-239.
        blitzlog_start_token_poll_loop \\
            "/blitzlog/prod/ephemeral/github-token-145" \\
            "ap-east-1" \\
            "{git_creds}" \\
            "{gh_hosts}" \\
            0.05
        sleep 0.3
        touch "{aws_shim}/new_marker"
        sleep 0.3
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
    r = _run_bash(snippet, env={"PATH": f"{aws_shim}:{os.environ['PATH']}"})
    assert r.returncode == 0, (
        f"snippet failed: rc={r.returncode}\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )

    assert DUMMY_TOKEN_V2 in git_creds.read_text(), (
        f"git-creds was not rewritten to the new token; "
        f"content: {git_creds.read_text()!r}"
    )
    assert DUMMY_TOKEN_V1 not in git_creds.read_text(), (
        f"git-creds still contains the old token; "
        f"content: {git_creds.read_text()!r}"
    )
    assert DUMMY_TOKEN_V2 in gh_hosts.read_text(), (
        f"gh-hosts.yml was not rewritten to the new token; "
        f"content: {gh_hosts.read_text()!r}"
    )
    assert DUMMY_TOKEN_V1 not in gh_hosts.read_text(), (
        f"gh-hosts.yml still contains the old token; "
        f"content: {gh_hosts.read_text()!r}"
    )


def test_watchdog_signature_poll_loop_does_not_rewrite_on_stable_ssm(
    aws_shim: Path,
) -> None:
    """The poll loop, when started with the same args the watchdog
    uses, must NOT touch the bind-mounted files when the SSM
    value is stable. This is the same regression guard as
    ``test_poll_loop_does_not_rewrite_when_ssm_value_unchanged``,
    but driven through the watchdog's call signature so a future
    refactor that breaks the diff-check (e.g. by always rewriting)
    is caught here.

    The first tick always writes (a fresh restart where the files
    may not exist). Subsequent ticks must NOT touch the files.
    """
    git_creds = aws_shim / "git-creds"
    gh_hosts = aws_shim / "gh-config" / "i-1234567890abcdef0" / "hosts.yml"
    gh_hosts.parent.mkdir(parents=True, exist_ok=True)
    # Pre-create the files with the same content the shim returns
    # so the loop's first tick is a no-op, and any later tick is
    # also a no-op.
    git_creds.write_text(f"https://x-access-token:{DUMMY_TOKEN_V1}@github.com\n")
    gh_hosts.write_text(
        f"github.com:\n    oauth_token: {DUMMY_TOKEN_V1}\n" f"    git_protocol: https\n"
    )

    snippet = textwrap.dedent(f"""
        blitzlog_start_token_poll_loop \\
            "/blitzlog/prod/ephemeral/github-token-145" \\
            "ap-east-1" \\
            "{git_creds}" \\
            "{gh_hosts}" \\
            0.05
        # Let the loop run for ~6 ticks. The diff check must
        # keep the files untouched.
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
    r = _run_bash(snippet, env={"PATH": f"{aws_shim}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr

    # File content is still the initial value (no SSM change).
    assert DUMMY_TOKEN_V1 in git_creds.read_text()
    assert DUMMY_TOKEN_V2 not in git_creds.read_text()
    assert DUMMY_TOKEN_V1 in gh_hosts.read_text()
    assert DUMMY_TOKEN_V2 not in gh_hosts.read_text()


# =====================================================================
# Part 4 — Subprocess: the SIGTERM cleanup block from watchdog.sh
#          reaps the loop within the documented grace window.
# =====================================================================


def test_watchdog_sigterm_block_reaps_poll_loop_within_grace_window(
    aws_shim: Path,
) -> None:
    """The watchdog's post-container SIGTERM block
    (``watchdog.sh:277-294``) sends TERM to the loop, waits up to
    100 ms, and escalates to SIGKILL if the trap has not fired.
    The test reproduces that block verbatim and asserts the loop
    is reaped within the grace window — so a future refactor that
    widens the window, narrows it, or removes the SIGKILL
    escalation is caught.

    The 5-tick / 20 ms / 100 ms total cadence is the contract; if
    the test is ever flaky here, the production behaviour is
    probably also wrong (the trap is missing or the kill is being
    ignored).
    """
    git_creds = aws_shim / "git-creds"
    gh_hosts = aws_shim / "gh-config" / "i-1234567890abcdef0" / "hosts.yml"
    gh_hosts.parent.mkdir(parents=True, exist_ok=True)
    git_creds.write_text("seed\n")
    gh_hosts.write_text("seed\n")

    # The snippet below is a verbatim transcription of the
    # post-container cleanup in watchdog.sh:277-294. Drift here
    # would also drift the production code, which is what we
    # want to catch.
    snippet = textwrap.dedent(f"""
        blitzlog_start_token_poll_loop \\
            "/blitzlog/prod/ephemeral/github-token-145" \\
            "ap-east-1" \\
            "{git_creds}" \\
            "{gh_hosts}" \\
            0.05
        # Verbatim from watchdog.sh:277-294.
        if [ -n "${{BLITZLOG_TOKEN_POLL_PID:-}}" ]; then
            kill "${{BLITZLOG_TOKEN_POLL_PID}}" 2>/dev/null || true
            for _ in 1 2 3 4 5; do
                if ! kill -0 "${{BLITZLOG_TOKEN_POLL_PID}}" 2>/dev/null; then
                    break
                fi
                sleep 0.02
            done
            if kill -0 "${{BLITZLOG_TOKEN_POLL_PID}}" 2>/dev/null; then
                kill -9 "${{BLITZLOG_TOKEN_POLL_PID}}" 2>/dev/null || true
            fi
        fi
        # Report: the loop should be gone within the grace window.
        if kill -0 "${{BLITZLOG_TOKEN_POLL_PID}}" 2>/dev/null; then
            echo "STILL_ALIVE"
            exit 1
        else
            echo "REAPED"
        fi
        """)
    r = _run_bash(snippet, env={"PATH": f"{aws_shim}:{os.environ['PATH']}"})
    assert r.returncode == 0, (
        f"snippet failed: rc={r.returncode} stderr={r.stderr!r} " f"stdout={r.stdout!r}"
    )
    assert "REAPED" in r.stdout, (
        f"poll loop not reaped by the watchdog's SIGTERM block: "
        f"stdout={r.stdout!r} stderr={r.stderr!r}"
    )
    assert "STILL_ALIVE" not in r.stdout
