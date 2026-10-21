"""Static analysis tests for the multi-env Terraform layout.

These tests do NOT need AWS credentials. They parse the HCL/JSON in
infra/aws/modules/core/iam.tf and assert that:

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
IAM_TF = REPO_ROOT / "infra" / "aws" / "modules" / "core" / "iam.tf"
LAMBDA_TF = REPO_ROOT / "infra" / "aws" / "modules" / "core" / "lambda.tf"
EC2_TF = REPO_ROOT / "infra" / "aws" / "modules" / "core" / "ec2.tf"
ALERTING_TF = REPO_ROOT / "infra" / "aws" / "modules" / "core" / "alerting.tf"
STORAGE_TF = REPO_ROOT / "infra" / "aws" / "modules" / "core" / "storage.tf"
LOCALS_TF = REPO_ROOT / "infra" / "aws" / "modules" / "core" / "locals.tf"
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
            REPO_ROOT / "infra" / "aws" / "modules" / "core" / "variables.tf"
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

    def test_ec2_agent_policy_can_read_user_data(self):
        """The EC2 agent role must allow s3:GetObject on user-data/*.

        The Lambda writes one ephemeral bootstrap script per EC2 launch to
        s3://<bucket>/user-data/... (no env prefix — shared infra). The
        matching EC2 instance reads it via the user-data downloader
        (_build_s3_downloader_script does `aws s3 cp`). The <env>/*
        restriction elsewhere on the EC2 IAM is for *run logs*, not for
        user-data. Without this grant, cloud-init fails with
        `HeadObject 403` and the bootstrap script never runs.
        """
        body = _policy_body_for_role("ec2_agent_policy", self.iam_tf)
        self.assertIn(
            "${data.aws_s3_bucket.agent_logs.arn}/user-data/*",
            body,
            "EC2 agent policy must grant s3:GetObject on "
            "${data.aws_s3_bucket.agent_logs.arn}/user-data/* — the downloader "
            "script in _build_s3_downloader_script does an `aws s3 cp` of the "
            "Lambda-uploaded bootstrap.sh. Without this grant, cloud-init fails "
            "with `HeadObject 403` and /tmp/bootstrap.sh is never created.",
        )

    def test_lambda_policy_can_write_user_data(self):
        """The Lambda policy must allow s3:PutObject on user-data/*.

        The Lambda uploads the bootstrap script to s3://<bucket>/user-data/...
        (lambda/handler.py:350). Without s3:PutObject on that prefix, the
        upload fails before the EC2 instance is even launched.
        """
        body = _policy_body_for_role("lambda_policy", self.iam_tf)
        self.assertIn(
            "/user-data/*",
            body,
            "Lambda policy must grant s3:PutObject on user-data/* "
            "(handler.py:350 uploads the bootstrap script there)",
        )

    def test_storage_uses_data_sources_not_resources(self):
        """storage.tf must declare both buckets as data sources, not resources.

        Both buckets are owned by the infra/aws/bootstrap/ stack. Each env (prod,
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
        # infra/aws/bootstrap/.
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
                f"storage.tf must not contain {forbidden!r} — bucket config is owned by infra/aws/bootstrap/",
            )

    def test_bootstrap_stack_exists_and_owns_buckets(self):
        """The infra/aws/bootstrap/ stack must exist and contain the bucket resources.

        Without it, per-env `terraform apply` fails because the data sources
        point at non-existent bucket names.
        """
        bootstrap_main = (
            REPO_ROOT / "infra" / "aws" / "bootstrap" / "main.tf"
        ).read_text()
        self.assertIn(
            'resource "aws_s3_bucket" "agent_logs"',
            bootstrap_main,
            "infra/aws/bootstrap/main.tf must own the agent_logs bucket",
        )
        self.assertIn(
            'resource "aws_s3_bucket" "stt_models"',
            bootstrap_main,
            "infra/aws/bootstrap/main.tf must own the stt_models bucket",
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
                f"infra/aws/bootstrap/main.tf must configure {required} on the shared buckets",
            )

    def test_lambda_wires_spot_instance_types_env_var(self):
        """Issue #26: SPOT_INSTANCE_TYPES_JSON must be passed to the Lambda so
        operators can switch spot instance families per region without
        rebuilding the Lambda zip. The Lambda parses this env var in
        lambda/ec2.py::_load_spot_instance_types.

        If a future refactor drops the env var (or hardcodes the list back
        in the Lambda zip), the operator override in terraform.tfvars
        silently stops working — this test makes that drift visible.
        """
        self.assertIn(
            "SPOT_INSTANCE_TYPES_JSON",
            self.lambda_tf,
            "infra/aws/modules/core/lambda.tf must pass SPOT_INSTANCE_TYPES_JSON "
            "as a Lambda env var so operators can override spot instance "
            "families per region (issue #26)",
        )
        self.assertIn(
            "jsonencode(var.spot_instance_types)",
            self.lambda_tf,
            "SPOT_INSTANCE_TYPES_JSON must be jsonencode(var.spot_instance_types) "
            "so the Lambda parses a single ordered list from one env var",
        )
        # The variable must be declared in variables.tf so prod/dev layers
        # can override it in terraform.tfvars.
        variables_tf = (
            REPO_ROOT / "infra" / "aws" / "modules" / "core" / "variables.tf"
        ).read_text()
        self.assertIn(
            'variable "spot_instance_types"',
            variables_tf,
            "infra/aws/modules/core/variables.tf must declare spot_instance_types",
        )
        # Plumbed through both env layers so terraform.tfvars overrides
        # actually reach the core module.
        for layer in ("prod", "dev"):
            layer_main = (REPO_ROOT / "infra" / "aws" / layer / "main.tf").read_text()
            self.assertIn(
                "spot_instance_types = var.spot_instance_types",
                layer_main,
                f"infra/aws/{layer}/main.tf must pass spot_instance_types to the core module",
            )

    def test_log_upload_path_is_env_prefixed(self):
        """The host's watchdog.sh log-upload path must include
        `$S3_LOG_PREFIX` (which the lambda renders as
        `${BLITZLOG_ENV}/${repo}/issue/${N}/`) as the first key segment.

        Without the env prefix, the upload lands at
        `s3://<bucket>/logs/...`, which is outside the EC2
        agent role's `s3:PutObject` grant at iam.tf:191-200 (scoped to
        `${var.environment}/*`). The `|| true` on the `aws s3 cp` line then
        swallows the resulting `AccessDenied`, and the bash log line
        `Logs uploaded to S3` lies — no log is persisted.

        Issue #123 refactored the inline `aws s3 cp` calls into the
        `blitzlog_s3_put` helper from blitzlog_uploader.sh, so the
        S3 URL is now constructed by the helper from positional args
        rather than appearing as a single literal in the watchdog. The
        helper takes `(bucket, key, local_path)`, so the same
        `S3_LOGS_BUCKET` → `S3_LOG_PREFIX` ordering shows up across a
        line continuation rather than a single string literal. The
        regex below allows whitespace + bash line-continuations between
        the two names; the semantic invariant (bucket first, env-prefixed
        key second) is unchanged.
        """
        watchdog = (
            REPO_ROOT
            / "infra"
            / "aws"
            / "packer"
            / "scripts-docker-ubuntu"
            / "watchdog.sh"
        ).read_text()
        self.assertRegex(
            watchdog,
            r"\$\{S3_LOGS_BUCKET\}[\"'\s\\]*\$\{S3_LOG_PREFIX\}",
            "watchdog.sh log-upload must pass ${S3_LOG_PREFIX} (rendered by "
            "the lambda as ${BLITZLOG_ENV}/${repo}/issue/${N}/) as the key "
            "to blitzlog_s3_put (issue #123), so the destination falls "
            "inside the EC2 agent role's s3:PutObject grant "
            "(iam.tf:191-200, scoped to ${var.environment}/*).",
        )

    def test_ec2_agent_policy_grants_put_object_for_its_own_env_prefix(self):
        """The EC2 agent role must allow `s3:PutObject` on `${var.environment}/*`.

        This is the IAM half of the log-upload contract: the upload code
        (autonomous.py + assisted.py) prefixes keys with `$BLITZLOG_ENV/`,
        and this grant must match that prefix. If a future refactor narrows
        the resource pattern to e.g. a hard-coded `prod/*` literal or adds a
        `StringEquals` condition that excludes log-upload keys, the upload
        silently fails (swallowed by `|| true`).
        """
        body = _policy_body_for_role("ec2_agent_policy", self.iam_tf)
        self.assertIn(
            "s3:PutObject",
            body,
            "ec2_agent_policy must include s3:PutObject — without it, the "
            "watchdog's `aws s3 cp /var/log/backend-bootstrap.log ...` upload "
            "fails with AccessDenied and is silently swallowed by `|| true`.",
        )
        self.assertIn(
            "${data.aws_s3_bucket.agent_logs.arn}/${var.environment}/*",
            body,
            "ec2_agent_policy must grant on "
            "${data.aws_s3_bucket.agent_logs.arn}/${var.environment}/* — the "
            "log-upload code (autonomous.py + assisted.py) prefixes keys with "
            "$BLITZLOG_ENV/, so this is the path the runtime actually uses.",
        )


class TestAlicloudProviderDispatch(unittest.TestCase):
    """Static analysis tests for the Alicloud Terraform split (issue #126).

    The issue adds `infra/alicloud/{modules/core,dev,prod,bootstrap}/`
    and a per-env tfvars file. The dispatch is at the
    `.github/workflows/release.yml` level: `vars.BLITZLOG_PROVIDER`
    defaults to `aws`, the working-directory includes the provider
    prefix, and the backend switches from S3 to OSS.

    These tests guard the invariants of the split without touching
    AWS credentials or Alicloud credentials. They complement the
    AWS-side `TestEnvNamespacing` tests above.
    """

    REPO_ROOT = Path(__file__).resolve().parent.parent
    ALICLOUD_MODULE = REPO_ROOT / "infra" / "alicloud" / "modules" / "core"
    ALICLOUD_DEV = REPO_ROOT / "infra" / "alicloud" / "dev"
    ALICLOUD_PROD = REPO_ROOT / "infra" / "alicloud" / "prod"
    ALICLOUD_BOOTSTRAP = REPO_ROOT / "infra" / "alicloud" / "bootstrap"
    RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"

    def test_alicloud_module_directory_layout(self):
        """The Alicloud module tree must mirror the AWS one 1-to-1.

        Issue #126 acceptance criteria: each resource file in
        `infra/alicloud/modules/core/` has a top-of-file mapping
        comment pointing to the AWS analogue. We assert both the
        file existence and the comment presence.
        """
        expected_files = {
            "function_compute.tf": "infra/aws/modules/core/lambda.tf",
            "ecs.tf": "infra/aws/modules/core/ec2.tf",
            "ram.tf": "infra/aws/modules/core/iam.tf",
            "apigateway.tf": "infra/aws/modules/core/apigateway.tf",
            "oss.tf": "infra/aws/modules/core/storage.tf",
            "mns.tf": "infra/aws/modules/core/alerting.tf",
            "kms.tf": "infra/aws/modules/core/iam.tf",
            "variables.tf": "infra/aws/modules/core/variables.tf",
            "outputs.tf": "infra/aws/modules/core/outputs.tf",
            "locals.tf": "infra/aws/modules/core/locals.tf",
            "main.tf": "infra/aws/modules/core/main.tf",
        }
        for filename, aws_analogue in expected_files.items():
            path = self.ALICLOUD_MODULE / filename
            self.assertTrue(
                path.exists(),
                f"{path} must exist (analogue of {aws_analogue})",
            )
            # First 20 lines must mention the AWS analogue somewhere.
            head = "\n".join(path.read_text().splitlines()[:20])
            self.assertIn(
                aws_analogue,
                head,
                f"{filename} must reference its AWS analogue ({aws_analogue}) "
                f"in the top-of-file comment block, so a reader knows the mapping.",
            )

    def test_alicloud_dev_prod_have_no_count_gated_module(self):
        """The per-cloud directory layout removes the need for
        `count` on the `module "core"` call.

        The issue body's draft proposed a `local.provider` switch
        with `count = local.provider == "alicloud" ? 1 : 0`; the
        chosen layout (`infra/aws/<env>` vs `infra/alicloud/<env>`)
        makes the gating obsolete. This test guards the simpler
        shape: each per-cloud main.tf calls `module "core"` once
        with no `count`.
        """
        for layer_dir, env in (
            (self.ALICLOUD_DEV, "dev"),
            (self.ALICLOUD_PROD, "prod"),
        ):
            main = (layer_dir / "main.tf").read_text()
            self.assertIn(
                'module "core"',
                main,
                f"infra/alicloud/{env}/main.tf must call module 'core'",
            )
            # The block must NOT have a `count` argument on the
            # module call. We grep for `module "core" {` and
            # assert the body has no `count =` line.
            start = main.find('module "core"')
            end = main.find("}", start)
            body = main[start:end]
            self.assertNotIn(
                "count",
                body,
                f"infra/alicloud/{env}/main.tf must not count-gate the "
                f"module call (per-cloud directory layout makes it obsolete); "
                f"got:\n{body}",
            )

    def test_alicloud_dev_prod_terraform_tfvars_example_exists(self):
        """The per-env tfvars.example must exist for the operator to copy from.

        The release workflow's `terraform plan` step requires
        `<provider>.tfvars` to exist (it fails fast if not). The
        operator copies the `.tfvars.example` to the actual tfvars
        file. Same convention as the AWS path.
        """
        for layer_dir, env in (
            (self.ALICLOUD_DEV, "dev"),
            (self.ALICLOUD_PROD, "prod"),
        ):
            example = layer_dir / "alicloud.tfvars.example"
            self.assertTrue(
                example.exists(),
                f"infra/alicloud/{env}/alicloud.tfvars.example must exist "
                f"as the template the operator copies to alicloud.tfvars",
            )

    def test_alicloud_bootstrap_exists(self):
        """The Alicloud bootstrap stack must exist (mirrors
        `infra/aws/bootstrap/`) — both for the OSS buckets and as the
        future home of the Aliyun OIDC role for the GHA workflow.

        Issue #126 doesn't list a bootstrap file explicitly, but
        without one the per-env `data "alicloud_oss_buckets"`
        references would fail at `terraform apply` time (the
        operator would have to create the buckets manually). The
        bootstrap is the natural one-time setup.
        """
        for filename in ("main.tf", "variables.tf", "outputs.tf"):
            self.assertTrue(
                (self.ALICLOUD_BOOTSTRAP / filename).exists(),
                f"infra/alicloud/bootstrap/{filename} must exist",
            )

    def test_function_compute_sets_blitzlog_provider_alicloud(self):
        """The FC function's `environment_variables` block must set
        `BLITZLOG_PROVIDER = "alicloud"`.

        This is the runtime half of the dispatch: the lambda's
        provider factory (lambda/provider/__init__.py) reads this
        env var on cold start and picks `AlicloudCompute` /
        `AlicloudSecretStore` / `AlicloudObjectStore`. If the value
        is missing or wrong, the FC falls back to AWS and the
        runtime fails at the first AWS API call.
        """
        fc = (self.ALICLOUD_MODULE / "function_compute.tf").read_text()
        # Find the environment_variables block.
        start = fc.find("environment_variables")
        self.assertGreater(
            start, 0, "function_compute.tf must have an environment_variables block"
        )
        # Walk braces to find the end of the block.
        depth = 0
        i = start
        while i < len(fc):
            if fc[i] == "{":
                depth += 1
            elif fc[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        block = fc[start : i + 1]
        self.assertIn(
            'BLITZLOG_PROVIDER     = "alicloud"',
            block,
            f'environment_variables must set BLITZLOG_PROVIDER = "alicloud"; got:\n{block}',
        )

    def test_ram_does_not_reference_aws_iam(self):
        """Defensive guard: the Alicloud RAM file must not contain
        any `aws_iam_*` resource type. A copy-paste from iam.tf
        would silently try to provision AWS resources from the
        Alicloud tree, which would fail at `terraform apply` (no AWS
        creds in the alicloud context).

        Comments and prose are fine — the test scans only code
        lines (anything containing a `=` or starting with
        whitespace + `resource `).
        """
        ram = (self.ALICLOUD_MODULE / "ram.tf").read_text()
        for line in ram.splitlines():
            stripped = line.lstrip()
            # Skip pure-comment lines and blank lines.
            if not stripped or stripped.startswith("#"):
                continue
            # Code lines: a `resource "aws_iam_*" "` would be a
            # bug. We don't care about mentions in action names,
            # which would appear in `jsonencode` blocks (those
            # don't contain `aws_iam_` as a substring anyway —
            # AWS IAM actions use lowercase `iam:*`).
            if "aws_iam_" in stripped:
                self.fail(
                    f"ram.tf code line contains 'aws_iam_': {line!r}\n"
                    f"Use alicloud_ram_* resource types instead. "
                    f"A copy-paste from iam.tf would silently try to "
                    f"provision AWS resources."
                )

    def test_release_workflow_dispatches_by_provider(self):
        """The release workflow's deploy job must read
        `vars.BLITZLOG_PROVIDER`, default to `aws`, and use it in
        the working-directory of every terraform step.

        Without this, a `BLITZLOG_PROVIDER=alicloud` deploy would
        apply the AWS tree (default fallback) — the wrong cloud.
        """
        text = self.RELEASE_YML.read_text()
        # Find the deploy job section.
        deploy_idx = text.find("deploy:\n")
        self.assertGreater(deploy_idx, 0, "release.yml must have a deploy job")
        # Roughly the deploy job body (until the next top-level `name:` or
        # end of file — heuristic, but sufficient for a regression guard).
        body = text[deploy_idx:]

        self.assertIn(
            "BLITZLOG_PROVIDER: ${{ vars.BLITZLOG_PROVIDER || 'aws' }}",
            body,
            "deploy job env must define BLITZLOG_PROVIDER with the "
            "vars.BLITZLOG_PROVIDER || 'aws' default",
        )
        # Every terraform step in the deploy job must use the
        # provider prefix in working-directory. We count the
        # occurrences of `working-directory: infra/${{ env.BLITZLOG_ENV }}`
        # (the pre-#126 shape) — there must be ZERO, and at least
        # one `working-directory: infra/${{ env.BLITZLOG_PROVIDER }}/${{ env.BLITZLOG_ENV }}`.
        self.assertNotIn(
            "working-directory: infra/${{ env.BLITZLOG_ENV }}",
            body,
            "deploy job working-directory must include the provider prefix; "
            "the pre-#126 shape `infra/${{ env.BLITZLOG_ENV }}` would skip the "
            "BLITZLOG_PROVIDER dispatch.",
        )
        self.assertIn(
            "working-directory: infra/${{ env.BLITZLOG_PROVIDER }}/${{ env.BLITZLOG_ENV }}",
            body,
            "deploy job working-directory must use the provider prefix",
        )

    def test_release_workflow_fails_loud_on_alicloud_without_phase5(self):
        """The Packer step is AWS-only. The deploy job must fail
        fast when `BLITZLOG_PROVIDER=alicloud` (until the Aliyun
        image builder lands in Phase 5).

        Without this guard, an operator setting
        `vars.BLITZLOG_PROVIDER=alicloud` would have the Packer
        step run against the AWS account (wrong image) before the
        Alicloud terraform plan/apply ever runs.
        """
        text = self.RELEASE_YML.read_text()
        deploy_idx = text.find("deploy:\n")
        body = text[deploy_idx:]
        self.assertIn(
            "BLITZLOG_PROVIDER=alicloud is not yet supported by the Packer step",
            body,
            "deploy job must fail fast when BLITZLOG_PROVIDER=alicloud; "
            "Packer is AWS-only until Phase 5.",
        )

    def test_release_workflow_uses_per_provider_tfvars(self):
        """The `terraform plan` step must pass `-var-file=${BLITZLOG_PROVIDER}.tfvars`.

        Without the per-provider tfvars file, the operator's
        Alicloud-specific config (region, VSwitch ids, GitHub App
        creds) would be missing and the plan would fail.
        """
        text = self.RELEASE_YML.read_text()
        deploy_idx = text.find("deploy:\n")
        body = text[deploy_idx:]
        self.assertIn(
            "${BLITZLOG_PROVIDER}.tfvars",
            body,
            "terraform plan step must pass -var-file=${BLITZLOG_PROVIDER}.tfvars",
        )

    def test_release_workflow_picks_oss_backend_for_alicloud(self):
        """The `terraform init` step must switch backends based on
        `BLITZLOG_PROVIDER`. AWS uses S3 + ap-east-1; Alicloud uses
        OSS + cn-hangzhou.

        Without the switch, the Alicloud apply would try to
        initialise an S3 backend (wrong cloud).
        """
        text = self.RELEASE_YML.read_text()
        deploy_idx = text.find("deploy:\n")
        body = text[deploy_idx:]
        self.assertIn(
            "TF_BACKEND_BUCKET_OSS",
            body,
            "terraform init step must reference TF_BACKEND_BUCKET_OSS for the Alicloud path",
        )
        self.assertIn(
            "region=cn-hangzhou",
            body,
            "terraform init step must use cn-hangzhou as the OSS backend region",
        )

    # ------------------------------------------------------------------
    # Provider-currency + deprecation-cleanup guards (PR #155 follow-up).
    # ------------------------------------------------------------------

    def test_alicloud_provider_pinned_to_v2(self):
        """Every Alicloud stack must declare `aliyun/alicloud ~> 2.0`.

        The provider shipped 2.0.0 on 2026-10-12. The PR started on
        `~> 1.0` (locked at 1.294.0). The v2 upgrade guide's
        breaking changes (api_gateway_instance to_connect_vpc_ip_block,
        cr_repo, cs_kubernetes*, db_instance_classes, instance_types)
        do NOT touch any resource blitzlog uses, so the jump is
        safe — but only if every stack *requests* 2.0. A drift in
        one of the four stacks would silently downgrade the others.
        """
        for stack in (
            self.ALICLOUD_MODULE / "main.tf",
            self.ALICLOUD_DEV / "main.tf",
            self.ALICLOUD_PROD / "main.tf",
            self.ALICLOUD_BOOTSTRAP / "main.tf",
        ):
            text = stack.read_text()
            self.assertIn(
                'source  = "aliyun/alicloud"',
                text,
                f"{stack} must declare the aliyun/alicloud provider source explicitly",
            )
            self.assertIn(
                'version = "~> 2.0"',
                text,
                f"{stack} must pin aliyun/alicloud to ~> 2.0",
            )

    def test_required_version_floor(self):
        """All four Alicloud stacks must require Terraform >= 1.10.

        aliyun/alicloud 2.0's framework-provider features (ephemeral
        resources) need >= 1.10; latest stable is 1.16.6. The PR
        previously used ">= 1.0" which would have permitted a host
        too old to load the 2.0 provider.
        """
        for stack in (
            self.ALICLOUD_MODULE / "main.tf",
            self.ALICLOUD_DEV / "main.tf",
            self.ALICLOUD_PROD / "main.tf",
            self.ALICLOUD_BOOTSTRAP / "main.tf",
        ):
            text = stack.read_text()
            self.assertRegex(
                text,
                r'required_version\s*=\s*">=\s*1\.10"',
                f"{stack} must require terraform_version >= 1.10",
            )

    def test_dev_and_prod_declare_alicloud_provider(self):
        """infra/alicloud/{dev,prod}/main.tf must declare
        `required_providers { alicloud = { source = "aliyun/alicloud" } }`.

        Without it, the root's `provider "alicloud" {}` block resolves
        to Terraform's implicit `hashicorp/alicloud` source rather than
        `aliyun/alicloud`. The PR observed this bug: both lock files
        contained a stray `registry.terraform.io/hashicorp/alicloud
        2.0.0` entry alongside `aliyun/alicloud 1.294.0` — i.e. a
        dead 200 MB provider binary downloaded and configured per
        deploy. This test fails the moment someone removes the
        `required_providers` block.
        """
        for stack, env in (
            (self.ALICLOUD_DEV, "dev"),
            (self.ALICLOUD_PROD, "prod"),
        ):
            text = (stack / "main.tf").read_text()
            self.assertIn(
                "required_providers",
                text,
                f"infra/alicloud/{env}/main.tf must declare required_providers "
                f"so the implicit-source drift doesn't pull in hashicorp/alicloud.",
            )
            self.assertIn(
                'source  = "aliyun/alicloud"',
                text,
                f"infra/alicloud/{env}/main.tf required_providers must source "
                f"aliyun/alicloud (NOT hashicorp/alicloud).",
            )

    def test_no_legacy_fc_or_mns_resources(self):
        """The Alicloud module must use `alicloud_fcv3_function` +
        `alicloud_message_service_queue` — the v1
        `alicloud_fc_service` / `alicloud_fc_function` /
        `alicloud_mns_queue` resources are all deprecated.

        The 2.0 provider still accepts these aliases (forwarded to
        the v3 implementations under the hood), but the user
        explicitly wants no starting technical debt — so we
        migrate forward now.
        """
        module_dir = self.ALICLOUD_MODULE
        all_tf = "\n".join(p.read_text() for p in module_dir.glob("*.tf"))

        for legacy in (
            "alicloud_fc_service",
            "alicloud_fc_function",
            "alicloud_mns_queue",
        ):
            for line in all_tf.splitlines():
                stripped = line.lstrip()
                if not stripped or stripped.startswith("#"):
                    continue
                self.assertNotIn(
                    f'resource "{legacy}"',
                    stripped,
                    f"legacy resource type {legacy!r} is deprecated by provider "
                    f"v2.0.0; migrate to the replacement (alicloud_fcv3_function "
                    f"or alicloud_message_service_queue).\nLine: {line!r}",
                )

        self.assertIn(
            'resource "alicloud_fcv3_function" "handler"',
            all_tf,
            "alicloud_fcv3_function.handler must replace the legacy "
            "alicloud_fc_function pair",
        )
        self.assertIn(
            'resource "alicloud_message_service_queue" "dlq"',
            all_tf,
            "alicloud_message_service_queue.dlq must replace the deprecated "
            "alicloud_mns_queue.dlq",
        )

    def test_no_deprecated_field_or_attribute_names(self):
        """Field-level deprecation cleanup: the v2 provider still
        emits warnings for the old field/attribute names even
        though they still work. Validate the migrations landed:

          - alicloud_security_group.name        -> security_group_name
          - alicloud_kms_key.key_state         -> status
          - alicloud_ram_role.name             -> role_name
          - <ram role>.name  attribute         -> .role_name

        We scan only resource/code lines (skip comments and
        jsonencode blocks where action names can contain
        substring matches by accident).
        """
        module_dir = self.ALICLOUD_MODULE

        def code_lines(path: Path) -> list[str]:
            out = []
            for line in path.read_text().splitlines():
                stripped = line.lstrip()
                if not stripped or stripped.startswith("#"):
                    continue
                out.append(line)
            return out

        # ecs.tf: security_group.name -> security_group_name
        ecs = code_lines(module_dir / "ecs.tf")
        self.assertNotIn(
            'name        = "blitzlog-${var.environment}-agent-sg"',
            "\n".join(ecs),
            "alicloud_security_group.name is deprecated; use security_group_name",
        )
        self.assertTrue(
            any("security_group_name" in ln for ln in ecs),
            "ecs.tf must set security_group_name on alicloud_security_group",
        )

        # kms.tf: key_state -> status
        kms = code_lines(module_dir / "kms.tf")
        self.assertNotIn(
            'key_state              = "Enabled"',
            "\n".join(kms),
            "alicloud_kms_key.key_state is deprecated; use status",
        )
        self.assertTrue(
            any('status                 = "Enabled"' in ln for ln in kms),
            "kms.tf must set status on alicloud_kms_key",
        )

        # ram.tf: no `name =` on alicloud_ram_role resources,
        # and no `.name` references on role_policy_attachment /
        # FC env-var / outputs.
        ram = code_lines(module_dir / "ram.tf")
        ram_text = "\n".join(ram)
        self.assertNotRegex(
            ram_text,
            r'resource "alicloud_ram_role" "[^"]+"\s*\{[^}]*?\n\s*name\s*=',
            "alicloud_ram_role.name is deprecated; use role_name",
        )
        self.assertNotIn(
            "role_name   = alicloud_ram_role.lambda_role.name",
            ram_text,
            "role_policy_attachment.role_name must use .role_name (not .name)",
        )
        self.assertNotIn(
            "role_name   = alicloud_ram_role.ecs_agent_role.name",
            ram_text,
            "role_policy_attachment.role_name must use .role_name (not .name)",
        )

        # function_compute.tf: the FC env-var must use .role_name for
        # RAM_INSTANCE_PROFILE_NAME. Outputs already use .role_name.
        fc = code_lines(module_dir / "function_compute.tf")
        fc_text = "\n".join(fc)
        self.assertNotIn(
            "RAM_INSTANCE_PROFILE_NAME = alicloud_ram_role.ecs_agent_role.name",
            fc_text,
            "FC env-var RAM_INSTANCE_PROFILE_NAME must use .role_name (not .name)",
        )
        self.assertIn(
            "RAM_INSTANCE_PROFILE_NAME = alicloud_ram_role.ecs_agent_role.role_name",
            fc_text,
            "FC env-var RAM_INSTANCE_PROFILE_NAME must be wired to "
            "alicloud_ram_role.ecs_agent_role.role_name",
        )

    def test_function_compute_uses_oss_object_for_code(self):
        """alicloud_fcv3_function has no `filename` arg — code must
        reference an OSS object. Validate the upload exists and the
        fcv3 function's `code` block points at it.

        Avoids re-introducing the v1 alicloud_fc_function shape (which
        accepted a local filename) via copy-paste.
        """
        fc = (self.ALICLOUD_MODULE / "function_compute.tf").read_text()
        self.assertIn(
            'resource "alicloud_oss_bucket_object" "fc_code"',
            fc,
            "function_compute.tf must upload the zip via "
            "alicloud_oss_bucket_object.fc_code (fcv3 has no filename arg)",
        )
        # The fcv3 function's code block must reference the OSS object
        # (not the deprecated base64 zip_file path with a huge state blob).
        self.assertIn(
            "oss_bucket_name = local.agent_logs_bucket_name_resolved",
            fc,
            "alicloud_fcv3_function code{} must reference the OSS bucket",
        )
        self.assertIn(
            "oss_object_name = local.fc_code_object_key",
            fc,
            "alicloud_fcv3_function code{} must reference local.fc_code_object_key",
        )

    def test_lambda_requirements_variable(self):
        """The hard error from PR #155's CI: the Alicloud module's
        null_resource.lambda_build referenced a path that no
        longer exists (`lambda/requirements.txt`). The fix routes
        the requirements file through `var.lambda_requirements`
        (default `requirements-alicloud.txt`) — the same shape as
        the AWS side.

        Both the `filemd5` triggers and the `pip install -r` call
        must go through the variable; a bare `requirements.txt`
        reference anywhere in function_compute.tf regresses the fix.
        """
        variables = (self.ALICLOUD_MODULE / "variables.tf").read_text()
        self.assertIn(
            'variable "lambda_requirements"',
            variables,
            "modules/core/variables.tf must declare lambda_requirements",
        )
        self.assertIn(
            'default     = "requirements-alicloud.txt"',
            variables,
            "lambda_requirements must default to requirements-alicloud.txt",
        )

        fc = (self.ALICLOUD_MODULE / "function_compute.tf").read_text()
        self.assertNotIn(
            '"${path.module}/../../../../lambda/requirements.txt"',
            fc,
            "function_compute.tf must not hardcode lambda/requirements.txt "
            "(the file was renamed in the per-cloud requirements split)",
        )
        # Both the filemd5 trigger AND the pip install -r line must
        # interpolate var.lambda_requirements.
        self.assertIn(
            'filemd5("${path.module}/../../../../lambda/${var.lambda_requirements}")',
            fc,
            "filemd5 trigger must hash `${var.lambda_requirements}`",
        )
        self.assertIn(
            "-r ${path.module}/../../../../lambda/${var.lambda_requirements}",
            fc,
            "pip install -r must read `${var.lambda_requirements}`",
        )

    def test_apigateway_uses_fcv3(self):
        """The API Gateway backend must point at FCV3 (function_version
        = "3.0"). Without this, the legacy default of "2.0" expects
        `service_name`, which doesn't exist in the FCV3 model, and
        API Gateway fails to wire to the function.
        """
        api = (self.ALICLOUD_MODULE / "apigateway.tf").read_text()
        self.assertIn(
            'function_version = "3.0"',
            api,
            'apigateway.tf fc_service_config must use function_version = "3.0"',
        )
        # In v3 the service resource does not exist; we must NOT
        # reference it on a *code* line. (Comments are fine — the
        # legacy resource name is the natural way to explain what
        # we're moving away from.)
        for line in api.splitlines():
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            self.assertNotIn(
                "alicloud_fc_service",
                stripped,
                f"apigateway.tf must not reference the legacy "
                f"alicloud_fc_service on a code line.\nLine: {line!r}",
            )
            self.assertNotIn(
                "alicloud_fc_function",
                stripped,
                f"apigateway.tf must not reference the legacy "
                f"alicloud_fc_function on a code line.\nLine: {line!r}",
            )

    def test_fc_code_oss_grant_in_ram(self):
        """The FC execution role's RAM policy must grant
        `oss:GetObject` on the function-code object key — without
        this the function cannot fetch its own zip on cold start.

        The grant is scoped to `${bucket}/${fc_code_object_key}`
        (exact path) to keep blast radius minimal.
        """
        ram = (self.ALICLOUD_MODULE / "ram.tf").read_text()
        self.assertIn(
            "${local.agent_logs_bucket_arn}/${local.fc_code_object_key}",
            ram,
            "ram.tf must grant oss:GetObject on the per-env FC code object key "
            "(local.agent_logs_bucket_arn/local.fc_code_object_key). Without "
            "this, alicloud_fcv3_function fails to fetch its own code at cold start.",
        )
        self.assertIn(
            "oss:GetObject",
            ram,
            "ram.tf must grant oss:GetObject for the FC code download",
        )

    def test_terraform_pinned_to_1_16_6(self):
        """Every workflow that installs Terraform + mise.toml must
        use the same version (1.16.6). A drift between them causes
        `terraform init` to silently re-pick a different provider
        set on CI vs local.
        """
        # mise.toml
        mise_toml = (self.REPO_ROOT / "mise.toml").read_text()
        self.assertIn(
            'terraform = "1.16.6"',
            mise_toml,
            'mise.toml must pin terraform = "1.16.6"',
        )

        # workflows
        for wf_rel in (
            ".github/workflows/lint.yml",
            ".github/workflows/release.yml",
            ".github/workflows/terraform-apply-dev.yml",
        ):
            text = (self.REPO_ROOT / wf_rel).read_text()
            self.assertIn(
                "terraform_version: 1.16.6",
                text,
                f"{wf_rel} must install terraform_version 1.16.6",
            )
            # Old pin must be gone.
            self.assertNotIn(
                "terraform_version: 1.9.8",
                text,
                f"{wf_rel} must not still pin terraform 1.9.8",
            )

    def test_mise_tf_plan_points_at_real_dir(self):
        """The mise `tf-plan` / `tf-apply` tasks used to point at
        `dir = "infra"` — but `infra/` is no longer a valid Terraform
        working directory after the per-cloud split (it now contains
        only per-cloud subdirectories, not a root `main.tf`).

        Both tasks must point at a concrete per-cloud tree. We assert
        the AWS dev tree because that's the default operator path;
        per-cloud override is via $BLITZLOG_PROVIDER / $BLITZLOG_ENV.
        """
        mise_toml = (self.REPO_ROOT / "mise.toml").read_text()
        # The bad shape from #126 must be gone.
        self.assertNotRegex(
            mise_toml,
            r'\["tf-(?:plan|apply)"\][^[]*?dir\s*=\s*"infra"\s*\n',
            'mise.toml tasks[].dir = "infra" is invalid after the per-cloud split',
        )
        # Default target is infra/aws/dev.
        self.assertIn(
            'dir = "infra/aws/dev"',
            mise_toml,
            'mise.toml tf-plan / tf-apply must default to dir = "infra/aws/dev"',
        )


if __name__ == "__main__":
    unittest.main()
