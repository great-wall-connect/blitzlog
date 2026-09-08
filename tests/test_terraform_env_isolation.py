"""Static analysis tests for the multi-env Terraform layout.

These tests do NOT need AWS credentials. They parse the HCL/JSON in
infra/modules/core/iam.tf and assert that:

- The Lambda role for any single env does not reference the OTHER env's SSM prefix
- Both env-namespaced SSM prefixes are referenced via `${local.ssm_root}` etc.
- Resource names embed var.environment so prod and dev stacks never collide
- ec2:TerminateInstances is gated on the matching Environment tag
- The user-pool module requires environment and namespaces its SSM paths

If a refactor accidentally grants cross-env SSM access (e.g. by widening
the resource pattern to /blitzlog/*), these tests catch it at PR review
time instead of after a bad apply.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IAM_TF = REPO_ROOT / "infra" / "modules" / "core" / "iam.tf"
LAMBDA_TF = REPO_ROOT / "infra" / "modules" / "core" / "lambda.tf"
EC2_TF = REPO_ROOT / "infra" / "modules" / "core" / "ec2.tf"
ALERTING_TF = REPO_ROOT / "infra" / "modules" / "core" / "alerting.tf"
STORAGE_TF = REPO_ROOT / "infra" / "modules" / "core" / "storage.tf"
LOCALS_TF = REPO_ROOT / "infra" / "modules" / "core" / "locals.tf"
USER_POOL_MAIN = REPO_ROOT / "infra" / "modules" / "core" / "user-pool" / "main.tf"
USER_POOL_VARS = REPO_ROOT / "infra" / "modules" / "core" / "user-pool" / "variables.tf"


def _policy_body_for_role(role_resource_name: str, iam_tf: str) -> str:
    """Return the HCL body of the named role's `policy = jsonencode({...})` block.

    This is *HCL*, not JSON — we don't try to parse it as JSON because HCL
    uses `key = "value"` instead of JSON's `"key": "value"`. The callers
    below use regex over the HCL body to extract SSM ARN literals.
    """
    start_marker = f'resource "aws_iam_role_policy" "{role_resource_name}" {{'
    start_idx = iam_tf.find(start_marker)
    assert start_idx >= 0, f"could not find {start_marker}"
    body_open = iam_tf.find("{", start_idx)
    depth = 0
    i = body_open
    while i < len(iam_tf):
        c = iam_tf[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = iam_tf[body_open + 1 : i]
    jm = re.search(r"jsonencode\s*\(", body)
    assert jm, f"role {role_resource_name} has no jsonencode(...)"
    start = jm.end()
    depth = 0
    for j in range(start, len(body)):
        c = body[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return body[start : j + 1]
    raise AssertionError(f"unterminated jsonencode in role {role_resource_name}")


def _ssm_arn_literals_in_policy(role_resource_name: str, iam_tf: str) -> list[str]:
    """Return every `arn:aws:ssm:...` literal that appears in a role's policy.

    This catches both:
    - direct string literals: Resource = "arn:aws:ssm:...:parameter/blitzlog/prod/x"
    - HCL interpolations: Resource = "arn:aws:ssm:...:parameter/${local.ssm_root}/*"
      (we report the literal *prefix* up to the first `${` so callers can decide).
    """
    body = _policy_body_for_role(role_resource_name, iam_tf)
    arns = []
    # match "arn:aws:ssm:..." up to either a closing quote or a `${`
    for m in re.finditer(r'"(arn:aws:ssm:[^"${}]*)', body):
        arns.append(m.group(1))
    return arns


def _ssm_resource_arns(policy: dict) -> list[str]:
    """All SSM ARNs a policy's statements grant access to."""
    arns = []
    for stmt in policy.get("Statement", []):
        resources = stmt.get("Resource", [])
        if isinstance(resources, str):
            resources = [resources]
        for r in resources:
            if isinstance(r, str) and ":ssm:" in r:
                arns.append(r)
    return arns


# Re-implement the policy check using the HCL-level literal scan.
# (We don't parse the policy as JSON because the HCL body is not valid JSON.)


