"""Behavioural tests for
``infra/aws/packer/scripts-docker-ubuntu/blitzlog_uploader.sh``.

Issue #123 introduces this library as the single source of truth for
every host-side S3 verb (watchdog.sh, 03-bake-images.sh, and the
Lambda-bootstrap user-data heredocs). These tests pin:

* The four public functions (``blitzlog_s3_put``, ``blitzlog_s3_get``,
  ``blitzlog_s3_rm``, ``blitzlog_s3_ls``) wrap the matching ``aws s3
  ...`` call with the right verb/region/URI shape.
* Each function returns the underlying ``aws`` CLI exit code on
  failure — i.e. failures are *surfaced*, not swallowed. The library
  logs an ``ERROR:`` line and propagates the exit code to the caller,
  which is free to swallow via ``|| true`` at the call site.
* The library prefers the caller's ``log()`` if defined (watchdog +
  Lambda-bootstrap user-data) and falls back to its own logger if not
  (e.g. when sourced from a 03-bake-images.sh context that hasn't
  defined ``log()``).
* Region resolution: explicit arg > ``$REGION`` > ``$AWS_REGION`` >
  empty.

The tests run the library functions through a real ``bash`` subprocess
with a fake ``aws`` CLI shimmed into ``PATH``. No network calls, no
mocking framework — the shim is a 5-line bash script that records its
arguments and exits with a configurable code. The shim lives in a
pytest-managed tmpdir, so it can never collide with a real ``aws`` on
the test host.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "infra/aws/packer/scripts-docker-ubuntu/blitzlog_uploader.sh"


# --- the fake aws shim -------------------------------------------------
#
# Records each invocation to $FAKE_AWS_LOG (one line per call) and exits
# with a code based on the `FAKE_AWS_FAIL` knob:
#   FAKE_AWS_FAIL=""   → always 0
#   FAKE_AWS_FAIL=put  → exit 7 on `aws s3 cp` (uploads + downloads both)
#   FAKE_AWS_FAIL=rm   → exit 7 on `aws s3 rm`
#   FAKE_AWS_FAIL=ls   → exit 7 on `aws s3 ls`
#
# The aws CLI does not distinguish `cp upload` from `cp download` via
# the verb alone (`aws s3 cp` is overloaded), so the shim treats all
# `cp` calls as failing under FAKE_AWS_FAIL=put. The point of the test
# is that the *library* surfaces the exit code — not that we faithfully
# reproduce aws's internal failure semantics.
SHIM_BODY = """\
#!/bin/bash
printf '%s\\n' "$*" >> "${FAKE_AWS_LOG:-/tmp/blitzlog_test_fake_aws.log}"
case "$1 $2" in
    "s3 cp")
        [ "${FAKE_AWS_FAIL:-}" = "put" ] && exit 7
        exit 0
        ;;
    "s3 rm")
        [ "${FAKE_AWS_FAIL:-}" = "rm" ] && exit 7
        exit 0
        ;;
    "s3 ls")
        [ "${FAKE_AWS_FAIL:-}" = "ls" ] && exit 7
        # Pretend the key exists (real `aws s3 ls s3://b/k` prints
        # one line on hit). Tests for the `ls` probe assert the exit
        # code, not the printed output.
        echo "2026-01-01 00:00:00       42 metadata.json"
        exit 0
        ;;
