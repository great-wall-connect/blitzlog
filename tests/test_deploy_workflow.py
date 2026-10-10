"""Regression guards for the GitHub Actions CI/CD deploy plumbing.

These tests are static (string-level checks on
.github/workflows/*.yml and infra/bootstrap/*.tf). They mirror
tests/test_release_workflow.py's pattern: read a config file,
assert the shapes that prevent the bug from regressing. No shell,
no docker, no AWS.

What this guards against (issue #92):

- ``infra/bootstrap/deploy-role.tf`` losing its OIDC trust for
  GitHub Actions (the role becomes un-assumable from CI).
- ``deploy-role.tf`` gaining an EC2 service-principal statement
  (it doesn't need one — the local-exec runs on the GHA runner).
- ``deploy-role.tf`` widening S3 to ``s3:::gwc-infra-tf-state``
  without a key-prefix condition (a leaked delete on the wrong
  state file is a hard-to-recover outage).
- ``deploy-role.tf`` widening SSM to ``parameter/blitzlog/*``
  (would cross the per-env boundary that env-isolation tests
  protect).
- ``deploy-role.tf`` losing the ``iam:PassedToService`` condition
  on PassRole (the role can then be passed to any service, not
  just Lambda / EC2).
- ``release.yml`` losing the new ``deploy`` job (release-mode
  no longer auto-applies infra/prod).
- ``release.yml``'s deploy job losing the prod ``environment:
  production`` gate (prod applies skip CODEOWNERS approval).
- ``release.yml``'s deploy job losing the per-env concurrency
  group (two concurrent applies race on the same state file).
- ``release.yml``'s deploy job no longer mapping mode -> env
  (pr-test must apply infra/dev, release must apply infra/prod).
- ``terraform-apply-dev.yml`` losing its workflow_dispatch-only
  trigger (auto-apply on push would surprise reviewers).
- ``terraform-apply-dev.yml`` losing the per-env concurrency
  group or the ref input.
- Either workflow dropping the backend-config inline args
  (a ``*.hcl`` materialised on the runner is a leak vector).
- Either workflow dropping the ``secrets[format(...)]``
  index access (regression to a per-env-suffixed switch would
  duplicate the secret list).

We parse the YAML with regex rather than pyyaml so we don't pull
a new dev dependency in for what is fundamentally a set of shape
assertions on small, stable workflows. The pattern matches below
are intentionally loose — they're guards, not parsers — but they
catch every realistic regression (typos, deleted lines, swapped
keys).
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"
APPLY_DEV_YML = REPO_ROOT / ".github" / "workflows" / "terraform-apply-dev.yml"
DEPLOY_ROLE_TF = REPO_ROOT / "infra" / "bootstrap" / "deploy-role.tf"
PACKER_ROLE_TF = REPO_ROOT / "infra" / "bootstrap" / "packer-role.tf"


def _policy_body_for_role(role_resource_name: str, iam_tf: str) -> str:
    """Return the HCL body of the named role's `policy = jsonencode({...})` block.

    Same approach as tests/test_terraform_env_isolation.py — we
    scan for the resource's `policy = jsonencode(...)` call and
    return the inner HCL body so callers can regex over it.
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


def _action_literals_in_policy(role_resource_name: str, iam_tf: str) -> list[str]:
    """Return every `Action: "X"` or `Action = [ "X", ... ]` literal in a role.

    Pulled from the `policy = jsonencode(...)` body. Used to assert
    presence/absence of specific actions in the deploy role.
    """
    body = _policy_body_for_role(role_resource_name, iam_tf)
    actions = []
    # Single-line: Action = "lambda:GetFunction"  (string literal)
    # or          Action = ["lambda:GetFunction", "lambda:CreateFunction"]
    # The body uses HCL string-literal syntax, so we just need to
    # pull the quoted tokens.
    for m in re.finditer(r'"([a-zA-Z][a-zA-Z0-9:*]+)"', body):
        actions.append(m.group(1))
    return actions


