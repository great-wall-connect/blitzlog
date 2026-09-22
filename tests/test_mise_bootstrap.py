"""Regression tests for the `mise.toml` `[tasks.bootstrap]` and `[tools]`.

Issue #62 (Issue 3): the EC2 worker previously had no `mise run bootstrap`
hook to install the Python test/lint dependencies declared in
`requirements.txt` (which transitively pulls in `requirements-dev.txt`).
Without those, the agent could not run `pytest`, `ruff`, or `black` on the
worker — and the agent working around it with `pip install -r
requirements-dev.txt` violated `docs/BOOTSTRAP.md`'s "no self-healing"
rule. The bootstrap task is the project's single, reviewable declaration
of what the worker needs to be able to verify its own PRs.

A related gap: the agent could not invoke `mise run build` (which runs
`terraform init`) because `terraform` was not declared in `[tools]`. The
bootstrap task plus the `[tools] terraform = "1.9.8"` pin keep both the
test/lint and the Terraform paths in sync with CI (`1.9.8`, pinned rather
than `1.9.x` for lockstep).

These tests are static — they parse the file as TOML and assert structural
invariants, so a refactor that drops the bootstrap task, removes the
terraform pin, or re-targets the wrong requirements file is caught at PR
review time instead of after a wasted EC2 run.
"""

import re
import unittest
from pathlib import Path

import tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
MISE_TOML = REPO_ROOT / "mise.toml"
LINT_YML = REPO_ROOT / ".github" / "workflows" / "lint.yml"


