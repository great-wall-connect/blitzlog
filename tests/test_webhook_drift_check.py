"""Static analysis tests for the GitHub webhook secret drift check (issue #30).

These tests do NOT need AWS credentials or GitHub. They assert that:

- The core module exposes the two opt-in variables with the right defaults
  (token null, repos []) so a stack without drift-check tfvars is byte-identical
  to a stack that never had this feature.
- The prod/dev wrappers forward both variables to the core module so an operator
  setting `github_webhook_check_*` in tfvars is not silently dropped.
- The `terraform_data.webhook_drift_check` resource exists, is gated on both
  variables being set (count = 0 when either is empty), and depends on the SSM
  parameter being written first — otherwise the script reads the OLD secret.
- The drift-check script itself exists, is executable, sets `set -u`, and exits
  0 unconditionally so drift never blocks an apply.
- The prod/dev terraform.tfvars.example documents the opt-in flag.
- The README troubleshooting section references the pre-flight sub-step and the
  Monitoring section mentions the apply-time warning emission.

If a refactor accidentally inverts the opt-in (e.g. defaults the token to
"required" so every apply is suddenly forced to provide one), removes the
script, or removes the WARNING-on-drift path, these tests catch it at PR
review time instead of after a silent regression.
"""

import os
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_VARIABLES_TF = REPO_ROOT / "infra" / "modules" / "core" / "variables.tf"
CORE_WEBHOOK_DF = REPO_ROOT / "infra" / "modules" / "core" / "webhook_drift_check.tf"
CORE_LOCALS_TF = REPO_ROOT / "infra" / "modules" / "core" / "locals.tf"
DRIFT_CHECK_SCRIPT = (
    REPO_ROOT / "infra" / "modules" / "core" / "scripts" / "check_webhook_secret.sh"
)
PROD_VARIABLES_TF = REPO_ROOT / "infra" / "prod" / "variables.tf"
PROD_MAIN_TF = REPO_ROOT / "infra" / "prod" / "main.tf"
PROD_TFVARS_EXAMPLE = REPO_ROOT / "infra" / "prod" / "terraform.tfvars.example"
DEV_VARIABLES_TF = REPO_ROOT / "infra" / "dev" / "variables.tf"
DEV_MAIN_TF = REPO_ROOT / "infra" / "dev" / "main.tf"
DEV_TFVARS_EXAMPLE = REPO_ROOT / "infra" / "dev" / "terraform.tfvars.example"
README_MD = REPO_ROOT / "README.md"


def _variable_block(tf_text: str, var_name: str) -> str:
    """Return the body of the named `variable "<name>" { ... }` block.

    Returns "" if the variable is not declared. Walks brace depth so a variable
    whose description contains a `}` character (none today, but defensive) does
    not desync the parser.
    """
    marker = f'variable "{var_name}" {{'
    start = tf_text.find(marker)
    if start < 0:
        return ""
    body_open = tf_text.find("{", start)
    depth = 0
    for i in range(body_open, len(tf_text)):
        c = tf_text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return tf_text[body_open + 1 : i]
    return ""