esac
exit 0
"""


@pytest.fixture
def aws_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install a fake ``aws`` CLI into PATH for one test.

    Returns the tmp_path so callers can find the per-test
    ``fake_aws.log`` (records every argv).
    """
    shim = tmp_path / "aws"
    shim.write_text(SHIM_BODY)
    shim.chmod(0o755)
    log = tmp_path / "fake_aws.log"
    log.write_text("")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_AWS_LOG", str(log))
    # Default region resolver inputs. Tests can override these.
    monkeypatch.setenv("REGION", "ap-east-1")
    monkeypatch.delenv("FAKE_AWS_FAIL", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    return tmp_path


def _run(
    aws_shim: Path,
    snippet: str,
    *extra_env: str,
) -> subprocess.CompletedProcess:
    """Run ``snippet`` in a bash that has the library pre-sourced.

    ``snippet`` is appended after `source LIB` — typically a single
    function call. The helper appends ``echo EXIT=$?`` to the script,
    so the test caller can read the *library function's* exit code
    from the subprocess's stdout (not ``CompletedProcess.returncode``,
    which is the bash process's exit code, i.e. that of ``echo``,
    which is always 0).

    Stderr is captured (the library writes ERROR lines there on
    failure). ``extra_env`` are "KEY=VALUE" pairs to overlay on the
    inherited environment (so tests can flip FAKE_AWS_FAIL etc
    *for the subprocess only* without leaking to the pytest process).
    """
    env = os.environ.copy()
    for pair in extra_env:
        k, _, v = pair.partition("=")
        env[k] = v
    script = f"set -u\nsource {LIB}\n{snippet}\necho EXIT=$?\n"
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _function_exit_code(result: subprocess.CompletedProcess) -> int:
    """Parse the ``EXIT=N`` marker that ``_run`` appends to stdout.

    ``_run`` always ends with ``echo EXIT=$?`` so the *function being
    tested*'s exit code is recoverable from the subprocess output. The
    subprocess's own ``returncode`` is the exit code of ``echo``,
    which is always 0.
    """
    for line in result.stdout.splitlines():
        if line.startswith("EXIT="):
            return int(line.removeprefix("EXIT="))
    raise AssertionError(
        f"missing EXIT= marker in subprocess stdout: {result.stdout!r}"
    )


def _fake_aws_log(aws_shim: Path) -> str:
    return (aws_shim / "fake_aws.log").read_text()


# --- happy path: each function shapes the aws s3 call correctly -------


def test_put_passes_uri_and_region(aws_shim: Path) -> None:
    src = aws_shim / "local.txt"
    src.write_text("hello")
    r = _run(
        aws_shim,
        f"blitzlog_s3_put my-bucket path/key {src}",
    )
    assert _function_exit_code(r) == 0
    log = _fake_aws_log(aws_shim)
    assert "s3://my-bucket/path/key" in log
    assert "--region" in log
    assert "ap-east-1" in log


def test_get_passes_uri_and_region(aws_shim: Path) -> None:
    out = aws_shim / "out.bin"
    r = _run(
        aws_shim,
        f"blitzlog_s3_get models-bucket models/ggml-base.en.bin {out}",
    )
    assert _function_exit_code(r) == 0
    log = _fake_aws_log(aws_shim)
    assert "s3://models-bucket/models/ggml-base.en.bin" in log
    assert str(out) in log
    assert "--region ap-east-1" in log


def test_rm_for_bot_pool_lock_release(aws_shim: Path) -> None:
    # Mirrors watchdog.sh's `blitzlog_s3_rm ${S3_LOGS_BUCKET}
    # bot-pool-locks/${SENDER}/${BOT}.json` call.
    r = _run(
        aws_shim,
        "blitzlog_s3_rm logs-bucket bot-pool-locks/alice/prod.json",
    )
    assert _function_exit_code(r) == 0
    log = _fake_aws_log(aws_shim)
    assert "s3://logs-bucket/bot-pool-locks/alice/prod.json" in log
    assert "rm" in log


def test_ls_probe_uses_s3_ls(aws_shim: Path) -> None:
    # Mirrors assisted.py's metadata.json existence probe.
    r = _run(
        aws_shim,
        "blitzlog_s3_ls session-bucket env/repo/issue/9/metadata.json",
    )
    assert _function_exit_code(r) == 0
    log = _fake_aws_log(aws_shim)
    assert "s3://session-bucket/env/repo/issue/9/metadata.json" in log
    assert "ls" in log


# --- failure path: each function returns the underlying exit code and
# --- emits an ERROR log line. Failures are surfaced, not swallowed. --


def test_put_failure_returns_exit_code_and_logs_error(aws_shim: Path) -> None:
    r = _run(
        aws_shim,
        "blitzlog_s3_put bkt key /nonexistent",
        "FAKE_AWS_FAIL=put",
    )
    assert _function_exit_code(r) == 7
    assert "ERROR: S3 put failed" in r.stderr
    assert "bucket=bkt" in r.stderr
    assert "key=key" in r.stderr
    assert "exit=7" in r.stderr


def test_get_failure_returns_exit_code_and_logs_error(aws_shim: Path) -> None:
    r = _run(
        aws_shim,
        "blitzlog_s3_get bkt key /tmp/out",
        "FAKE_AWS_FAIL=put",
    )
    assert _function_exit_code(r) == 7
    assert "ERROR: S3 get failed" in r.stderr
    assert "bucket=bkt" in r.stderr
    assert "key=key" in r.stderr


def test_rm_failure_returns_exit_code_and_logs_error(aws_shim: Path) -> None:
    r = _run(
        aws_shim,
        "blitzlog_s3_rm bkt bot-pool-locks/u/b.json",
        "FAKE_AWS_FAIL=rm",
    )
    assert _function_exit_code(r) == 7
    assert "ERROR: S3 rm failed" in r.stderr


def test_ls_failure_returns_exit_code_and_logs_error(aws_shim: Path) -> None:
    r = _run(
        aws_shim,
        "blitzlog_s3_ls bkt missing/key",
        "FAKE_AWS_FAIL=ls",
    )
    assert _function_exit_code(r) == 7
    assert "ERROR: S3 ls failed" in r.stderr


# --- region resolution -------------------------------------------------


def test_explicit_region_overrides_env(aws_shim: Path) -> None:
    r = _run(
        aws_shim,
        "blitzlog_s3_put bkt key /tmp/x us-west-2",
        # Both env vars set; explicit arg must win.
        "REGION=ap-east-1",
        "AWS_REGION=ap-east-1",
    )
    assert _function_exit_code(r) == 0
    log = _fake_aws_log(aws_shim)
    assert "us-west-2" in log
    # The fake aws shim writes `--region us-west-2` on one line and
    # nothing else with the ap-east-1 region value.
    assert "ap-east-1" not in log


def test_region_env_var_used_when_no_explicit_arg(aws_shim: Path) -> None:
    r = _run(
        aws_shim,
        "blitzlog_s3_put bkt key /tmp/x",
        "REGION=ap-east-1",
        "AWS_REGION=eu-west-1",
    )
    assert _function_exit_code(r) == 0
    log = _fake_aws_log(aws_shim)
    assert "ap-east-1" in log
    # $AWS_REGION must NOT have been preferred over $REGION.
    assert "eu-west-1" not in log


def test_aws_region_env_var_used_when_no_region(aws_shim: Path) -> None:
    r = _run(
        aws_shim,
        "blitzlog_s3_get bkt key /tmp/x",
        "REGION=",
        "AWS_REGION=eu-west-1",
    )
    assert _function_exit_code(r) == 0
    log = _fake_aws_log(aws_shim)
    assert "eu-west-1" in log


# --- logging channel: prefer caller's log(), fall back to internal --


def test_uses_caller_log_when_defined(aws_shim: Path) -> None:
    # Define a caller's `log()` that writes to a known file. Then
    # trigger an S3 failure and assert the error went through THAT
    # channel, not the library's internal fallback.
    capture = aws_shim / "caller.log"
    capture.write_text("")
    snippet = (
        f'log() {{ printf "%s\\n" "$1" >> "{capture}"; }}\n'
        "blitzlog_s3_put bkt key /tmp/x\n"
    )
    r = _run(aws_shim, snippet, "FAKE_AWS_FAIL=put")
    assert _function_exit_code(r) == 7
    assert "ERROR: S3 put failed" in capture.read_text()


def test_falls_back_to_internal_log_when_caller_log_undefined(
    aws_shim: Path,
) -> None:
    # No caller's log() defined. The library uses `declare -F log`
    # (returns 1 cleanly under `set -u`) to detect that and falls
    # back to its internal append-to-/var/log/backend-bootstrap.log
    # helper. Trigger an S3 failure and assert the ERROR line appears
    # in the subprocess stderr (which is where the library's fallback
    # writes via `tee ... >&2`).
    r = _run(
        aws_shim,
        "blitzlog_s3_put bkt key /tmp/x",
        "FAKE_AWS_FAIL=put",
    )
    assert _function_exit_code(r) == 7
    assert "ERROR: S3 put failed" in r.stderr


# --- behavioural sanity: the library actually sources cleanly -------


def test_library_passes_bash_n() -> None:
    """Pin: the library is syntactically valid bash."""
    result = subprocess.run(
        ["bash", "-n", str(LIB)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_no_aws_s3_calls_outside_the_library() -> None:
    """Acceptance criterion from issue #123:

    > grep -nE '\\baws s3 (cp|rm|ls)\\b' infra/aws/packer/scripts-docker-ubuntu/ lambda/scripts/
    > returns no hits outside the new library.

    Pinned here so a future contributor adding a stray ``aws s3 cp``
    somewhere fails CI before review.
    """
    targets = [
        REPO_ROOT / "infra/aws/packer/scripts-docker-ubuntu",
        REPO_ROOT / "lambda" / "scripts",
    ]
    pattern = re.compile(r"\baws s3 (cp|rm|ls)\b")
    violations: list[str] = []
    for root in targets:
        for path in root.rglob("*.sh"):
            if path == LIB:
                continue
            text = path.read_text()
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    violations.append(f"{path}:{lineno}: {line.strip()}")
        for path in root.rglob("*.py"):
            text = path.read_text()
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    violations.append(f"{path}:{lineno}: {line.strip()}")
    assert violations == [], (
        "Issue #123: every `aws s3 cp/rm/ls` call must be replaced with a "
        "blitzlog_s3_* helper. Found:\n" + "\n".join(violations)
    )
