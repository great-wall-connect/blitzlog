"""Regression guards for the hash-pinned requirements locks.

`lambda/requirements-{aws,alicloud}.txt` are pip-compile locks: every
requirement carries a `--hash`, so pip installs them in
`--require-hashes` mode, where every transitive dependency must be
pinned with `==` or the install aborts.

Those locks target the Lambda runtime's Python (3.12 — see
`runtime = "python3.12"` in infra/aws/modules/core/lambda.tf).
Dependabot recompiles them under Python >= 3.13, where `aiohttp`'s
`typing_extensions>=4.4; python_version < "3.13"` marker is false, so
pip-compile concludes the dependency is unnecessary and DROPS the pin.
The lock then stops installing, and the `Lint + test (Python)` job dies
with an error that mentions nothing about Python versions. That broke
PRs #159 and #180.

What we guard:
- The alicloud `.in` declares `typing-extensions` explicitly, so the
  resolution is identical on 3.12 and 3.13+ and dependabot can no
  longer remove the pin.
- The compiled alicloud lock pins `typing-extensions`.
- The AWS lock does NOT pin it — `aiohttp` reaches the AWS bundle only
  through `oss2`, so the fix must not bloat the AWS Lambda bundle.
- `scripts/check_requirements_lock.py` parses the lock format correctly
  and flags a dropped pin.
- The guard is wired into `mise.toml` and the lint workflow, so drift
  fails CI with an actionable message instead of a cryptic pip error.

These are static checks — no network, no pip-compile invocation.
"""

import importlib.util
import os
import re
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD = REPO_ROOT / "scripts" / "check_requirements_lock.py"
ALICLOUD_IN = REPO_ROOT / "lambda" / "requirements-alicloud.in"
ALICLOUD_LOCK = REPO_ROOT / "lambda" / "requirements-alicloud.txt"
AWS_LOCK = REPO_ROOT / "lambda" / "requirements-aws.txt"
MISE_TOML = REPO_ROOT / "mise.toml"
LINT_YML = REPO_ROOT / ".github" / "workflows" / "lint.yml"


def _load_guard():
    """Import scripts/check_requirements_lock.py as a module."""
    spec = importlib.util.spec_from_file_location("check_requirements_lock", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _declared_requirements(path: Path) -> set[str]:
    """Return the requirement names a pip `.in` file declares.

    Skips comments, pip options (`-r`, `--flag`) and environment
    markers, and normalises names the way PEP 503 does.
    """
    names = set()
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].strip()
        if name:
            names.add(name.lower().replace("_", "-"))
    return names


class TestAlicloudSourceDeclaresTypingExtensions(unittest.TestCase):
    """The explicit declaration is what stops dependabot dropping the pin."""

    def test_alicloud_in_declares_typing_extensions(self):
        """`typing-extensions` must be an explicit requirement.

        `aiohttp` declares it as `typing_extensions>=4.4; python_version
        < "3.13"`. The lock targets Python 3.12, so the dependency is
        real there — but dependabot compiles under Python >= 3.13, where
        the marker is false, and drops the pin. Declaring it directly
        makes the dependency unconditional, so both interpreters resolve
        the same graph.
        """
        self.assertIn(
            "typing-extensions",
            _declared_requirements(ALICLOUD_IN),
            "lambda/requirements-alicloud.in must declare `typing-extensions` "
            "explicitly. Without it, dependabot's pip-compile run (Python "
            ">= 3.13) drops the pin that the Python 3.12 Lambda runtime "
            "needs, and the lock stops installing under --require-hashes.",
        )

    def test_alicloud_in_explains_why_the_declaration_exists(self):
        """The declaration carries a comment saying why it is there.

        A bare `typing-extensions` line in a `.in` file reads like a
        redundant transitive dependency, and the next person to tidy the
        file would delete it. The comment must mention the marker and
        the Python versions involved.
        """
        text = ALICLOUD_IN.read_text()
        comment = "\n".join(
            line for line in text.splitlines() if line.strip().startswith("#")
        )
        self.assertIn(
            "python_version",
            comment,
            "the .in file's comment must explain the `python_version < "
            '"3.13"` marker that makes this declaration necessary.',
        )
        self.assertIn(
            "3.12",
            comment,
            "the .in file's comment must name the Python version the lock "
            "targets (3.12, matching the Lambda runtime).",
        )

    def test_typing_extensions_is_not_declared_for_aws(self):
        """The fix is alicloud-scoped; the AWS bundle must not gain it.

        `aiohttp` only reaches the dependency graph through `oss2`,
        which is alicloud-only. Declaring `typing-extensions` in the
        shared `lambda/requirements.in` would put it in the AWS Lambda
        bundle for no reason.
        """
        shared_in = REPO_ROOT / "lambda" / "requirements.in"
        self.assertNotIn(
            "typing-extensions",
            _declared_requirements(shared_in),
            "lambda/requirements.in is shared by both providers. "
            "typing-extensions is only needed by the alicloud bundle "
            "(aiohttp arrives via oss2), so it belongs in "
            "lambda/requirements-alicloud.in.",
        )