class TestDriftCheckVariables(unittest.TestCase):
    """The two opt-in variables must exist in the core module with safe defaults."""

    @classmethod
    def setUpClass(cls):
        cls.core_variables_tf = CORE_VARIABLES_TF.read_text()
        cls.prod_variables_tf = PROD_VARIABLES_TF.read_text()
        cls.dev_variables_tf = DEV_VARIABLES_TF.read_text()

    def test_token_variable_is_opt_in(self):
        """Token must default to null + sensitive so a stack that never sets
        it does not silently carry an empty token through every apply."""
        body = _variable_block(self.core_variables_tf, "github_webhook_check_token")
        self.assertTrue(
            body, "core variables.tf must declare github_webhook_check_token"
        )
        self.assertIn(
            "default     = null",
            body,
            "github_webhook_check_token must default to null so the check is opt-in",
        )
        self.assertIn(
            "sensitive   = true",
            body,
            "github_webhook_check_token must be marked sensitive — it's a GitHub PAT",
        )

    def test_repos_variable_is_opt_in(self):
        """Repos must default to an empty list (not null, not a placeholder)."""
        body = _variable_block(self.core_variables_tf, "github_webhook_check_repos")
        self.assertTrue(
            body, "core variables.tf must declare github_webhook_check_repos"
        )
        self.assertIn(
            "type        = list(string)",
            body,
            "github_webhook_check_repos must be list(string) so empty [] disables the check",
        )
        self.assertIn(
            "default     = []",
            body,
            "github_webhook_check_repos must default to [] so the check is opt-in",
        )

    def test_prod_wrapper_forwards_both_variables(self):
        """infra/prod must pass both variables through to the core module —
        otherwise an operator setting them in tfvars is silently dropped."""
        main_tf = PROD_MAIN_TF.read_text()
        self.assertRegex(
            main_tf,
            r"github_webhook_check_token\s*=\s*var\.github_webhook_check_token",
            "infra/prod/main.tf must forward github_webhook_check_token to the core module",
        )
        self.assertRegex(
            main_tf,
            r"github_webhook_check_repos\s*=\s*var\.github_webhook_check_repos",
            "infra/prod/main.tf must forward github_webhook_check_repos to the core module",
        )

    def test_dev_wrapper_forwards_both_variables(self):
        """infra/dev must pass both variables through to the core module."""
        main_tf = DEV_MAIN_TF.read_text()
        self.assertRegex(
            main_tf,
            r"github_webhook_check_token\s*=\s*var\.github_webhook_check_token",
            "infra/dev/main.tf must forward github_webhook_check_token to the core module",
        )
        self.assertRegex(
            main_tf,
            r"github_webhook_check_repos\s*=\s*var\.github_webhook_check_repos",
            "infra/dev/main.tf must forward github_webhook_check_repos to the core module",
        )

    def test_prod_wrapper_declares_both_variables(self):
        body = _variable_block(self.prod_variables_tf, "github_webhook_check_token")
        self.assertTrue(
            body,
            "infra/prod/variables.tf must declare github_webhook_check_token so it can be set in tfvars",
        )
        self.assertIn(
            "default     = null",
            body,
            "infra/prod/variables.tf must keep the opt-in null default",
        )
        body = _variable_block(self.prod_variables_tf, "github_webhook_check_repos")
        self.assertTrue(
            body,
            "infra/prod/variables.tf must declare github_webhook_check_repos so it can be set in tfvars",
        )
        self.assertIn(
            "default     = []",
            body,
            "infra/prod/variables.tf must keep the opt-in empty-list default",
        )

    def test_dev_wrapper_declares_both_variables(self):
        body = _variable_block(self.dev_variables_tf, "github_webhook_check_token")
        self.assertTrue(
            body,
            "infra/dev/variables.tf must declare github_webhook_check_token",
        )
        self.assertIn("default     = null", body)
        body = _variable_block(self.dev_variables_tf, "github_webhook_check_repos")
        self.assertTrue(
            body,
            "infra/dev/variables.tf must declare github_webhook_check_repos",
        )
        self.assertIn("default     = []", body)


