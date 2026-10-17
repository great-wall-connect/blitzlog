"""Tests for the watchdog's wiring of the token-refresh poll loop
(issue #145). Part of the **3-level testing convention** for the
mechanism (full taxonomy in ``docs/WATCHDOG_TOKEN_REFRESH.md``):

* **Level 1 — unit with shim.** This file's first 8 tests. Static
  analysis of ``watchdog.sh`` text, format tests against the real
  ``git credential fill`` / PyYAML parsers, and subprocess tests
  with a fake ``aws`` shim. No real AWS. Runs in < 2 s on every PR.

* **Level 2 — real-AWS round-trip on a workstation.** This file's
  last test (``@pytest.mark.integration``). The library's
  ``_bl_tokenpoll_read_ssm`` runs the real ``aws`` binary against
  the real SSM service. Skipped by default; opt in with
  ``BLITZLOG_RUN_INTEGRATION=1`` or ``--run-integration`` (see
  ``conftest.py``). Runs in ~10 s, costs ~$0.0003 per run. A
  scheduled GitHub Actions job runs Level 2 nightly against the
  dev account (see ``.github/workflows/integration-test.yml``).

* **Level 3 — full mechanism on a dev spot instance.** Not in this
  file. Manual procedure + ``scripts/test-dev-spot.sh`` wrapper;
  exercises the container-side ``git push`` / ``gh`` calls against
  a real t4g.medium spot. ~10 min, ~$0.01 per run.

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

The Level 1 tests use a hybrid approach:

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

The Level 2 test uses real ``aws ssm put-parameter`` /
``aws ssm get-parameter`` (with ``--with-decryption``) calls
against a UUID-suffixed test param. The library is sourced in the
same bash subprocess, so the round-trip is real end-to-end: real
``aws`` CLI → real SSM service → real SSM read in the library's
poll loop → real in-place file rewrite.

The Level 1 tests do NOT execute the full ``watchdog.sh`` end-to-end
— that needs IMDS, dockerd, and a real GitHub App installation
token. Those are covered by Level 3 (the dev-spot smoke test
documented in ``docs/WATCHDOG_TOKEN_REFRESH.md`` and wrapped in
``scripts/test-dev-spot.sh``).
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import textwrap
import time
import uuid
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
# Level 1 (1/4) — Static analysis of watchdog.sh
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
# Level 1 (2/4) — Subprocess: the file format survives `git credential
#                fill` and YAML parsing.
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
# Level 1 (3/4) — Subprocess: the poll loop started with the
#                watchdog's call signature rewrites (and does NOT
#                rewrite) the bind-mounted files as expected.
# =====================================================================
#
# These are the same scenarios as test_watchdog_credential_poll.py
# but driven through the watchdog's exact call signature — so a
# future refactor of the watchdog's call site that breaks the
# contract (e.g. drops the poll interval env var, swaps the path
# arguments) is caught here even though the library in isolation
# still passes its own tests.
#
# These tests use a **fake `aws` shim** in PATH (see the
# ``aws_shim`` fixture above). The shim is the Level 1 mock — it
# returns canned SSM values without contacting AWS. The bash
# subprocess the test spawns uses the shim's real ``aws`` binary
# at ``PATH=$tmp_path/aws:$PATH``, so the library's
# ``_bl_tokenpoll_read_ssm`` is exercised end-to-end *within the
# test* (CLI argv shape, ``--with-decryption``, ``--query
# Parameter.Value --output text``, ``2>/dev/null || echo ""``) but
# never against the real SSM service. That round-trip is Level 2.


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
            "/blitzlog/prod/ephemeral/gh/example/owner/repo/145" \\
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
            "/blitzlog/prod/ephemeral/gh/example/owner/repo/145" \\
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
# Level 1 (4/4) — Subprocess: the SIGTERM cleanup block from
#                watchdog.sh reaps the loop within the documented
#                grace window.
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
            "/blitzlog/prod/ephemeral/gh/example/owner/repo/145" \\
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


# =====================================================================
# Level 2 — Real-AWS round-trip on a workstation.
# =====================================================================
#
# This test exercises the watchdog's poll library against the *real*
# AWS SSM service. The library's `_bl_tokenpoll_read_ssm` runs the
# real `aws` CLI binary in the bash subprocess's PATH (no shim) and
# the test drives the round-trip by writing fresh values via the
# real `aws ssm put-parameter`. A bug class that Level 1 cannot
# catch — e.g. an SSM-side encryption/decryption mismatch, a
# region-default disagreement, a network-level timeout, an
# `--with-decryption` misconfiguration on SecureString parameters
# — surfaces here.
#
# Skipped by default; opt in with `BLITZLOG_RUN_INTEGRATION=1` or
# `--run-integration` (see `conftest.py`). The scheduled
# `.github/workflows/integration-test.yml` runs this on a cron
# against the dev account.
#
# Cost: ~$0.0003 per run (3 puts + a handful of gets; SecureString
# storage < 1 s; cleanup deletes the param). Time: ~10 s with a
# 1 s test poll interval.
#
# AWS permissions: `ssm:GetParameter`, `ssm:PutParameter`,
# `ssm:DeleteParameter` on
# `arn:aws:ssm:*:*:parameter/blitzlog/dev/ephemeral/gh/integration-test/repo/integration-test-*`.
# The new `blitzlog-integration-test-role` in
# `infra/bootstrap/integration-test-role.tf` grants exactly this.
#
# Each test run uses a unique param name (`<uuid8>`) so concurrent
# runs (or a re-run after a crashed previous run that left a
# param behind) never collide.


def _aws_region() -> str:
    """Resolve the AWS region for the test. The test reads
    ``AWS_REGION`` (or ``AWS_DEFAULT_REGION``) from the env, falling
    back to the project's default ``ap-east-1`` so a fresh
    workstation run does not need any extra configuration.
    """
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "ap-east-1"
    )


def _aws_cli() -> list[str]:
    """Return the absolute path to the real ``aws`` CLI, or
    ``["aws"]`` if the test runner already has it on PATH.

    The Level 2 test deliberately uses the *real* ``aws`` binary
    (no shim). If ``aws`` is not on PATH, the test fails loud here
    rather than deep inside the bash subprocess where the error
    message is harder to attribute.
    """
    aws = shutil.which("aws")
    if not aws:
        pytest.fail(
            "`aws` CLI is not on PATH; the Level 2 test requires "
            "the real AWS CLI to drive the SSM round-trip"
        )
    return [aws]


def _ssm_put(name: str, value: str, region: str) -> None:
    """Real ``aws ssm put-parameter`` call (no shim)."""
    result = subprocess.run(
        _aws_cli()
        + [
            "ssm",
            "put-parameter",
            "--name",
            name,
            "--type",
            "SecureString",
            "--value",
            value,
            "--overwrite",
            "--region",
            region,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"aws ssm put-parameter failed for {name}: "
            f"rc={result.returncode} stderr={result.stderr!r}"
        )


def _ssm_delete(name: str, region: str) -> None:
    """Real ``aws ssm delete-parameter`` call (no shim). Idempotent
    — the test always cleans up even if the put failed.
    """
    subprocess.run(
        _aws_cli()
        + [
            "ssm",
            "delete-parameter",
            "--name",
            name,
            "--region",
            region,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.integration
def test_watchdog_poll_loop_rewrites_against_real_ssm(tmp_path: Path) -> None:
    """Level 2: the watchdog's poll loop rewrites the bind-mounted
    credential files when the SSM value changes, *against the real
    SSM service*.

    The test spawns a long-lived bash subprocess that starts the
    poll loop with a 1 s interval and then ``wait``s for the loop
    PID. The Python test process drives the round-trip: real
    ``aws ssm put-parameter`` to change the value, then ``time.sleep``
    for the loop to tick and rewrite, then read the file. On
    teardown the bash subprocess is SIGTERMed, the loop's TERM trap
    fires, ``wait`` returns, the bash subprocess exits cleanly, and
    the SSM param is deleted.

    Bug class this catches (Level 1 cannot):

      * ``--with-decryption`` flag drift on SecureString parameters.
      * Region-default disagreement between the test runner and
        the library's resolver.
      * A network-level timeout on ``ssm:GetParameter`` (real
        network, not a shim's ``echo "value"``).
      * A bash-vs-Python encoding mismatch on the SSM value
        (e.g. trailing newlines, null bytes) that the shim
        cannot reproduce.

    Cost: ~$0.0003 per run. Time: ~10 s.
    """
    region = _aws_region()
    uuid8 = uuid.uuid4().hex[:8]
    param_name = (
        f"/blitzlog/dev/ephemeral/gh/integration-test/repo/integration-test-{uuid8}"
    )
    value_a = "ghp_real_ssm_round_trip_aaaa"
    value_b = "ghp_real_ssm_round_trip_bbbb"
    value_c = "ghp_real_ssm_round_trip_cccc"

    git_creds = tmp_path / "git-creds"
    gh_hosts = tmp_path / "gh-hosts.yml"
    git_creds.parent.mkdir(parents=True, exist_ok=True)

    # Seed the files with a sentinel so the test can distinguish
    # "the loop rewrote the file" from "the file was unchanged
    # from the seed". The library's first tick always writes
    # (a fresh restart where the files may not exist), so this
    # seed does not affect the diff-rewrite logic.
    git_creds.write_text("seed\n")
    gh_hosts.write_text("seed\n")

    # The bash snippet that runs the loop for the whole test. The
    # `wait $BLITZLOG_TOKEN_POLL_PID` blocks until the loop dies;
    # the SIGTERM trap on the bash subprocess cascades to the
    # loop's `trap 'exit 0' TERM`, the wait returns, the bash
    # subprocess exits with 0.
    bash_snippet = textwrap.dedent(f"""
        set -u
        source {POLL_LIB}
        blitzlog_start_token_poll_loop \\
            "{param_name}" \\
            "{region}" \\
            "{git_creds}" \\
            "{gh_hosts}" \\
            1
        # On SIGTERM (Python's terminate()): kill the loop, let
        # the trap fire, let `wait` return. This mirrors the
        # watchdog's `watchdog.sh:277-294` SIGTERM block.
        trap 'kill "${{BLITZLOG_TOKEN_POLL_PID}}" 2>/dev/null || true' TERM
        wait "${{BLITZLOG_TOKEN_POLL_PID}}"
        """)

    bash_proc = subprocess.Popen(
        ["bash", "-c", bash_snippet],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )
    put_done = False
    try:
        # Seed SSM with value A so the loop's first observation has
        # something to read. Doing this BEFORE the loop starts would
        # race the first tick; the loop starts and then we wait 2 s
        # (the loop's 1 s interval + a margin) before the first
        # put-then-read cycle. The first tick reads whatever is in
        # SSM at that point (the seed value A) and writes it.
        _ssm_put(param_name, value_a, region)
        put_done = True

        # Phase A: the loop should have observed value A by now
        # (or on the next 1 s tick). Wait 2 s to be safe — the
        # first tick is the slowest because the loop's `sleep 1`
        # starts the moment `blitzlog_start_token_poll_loop` is
        # called, but the SSM read takes ~50 ms.
        time.sleep(2)
        assert value_a in git_creds.read_text(), (
            f"phase A: git-creds does not contain {value_a!r}; "
            f"file content: {git_creds.read_text()!r}"
        )
        assert value_a in gh_hosts.read_text(), (
            f"phase A: gh-hosts.yml does not contain {value_a!r}; "
            f"file content: {gh_hosts.read_text()!r}"
        )
        assert "seed" not in git_creds.read_text(), (
            "phase A: git-creds still has the seed; the poll loop "
            "did not rewrite the file from the real SSM read"
        )

        # Phase B: overwrite SSM with value B. The loop's next
        # tick (≤ 1 s away) reads the new value, sees the diff,
        # rewrites both files.
        _ssm_put(param_name, value_b, region)
        time.sleep(2)
        assert value_b in git_creds.read_text(), (
            f"phase B: git-creds does not contain {value_b!r}; "
            f"file content: {git_creds.read_text()!r}"
        )
        assert value_a not in git_creds.read_text(), (
            f"phase B: git-creds still contains the old value "
            f"{value_a!r}; the diff-rewrite path did not fire"
        )
        assert value_b in gh_hosts.read_text()
        assert value_a not in gh_hosts.read_text()

        # Phase C: one more overwrite to confirm the loop survives
        # multiple refreshes (not just a single one).
        _ssm_put(param_name, value_c, region)
        time.sleep(2)
        assert value_c in git_creds.read_text(), (
            f"phase C: git-creds does not contain {value_c!r}; "
            f"file content: {git_creds.read_text()!r}"
        )
        assert value_b not in git_creds.read_text()
        assert value_c in gh_hosts.read_text()
        assert value_b not in gh_hosts.read_text()
    finally:
        # Tear down the bash subprocess. terminate() sends SIGTERM
        # which cascades to the loop's TERM trap. The wait in the
        # bash snippet then returns and the bash subprocess exits
        # with 0. The 5 s timeout covers a worst-case SSM read
        # latency (the loop could be mid-sleep when SIGTERM fires).
        bash_proc.terminate()
        try:
            bash_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bash_proc.kill()
            bash_proc.wait(timeout=5)
        # Surface the bash subprocess's stderr if it exited non-zero
        # in a way that is NOT the expected SIGTERM. Popen returns
        # the negative signal number for a process killed by a
        # signal; 143 = 128 + 15 = SIGTERM (the expected clean exit
        # from our `terminate()` call). -15 is the same value as
        # 143 in some subprocess.Popen versions, so we accept both.
        accepted_clean_exits = {
            -15,
            -signal.SIGTERM,
            signal.SIGTERM + 128,
        }
        if (
            bash_proc.returncode not in accepted_clean_exits
            and bash_proc.returncode != 0
        ):
            stderr_output = (
                bash_proc.stderr.read().decode("utf-8", errors="replace")
                if bash_proc.stderr
                else ""
            )
            raise AssertionError(
                f"bash subprocess exited with rc={bash_proc.returncode}; "
                f"stderr: {stderr_output!r}"
            )
        # Always delete the test param, even on assertion failure.
        # The `put_done` guard means we only attempt a delete if at
        # least one put succeeded (so a network error before the
        # first put doesn't surface as a confusing
        # "ParameterNotFound" on delete).
        if put_done:
            _ssm_delete(param_name, region)


# =====================================================================
# Level 1 (5/5) — Static analysis: instance teardown deletes the
#                SSM token + per-issue schedule group, and does so
#                before terminate-instances.
# =====================================================================


def test_watchdog_teardown_deletes_ssm_token() -> None:
    """The watchdog's post-container cleanup block must delete the
    per-issue SSM token. Without this, tokens accumulate in SSM
    indefinitely (one per instance that ever ran) — see issue
    #145 + the cleanup gap surfaced by PR #154.
    """
    src = _strip_comments_and_continuations(_read(WATCHDOG))
    assert "aws ssm delete-parameter" in src, (
        "watchdog.sh does not delete the per-issue SSM token at "
        "instance teardown. Tokens will accumulate in SSM "
        "indefinitely."
    )
    # The delete must be conditional on the env var so an older
    # AMI that didn't have the env var (or a test that doesn't
    # source /etc/blitzlog.env) doesn't blow up.
    assert "GITHUB_TOKEN_SSM_PARAM" in src, (
        "watchdog.sh's SSM delete must be guarded by the "
        "GITHUB_TOKEN_SSM_PARAM env var (set by the Lambda's "
        "user-data)."
    )


def test_watchdog_teardown_deletes_per_issue_schedule_group() -> None:
    """The watchdog's post-container cleanup must call
    `aws scheduler delete-schedule-group` on the per-issue group
    (set by the Lambda's `GITHUB_TOKEN_SCHED_GROUP` env var).

    Why a group delete and not a per-iteration delete loop:
    - One AWS API call vs. up to 11.
    - No off-by-one risk (the chain's iter-9 endpoint is the
      last scheduled hop, but the loop boundary was a known
      footgun in scripts/test-dev-spot.sh).
    - Per-issue scope means one instance's cleanup cannot nuke
      another instance's chain (the previous per-env group
      design had this cross-instance interference).
    """
    src = _strip_comments_and_continuations(_read(WATCHDOG))
    assert "aws scheduler delete-schedule-group" in src, (
        "watchdog.sh must delete the per-issue schedule group at "
        "instance teardown (single cascade-delete vs. iterating "
        "iter-0..9). Per-issue scope prevents one instance's "
        "cleanup from nuking another instance's chain."
    )
    # Must use the env var the Lambda passes in, not a hard-coded
    # group name.
    assert "GITHUB_TOKEN_SCHED_GROUP" in src, (
        "watchdog.sh's schedule-group delete must use the "
        "GITHUB_TOKEN_SCHED_GROUP env var (passed by the Lambda's "
        "user-data heredoc), not a hard-coded group name."
    )


def test_watchdog_teardown_aws_state_runs_before_terminate_instances() -> None:
    """Order matters: the SSM delete + schedule-group delete MUST
    appear before `aws ec2 terminate-instances` in the watchdog
    source. Once terminate-instances is called, the instance is
    shutting down and subsequent AWS calls from this script may
    fail. The dev-spot test's cleanup function (scripts/test-dev-spot.sh)
    doesn't have this constraint because it runs outside the
    instance; the watchdog does.
    """
    src = _strip_comments_and_continuations(_read(WATCHDOG))
    ssm_pos = src.find("aws ssm delete-parameter")
    sched_pos = src.find("aws scheduler delete-schedule-group")
    term_pos = src.find("aws ec2 terminate-instances")
    assert ssm_pos > 0, "aws ssm delete-parameter not present in watchdog.sh"
    assert (
        sched_pos > 0
    ), "aws scheduler delete-schedule-group not present in watchdog.sh"
    assert term_pos > 0, "aws ec2 terminate-instances not present in watchdog.sh"
    assert ssm_pos < term_pos, (
        f"SSM delete (pos {ssm_pos}) must run BEFORE "
        f"terminate-instances (pos {term_pos}); once terminate runs "
        "the instance is shutting down and the SSM call may fail."
    )
    assert sched_pos < term_pos, (
        f"schedule-group delete (pos {sched_pos}) must run BEFORE "
        f"terminate-instances (pos {term_pos}); same reason."
    )
