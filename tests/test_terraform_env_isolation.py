"""Static analysis tests for the multi-env Terraform layout.

These tests do NOT need AWS credentials. They parse the HCL/JSON in
infra/modules/core/iam.tf and assert that:

- The Lambda role for any single env does not reference the OTHER env's SSM prefix
- Resource names embed var.environment so prod and dev stacks never collide
- ec2:TerminateInstances is gated on the matching Environment tag
- Per-env ephemeral SSM params are env-scoped; per-user data (bot pool,
  local LLM config) is env-independent under /blitzlog/users/
- The user-pool module lives at infra/user-pool/ and creates params under
  /blitzlog/users/<login>/telegram/ (no env prefix)

If a refactor accidentally grants cross-env SSM access (e.g. by widening
the resource pattern to /blitzlog/*) or accidentally env-namespaces the
user-pool namespace, these tests catch it at PR review time instead of
after a bad apply.
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
USER_POOL_MAIN = REPO_ROOT / "infra" / "user-pool" / "main.tf"
USER_POOL_VARS = REPO_ROOT / "infra" / "user-pool" / "variables.tf"


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

    def test_user_pool_lives_under_infra_user_pool(self):
        """The user-pool module must live at infra/user-pool/ (standalone) and
        create SSM parameters under the env-independent /blitzlog/users/ namespace."""
        main_tf = USER_POOL_MAIN.read_text()
        vars_tf = USER_POOL_VARS.read_text()
        # No environment variable — user pool is shared across envs.
        self.assertNotIn(
            'variable "environment"',
            vars_tf,
            "user-pool variables.tf must NOT declare an environment variable — "
            "the user pool is shared across envs (both prod and dev Lambdas read "
            "the same /blitzlog/users/<login>/telegram/* parameters).",
        )
        # SSM paths use the literal /blitzlog/users/ root, not an env-prefixed one.
        self.assertIn(
            "/blitzlog/users/${var.owner_login}/telegram/pool/",
            main_tf,
            "user-pool main.tf must write bot tokens under /blitzlog/users/... "
            "(no env prefix)",
        )
        self.assertIn(
            "/blitzlog/users/${var.owner_login}/telegram/allowed-user-id",
            main_tf,
            "user-pool main.tf must write allowed-user-id under /blitzlog/users/... "
            "(no env prefix)",
        )
        # Defensive: nothing env-scoped should leak in.
        self.assertNotIn(
            "var.environment",
            main_tf,
            "user-pool main.tf must not reference var.environment",
        )

    def test_lambda_policy_can_read_user_bots(self):
        """The Lambda role must have explicit SSM access to /blitzlog/users/...

        Both prod and dev Lambdas need to read the same per-user bot pool and
        per-user local LLM config — these are env-independent on purpose.
        """
        body = _policy_body_for_role("lambda_policy", self.iam_tf)
        self.assertIn(
            "arn:aws:ssm:*:*:parameter/blitzlog/users",
            body,
            "Lambda policy must allow ssm:GetParameter on /blitzlog/users",
        )
        self.assertIn(
            "arn:aws:ssm:*:*:parameter/blitzlog/users/*",
            body,
            "Lambda policy must allow ssm:GetParameter on /blitzlog/users/*",
        )

    def test_ec2_agent_policy_can_read_user_local_llm(self):
        """The EC2 agent role must have explicit SSM access to local-llm config
        under /blitzlog/users/.../local-llm/*."""
        body = _policy_body_for_role("ec2_agent_policy", self.iam_tf)
        self.assertIn(
            "arn:aws:ssm:*:*:parameter/blitzlog/users/*/local-llm/*",
            body,
            "EC2 agent policy must allow ssm:GetParameter on /blitzlog/users/*/local-llm/*",
        )

    def test_iam_does_not_env_scope_user_pool_paths(self):
        """Per-user SSM ARNs in IAM policies must NOT include an env prefix.

        If a future refactor accidentally re-introduces env-namespacing for the
        user-pool namespace (e.g. ${local.ssm_root}/users/* instead of the
        literal /blitzlog/users/*), this test catches it.
        """
        for role_name in ("lambda_policy", "ec2_agent_policy"):
            body = _policy_body_for_role(role_name, self.iam_tf)
            for forbidden in (
                "${local.ssm_root}/users",
                "${local.ssm_user_pool_root}",
                "/blitzlog/prod/users",
                "/blitzlog/dev/users",
            ):
                self.assertNotIn(
                    forbidden,
                    body,
                    f"{role_name} policy references {forbidden!r} — user-pool "
                    "SSM paths must be env-independent (literal /blitzlog/users/...)",
                )

    def test_iam_policies_have_no_double_slash_resource_arns(self):
        """No IAM policy may contain 'parameter//' in any Resource string.

        A double slash in an SSM Resource ARN pattern is always wrong: SSM
        parameter names contain at most one '/' separator, and IAM glob
        matching is literal. This test catches the
        'parameter//blitzlog/<env>/ephemeral/*' shape that silently breaks
        ephemeral-token writes without producing a Terraform validation
        error. The most common cause is interpolating a local with a
        leading slash into an ARN template that already includes 'parameter/'.
        """
        for role in ("lambda_policy", "ec2_agent_policy"):
            body = _policy_body_for_role(role, self.iam_tf)
            self.assertNotIn(
                "parameter//",
                body,
                f"{role} policy contains 'parameter//' — a double slash in an "
                "SSM Resource ARN. This renders a policy that never matches the "
                "actual parameter path (e.g. 'parameter//blitzlog/dev/ephemeral/*' "
                "will not match the Lambda's write to '/blitzlog/dev/ephemeral/...'). "
                "Cause is usually a local with a leading slash interpolated into "
                "the ARN template.",
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