class TestDriftCheckResource(unittest.TestCase):
    """The terraform_data resource must gate on both variables and depend on
    the SSM parameter being written first."""

    @classmethod
    def setUpClass(cls):
        cls.drift_tf = CORE_WEBHOOK_DF.read_text()

    def test_resource_is_terraform_data(self):
        """Use terraform_data (not null_resource) so the replacement_trigger
        metadata is generated correctly under modern Terraform versions."""
        self.assertRegex(
            self.drift_tf,
            r'resource\s+"terraform_data"\s+"webhook_drift_check"',
            "core module must declare a terraform_data named webhook_drift_check",
        )

    def test_count_gates_on_both_variables(self):
        """The resource must not be created when either variable is empty —
        otherwise the script would run with no token / no repos and silently
        produce zero information."""
        match = re.search(
            r"count\s*=\s*\((?P<body>.*?)\)\s*\?\s*1\s*:\s*0",
            self.drift_tf,
            re.DOTALL,
        )
        self.assertIsNotNone(
            match,
            "webhook_drift_check must have a count expression that gates on both vars",
        )
        body = match.group("body")
        self.assertIn(
            "var.github_webhook_check_token",
            body,
            "count must depend on var.github_webhook_check_token",
        )
        self.assertIn(
            "var.github_webhook_check_repos",
            body,
            "count must depend on var.github_webhook_check_repos",
        )
        # Defensive: count must NOT be a plain `1` or `0` literal — that would
        # either run on every apply (privacy footgun) or never run (dead code).
        self.assertNotIn("count = 1", self.drift_tf.replace("count = (", ""))
        self.assertNotIn("count = 0", self.drift_tf.replace("count = (", ""))

    def test_depends_on_ssm_parameter(self):
        """The drift check must run AFTER the SSM parameter is written.

        Without depends_on, Terraform's parallelism can race the provisioner
        against the SSM write, and the script would probe with the OLD secret.
        """
        self.assertRegex(
            self.drift_tf,
            r"depends_on\s*=\s*\[\s*aws_ssm_parameter\.github_webhook_secret\s*\]",
            "webhook_drift_check must depends_on aws_ssm_parameter.github_webhook_secret "
            "so the script reads the just-applied secret, not the previous one",
        )

    def test_uses_local_ssm_path(self):
        """The SSM parameter name must come from local.ssm_github_webhook_secret_name
        so prod and dev scripts point at the right parameter without hardcoding."""
        self.assertIn(
            "local.ssm_github_webhook_secret_name",
            self.drift_tf,
            "webhook_drift_check must reference local.ssm_github_webhook_secret_name "
            "(not a literal /blitzlog/<env>/... string)",
        )

    def test_token_passes_through_environment_not_command(self):
        """The sensitive GitHub token MUST flow through `environment`, never the
        `command` string — otherwise it lands in the apply log."""
        self.assertIn(
            "BLITZLOG_GITHUB_TOKEN   = var.github_webhook_check_token",
            self.drift_tf,
            "webhook_drift_check must pass the token via the environment block",
        )
        # The command string must not interpolate the token directly.
        command_match = re.search(
            r'command\s*=\s*"([^"]*)"',
            self.drift_tf,
        )
        self.assertIsNotNone(command_match, "webhook_drift_check must have a command")
        command = command_match.group(1)
        self.assertNotIn(
            "var.github_webhook_check_token",
            command,
            "command string must NOT reference var.github_webhook_check_token "
            "directly — sensitive values must flow through `environment`",
        )
        # `nonsensitive(...)` would defeat the purpose by stripping the redaction.
        self.assertNotIn(
            "nonsensitive",
            self.drift_tf,
            "webhook_drift_check must NOT call nonsensitive(...) on the token — "
            "that strips the apply-log redaction",
        )