class TestMiseBootstrapTask(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tomllib.loads(MISE_TOML.read_text())

    def test_bootstrap_task_exists(self):
        """The runtime (`lambda/scripts/_common.py:_install_toolchain_script`)
        greps `mise tasks --name-only` for the exact token `bootstrap` and
        runs it via `mise run bootstrap` if present. If the task is renamed
        or removed, every EC2 worker stops installing its test/lint
        dependencies silently — pytest/ruff/black become unavailable and
        `mise run test`/`mise run lint` fail with `command not found`.
        """
        tasks = self.data.get("tasks", {})
        self.assertIn(
            "bootstrap",
            tasks,
            "mise.toml must declare a [tasks.bootstrap] block — the EC2 "
            "user-data in lambda/scripts/_common.py runs `mise run bootstrap` "
            "if defined; without it pytest/ruff/black/pip-compile are never "
            "installed and the worker can't run `mise run test` or "
            "`mise run lint`.",
        )

    def test_bootstrap_task_uses_set_euo_pipefail(self):
        """The bootstrap task runs on every agent invocation, so a partial
        failure (e.g. pip install succeeds but the python symlink fails) must
        abort rather than silently leave the worker in a half-configured
        state. `set -euo pipefail` is the same defensive default as the
        user-data script itself (see `lambda/scripts/_common.py:103`)."""
        run = self.data["tasks"]["bootstrap"]["run"]
        self.assertIn(
            "set -euo pipefail",
            run,
            "[tasks.bootstrap] run must start with `set -euo pipefail` so a "
            "partial failure (pip install OK but symlink step fails, etc.) "
            "aborts the worker bootstrap rather than leaving a half-configured env",
        )

    def test_bootstrap_installs_runtime_requirements(self):
        """The bootstrap must pip-install requirements.txt (which transitively
        pulls in both lambda/requirements.txt and requirements-dev.txt, the
        latter providing pytest/ruff/black/pip-tools). Referencing only
        requirements-dev.txt would skip the Lambda runtime deps; referencing
        only lambda/requirements.txt would skip the test tools."""
        run = self.data["tasks"]["bootstrap"]["run"]
        self.assertIn(
            "requirements.txt",
            run,
            "[tasks.bootstrap] run must install `requirements.txt` — that "
            "file includes both lambda/requirements.txt (boto3, pyjwt, "
            "cryptography, requests) and requirements-dev.txt (pytest, ruff, "
            "black, pip-tools, python-multipart). Installing only one of "
            "the two halves leaves the worker unable to run the full "
            "test+lint suite.",
        )

    def test_bootstrap_creates_python_symlink(self):
        """Regression for issue #62: the bootstrap must expose
        `/usr/local/bin/python` (a symlink to `python3`) so that `python -m
        ruff`, `python -m pytest`, etc. work — not just `python3 -m ruff`.
        Several tools (pip-compile-generated metadata, pre-commit hooks,
        some test plugins) invoke `python` directly; without the symlink
        the bootstrap reports success but the next step fails with
        `python: command not found`."""
        run = self.data["tasks"]["bootstrap"]["run"]
        self.assertIn(
            "/usr/local/bin/python",
            run,
            "[tasks.bootstrap] run must create a `/usr/local/bin/python` "
            "symlink (e.g. `ln -sf $(command -v python3) /usr/local/bin/python`) "
            "so `python -m ruff`/`python -m pytest` work — not just "
            "`python3 -m ruff`.",
        )


class TestMiseToolsBlock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tomllib.loads(MISE_TOML.read_text())
        cls.tools = cls.data.get("tools", {})

    def test_tools_declares_python(self):
        """Python is the project's primary toolchain; mise must install it
        so the runtime tools (pytest, ruff, black, pip-compile, mise itself)
        can resolve on PATH. Without this entry every `python3 -m ...` call
        in the user-data hits the AL2023 system python3.9 (incompatible with
        PEP 604 unions used in the codebase) and fails."""
        self.assertIn(
            "python",
            self.tools,
            "[tools] must declare `python` — the user-data runs "
            "`python3 -m pip install ...` and `python3 -c '...'` at multiple "
            "points; without the mise-installed Python those calls hit the "
            "AL2023 system python3.9 and fail to import the codebase.",
        )

    def test_tools_declares_terraform_pinned(self):
        """Regression for issue #62: `mise run build` runs `terraform init`,
        but Terraform was not in [tools], so the agent (and CI's local
        `mise run build`) hit `which: no terraform in (...)`. Pin to
        `1.9.8` (the same version CI uses, pinned rather than `1.9.x` so a
        future Terraform release can't silently change `terraform plan`
        output between worker and CI).
        """
        self.assertIn(
            "terraform",
            self.tools,
            "[tools] must declare `terraform` — `mise run build` (line ~25) "
            "runs `terraform init`, and the agent invokes `mise run build` "
            "when verifying Terraform PRs. Without the pin the worker has "
            "no terraform on PATH and the build fails.",
        )
        self.assertEqual(
            self.tools["terraform"],
            "1.9.8",
            "[tools].terraform must be pinned to `1.9.8` to lockstep with "
            "CI (`.github/workflows/lint.yml` terraform_version). A `1.9.x` "
            "wildcard silently drifts between worker and CI.",
        )


class TestCiTerraformVersionMatchesMise(unittest.TestCase):
    def test_ci_terraform_version_matches_mise(self):
        """CI uses `hashicorp/setup-terraform@v4` with `terraform_version` to
        pin the Terraform version. The mise.toml `[tools] terraform` must be
        the same version — otherwise a `mise run build` on the worker could
        disagree with CI's `terraform validate` (different provider
        versions, different state-file format), and the bug only surfaces
        on the agent's run, not on the maintainer's laptop."""
        data = tomllib.loads(MISE_TOML.read_text())
        mise_tf = data.get("tools", {}).get("terraform")
        self.assertIsNotNone(mise_tf, "mise.toml must declare terraform in [tools]")

        lint = LINT_YML.read_text()
        match = re.search(r"terraform_version:\s*([^\s#]+)", lint)
        self.assertIsNotNone(
            match,
            ".github/workflows/lint.yml must declare `terraform_version:`",
        )
        ci_tf = match.group(1)
        self.assertEqual(
            ci_tf,
            mise_tf,
            f"CI terraform_version ({ci_tf}) must match mise.toml [tools].terraform "
            f"({mise_tf}) — otherwise the worker and CI run different Terraform "
            "versions and `terraform plan` output can diverge silently",
        )


if __name__ == "__main__":
    unittest.main()