class TestCommittedLocks(unittest.TestCase):
    """The compiled locks carry the pins the sources declare."""

    def test_alicloud_lock_pins_typing_extensions(self):
        guard = _load_guard()
        pins = guard.parse_pins(ALICLOUD_LOCK.read_text())
        self.assertIn(
            "typing-extensions",
            pins,
            "lambda/requirements-alicloud.txt must pin typing-extensions. "
            "Without it the alicloud Lambda bundle has no typing_extensions "
            "and `import oss2` -> `import aiohttp` raises ImportError.",
        )

    def test_aws_lock_does_not_pin_typing_extensions(self):
        """The AWS bundle must not gain a package it never needed."""
        guard = _load_guard()
        pins = guard.parse_pins(AWS_LOCK.read_text())
        self.assertNotIn(
            "typing-extensions",
            pins,
            "lambda/requirements-aws.txt must not pin typing-extensions — "
            "the AWS dependency graph has no marker-gated requirement, and "
            "adding it would bloat the deployed Lambda bundle.",
        )

    def test_alicloud_lock_records_its_target_python(self):
        """The lock header must state the Python it was compiled for.

        The guard re-resolves under the running interpreter, so it needs
        to know which Python the lock was compiled for in order to
        refuse to compare against the wrong one.
        """
        guard = _load_guard()
        target = guard.target_python(ALICLOUD_LOCK.read_text())
        self.assertIsNotNone(
            target,
            "lambda/requirements-alicloud.txt must carry the pip-compile "
            "header naming its target Python.",
        )
        self.assertEqual(
            target,
            (3, 12),
            "the lambda locks target Python 3.12, matching "
            'runtime = "python3.12" in infra/aws/modules/core/lambda.tf.',
        )