class TestDriftCheckScript(unittest.TestCase):
    """The bash script must exist, be executable, and follow the safety contract."""

    def test_script_exists(self):
        self.assertTrue(
            DRIFT_CHECK_SCRIPT.is_file(),
            f"drift-check script must exist at {DRIFT_CHECK_SCRIPT}",
        )

    def test_script_is_executable(self):
        self.assertTrue(
            os.access(DRIFT_CHECK_SCRIPT, os.X_OK),
            f"drift-check script {DRIFT_CHECK_SCRIPT} must be executable (chmod +x)",
        )

    def test_script_does_not_fail_on_substep(self):
        """The script must NOT use `set -e` — per-webhook failures must not
        suppress warnings for the rest of the repos. We assert it uses `set -u`
        (catches unset var typos) but not `set -e`."""
        text = DRIFT_CHECK_SCRIPT.read_text()
        self.assertIn(
            "set -u",
            text,
            "drift-check script must enable `set -u` to catch unset variable typos",
        )
        self.assertIsNone(
            re.search(r"^\s*set\s+-e\s*$", text, re.MULTILINE),
            "drift-check script must NOT use bare `set -e` — per-webhook failures "
            "should be handled locally, not abort the whole check",
        )

    def test_script_exits_zero(self):
        """Drift must NEVER fail an apply — exit 0 unconditionally."""
        text = DRIFT_CHECK_SCRIPT.read_text()
        self.assertIsNotNone(
            re.search(r"^\s*exit\s+0\s*$", text, re.MULTILINE),
            "drift-check script must end with `exit 0` so drift never blocks an apply",
        )

    def test_script_emits_warning_for_drift(self):
        """On a 401 response the script must print a WARNING naming the webhook."""
        text = DRIFT_CHECK_SCRIPT.read_text()
        self.assertIn(
            "WARNING:",
            text,
            "drift-check script must emit WARNING: lines so the operator sees drift in apply output",
        )
        self.assertIn(
            "401",
            text,
            "drift-check script must handle HTTP 401 (signature mismatch) explicitly",
        )

    def test_script_references_gh_api_fix_command(self):
        """The script should hand the operator an actionable fix command —
        `gh api -X PATCH repos/<owner>/<repo>/hooks/<id> ...`."""
        text = DRIFT_CHECK_SCRIPT.read_text()
        self.assertIn(
            "gh api -X PATCH repos/${repo}/hooks/${hook_id}",
            text,
            "drift-check script must print a `gh api -X PATCH` fix command so the "
            "operator can resolve drift without consulting docs",
        )


class TestDriftCheckDocs(unittest.TestCase):
    """The README and tfvars.example must document the opt-in flag."""

    def test_prod_tfvars_example_documents_drift_check(self):
        text = PROD_TFVARS_EXAMPLE.read_text()
        self.assertIn(
            "github_webhook_check_token",
            text,
            "infra/prod/terraform.tfvars.example must document github_webhook_check_token",
        )
        self.assertIn(
            "github_webhook_check_repos",
            text,
            "infra/prod/terraform.tfvars.example must document github_webhook_check_repos",
        )

    def test_dev_tfvars_example_documents_drift_check(self):
        text = DEV_TFVARS_EXAMPLE.read_text()
        self.assertIn(
            "github_webhook_check_token",
            text,
            "infra/dev/terraform.tfvars.example must document github_webhook_check_token",
        )
        self.assertIn(
            "github_webhook_check_repos",
            text,
            "infra/dev/terraform.tfvars.example must document github_webhook_check_repos",
        )

    def test_readme_troubleshooting_mentions_preflight(self):
        """The HMAC troubleshooting section must reference the new pre-flight
        check so operators find it when they hit a 401 (issue #30 follow-up)."""
        text = README_MD.read_text()
        # Find the HMAC signature mismatch heading.
        hm_start = text.find("### HMAC signature mismatch")
        self.assertGreaterEqual(
            hm_start,
            0,
            "README must contain the `### HMAC signature mismatch` troubleshooting heading",
        )
        # Find the next `###` heading so we know we stay inside the HMAC section.
        next_heading = text.find("\n### ", hm_start + 1)
        section = text[hm_start : next_heading if next_heading > 0 else len(text)]
        self.assertIn(
            "Pre-flight",
            section,
            "HMAC troubleshooting section must contain a Pre-flight sub-step that "
            "references the terraform apply drift-check warning",
        )
        self.assertIn(
            "github_webhook_check_token",
            section,
            "HMAC troubleshooting section must name github_webhook_check_token so "
            "operators know which tfvars to set to enable the check",
        )

    def test_readme_monitoring_mentions_drift_warning(self):
        """The Monitoring section must mention the apply-time warning emission."""
        text = README_MD.read_text()
        mon_start = text.find("## Monitoring")
        self.assertGreaterEqual(
            mon_start, 0, "README must contain the `## Monitoring` section"
        )
        next_heading = text.find("\n## ", mon_start + 1)
        section = text[mon_start : next_heading if next_heading > 0 else len(text)]
        self.assertIn(
            "drift",
            section.lower(),
            "Monitoring section must reference the drift warning so operators "
            "knowing where to capture apply-time drift signals in CI",
        )


if __name__ == "__main__":
    unittest.main()