class TestEnvNamespacing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.iam_tf = IAM_TF.read_text()
        cls.lambda_tf = LAMBDA_TF.read_text()
        cls.ec2_tf = EC2_TF.read_text()
        cls.alerting_tf = ALERTING_TF.read_text()
        cls.storage_tf = STORAGE_TF.read_text()
        cls.locals_tf = LOCALS_TF.read_text()

    def test_lambda_policy_no_cross_env_ssm_reads(self):
        """The Lambda policy must not contain a literal /blitzlog/<other-env> path.

        All Lambda SSM resource ARNs must flow through `${local.ssm_root}` (or
        another env-scoped local) so prod and dev IAM policies are
        structurally identical and the env decides the prefix.
        """
        arns = _ssm_arn_literals_in_policy("lambda_policy", self.iam_tf)
        self.assertTrue(arns, "Lambda policy has no SSM ARN literals")

        for arn in arns:
            self.assertNotIn(
                "/blitzlog/prod/",
                arn,
                f"Lambda policy hardcodes /blitzlog/prod/ in {arn!r} — "
                "must reference ${{local.ssm_root}} so a dev apply doesn't inherit prod paths",
            )
            self.assertNotIn(
                "/blitzlog/dev/",
                arn,
                f"Lambda policy hardcodes /blitzlog/dev/ in {arn!r} — "
                "must reference ${{local.ssm_root}}",
            )

    def test_ec2_agent_policy_no_cross_env_ssm_reads(self):
        """Same check for the EC2 agent role."""
        arns = _ssm_arn_literals_in_policy("ec2_agent_policy", self.iam_tf)
        self.assertTrue(arns, "EC2 agent policy has no SSM ARN literals")

        for arn in arns:
            self.assertNotIn("/blitzlog/prod/", arn)
            self.assertNotIn("/blitzlog/dev/", arn)

    def test_lambda_policy_uses_env_scoped_locals(self):
        """The Lambda policy must reference at least one env-scoped local so a
        dev apply produces a structurally identical but env-prefixed policy.

        The actual references in this codebase are ${local.ssm_user_pool_root}
        and ${local.ssm_ephemeral_root} (no direct ${local.ssm_root} — GitHub
        App creds are referenced via ${aws_ssm_parameter.<name>.arn}, which is
        also env-scoped transitively).
        """
        body = _policy_body_for_role("lambda_policy", self.iam_tf)
        any_local = any(
            f"${{local.{name}}}" in body
            for name in (
                "ssm_root",
                "ssm_user_pool_root",
                "ssm_ephemeral_root",
                "ssm_user_llm_pattern",
            )
        )
        self.assertTrue(
            any_local,
            "Lambda policy must reference at least one ${local.ssm_*} so a dev "
            "apply produces a dev-only policy (currently no env-scoped local is used)",
        )

    def test_resource_names_embed_environment(self):
        """Every env-managed resource must have `blitzlog-` + env- in its name.

        Storage uses a local helper for the STT models bucket, so the literal
        prefix doesn't appear directly in storage.tf — but the helper itself
        does include the env.
        """
        for label, tf_file in (
            ("lambda.tf", self.lambda_tf),
            ("ec2.tf", self.ec2_tf),
            ("alerting.tf", self.alerting_tf),
            ("iam.tf", self.iam_tf),
        ):
            self.assertIn(
                "blitzlog-${var.environment}-",
                tf_file,
                f"{label} is missing the `blitzlog-${{var.environment}}-` name prefix",
            )
        # storage.tf uses local.stt_models_bucket (which contains the env), and
        # the agent_logs bucket name is user-supplied and must remain so for
        # bucket-name-globally-unique reasons. Verify the env flows through the
        # local helper in locals.tf.
        self.assertIn(
            "blitzlog-${var.environment}-stt-models",
            self.locals_tf,
            "locals.tf must derive the stt_models bucket name from var.environment",
        )

    def test_environment_variable_is_validated(self):
        """The environment variable must restrict input to safe characters."""
        variables_tf = (
            REPO_ROOT / "infra" / "modules" / "core" / "variables.tf"
        ).read_text()
        match = re.search(
            r'variable\s+"environment"\s*\{(?P<body>.*?)\n\}\n',
            variables_tf,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "environment variable block not found")
        body = match.group("body")
        self.assertIn(
            "validation", body, "environment variable must declare a validation block"
        )
        self.assertIn(
            "regex(", body, "environment variable validation must use regex()"
        )

    def test_terminate_instances_condition_scoped_to_environment(self):
        """ec2:TerminateInstances must require the matching Environment tag."""
        match = re.search(
            r"ec2:TerminateInstances.*?Condition\s*=\s*\{(?P<body>.*?)\n\s*\}\s*\n\s*\},",
            self.iam_tf,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "TerminateInstances policy missing Condition block")
        body = match.group("body")
        self.assertIn(
            "ec2:ResourceTag/Environment",
            body,
            "TerminateInstances condition must require ec2:ResourceTag/Environment",
        )
        self.assertIn(
            "var.environment",
            body,
            "TerminateInstances condition must reference var.environment",
        )

    def test_user_pool_namespaced_under_environment(self):
        """The user-pool module must require environment and prefix SSM paths."""
        main_tf = USER_POOL_MAIN.read_text()
        vars_tf = USER_POOL_VARS.read_text()
        self.assertIn("var.environment", main_tf)
        self.assertIn("/blitzlog/${var.environment}", main_tf)
        self.assertIn(
            'variable "environment"',
            vars_tf,
            "user-pool variables.tf must declare the environment variable",
        )

    def test_storage_uses_data_sources_not_resources(self):
        """storage.tf must declare both buckets as data sources, not resources.

        Both buckets are owned by the infra/bootstrap/ stack. Each env (prod,
        dev, ...) references them by name via `data "aws_s3_bucket"` so a
        second `terraform apply` does not try to recreate a bucket the first
        env already owns (BucketAlreadyOwnedByYou).
        """
        for bucket in ("agent_logs", "stt_models"):
            self.assertRegex(
                self.storage_tf,
                rf'data\s+"aws_s3_bucket"\s+"{bucket}"',
                f"storage.tf must reference `{bucket}` bucket via a data source",
            )
        # Per-env stacks must NOT own bucket-config resources (encryption,
        # versioning, lifecycle, public-access-block) — those live in
        # infra/bootstrap/.
        for forbidden in (
            'resource "aws_s3_bucket" "agent_logs"',
            'resource "aws_s3_bucket" "stt_models"',
            "aws_s3_bucket_versioning",
            "aws_s3_bucket_public_access_block",
            "aws_s3_bucket_lifecycle_configuration",
        ):
            self.assertNotIn(
                forbidden,
                self.storage_tf,
                f"storage.tf must not contain {forbidden!r} — bucket config is owned by infra/bootstrap/",
            )

    def test_bootstrap_stack_exists_and_owns_buckets(self):
        """The infra/bootstrap/ stack must exist and contain the bucket resources.

        Without it, per-env `terraform apply` fails because the data sources
        point at non-existent bucket names.
        """
        bootstrap_main = (REPO_ROOT / "infra" / "bootstrap" / "main.tf").read_text()
        self.assertIn(
            'resource "aws_s3_bucket" "agent_logs"',
            bootstrap_main,
            "infra/bootstrap/main.tf must own the agent_logs bucket",
        )
        self.assertIn(
            'resource "aws_s3_bucket" "stt_models"',
            bootstrap_main,
            "infra/bootstrap/main.tf must own the stt_models bucket",
        )
        # The bootstrap stack must configure the same hardening the old
        # per-env stack did (encryption, versioning, PAB, lifecycle).
        for required in (
            "aws_s3_bucket_server_side_encryption_configuration",
            "aws_s3_bucket_versioning",
            "aws_s3_bucket_public_access_block",
            "aws_s3_bucket_lifecycle_configuration",
        ):
            self.assertIn(
                required,
                bootstrap_main,
                f"infra/bootstrap/main.tf must configure {required} on the shared buckets",
            )


if __name__ == "__main__":
    unittest.main()
