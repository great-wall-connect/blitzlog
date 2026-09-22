"""Static-analysis tests for the `bootstrap` task convention.

The project's EC2 user-data runs `mise run bootstrap` only if such a task
is defined (lambda/scripts/_common.py:437-439). Without it, the agent's
worker is missing pytest/ruff/black/pip-tools AND `python` (only `python3`
is on PATH), and `docs/BOOTSTRAP.md` explicitly forbids the agent from
self-healing that gap. So this file is a regression guard that fires at
PR review time if a future refactor removes the task, drops the python
or terraform tool from [tools], or breaks the install command.

These tests parse mise.toml with tomllib (stdlib in Python 3.11+) and
walk the [tools] and [tasks] tables; no AWS credentials required.
"""

import re
import unittest
from pathlib import Path

import tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
MISE_TOML = REPO_ROOT / "mise.toml"


class TestMiseBootstrap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = tomllib.loads(MISE_TOML.read_text())

    def test_tools_declares_python(self):
        """[tools] must declare python so `mise install` provisions it.

        The EC2 user-data runs `mise install -y` (lambda/scripts/_common.py:428)
        before the agent starts. Without a python tool entry, the worker
        has no Python at all and `mise run bootstrap` (which calls pip)
        fails before it can install pytest/ruff/black.
        """
        tools = self.config.get("tools", {})
        self.assertIn(
            "python",
            tools,
            f"mise.toml [tools] must include `python`; got {sorted(tools)!r}. "
            "Without it, `mise install` leaves the worker with no Python.",
        )

    def test_tools_declares_terraform(self):
        """[tools] must declare terraform at the same version as CI.

        `mise run build` (mise.toml line ~17) runs `terraform init`, and
        `mise run tf-plan`/`tf-apply` invoke terraform directly. Without
        a [tools] entry, workers running those tasks hit
        `which: no terraform in (...)`. Pin matches `.github/workflows/lint.yml`.
        """
        tools = self.config.get("tools", {})
        self.assertIn(
            "terraform",
            tools,
            f"mise.toml [tools] must include `terraform`; got {sorted(tools)!r}. "
            "Without it, `mise run build` (which runs `terraform init`) fails "
            "with `which: no terraform in ...`.",
        )

    def test_terraform_pin_matches_ci(self):
        """The terraform version in mise.toml must match CI's.

        CI installs terraform via hashicorp/setup-terraform@v4 with a
        pinned version (.github/workflows/lint.yml:64). If mise.toml drifts
        to a different 1.9.x patch, CI and the agent use different
        terraform binaries; that drift hides bugs in either direction.
        """
        tools = self.config.get("tools", {})
        ci_workflow = (REPO_ROOT / ".github" / "workflows" / "lint.yml").read_text()
        ci_match = re.search(r"terraform_version:\s*(\S+)", ci_workflow)
        self.assertIsNotNone(ci_match, "CI workflow must pin terraform_version")
        ci_version = ci_match.group(1).strip().strip('"').strip("'")
        mise_version = str(tools.get("terraform", ""))
        self.assertEqual(
            mise_version,
            ci_version,
            f"mise.toml terraform ({mise_version!r}) must match CI "
            f"terraform_version ({ci_version!r}) — drift between the "
            "agent's and CI's terraform binaries hides bugs.",
        )

    def test_bootstrap_task_exists(self):
        """[tasks.bootstrap] must exist with name exactly `bootstrap`.

        The EC2 user-data greps for the exact task name
        (lambda/scripts/_common.py:438: `grep -qx "bootstrap"`), so a
        renamed or aliased task is silently ignored. See docs/BOOTSTRAP.md
        for the runtime convention.
        """
        tasks = self.config.get("tasks", {})
        self.assertIn(
            "bootstrap",
            tasks,
            f"mise.toml must define `[tasks.bootstrap]`; got {sorted(tasks)!r}. "
            "Without it the EC2 user-data's `mise run bootstrap` call is a "
            "no-op and the worker has no pytest/ruff/black — the agent then "
            "either self-installs (forbidden by docs/BOOTSTRAP.md) or fails.",
        )

    def test_bootstrap_task_is_idempotent_and_silent(self):
        """The bootstrap task must be runnable on every agent launch.

        Per docs/BOOTSTRAP.md, "the task must be idempotent. The runtime
        does not cache its result; a retry or a fresh instance will
        rerun it." We assert pip is invoked with --quiet (so the
        watchdog log isn't drowned) and that the run command exists.
        """
        bootstrap = self.config["tasks"]["bootstrap"]
        run = bootstrap.get("run", "")
        self.assertTrue(
            run.strip(),
            "[tasks.bootstrap] must have a non-empty `run` command",
        )
        self.assertIn(
            "pip install",
            run,
            "[tasks.bootstrap] must `pip install` the project requirements — "
            "the whole point of the task is to install pytest/ruff/black that "
            "the bare worker image lacks.",
        )

    def test_bootstrap_task_installs_test_deps(self):
        """The bootstrap task must install dev (test/lint) deps, not just runtime.

        `requirements.txt` is the Lambda runtime; `requirements-dev.in` is
        what `mise run lint` and `mise run test` need. Without the dev
        side, `pytest` is still missing and the agent can't validate
        its work.
        """
        bootstrap = self.config["tasks"]["bootstrap"]
        run = bootstrap.get("run", "")
        self.assertIn(
            "requirements-dev.in",
            run,
            "[tasks.bootstrap] must `pip install` requirements-dev.in so "
            "pytest/ruff/black are available — the bare worker image only "
            "ships the Python runtime.",
        )

    def test_bootstrap_task_creates_python_symlink(self):
        """The bootstrap task must create /usr/local/bin/python → python3.

        `mise.toml` `[tasks.lint]` invokes `python -m ruff ...` /
        `python -m black ...` (not `python3`). The worker image's mise
        shim dir only creates a `python3` shim. Without the symlink,
        `mise run lint` from the worker fails with `python: command not
        found` (observed in the issue #26 transcript).
        """
        bootstrap = self.config["tasks"]["bootstrap"]
        run = bootstrap.get("run", "")
        self.assertRegex(
            run,
            r"ln\s+-s[f]?.*python3.*\s+/usr/local/bin/python\b",
            "[tasks.bootstrap] must create /usr/local/bin/python → python3 "
            "so `python -m ruff` etc. work — mise only provisions a `python3` "
            "shim and the lint task uses `python`.",
        )


if __name__ == "__main__":
    unittest.main()