class TestDeployRole(unittest.TestCase):
    """Static checks on infra/bootstrap/deploy-role.tf."""

    @classmethod
    def setUpClass(cls):
        cls.text = DEPLOY_ROLE_TF.read_text()
        cls.actions = _action_literals_in_policy("deploy", cls.text)

    def test_role_exists(self):
        """deploy-role.tf MUST declare aws_iam_role.deploy."""
        self.assertIn(
            'resource "aws_iam_role" "deploy"',
            self.text,
            "deploy-role.tf must declare aws_iam_role.deploy",
        )

    def test_role_name_is_blitzlog_deploy_role(self):
        """The role's `name` attribute MUST be `blitzlog-deploy-role`.

        The deploy workflows hard-code the suffix
        `:role/blitzlog-deploy-role` in the OIDC `role-to-assume`
        string. A rename here without updating both workflows is a
        silent break.
        """
        m = re.search(
            r'resource "aws_iam_role" "deploy" \{(?P<body>.*?)\n\}',
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "deploy-role.tf must have an aws_iam_role.deploy block")
        self.assertRegex(
            m.group("body"),
            r'(?m)^\s*name\s*=\s*"blitzlog-deploy-role"\s*$',
            "aws_iam_role.deploy must be named `blitzlog-deploy-role` "
            "(the deploy workflows hard-code this suffix in `role-to-assume`)",
        )

    def test_trust_policy_includes_github_oidc(self):
        """The trust policy MUST allow the GitHub OIDC provider.

        Without this, no workflow can assume the role and CI is
        dead in the water.
        """
        # Mirror packer-role's StringLike on the `sub` claim so the
        # two roles share an identical trust surface.
        self.assertIn(
            "token.actions.githubusercontent.com:aud",
            self.text,
            "deploy-role.tf's trust policy must pin OIDC `aud=sts.amazonaws.com`",
        )
        self.assertRegex(
            self.text,
            r'"token\.actions\.githubusercontent\.com:sub"\s*=\s*"repo:great-wall-connect\*/blitzlog\*:ref:refs/heads/\*"',
            "deploy-role.tf's trust policy must use the same `repo:great-wall-connect*/blitzlog*:ref:refs/heads/*` "
            "StringLike as the packer role so the two share an OIDC surface",
        )
        # `aud=sts.amazonaws.com` MUST be pinned (matches the OIDC
        # provider's client_id_list in github-oidc-provider.tf).
        self.assertIn(
            '"token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"',
            self.text,
            "deploy-role.tf must pin OIDC `aud=sts.amazonaws.com` "
            "(matches github-oidc-provider.tf's client_id_list)",
        )

    def test_trust_policy_no_ec2_service_principal(self):
        """The trust policy MUST NOT allow ec2.amazonaws.com to assume this role.

        The packer role needs an EC2 service-principal statement
        because the Packer build VM assumes it via instance profile
        (see infra/bootstrap/packer-role.tf:53-64). The deploy role
        does NOT need this — the local-exec in
        infra/modules/core/lambda.tf:35-92 runs on the GHA runner,
        not on an EC2 instance. A future copy-paste from packer-role
        that leaves the EC2 statement in place is a privilege
        escalation: any EC2 instance in the account could then
        assume the deploy role and run terraform apply.
        """
        # Find the trust policy block by anchoring on
        # `aws_iam_role.deploy` and scanning forward for the
        # `assume_role_policy = jsonencode({ ... })` body.
        start = self.text.find('resource "aws_iam_role" "deploy"')
        self.assertGreaterEqual(start, 0)
        # Scope the search to the assume_role_policy jsonencode.
        m = re.search(
            r"assume_role_policy\s*=\s*jsonencode\(\{(?P<body>.*?)\}\s*\)",
            self.text[start:],
            re.DOTALL,
        )
        self.assertIsNotNone(
            m,
            "deploy-role.tf must have an assume_role_policy jsonencode",
        )
        body = m.group("body")
        self.assertNotIn(
            '"Service" = "ec2.amazonaws.com"',
            body,
            "deploy role's trust policy must NOT include an ec2.amazonaws.com "
            "service-principal statement (the local-exec runs on the GHA "
            "runner; the deploy role does not need to be assumable by EC2)",
        )

    def test_admin_sso_escape_hatch_present(self):
        """The trust policy MUST allow the admin-SSO principal to assume the role.

        Mirrors packer-role.tf:65-76 so a maintainer can still
        `terraform apply` from their laptop with AdministratorAccess
        without going through GitHub Actions.
        """
        self.assertIn(
            "data.aws_iam_role.admin_sso.arn",
            self.text,
            "deploy-role.tf must reference data.aws_iam_role.admin_sso "
            "(the admin-SSO escape hatch for local applies)",
        )
        # Also require the `aws:RequestedRegion = ap-east-1`
        # condition to scope local applies to the blitzlog region.
        self.assertIn(
            '"aws:RequestedRegion" = "ap-east-1"',
            self.text,
            "deploy role's admin-SSO statement must scope "
            "aws:RequestedRegion=ap-east-1 (matches the project's region)",
        )

    def test_state_bucket_scope_is_dev_and_prod(self):
        """S3 state-bucket actions MUST be scoped to dev/* and prod/* prefixes.

        A widened S3 grant (e.g. `arn:aws:s3:::<bucket>/*`) lets the
        role delete the bootstrap state file or other unrelated
        objects in the shared bucket.
        """
        body = _policy_body_for_role("deploy", self.text)
        # The state-file resource patterns MUST be exactly
        # dev/* and prod/* under the bucket.
        for prefix in (
            '"arn:aws:s3:::${var.tf_backend_bucket}/dev/*"',
            '"arn:aws:s3:::${var.tf_backend_bucket}/prod/*"',
        ):
            self.assertIn(
                prefix,
                body,
                f"deploy policy must scope S3 state-file access to {prefix} "
                "(without per-env prefix the role can mutate the bootstrap state)",
            )
        # And MUST NOT have an unscoped state-bucket pattern.
        self.assertNotIn(
            '"arn:aws:s3:::${var.tf_backend_bucket}/*"',
            body,
            "deploy policy must NOT grant `arn:aws:s3:::${var.tf_backend_bucket}/*` "
            "without a per-env prefix (a leaked delete on the bootstrap state is a hard outage)",
        )

    def test_ssm_scope_is_dev_and_prod_blitzlog_only(self):
        """SSM permissions MUST be scoped to /blitzlog/dev/* and /blitzlog/prod/*.

        Widening to /blitzlog/* would cross the per-env boundary
        that env-isolation tests protect (test_terraform_env_isolation
        .py::test_lambda_policy_no_cross_env_ssm_reads). The
        user-pool namespace (/blitzlog/users/*) is env-independent
        and per-env stacks don't write there, so it's intentionally
        NOT in this grant.
        """
        body = _policy_body_for_role("deploy", self.text)
        for env in ("dev", "prod"):
            arn = (
                f'"arn:aws:ssm:${{var.aws_region}}:${{data.aws_caller_identity.current.account_id}}:'
                f'parameter/blitzlog/{env}/*"'
            )
            self.assertIn(
                arn,
                body,
                f"deploy policy must grant SSM access to {arn} "
                f"(scoped to /blitzlog/{env}/*, not the wildcard /blitzlog/*)",
            )
        # Must NOT widen to /blitzlog/*.
        self.assertNotIn(
            "parameter/blitzlog/*",
            body,
            "deploy policy must NOT widen SSM to /blitzlog/* "
            "(would cross the per-env boundary the env-isolation tests protect)",
        )
        # Must NOT touch the user-pool namespace.
        self.assertNotIn(
            "parameter/blitzlog/users",
            body,
            "deploy policy must NOT include the user-pool namespace "
            "(per-env stacks don't write there; user-pool is a separate stack)",
        )

    def test_passrole_lambda_conditional(self):
        """iam:PassRole for the Lambda execution role MUST be conditional on
        iam:PassedToService=lambda.amazonaws.com.

        Without the condition, the deploy role could pass the
        Lambda execution role to any service (e.g. an attacker
        could attach it to an arbitrary EC2 instance to inherit
        the Lambda's permissions).
        """
        body = _policy_body_for_role("deploy", self.text)
        # The Lambda PassRole statement must reference
        # iam:PassedToService = lambda.amazonaws.com.
        self.assertIn(
            '"iam:PassedToService" = "lambda.amazonaws.com"',
            body,
            "deploy policy's Lambda PassRole statement must be "
            "conditional on iam:PassedToService=lambda.amazonaws.com "
            "(otherwise the role can be passed to any service)",
        )

    def test_passrole_ec2_conditional(self):
        """iam:PassRole for the EC2 agent role MUST be conditional on
        iam:PassedToService=ec2.amazonaws.com.
        """
        body = _policy_body_for_role("deploy", self.text)
        self.assertIn(
            '"iam:PassedToService" = "ec2.amazonaws.com"',
            body,
            "deploy policy's EC2 PassRole statement must be "
            "conditional on iam:PassedToService=ec2.amazonaws.com "
            "(otherwise the role can be passed to any service)",
        )

    def test_lambda_management_actions_present(self):
        """The deploy policy MUST include the actions terraform needs
        to create/update the blitzlog Lambda.
        """
        for required in (
            "lambda:CreateFunction",
            "lambda:UpdateFunctionCode",
            "lambda:UpdateFunctionConfiguration",
            "lambda:DeleteFunction",
        ):
            self.assertIn(
                required,
                self.actions,
                f"deploy policy must include {required} "
                "(terraform needs it to manage blitzlog-<env>-handler)",
            )

    def test_iam_management_actions_present(self):
        """The deploy policy MUST include the actions terraform needs
        to manage the blitzlog Lambda + EC2 agent roles and policies.
        """
        for required in (
            "iam:CreateRole",
            "iam:DeleteRole",
            "iam:PutRolePolicy",
            "iam:DeleteRolePolicy",
            "iam:AttachRolePolicy",
            "iam:DetachRolePolicy",
            "iam:PassRole",
            "iam:CreateInstanceProfile",
            "iam:DeleteInstanceProfile",
            "iam:AddRoleToInstanceProfile",
            "iam:RemoveRoleFromInstanceProfile",
        ):
            self.assertIn(
                required,
                self.actions,
                f"deploy policy must include {required} "
                "(terraform needs it to manage the core module's IAM resources)",
            )

    def test_apigateway_management_actions_present(self):
        """The deploy policy MUST include the actions terraform needs
        to manage the API Gateway v2 webhook API.
        """
        for required in (
            "apigateway:GET",
            "apigateway:POST",
            "apigateway:PATCH",
            "apigateway:DELETE",
        ):
            self.assertIn(
                required,
                self.actions,
                f"deploy policy must include {required} "
                "(terraform needs it to manage the API Gateway v2 webhook API)",
            )

    def test_deploy_role_arn_output_present(self):
        """The deploy role MUST expose its ARN as an output.

        The deploy workflows need the ARN to populate
        `role-to-assume`. Without the output, the operator has to
        construct the ARN by hand.
        """
        self.assertRegex(
            self.text,
            r'(?m)^\s*output\s+"deploy_role_arn"\s*\{',
            "deploy-role.tf must declare an output `deploy_role_arn` "
            "(the deploy workflows read this to populate `role-to-assume`)",
        )
        # The output must reference the role's `.arn` attribute.
        self.assertIn(
            "value       = aws_iam_role.deploy.arn",
            self.text,
            "deploy_role_arn output must reference `aws_iam_role.deploy.arn`",
        )

    def test_packer_role_unchanged(self):
        """packer-role.tf MUST still have its EC2 service-principal
        statement.

        The deploy role's lack of an EC2 statement is a deliberate
        asymmetry (the local-exec runs on the runner, not on EC2).
        If a future refactor "consolidates" the two trust policies
        and drops the EC2 statement from the packer role, the
        Packer build VM can no longer assume the role and AMI
        bakes fail. This test guards the packer role's
        EC2 statement.
        """
        packer_text = PACKER_ROLE_TF.read_text()
        # The HCL trust policy uses HCL identifier syntax, so
        # `Service = "ec2.amazonaws.com"` (no quotes around the
        # key) is the literal we want to find — not the JSON form
        # `"Service" = "ec2.amazonaws.com"`.
        self.assertIn(
            'Service = "ec2.amazonaws.com"',
            packer_text,
            "packer-role.tf must keep its ec2.amazonaws.com service-principal "
            "statement (the Packer build VM assumes the role via instance profile; "
            "removing it breaks AMI bakes)",
        )


