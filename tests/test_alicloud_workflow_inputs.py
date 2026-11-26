"""Guards for the Alicloud legs of the deploy workflows.

Two bugs had the same shape: an Alicloud step read a value that was
never put in its environment.

* The bake job's "Resolve Alicloud bake network + source image" step
  read ``$ALICLOUD_VPC_ID`` / ``$ALICLOUD_VSWITCH_ID`` /
  ``$ALICLOUD_EIP_ID`` from the shell, but ``vars.*`` is NOT an
  environment variable in Actions — each one has to be mapped into the
  step's ``env:``. Without that the step reports them as "not set"
  even though they are.
* The deploy leg's "Export Alicloud credentials" step never wrote the
  aliyun CLI profile, so every ``aliyun oos ...`` call that followed
  ran unauthenticated ("profile configuration failed") and the step
  misreported the secret it wanted as missing.

Both were invisible to review and surfaced only as a CI run failing
with a misleading message, so they are pinned here.

The shell syntax of every step is checked too: a step whose script
does not parse fails the run with "syntax error near unexpected
token" — which is exactly what a botched edit to one of these
workflows looks like.
"""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_YML = REPO_ROOT / ".github/workflows/release.yml"
APPLY_DEV_YML = REPO_ROOT / ".github/workflows/terraform-apply-dev.yml"


def _shell_variables_read(run: str) -> set[str]:
    """The shell variables a script reads WITHOUT a default.

    A `${VAR:-default}` reference is fine — it always yields a value.
    A bare `$VAR` / `${VAR}` is not: the script gets an empty string
    unless something put it in the environment.
    """
    assigned = set(
        re.findall(
            r"(?m)^\s*(?:export\s+|declare\s+(?:-\w+\s+)?)?([A-Z_][A-Z0-9_]*)\s*=",
            run,
        )
    )
    used: set[str] = set()
    for name, modifier, bare in re.findall(
        r"\$\{([A-Z_][A-Z0-9_]*)([^}]*)\}|\$([A-Z_][A-Z0-9_]*)", run
    ):
        if bare:
            used.add(bare)
        elif modifier.startswith((":-", ":=")):
            continue  # has a default
        else:
            used.add(name)
    return used - assigned