class TestGuardScript(unittest.TestCase):
    """The guard's parsing and comparison logic."""

    def test_guard_exists_and_is_executable(self):
        self.assertTrue(GUARD.exists(), f"{GUARD} must exist")
        self.assertTrue(
            os.access(GUARD, os.X_OK),
            f"{GUARD} must be executable",
        )

    def test_parse_pins_reads_the_lock_format(self):
        """`name==version \\` plus indented `--hash=` lines."""
        guard = _load_guard()
        text = (
            "# a comment\n"
            "aiohttp==3.14.5 \\\n"
            "    --hash=sha256:abc \\\n"
            "    --hash=sha256:def\n"
            "    # via oss2\n"
            "typing-extensions==4.16.0 \\\n"
            "    --hash=sha256:123\n"
        )
        pins = guard.parse_pins(text)
        self.assertEqual(pins.get("aiohttp"), "3.14.5")
        self.assertEqual(pins.get("typing-extensions"), "4.16.0")
        self.assertNotIn(
            "hash",
            pins,
            "the --hash continuation lines must not be parsed as pins",
        )

    def test_parse_pins_normalises_names(self):
        """PEP 503: case-insensitive, and `_` is equivalent to `-`."""
        guard = _load_guard()
        pins = guard.parse_pins("AlibabaCloud_ECS20140526==7.11.8\n")
        self.assertIn("alibabacloud-ecs20140526", pins)

    def test_diff_pins_flags_a_dropped_pin(self):
        """The bug this guard exists for: a pin vanishes from the lock."""
        guard = _load_guard()
        committed = {"aiohttp": "3.14.5"}
        resolved = {"aiohttp": "3.14.5", "typing-extensions": "4.16.0"}
        missing, extra, changed = guard.diff_pins(committed, resolved)
        self.assertEqual(missing, ["typing-extensions"])
        self.assertEqual(extra, [])
        self.assertEqual(changed, [])

    def test_diff_pins_flags_a_stale_pin(self):
        """The mirror case: the lock keeps a pin the graph no longer needs."""
        guard = _load_guard()
        committed = {"aiohttp": "3.14.5", "typing-extensions": "4.16.0"}
        resolved = {"aiohttp": "3.14.5"}
        missing, extra, changed = guard.diff_pins(committed, resolved)
        self.assertEqual(missing, [])
        self.assertEqual(extra, ["typing-extensions"])
        self.assertEqual(changed, [])

    def test_diff_pins_flags_a_version_change(self):
        guard = _load_guard()
        missing, extra, changed = guard.diff_pins(
            {"aiohttp": "3.14.5"}, {"aiohttp": "3.14.6"}
        )
        self.assertEqual(missing, [])
        self.assertEqual(extra, [])
        self.assertEqual(changed, ["aiohttp"])

    def test_diff_pins_reports_no_drift_for_equal_sets(self):
        guard = _load_guard()
        pins = {"aiohttp": "3.14.5", "typing-extensions": "4.16.0"}
        self.assertEqual(guard.diff_pins(pins, dict(pins)), ([], [], []))

    def test_target_python_requires_the_header(self):
        """A lock with no header must not be silently accepted."""
        guard = _load_guard()
        self.assertIsNone(guard.target_python("aiohttp==3.14.5\n"))

    def test_target_python_reads_the_header(self):
        guard = _load_guard()
        text = (
            "# This file is autogenerated by pip-compile with Python 3.12\n"
            "# by the following command:\n"
        )
        self.assertEqual(guard.target_python(text), (3, 12))


class TestGuardIsWiredIn(unittest.TestCase):
    """The guard only helps if it actually runs."""

    def test_mise_has_a_check_lock_task(self):
        config = tomllib.loads(MISE_TOML.read_text())
        tasks = config.get("tasks", {})
        self.assertIn(
            "check-lock",
            tasks,
            "mise.toml must define a `check-lock` task so the lock drift "
            "check is runnable locally.",
        )
        self.assertIn(
            "check_requirements_lock.py",
            tasks["check-lock"]["run"],
            "the `check-lock` task must run scripts/check_requirements_lock.py.",
        )

    def test_lock_task_pins_pip_tools(self):
        """`mise run lock` must use the pinned pip-tools.

        The guard re-resolves with the same pip-tools version. If the
        lock task used whatever pip-tools was latest, a pip-tools release
        that changed the resolution (8.0.0 flips `--strip-extras` to the
        default) would make the two disagree.
        """
        config = tomllib.loads(MISE_TOML.read_text())
        run = config["tasks"]["lock"]["run"]
        self.assertIn(
            "pip-tools==",
            run,
            "the `lock` task must pin pip-tools to the version in "
            "requirements-dev.txt, so the lock and the guard resolve "
            "identically.",
        )

    def test_lint_workflow_runs_the_guard(self):
        """CI must run the guard on every pull request."""
        text = LINT_YML.read_text()
        self.assertIn(
            "requirements-lock:",
            text,
            ".github/workflows/lint.yml must define a `requirements-lock` job.",
        )
        self.assertIn(
            "scripts/check_requirements_lock.py",
            text,
            "the lint workflow must run scripts/check_requirements_lock.py.",
        )