class TestReleaseDeployJob(unittest.TestCase):
    """Static checks on the new `deploy` job in release.yml."""

    @classmethod
    def setUpClass(cls):
        cls.text = RELEASE_YML.read_text()

    def test_deploy_job_exists(self):
        """release.yml MUST declare a `deploy:` job (id: deploy is too
        restrictive — job names use `name:` for display, and the job
        key is the `deploy:` line)."""
        self.assertRegex(
            self.text,
            r"(?m)^  deploy:\s*$",
            "release.yml must declare a `deploy:` job "
            "(applies the env-implied stack after the release job succeeds)",
        )

    def test_deploy_depends_on_release(self):
        """The deploy job MUST depend on the release job.

        Without `needs: release`, the deploy runs in parallel and
        races the image build / Packer bake. The release job's AMI
        publish to SSM must complete before the deploy's
        `terraform apply` reads the parameter.
        """
        # Scope to the deploy: job body so a stray `needs:`
        # elsewhere in the file (or in the release job's outputs)
        # can't pass this test.
        m = re.search(
            r"(?ms)^  deploy:\s*\n(?P<body>(?:    .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(m, "release.yml must have a `deploy:` job")
        body = m.group("body")
        self.assertRegex(
            body,
            r"(?m)^\s+needs:\s*release\s*$",
            "release.yml's deploy job must have `needs: release` "
            "(so the deploy waits for the image build + Packer bake to complete)",
        )

    def test_deploy_runs_terraform_plan(self):
        """The deploy job MUST run `terraform plan` to a tfplan file."""
        self.assertRegex(
            self.text,
            r"terraform plan -no-color -input=false -out=tfplan",
            "release.yml's deploy job must run `terraform plan -out=tfplan` "
            "(the apply consumes the saved plan, never re-plans in-band)",
        )

    def test_deploy_runs_terraform_apply(self):
        """The deploy job MUST run `terraform apply` against the saved plan."""
        self.assertRegex(
            self.text,
            r"terraform apply -no-color -input=false tfplan",
            "release.yml's deploy job must run `terraform apply tfplan` "
            "(consumes the plan from the previous step, no in-band re-plan)",
        )

    def test_deploy_uploads_plan_artifact(self):
        """The deploy job MUST upload the tfplan as a workflow artifact
        (so the operator can re-review or audit the plan that drove
        the apply)."""
        self.assertRegex(
            self.text,
            r"actions/upload-artifact@v4",
            "release.yml's deploy job must use actions/upload-artifact@v4 "
            "to publish the plan for review/audit",
        )
        self.assertRegex(
            self.text,
            r"(?m)name:\s*tfplan-\$\{\{\s*env\.BLITZLOG_ENV\s*\}\}",
            "release.yml's deploy plan artifact must be named "
            "`tfplan-${{ env.BLITZLOG_ENV }}` (so dev and prod plans are "
            "distinguishable in the Actions UI)",
        )

    def test_deploy_backend_config_inline(self):
        """The deploy job MUST pass backend config inline (no `*.hcl`
        file materialized on the runner).

        A `*.hcl` file would persist the state-bucket name on the
        runner's filesystem and potentially leak via debug logs
        or future caching.
        """
        # YAML line-continuation backslashes (\) + newlines in the
        # `run: |` block need to be collapsed before matching so
        # the regex doesn't break on the multi-line terraform init.
        collapsed = re.sub(r"\\\s+", " ", self.text)
        collapsed = re.sub(r"\s+", " ", collapsed)
        self.assertRegex(
            collapsed,
            r"-backend-config=\"bucket=",
            "release.yml's deploy job must pass "
            "`-backend-config=bucket=...` inline "
            "(no backend.hcl materialised on the runner)",
        )
        self.assertRegex(
            collapsed,
            r"-backend-config=\"key=",
            "release.yml's deploy job must pass " "`-backend-config=key=...` inline",
        )

    def test_deploy_uses_oidc_for_deploy_role(self):
        """The deploy job MUST use OIDC to assume the blitzlog-deploy-role."""
        self.assertRegex(
            self.text,
            r"role-to-assume:\s*arn:aws:iam::\$\{\{\s*vars\.AWS_ACCOUNT_ID\s*\}\}:role/blitzlog-deploy-role",
            "release.yml's deploy job must OIDC into "
            "`arn:aws:iam::${{ vars.AWS_ACCOUNT_ID }}:role/blitzlog-deploy-role`",
        )

    def test_deploy_prod_environment_gate(self):
        """When mode=release (prod apply), the deploy job MUST declare
        `environment: production` so the prod GitHub Environment's
        required reviewers (CODEOWNERS) gate the apply.

        Without this, a release.yml run on `main` would silently
        apply infra/prod without human approval, which the
        user explicitly does NOT want.
        """
        self.assertRegex(
            self.text,
            r"environment:\s*\n\s+name:\s*\$\{\{\s*needs\.release\.outputs\.mode\s*==\s*'release'\s*&&\s*'production'\s*\|\|\s*'dev'\s*\}\}",
            "release.yml's deploy job must declare "
            "`environment.name = production` when mode==release "
            "(so CODEOWNERS approval gates the prod apply)",
        )

    def test_deploy_per_env_concurrency(self):
        """The deploy job MUST serialize per-env via a concurrency group.

        Two concurrent dev applies (or a dev + prod race) would
        contend on the same state file. The project chose no
        DynamoDB lock table (README.md:295-307), so the GH Actions
        concurrency group is the only serialization mechanism.
        """
        self.assertRegex(
            self.text,
            r"group:\s*terraform-\$\{\{\s*needs\.release\.outputs\.mode\s*==\s*'release'\s*&&\s*'prod'\s*\|\|\s*'dev'\s*\}\}",
            "release.yml's deploy job must use a per-env "
            "concurrency group `terraform-{prod|dev}` "
            "(serializes applies on the same state file)",
        )
        self.assertRegex(
            self.text,
            r"cancel-in-progress:\s*false",
            "release.yml's deploy concurrency must be "
            "`cancel-in-progress: false` "
            "(a long apply should not be killed by a new dispatch)",
        )

    def test_deploy_only_required_ids_token_write(self):
        """The deploy job MUST request only `contents: read` and
        `id-token: write`. It does NOT need `packages: write`
        (no image push), `pull-requests: write` (no PR comment),
        or `contents: write` (no commits/tags).
        """
        # Pull the deploy: job body.
        m = re.search(
            r"(?ms)^  deploy:\s*\n(?P<body>(?:    .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(m, "release.yml must have a `deploy:` job")
        body = m.group("body")
        self.assertRegex(
            body,
            r"permissions:\s*\n\s+contents:\s+read\s*\n\s+id-token:\s+write",
            "release.yml's deploy job permissions must be "
            "`contents: read` + `id-token: write` (no write scopes needed)",
        )
        # Defensive: the deploy job must not request packages: write
        # (the release job above does, but deploy doesn't push images).
        self.assertNotIn(
            "packages: write",
            body,
            "release.yml's deploy job must NOT request `packages: write` "
            "(the deploy path doesn't push images; only the release job needs it)",
        )

    def test_deploy_maps_mode_to_env(self):
        """The deploy job's BLITZLOG_ENV env var MUST be `dev` when
        mode=pr-test, `prod` when mode=release.

        The release.yml design is:
          pr-test  -> mode=pr-test  -> BLITZLOG_ENV=dev
          release  -> mode=release  -> BLITZLOG_ENV=prod

        A swap here would deploy prod images to dev (or vice versa).
        """
        self.assertRegex(
            self.text,
            r"BLITZLOG_ENV:\s*\$\{\{\s*needs\.release\.outputs\.mode\s*==\s*'release'\s*&&\s*'prod'\s*\|\|\s*'dev'\s*\}\}",
            "release.yml's deploy job must map "
            "`mode==release ? prod : dev` to BLITZLOG_ENV "
            "(pr-test deploys go to dev, release deploys go to prod)",
        )

    def test_release_job_exposes_outputs(self):
        """The `release` job MUST expose `mode` (and at least
        `pr_number` and `new_version`) as job-level outputs so the
        `deploy` job can read them via `needs.release.outputs.X`.

        Without these, the deploy job can't map mode -> env and
        would have to re-detect the mode (DRY violation + risk of
        drift between the two detects).
        """
        # Pull the `release:` job's `outputs:` block.
        m = re.search(
            r"(?ms)^  release:\s*\n(?P<body>(?:    .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(m, "release.yml must have a `release:` job")
        body = m.group("body")
        self.assertRegex(
            body,
            r"(?m)^\s+outputs:\s*$",
            "release.yml's `release` job must declare an `outputs:` block "
            "(so the `deploy` job can read mode via `needs.release.outputs.mode`)",
        )
        # mode is the critical one — without it the deploy can't decide
        # which env to apply.
        self.assertRegex(
            body,
            r"mode:\s*\$\{\{\s*steps\.detect\.outputs\.mode\s*\}\}",
            "release.yml's `release.outputs` must expose `mode` "
            "from steps.detect.outputs.mode",
        )


class TestTerraformApplyDevWorkflow(unittest.TestCase):
    """Static checks on .github/workflows/terraform-apply-dev.yml."""

    @classmethod
    def setUpClass(cls):
        cls.text = APPLY_DEV_YML.read_text()

    def test_triggered_only_by_workflow_dispatch(self):
        """terraform-apply-dev.yml MUST fire only via workflow_dispatch.

        Auto-apply on push/PR would surprise reviewers and bypass
        the "operator dispatches" contract that the issue #92
        design relies on. There is no `push:` block, no
        `pull_request:` block, no `schedule:` block.
        """
        self.assertNotIn(
            "push:",
            self.text,
            "terraform-apply-dev.yml must not have a `push:` trigger",
        )
        self.assertNotIn(
            "pull_request:",
            self.text,
            "terraform-apply-dev.yml must not have a `pull_request:` trigger",
        )
        self.assertNotIn(
            "schedule:",
            self.text,
            "terraform-apply-dev.yml must not have a `schedule:` trigger",
        )
        self.assertRegex(
            self.text,
            r"(?m)^on:\s*\n\s+workflow_dispatch:\s*$",
            "terraform-apply-dev.yml must declare `on: workflow_dispatch`",
        )

    def test_dispatch_has_ref_input(self):
        """terraform-apply-dev.yml's workflow_dispatch MUST have a
        `ref` input (so the operator can dispatch against a feature
        branch, not just the default ref_name).
        """
        self.assertRegex(
            self.text,
            r"(?ms)^  workflow_dispatch:\s*\n\s+inputs:\s*\n\s+ref:",
            "terraform-apply-dev.yml's workflow_dispatch must declare a `ref` input "
            "(so operators can dispatch against a feature branch)",
        )

    def test_runs_terraform_init_with_inline_backend(self):
        """The dev workflow MUST run `terraform init` with the backend
        config inline (no `*.hcl` file).
        """
        # Inline backend config is a single bash run with multiple
        # -backend-config=... flags on the same terraform init line.
        # The `run: |` block uses YAML line-continuation backslashes
        # (\\) that collapse to a literal `\` followed by whitespace.
        # Match a collapsed form that allows an optional `\` between
        # the `terraform init` and the first `-backend-config`.
        collapsed = re.sub(r"\\\s+", " ", self.text)
        collapsed = re.sub(r"\s+", " ", collapsed)
        self.assertRegex(
            collapsed,
            r"terraform init -input=false -backend-config=\"bucket=",
            "terraform-apply-dev.yml must run "
            "`terraform init -backend-config=bucket=...` inline "
            "(no backend.hcl materialised on the runner)",
        )
        # The state key is rendered from `${{ env.STATE_KEY }}` at
        # runtime; the env var is set to `dev/blitzlog.tfstate`
        # earlier in the workflow. We just need to confirm the
        # -backend-config=key= line exists.
        self.assertRegex(
            collapsed,
            r"-backend-config=\"key=",
            "terraform-apply-dev.yml's init must pass "
            "`-backend-config=key=...` inline",
        )

    def test_runs_terraform_plan(self):
        """The dev workflow MUST run `terraform plan` to a tfplan file."""
        self.assertRegex(
            self.text,
            r"terraform plan -no-color -input=false -out=tfplan",
            "terraform-apply-dev.yml must run "
            "`terraform plan -no-color -input=false -out=tfplan`",
        )

    def test_runs_terraform_apply(self):
        """The dev workflow MUST run `terraform apply` against the saved plan."""
        self.assertRegex(
            self.text,
            r"terraform apply -no-color -input=false tfplan",
            "terraform-apply-dev.yml must run "
            "`terraform apply -no-color -input=false tfplan` "
            "(consumes the plan from the previous step)",
        )

    def test_uploads_plan_artifact(self):
        """The dev workflow MUST upload the tfplan as a workflow artifact."""
        self.assertRegex(
            self.text,
            r"name:\s*tfplan-dev",
            "terraform-apply-dev.yml's plan artifact must be named `tfplan-dev`",
        )
        self.assertRegex(
            self.text,
            r"retention-days:\s*14",
            "terraform-apply-dev.yml's plan artifact must have retention-days: 14 "
            "(matches the release.yml deploy path)",
        )

    def test_uses_oidc_for_deploy_role(self):
        """The dev workflow MUST use OIDC to assume the blitzlog-deploy-role."""
        self.assertRegex(
            self.text,
            r"role-to-assume:\s*arn:aws:iam::\$\{\{\s*vars\.AWS_ACCOUNT_ID\s*\}\}:role/blitzlog-deploy-role",
            "terraform-apply-dev.yml must OIDC into "
            "`arn:aws:iam::${{ vars.AWS_ACCOUNT_ID }}:role/blitzlog-deploy-role`",
        )

    def test_permissions_id_token_write(self):
        """The dev workflow MUST request `id-token: write` for OIDC."""
        self.assertRegex(
            self.text,
            r"(?m)^permissions:\s*\n\s+contents:\s+read\s*\n\s+id-token:\s+write",
            "terraform-apply-dev.yml must declare "
            "`permissions: contents: read, id-token: write`",
        )

    def test_concurrency_group_for_dev(self):
        """The dev workflow MUST use a `terraform-dev` concurrency
        group so a long apply blocks the next one.
        """
        self.assertRegex(
            self.text,
            r"group:\s*terraform-dev",
            "terraform-apply-dev.yml must use "
            "`concurrency.group: terraform-dev` (per-env serialisation)",
        )
        self.assertRegex(
            self.text,
            r"cancel-in-progress:\s*false",
            "terraform-apply-dev.yml's concurrency must be "
            "`cancel-in-progress: false` (a long apply should not be killed)",
        )

    def test_renders_tf_vars_from_dev_secrets(self):
        """The dev workflow MUST map per-env `_DEV` secrets to
        `TF_VAR_<name>` env vars via the `secrets[...]` index access.

        A regression to a `case`/`if` switch would duplicate the
        secret list and drift. A regression that hard-codes the
        secrets (e.g. `secrets.TF_VAR_FOO_PROD` for a dev workflow)
        would silently apply prod secrets to dev.
        """
        # Pull every TF_VAR_<NAME>_DEV secret reference.
        for required_var in (
            "TF_VAR_AWS_REGION_DEV",
            "TF_VAR_VPC_ID_DEV",
            "TF_VAR_GITHUB_APP_ID_DEV",
            "TF_VAR_GITHUB_APP_PRIVATE_KEY_DEV",
            "TF_VAR_GITHUB_WEBHOOK_SECRET_DEV",
            "TF_VAR_OPENCODE_API_KEY_DEV",
            "TF_VAR_AGENT_LOGS_BUCKET_NAME_DEV",
        ):
            self.assertIn(
                f"secrets.{required_var}",
                self.text,
                f"terraform-apply-dev.yml must read `{required_var}` "
                f"via the direct secrets.X access (env is hard-coded to dev)",
            )

    def test_no_prod_secret_access(self):
        """The dev workflow MUST NOT read `_PROD` secrets.

        Reading `_PROD` secrets in a dev workflow is either a
        copy-paste error (the prod path would deploy to dev's
        state with prod's secrets) or a privilege-escalation
        primitive (a dev dispatch with prod secrets would update
        prod SSM parameters from the dev apply).
        """
        for forbidden in (
            "TF_VAR_AWS_REGION_PROD",
            "TF_VAR_VPC_ID_PROD",
            "TF_VAR_GITHUB_APP_ID_PROD",
            "TF_VAR_GITHUB_WEBHOOK_SECRET_PROD",
            "TF_VAR_OPENCODE_API_KEY_PROD",
        ):
            self.assertNotIn(
                forbidden,
                self.text,
                f"terraform-apply-dev.yml must NOT read `{forbidden}` "
                "(the dev workflow is dev-only; prod secrets stay in the release cycle)",
            )

    def test_local_exec_python_resolution_works(self):
        """The dev workflow MUST install Python 3.12 via `mise install`.

        The local-exec in `null_resource.lambda_build` resolves
        Python 3.12 from `~/.local/share/mise/installs/python/...`
        (see infra/modules/core/lambda.tf:65-75). Without mise,
        the apply fails with "Python 3.12 not found".
        """
        self.assertIn(
            "jdx/mise-action@v4",
            self.text,
            "terraform-apply-dev.yml must use jdx/mise-action to install mise",
        )
        self.assertIn(
            "mise install",
            self.text,
            "terraform-apply-dev.yml must run `mise install` "
            "(puts Python 3.12 in ~/.local/share/mise/installs/python/... "
            "where the local-exec resolves it)",
        )


if __name__ == "__main__":
    unittest.main()