class TestAlicloudWorkflowStepsGetTheirInputs(unittest.TestCase):
    """Every value an Alicloud step reads must reach it — through the
    step's `env:`, the job's, or the runner's own."""

    # Variables the runner provides; a step never has to map them.
    SAFE: frozenset[str] = frozenset(
        {
            "BASH_ENV",
            "CI",
            "GITHUB_ACTIONS",
            "GITHUB_ENV",
            "GITHUB_OUTPUT",
            "GITHUB_PATH",
            "GITHUB_STEP_SUMMARY",
            # The runner provides it: the checkout's root, used to
            # address a repo script by an absolute path.
            "GITHUB_WORKSPACE",
            "HOME",
            "PATH",
            "PWD",
            "RUNNER_OS",
            "RUNNER_TEMP",
            "RUNNER_TOOL_CACHE",
            "SHELL",
            "TMPDIR",
        }
    )

    @classmethod
    def setUpClass(cls):
        cls.workflows = {
            "release.yml": yaml.safe_load(RELEASE_YML.read_text()),
            "terraform-apply-dev.yml": yaml.safe_load(APPLY_DEV_YML.read_text()),
        }

    def _steps(self):
        for wf_name, wf in self.workflows.items():
            for job_name, job in (wf.get("jobs") or {}).items():
                for step in job.get("steps") or []:
                    yield wf_name, job_name, step

    def test_alicloud_steps_get_the_values_they_read(self):
        problems = []
        for wf_name, job_name, step in self._steps():
            run = step.get("run") or ""
            if (
                "ALICLOUD_" not in run
                and "alicloud" not in (step.get("name") or "").lower()
            ):
                continue
            job = self.workflows[wf_name]["jobs"][job_name]
            provided = set(job.get("env") or {}) | set(step.get("env") or {})
            for var in sorted(_shell_variables_read(run) - provided - self.SAFE):
                problems.append(
                    f"{wf_name}:{job_name}: step {step.get('name')!r} reads "
                    f"${var} but nothing provides it"
                )
        self.assertEqual(
            problems,
            [],
            "an Alicloud step reads a variable that is never put in its "
            "environment (vars.* is not an env var in Actions):\n  "
            + "\n  ".join(problems),
        )

    def test_alicloud_legs_write_the_aliyun_cli_profile(self):
        """The CLI does not read the `ALIBABA_CLOUD_*` env vars — it
        needs a profile, and for STS credentials it must be in
        "StsToken" mode. A leg that calls `aliyun ...` without writing
        one runs unauthenticated and the step misreports the secret it
        wanted as missing."""
        problems = []
        for wf_name, job_name, step in self._steps():
            run = step.get("run") or ""
            if "aliyun " not in run:
                continue
            job_text = yaml.safe_dump(self.workflows[wf_name]["jobs"][job_name])
            if "config.json" not in job_text and "configure set" not in job_text:
                problems.append(
                    f"{wf_name}:{job_name}: step {step.get('name')!r} calls the "
                    "aliyun CLI but no step in the job writes its profile"
                )
        self.assertEqual(
            problems,
            [],
            "an Alicloud step calls the aliyun CLI with no profile in the "
            "job:\n  " + "\n  ".join(problems),
        )

    def test_every_step_script_is_valid_shell(self):
        """A step whose script does not parse fails the run with
        "syntax error near unexpected token" — a botched edit shows
        up as that, with nothing pointing at the workflow."""
        problems = []
        for wf_name, job_name, step in self._steps():
            run = step.get("run")
            if not run:
                continue
            with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
                fh.write(run)
                path = fh.name
            try:
                result = subprocess.run(
                    ["bash", "-n", path], capture_output=True, text=True, check=False
                )
            finally:
                Path(path).unlink(missing_ok=True)
            if result.returncode != 0:
                problems.append(
                    f"{wf_name}:{job_name}: step {step.get('name')!r} is not "
                    f"valid shell: {result.stderr.strip()}"
                )
        self.assertEqual(
            problems,
            [],
            "a workflow step's script does not parse:\n  " + "\n  ".join(problems),
        )

    def test_the_reader_is_addressed_by_an_absolute_path(self):
        """A step that `cd`s anywhere makes a relative path resolve
        somewhere else.

        The aliyun CLI install does `cd /tmp` — and the render step
        ran the reader afterwards as `python3 scripts/...`, which
        resolved to /tmp/scripts/... and failed with "can't open
        file". The path is absolute for that reason."""
        for path in (RELEASE_YML, APPLY_DEV_YML):
            text = path.read_text()
            self.assertNotIn(
                "python3 scripts/oos_read_parameter.py",
                text,
                f"{path.name} must address the reader by an absolute "
                "path — a step that changes directory would break it",
            )
            self.assertIn(
                'python3 "$GITHUB_WORKSPACE/scripts/oos_read_parameter.py"',
                text,
                f"{path.name} must address the reader by an absolute path",
            )


class TestTheApplyDevWorkflowCanApplyTheAlicloudStack(unittest.TestCase):
    """The fast feedback path: terraform-apply-dev.yml must be
    able to run the Alicloud dev stack, so it does not have to
    wait for the AWS AMI bake."""

    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(APPLY_DEV_YML.read_text())

    def test_the_workflow_can_apply_the_alicloud_stack(self):
        job = self.workflow["jobs"]["apply-dev"]
        self.assertIn(
            "BLITZLOG_PROVIDER",
            job.get("env") or {},
            "the job must select the cloud from vars.BLITZLOG_PROVIDER",
        )
        steps = job.get("steps") or []
        names = [s.get("name") or "" for s in steps]
        self.assertIn(
            "Configure Alicloud credentials (OIDC) for terraform",
            names,
            "the workflow must be able to authenticate to Alicloud",
        )
        self.assertIn(
            "Render TF_VAR_* env from OOS",
            names,
            "the Alicloud secrets come from OOS, not SSM",
        )
        for step in steps:
            if (step.get("name") or "").startswith("terraform "):
                self.assertEqual(
                    step.get("working-directory"),
                    "infra/${{ env.BLITZLOG_PROVIDER }}/dev",
                    "the terraform steps must run in the selected cloud's " "tree",
                )


if __name__ == "__main__":
    unittest.main()
