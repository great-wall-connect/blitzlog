"""Tests for the Alicloud DEPLOY path (issue #125).

Issue #125 ships the Alicloud Packer tree + host bootstrap. This
file covers the other half of "getting Alicloud running": the
workflow + Terraform wiring that actually applies the Alicloud
stacks.

The bug class these guard against is a CLASS of mismatch, not a
single one. An OSS bucket name is globally unique but its REGION is
not, so pointing the backend at the wrong region fails with
"Authorization header is invalid" rather than a clear "no such
bucket" — the operator cannot tell that the bucket name is also
wrong. Every cross-file constant (bucket name, region) is therefore
pinned across BOTH sides that must agree:

  * the workflow's `-backend-config` flags,
  * the backend `.hcl.example` files,
  * the `backend "oss"` blocks in the stacks,
  * the `alicloud_region` variable defaults.

A drift in any one of them is a deploy failure with a misleading
error message.
"""

import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RELEASE_YML = os.path.join(REPO_ROOT, ".github/workflows/release.yml")

ALICLOUD_BOOTSTRAP = os.path.join(REPO_ROOT, "infra/alicloud/bootstrap")
ALICLOUD_DEV = os.path.join(REPO_ROOT, "infra/alicloud/dev")
ALICLOUD_PROD = os.path.join(REPO_ROOT, "infra/alicloud/prod")

# The REAL values for the blitzlog Alicloud account. These are the
# constants that must agree everywhere.
STATE_BUCKET = "gwc-infra-tf-state"
REGION = "cn-hongkong"


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class TestAlicloudBackendConstantsAreConsistent(unittest.TestCase):
    """The state bucket + region must be the same everywhere.

    The previous defaults were `gwc-infra-tfstate` (no hyphen) in
    `cn-hangzhou` — a bucket that does not exist, in a region that is
    not the bucket's. Both errors are silent: the bucket name is
    globally unique so a near-miss does not 404 with a clear message,
    and the region mismatch surfaces as an auth failure.
    """

    def test_backend_hcl_examples_use_the_real_bucket_and_region(self):
        """The `.example` files are the committed templates — they are
        what CI can see, and what an operator copies.

        Note: the bootstrap stack's REAL `bootstrap-backend.hcl` is
        gitignored (`.gitignore`: `infra/**/*-backend.hcl`), so it is
        not in the repo — hence the `.example` sibling. The test
        below also checks the real file when it is present locally, so
        a drift between the two is caught on the machine that has it.
        """
        for path in (
            os.path.join(ALICLOUD_BOOTSTRAP, "bootstrap-backend.hcl.example"),
            os.path.join(ALICLOUD_DEV, "dev-backend.hcl.example"),
            os.path.join(ALICLOUD_PROD, "prod-backend.hcl.example"),
        ):
            text = _read(path)
            self.assertIn(
                f'bucket = "{STATE_BUCKET}"',
                text,
                f"{path} must name the REAL state bucket {STATE_BUCKET!r}",
            )
            self.assertIn(
                f'region = "{REGION}"',
                text,
                f"{path} must use the real bucket region {REGION!r}",
            )
            self.assertNotIn(
                "gwc-infra-tfstate",
                text,
                f"{path} still names the nonexistent bucket "
                "'gwc-infra-tfstate' (no hyphen)",
            )

    def test_local_bootstrap_backend_matches_the_example(self):
        """The bootstrap stack's real `bootstrap-backend.hcl` is
        gitignored, so it exists only on the operator's machine. When
        it is there, it must agree with the committed example —
        otherwise the local bootstrap apply writes state to a
        different place than CI reads."""
        real = os.path.join(ALICLOUD_BOOTSTRAP, "bootstrap-backend.hcl")
        if not os.path.isfile(real):
            self.skipTest(
                "bootstrap-backend.hcl is gitignored and not present on "
                "this machine (CI, and any clone without a local copy)"
            )
        text = _read(real)
        self.assertIn(
            f'bucket = "{STATE_BUCKET}"',
            text,
            f"{real} must name the REAL state bucket {STATE_BUCKET!r}",
        )
        self.assertIn(
            f'region = "{REGION}"',
            text,
            f"{real} must use the real bucket region {REGION!r}",
        )

    def test_backend_blocks_in_the_stacks_match(self):
        """The `backend "oss"` blocks carry a default region — it must
        match the bucket's, or a stack applied WITHOUT a
        -backend-config region lands in the wrong region."""
        for stack in (ALICLOUD_BOOTSTRAP, ALICLOUD_DEV, ALICLOUD_PROD):
            main = _read(os.path.join(stack, "main.tf"))
            match = re.search(r'backend "oss" \{(.*?)\}', main, flags=re.DOTALL)
            self.assertIsNotNone(
                match, f'{stack}/main.tf must declare a backend "oss" block'
            )
            block = match.group(1)
            self.assertIn(
                f'region = "{REGION}"',
                block,
                f"{stack}/main.tf's backend block must default to the "
                f"bucket's region {REGION!r}",
            )

    def test_alicloud_region_variable_defaults_match(self):
        for stack in (ALICLOUD_BOOTSTRAP, ALICLOUD_DEV, ALICLOUD_PROD):
            text = _read(os.path.join(stack, "variables.tf"))
            self.assertIn(
                f'default     = "{REGION}"',
                text,
                f"{stack}/variables.tf must default alicloud_region to " f"{REGION!r}",
            )
            self.assertNotIn(
                "cn-hangzhou",
                text,
                f"{stack}/variables.tf still mentions cn-hangzhou — the "
                "Alicloud region defaults must be consistent",
            )

    def test_workflow_uses_the_same_bucket_and_region(self):
        text = _read(RELEASE_YML)
        self.assertIn(
            f"vars.TF_BACKEND_BUCKET_OSS || '{STATE_BUCKET}'",
            text,
            "the workflow's Alicloud backend bucket default must match "
            "the backend .hcl files",
        )
        self.assertIn(
            f"vars.ALICLOUD_REGION || '{REGION}'",
            text,
            "the workflow's Alicloud region default must match the "
            "backend .hcl files",
        )

    def test_state_keys_match_the_backend_hcl_examples(self):
        """The workflow passes the key via `-backend-config="key=..."`
        from a single expression; the `.hcl.example` files hard-code
        it. The two must agree or the workflow reads a DIFFERENT
        state file than the operator's local apply — which looks
        exactly like "the state was lost".

        Issue #191 moved the shared layout to `blitzlog/<env>.tfstate`
        on BOTH clouds, so the workflow expression and the Alicloud
        templates must both name the new keys."""
        text = _read(RELEASE_YML)
        self.assertIn(
            "'blitzlog/prod.tfstate' || 'blitzlog/dev.tfstate'",
            text,
            "the workflow's STATE_KEY expression must produce the same "
            "keys the backend .hcl examples use",
        )
        for path, key in (
            (
                os.path.join(ALICLOUD_DEV, "dev-backend.hcl.example"),
                "blitzlog/dev.tfstate",
            ),
            (
                os.path.join(ALICLOUD_PROD, "prod-backend.hcl.example"),
                "blitzlog/prod.tfstate",
            ),
        ):
            self.assertIn(
                f'key    = "{key}"',
                _read(path),
                f"{path} must use the same state key the workflow passes",
            )


class TestAlicloudBackendSupportsCredentials(unittest.TestCase):
    """The `oss` backend needs a credential. Without a documented path
    to one, `terraform init` fails with an opaque auth error."""

    def test_backend_blocks_expose_the_profile_option(self):
        """`profile` is what makes `~/.aliyun/config.json` work."""
        for stack in (ALICLOUD_BOOTSTRAP, ALICLOUD_DEV, ALICLOUD_PROD):
            main = _read(os.path.join(stack, "main.tf"))
            match = re.search(r'backend "oss" \{(.*?)\}', main, flags=re.DOTALL)
            self.assertIsNotNone(match)
            self.assertIn(
                "profile",
                match.group(1),
                f"{stack}/main.tf's backend block must expose the "
                "`profile` option so `~/.aliyun/config.json` can supply "
                "the credential",
            )

    def test_backend_examples_document_the_credential_paths(self):
        """The operator must be able to work out how to authenticate
        without reading terraform's source."""
        for path in (
            os.path.join(ALICLOUD_DEV, "dev-backend.hcl.example"),
            os.path.join(ALICLOUD_PROD, "prod-backend.hcl.example"),
        ):
            text = _read(path)
            self.assertIn(
                "ALICLOUD_ACCESS_KEY",
                text,
                f"{path} must document the env-var credential path",
            )
            self.assertIn(
                "~/.aliyun/config.json",
                text,
                f"{path} must document the shared-credentials path",
            )

    def test_workflow_exports_both_credential_names(self):
        """The backend and the provider read DIFFERENT env var names.

        The `oss` backend resolves credentials BEFORE the provider is
        configured, so it needs the `ALICLOUD_*` names while the
        provider wants `ALIBABA_CLOUD_*`. Setting only one set means
        one of the two silently has no credential.
        """
        text = _read(RELEASE_YML)
        for var in (
            "ALIBABA_CLOUD_ACCESS_KEY_ID",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
            "ALIBABA_CLOUD_SECURITY_TOKEN",
            "ALICLOUD_ACCESS_KEY",
            "ALICLOUD_SECRET_KEY",
            "ALICLOUD_SECURITY_TOKEN",
        ):
            self.assertIn(
                var,
                text,
                f"the workflow must export {var} — the provider and the "
                "oss backend read different credential env var names",
            )


class TestAlicloudDeployIsOptIn(unittest.TestCase):
    """The Alicloud path reaches a REAL account. It must be opt-in."""

    def test_deploy_is_gated_on_a_repo_variable(self):
        text = _read(RELEASE_YML)
        self.assertIn(
            "vars.ENABLE_ALICLOUD_DEPLOY",
            text,
            "the Alicloud deploy must be gated on the "
            "ENABLE_ALICLOUD_DEPLOY repo variable",
        )
        self.assertIn(
            'if [ "${ENABLE_ALICLOUD_DEPLOY:-}" != "true" ]; then',
            text,
            "the gate must be an exact == 'true' comparison so an unset "
            "variable defaults to disabled",
        )

    def test_the_old_hard_stop_is_gone(self):
        """The pre-#125 gate refused Alicloud outright. It is now an
        opt-in — the hard-stop message must not survive, or the
        opt-in path can never be reached."""
        text = _read(RELEASE_YML)
        self.assertNotIn(
            "FATAL: Alicloud deploy is disabled by design.",
            text,
            "the old hard-stop message must be replaced by the opt-in " "gate",
        )

    def test_aws_needs_no_gate(self):
        """AWS is the default path — gating it would be a regression."""
        text = _read(RELEASE_YML)
        self.assertIn(
            "BLITZLOG_PROVIDER: ${{ vars.BLITZLOG_PROVIDER || 'aws' }}",
            text,
            "BLITZLOG_PROVIDER must still default to 'aws'",
        )


class TestAlicloudOidcWiring(unittest.TestCase):
    """The OIDC assumption must name the right action and the right
    inputs.

    The first draft of this used `aliyun/aliyun-credentials-action` with
    `role_arn` + `region` inputs. The real action is
    `aliyun/configure-aliyun-credentials-action`, and its inputs are
    `oidc-provider-arn` + `role-to-assume` + `audience`. A wrong
    action name fails at job start; wrong input names fail at
    credential time — after the plan has already been built.
    """

    def test_uses_the_correct_action(self):
        text = _read(RELEASE_YML)
        self.assertIn(
            "aliyun/configure-aliyun-credentials-action@v1",
            text,
            "the Alicloud credential step must use the real action — "
            "aliyun/aliyun-credentials-action does not exist",
        )
        self.assertNotIn(
            "aliyun/aliyun-credentials-action",
            text,
            "the non-existent aliyun/aliyun-credentials-action must not " "survive",
        )

    def test_uses_the_correct_input_names(self):
        text = _read(RELEASE_YML)
        self.assertIn(
            "oidc-provider-arn:",
            text,
            "the action's OIDC provider input is `oidc-provider-arn`",
        )
        self.assertIn(
            "role-to-assume:",
            text,
            "the action's role input is `role-to-assume`, not `role_arn`",
        )
        self.assertIn(
            "audience: sts.aliyuncs.com",
            text,
            "the audience must match the OIDC provider's client id",
        )

    def test_both_roles_are_assumed(self):
        """Two different roles: the Packer build role and the deploy
        role. They have different grants — the Packer one may publish
        the image id, the deploy one may not."""
        text = _read(RELEASE_YML)
        self.assertIn("blitzlog-packer-build-role", text)
        self.assertIn("blitzlog-deploy-role", text)


class TestAlicloudOidcSubjectIsNotDoubled(unittest.TestCase):
    """The RAM role trust policies must match the real GitHub OIDC
    `sub` claim.

    The claim for this repo is:
        repo:great-wall-connect/blitzlog:ref:refs/heads/main

    `local.github_repo` is already "owner/repo", so prefixing it with
    the org again produced:
        repo:great-wall-connect*/great-wall-connect/blitzlog*:ref:refs/heads/*

    which matches NOTHING — so no workflow could ever assume the role.
    The failure is silent: the assume-role step in CI fails with an
    opaque error, and nothing in the plan looks wrong.
    """

    @classmethod
    def setUpClass(cls):
        cls.packer = _read(os.path.join(ALICLOUD_BOOTSTRAP, "packer-role.tf"))
        cls.deploy = _read(os.path.join(ALICLOUD_BOOTSTRAP, "deploy-role.tf"))
        cls.workflow = _read(RELEASE_YML)

    def test_no_role_prefixes_the_org_twice(self):
        for name, text in (("packer", self.packer), ("deploy", self.deploy)):
            self.assertNotIn(
                "${local.github_org}",
                text,
                f"the {name} role must not reference local.github_org — "
                "local.github_repo is already 'owner/repo', so prefixing "
                "the org again produces a subject that matches nothing",
            )
            self.assertNotIn(
                "github_org",
                text,
                f"the {name} role must not define or use a github_org "
                "local — it is what caused the doubled subject",
            )

    def test_the_subject_matches_the_real_claim(self):
        """Alicloud's RAM does not use the AWS condition keys for an
        OIDC token — the claims live under `oidc:aud` / `oidc:iss` /
        `oidc:sub`. With the AWS key names
        (`oidc:token.actions.githubusercontent.com:*`) the service
        populates nothing under them, so the conditions can never be
        satisfied and every assume is denied with
        `NoPermissionType: ImplicitDeny`."""
        for name, text in (("packer", self.packer), ("deploy", self.deploy)):
            self.assertIn(
                '"oidc:sub" = "repo:great-wall-connect*/blitzlog*' ':ref:refs/heads/*"',
                text,
                f"the {name} role's sub condition must use Alicloud's "
                '"oidc:sub" key and a wildcard that covers the numeric '
                "owner/repo ids GitHub embeds in the subject",
            )
            self.assertIn(
                '"oidc:aud" = "sts.aliyuncs.com"',
                text,
                f"the {name} role's audience condition must use "
                'Alicloud\'s "oidc:aud" key — the claim is a plain '
                "string",
            )
            self.assertNotIn(
                '"oidc:token.actions.githubusercontent.com:aud"',
                text,
                f"the {name} role must not use the AWS condition keys — "
                "Alicloud's STS never populates them, so the conditions "
                "can never match",
            )

    def test_the_oidc_provider_arn_matches_the_workflow(self):
        """The role's Federated principal and the workflow's
        `oidc-provider-arn` must name the SAME provider.

        The provider ARN's last segment is the IdP NAME from the RAM
        console ("blitzlog"), NOT an `<org>/<repo>` path: Alicloud's
        IdP-name field takes no slash, so the provider cannot be
        registered as "great-wall-connect/blitzlog". Naming a
        provider that does not exist fails the OIDC step with
        "No such OIDC Provider registered"."""
        for name, text in (("packer", self.packer), ("deploy", self.deploy)):
            self.assertIn(
                "oidc-provider/${local.oidc_provider_name}",
                text,
                f"the {name} role's Federated principal must name the "
                "provider by its IdP name",
            )
        self.assertIn(
            "oidc-provider-arn: acs:ram::${{ vars.ALICLOUD_ACCOUNT_ID }}:"
            "oidc-provider/blitzlog",
            self.workflow,
            "the workflow's oidc-provider-arn must match the trust "
            "policy's Federated principal",
        )

    def test_the_workflow_does_not_double_the_org_either(self):
        """The workflow hard-codes the provider ARN — it must not
        repeat the org there either."""
        self.assertNotIn(
            "oidc-provider/great-wall-connect/great-wall-connect",
            self.workflow,
            "the workflow's oidc-provider-arn must not repeat the org",
        )


class TestAlicloudDeployRoleExists(unittest.TestCase):
    """The deploy role has to exist, with an OIDC trust policy and
    the grants the apply needs."""

    @classmethod
    def setUpClass(cls):
        cls.path = os.path.join(ALICLOUD_BOOTSTRAP, "deploy-role.tf")
        cls.text = _read(cls.path)

    def test_role_file_exists(self):
        self.assertTrue(os.path.isfile(self.path))

    def test_role_has_an_oidc_trust_policy(self):
        self.assertIn('resource "alicloud_ram_role" "deploy"', self.text)
        self.assertIn(
            '"oidc:aud"',
            self.text,
            "the trust policy must condition on the OIDC audience "
            "(Alicloud's key, not the AWS one)",
        )
        self.assertIn(
            "sts.aliyuncs.com",
            self.text,
            "the trust policy's audience must be sts.aliyuncs.com",
        )
        self.assertIn(
            '"oidc:sub" = "repo:great-wall-connect*/blitzlog*' ':ref:refs/heads/*"',
            self.text,
            "the trust policy must match the same subject wildcard as the "
            "packer role (GitHub embeds the numeric owner/repo ids in "
            "the subject, so the wildcard has to cover them)",
        )

    def test_role_grants_the_resources_the_apply_manages(self):
        """`terraform apply` must be able to manage every resource the
        core module creates. A missing grant means the apply fails
        halfway — with state already partially written."""
        for action in (
            "fc:CreateFunction",
            "fc:UpdateFunction",
            "apigateway:CreateApi",
            "mns:CreateQueue",
            "vpc:CreateVpc",
            "vpc:CreateVSwitch",
            "ecs:CreateSecurityGroup",
            "oos:CreateSecretParameter",
            "oos:UpdateSecretParameter",
            "oos:GetSecretParameter",
            "oss:PutObject",
            "ram:CreateRole",
            "ram:AttachPolicy",
            "ram:PassRole",
        ):
            self.assertIn(
                action,
                self.text,
                f"the deploy role must grant {action} — the apply manages "
                "these resources and a missing grant fails mid-apply",
            )

    def test_role_is_attached_to_its_policy(self):
        self.assertIn(
            'resource "alicloud_ram_role_policy_attachment" "deploy"',
            self.text,
        )


class TestAlicloudOosSecretFetchReplacesTfvars(unittest.TestCase):
    """CI must not need a gitignored tfvars file.

    `*.tfvars` is gitignored, so it can never exist in a fresh CI
    checkout. The AWS path solved this by rendering TF_VAR_* from
    SSM; the Alicloud path needs the same from the OOS Parameter
    Store (the Alicloud secret store).
    """

    @classmethod
    def setUpClass(cls):
        cls.text = _read(RELEASE_YML)

    def test_oos_render_step_exists(self):
        self.assertIn(
            "Render TF_VAR_* env from OOS",
            self.text,
            "the Alicloud path needs an OOS-backed TF_VAR render — the "
            "analogue of the AWS SSM fetch",
        )

    def test_oos_step_reads_the_parameter_paths(self):
        """The deploy-time parameters are the bootstrap's, under
        `blitzlog/<env>/<leaf>` — the same leaf names the AWS side
        uses for its SSM parameters."""
        for secret in (
            "github-app-id",
            "github-app-private-key",
            "github-app-installation-id",
            "github-webhook-secret",
            "opencode-api-key",
            "stt-api-key",
            "stt-model",
            "stt-language",
            "stt-models-bucket-name",
        ):
            self.assertIn(
                f'"{secret}"',
                self.text,
                f"the OOS render step must read blitzlog/<env>/{secret}",
            )

    def test_oos_step_covers_every_required_secret_variable(self):
        """Each secret maps to a TF variable the stack declares. A
        missing mapping means the plan prompts for a value that CI
        cannot supply.

        The leaf name IS the variable name with hyphens replaced by
        underscores, so the mapping is mechanical — a leaf that does
        not map onto a declared variable would be a plan that prompts.
        """
        for var in (
            "github_app_id",
            "github_app_private_key",
            "github_app_installation_id",
            "github_webhook_secret",
            "opencode_api_key",
            "stt_api_key",
            "stt_model",
            "stt_language",
            "stt_models_bucket_name",
        ):
            self.assertIn(
                var.replace("_", "-"),
                self.text,
                f"the OOS render step must read the parameter behind {var}",
            )
        self.assertIn(
            'var="${leaf//-/_}"',
            self.text,
            "the leaf name must map onto the TF variable mechanically — "
            "a mapping table is one more thing to keep in step",
        )

    def test_plan_does_not_hard_fail_on_a_missing_tfvars_file(self):
        """The pre-#125 Alicloud plan FATAL'd when the tfvars file
        was absent — which is ALWAYS the case in CI. It must now fall
        through to the store-supplied values."""
        self.assertNotIn(
            "FATAL: ${TFVARS_FILE} not found",
            self.text,
            "the plan must not hard-fail on a missing tfvars file — in CI "
            "the file is always missing (it is gitignored) and the values "
            "come from the OOS Parameter Store",
        )

    def test_aws_plan_behaviour_is_unchanged(self):
        """The AWS plan must still pass NO -var-file: every AWS
        variable arrives as TF_VAR_* from SSM."""
        self.assertIn(
            "terraform plan -no-color -input=false -out=tfplan",
            self.text,
            "the AWS plan must stay var-file-free",
        )


class TestAlicloudBundleBuildTargetsTheRightModule(unittest.TestCase):
    """The FC bundle is built into the MODULE's build dir, and the
    Alicloud module reads `infra/alicloud/modules/core/build` — not
    the AWS path.

    Building only the AWS dir means the Alicloud plan's
    `data.archive_file` fails with "could not archive missing
    directory", and the Alicloud zip would ship the AWS requirements
    (no alibaba SDKs → the Lambda cannot talk to KMS/OSS/ECS).
    """

    @classmethod
    def setUpClass(cls):
        cls.text = _read(RELEASE_YML)

    def test_build_dir_is_provider_scoped(self):
        self.assertIn(
            'MODULE_DIR="infra/${BLITZLOG_PROVIDER}/modules/core"',
            self.text,
            "the bundle build must target the provider's module dir",
        )
        self.assertIn(
            'BUILD_DIR="${MODULE_DIR}/build"',
            self.text,
            "the bundle build must write into the module's build dir",
        )

    def test_requirements_file_is_provider_scoped(self):
        self.assertIn(
            "requirements-alicloud.txt",
            self.text,
            "the Alicloud bundle must install requirements-alicloud.txt — "
            "the alibaba SDKs (oss2, kms, ecs) are what the Lambda needs",
        )
        self.assertIn(
            "requirements-aws.txt",
            self.text,
            "the AWS bundle must install requirements-aws.txt",
        )


def _variable_block(text, name):
    """Return the body of `variable "<name>" { ... }` from a .tf file.

    Brace-balanced, so a nested `validation { ... }` block does not
    terminate the search early. Returns "" when the variable is
    absent.
    """
    marker = f'variable "{name}" {{'
    start = text.find(marker)
    if start == -1:
        return ""
    i = text.index("{", start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j]
    return ""


class TestAlicloudNetworkIsOwnedByTheBootstrap(unittest.TestCase):
    """Alicloud has NO default VPC per region (unlike AWS, where every
    account has one), so the deployment MUST create a VPC and at least
    one VSwitch — `RunInstances` requires a `VSwitchId`.

    The bootstrap stack owns that network. dev and prod share one
    Alicloud account, and Alicloud rejects a second VPC whose CIDR
    overlaps an existing one (`OperationDenied.CidrBlockOverlapped`),
    so per-env VPCs would need per-env CIDRs. One shared VPC sidesteps
    that entirely.

    The per-env stacks therefore take `vpc_id` + `vswitch_ids` as
    REQUIRED inputs, and the core module must NOT create the network.
    """

    @classmethod
    def setUpClass(cls):
        cls.bootstrap = _read(os.path.join(ALICLOUD_BOOTSTRAP, "network.tf"))
        cls.core_ecs = _read(
            os.path.join(REPO_ROOT, "infra/alicloud/modules/core/ecs.tf")
        )
        cls.core_vars = _read(
            os.path.join(REPO_ROOT, "infra/alicloud/modules/core/variables.tf")
        )
        cls.core_fc = _read(
            os.path.join(REPO_ROOT, "infra/alicloud/modules/core/function_compute.tf")
        )

    def test_bootstrap_creates_the_vpc_and_vswitches(self):
        self.assertIn(
            'resource "alicloud_vpc" "agent"',
            self.bootstrap,
            "the bootstrap stack must create the VPC — Alicloud has no " "default one",
        )
        self.assertIn(
            'resource "alicloud_vswitch" "agent"',
            self.bootstrap,
            "the bootstrap stack must create the VSwitches — an ECS "
            "launch requires one",
        )

    def test_vswitch_is_wired_to_the_vpc(self):
        """The VSwitch must attach to the CREATED VPC, not to a
        variable — the whole bug class this replaces was wiring the
        VSwitch to an empty `var.vpc_id`."""
        self.assertRegex(
            self.bootstrap,
            r"vpc_id\s*=\s*alicloud_vpc\.agent\.id",
            "the VSwitch must attach to the VPC the bootstrap creates",
        )

    def test_vswitch_zones_come_from_a_variable(self):
        """The zone list must NOT be hard-coded — the earlier
        `cn-hangzhou-*` list was the wrong region, and the account's
        actual zone set is per-account anyway (this one has
        cn-hongkong-b/c/d and NO cn-hongkong-a)."""
        self.assertIn(
            "var.vswitch_zone_ids[count.index]",
            self.bootstrap,
            "the VSwitch zone must come from a variable",
        )
        self.assertNotIn(
            "cn-hangzhou-",
            self.bootstrap,
            "the hard-coded cn-hangzhou zone list must be gone — it was "
            "the wrong region",
        )

    def test_cidr_is_a_variable(self):
        self.assertIn(
            "cidr_block  = var.vpc_cidr_block",
            self.bootstrap,
            "the VPC's CIDR must come from a variable",
        )
        self.assertIn(
            "cidrsubnet(var.vpc_cidr_block, 8, count.index)",
            self.bootstrap,
            "the VSwitch CIDRs must be carved out of the VPC's block",
        )

    def test_core_module_does_not_create_the_network(self):
        """The core module consumes the network; it does not create
        it. This mirrors the AWS side, which also has no `aws_vpc`
        resource (AWS has a default VPC)."""
        self.assertNotIn(
            'resource "alicloud_vpc"',
            self.core_ecs,
            "the core module must not create a VPC — the bootstrap owns it",
        )
        self.assertNotIn(
            'resource "alicloud_vswitch"',
            self.core_ecs,
            "the core module must not create VSwitches — the bootstrap " "owns them",
        )

    def test_core_module_inputs_are_required(self):
        """No default: an empty `vpc_id` means the security group is
        created with no VPC, and an empty `vswitch_ids` means the
        launch has nowhere to land."""
        for name in ("vpc_id", "vswitch_ids"):
            block = _variable_block(self.core_vars, name)
            self.assertTrue(block, f"the core module must declare {name}")
            self.assertNotRegex(
                block,
                r"^\s*default\s*=",
                f"{name} must have NO default — it is required. An empty "
                "default means the ECS launch has no VSwitch.",
            )

    def test_vswitch_ids_must_not_be_empty(self):
        """A required variable can still be an empty list, and
        `var.vswitch_ids[0]` on an empty list is a plan-time
        'Invalid index' — an unhelpful error for what is really a
        missing-configuration problem."""
        block = _variable_block(self.core_vars, "vswitch_ids")
        self.assertIn(
            "length(var.vswitch_ids) > 0",
            block,
            "vswitch_ids must be validated as non-empty — the on-demand "
            "fallback path reads vswitch_ids[0]",
        )

    def test_lambda_gets_the_network(self):
        """The FC function is what launches the ECS instance, so it
        needs the VPC + VSwitches as env vars.

        `ALIYUN_DEFAULT_VSWITCH_ID` was NEVER set before this: the
        provider's on-demand fallback path reads it
        (lambda/provider/alicloud.py) and passed an empty
        `v_switch_id` to RunInstances, so the launch failed with an
        unhelpful API error.
        """
        self.assertIn("VPC_ID", self.core_fc)
        self.assertIn("VSWITCH_IDS", self.core_fc)
        self.assertIn(
            "ALIYUN_DEFAULT_VSWITCH_ID = var.vswitch_ids[0]",
            self.core_fc,
            "the on-demand fallback path needs a default VSwitch",
        )

    def test_env_stacks_require_the_network(self):
        for env in ("dev", "prod"):
            text = _read(os.path.join(REPO_ROOT, "infra/alicloud", env, "variables.tf"))
            for name in ("vpc_id", "vswitch_ids"):
                block = _variable_block(text, name)
                self.assertTrue(
                    block, f"infra/alicloud/{env}/variables.tf must declare {name}"
                )
                self.assertNotRegex(
                    block,
                    r"^\s*default\s*=",
                    f"infra/alicloud/{env}/variables.tf: {name} must have "
                    "NO default — it is required",
                )

    def test_workflow_renders_the_network_variables(self):
        """CI has no tfvars file, so the required network variables
        must be rendered from repo variables."""
        text = _read(RELEASE_YML)
        self.assertIn(
            "TF_VAR_vpc_id=${ALICLOUD_VPC_ID}",
            text,
            "the Alicloud path must render TF_VAR_vpc_id from the repo " "variable",
        )
        self.assertIn(
            "TF_VAR_vswitch_ids=",
            text,
            "the Alicloud path must render TF_VAR_vswitch_ids from the "
            "repo variable",
        )
        self.assertIn(
            "ALICLOUD_VPC_ID and ALICLOUD_VSWITCH_IDS must be set",
            text,
            "the render step must fail loudly when the network variables "
            "are missing — the plan would otherwise prompt for them",
        )


class TestAlicloudPackerEipIsOwnedByTheBootstrap(unittest.TestCase):
    """The `alicloud-ecs` builder reaches a VPC instance over an
    elastic IP, and it cannot allocate one itself.

    Its EIP allocation and its RunInstances call share one bandwidth
    setting (`internet_max_bandwidth_out`), and the two API
    constraints contradict each other: AllocateEipAddress rejects a
    bandwidth below 1 (`IllegalParam.Bandwidth`), while any non-zero
    bandwidth makes ECS hand the instance an ECS-managed public IP
    ("natpublicip"), which Alicloud refuses to replace with an EIP
    (`EIP_CAN_NOT_ASSOCIATE_WITH_PUBLIC_IP`).

    So the bootstrap stack owns the EIP and the bake takes it in as
    `eip_id`.
    """

    @classmethod
    def setUpClass(cls):
        cls.eip = _read(os.path.join(ALICLOUD_BOOTSTRAP, "eip.tf"))
        cls.outputs = _read(os.path.join(ALICLOUD_BOOTSTRAP, "outputs.tf"))
        cls.template = _read(
            os.path.join(REPO_ROOT, "infra/alicloud/packer/agent-ecs-ubuntu.pkr.hcl")
        )
        cls.workflow = _read(RELEASE_YML)

    def test_bootstrap_creates_the_eip(self):
        self.assertIn(
            'resource "alicloud_eip_address" "packer"',
            self.eip,
            "the bootstrap stack must own the EIP — the builder cannot "
            "allocate one itself",
        )

    def test_eip_is_pay_as_you_go_by_traffic(self):
        """An international account can only buy a pay-as-you-go EIP
        billed by traffic — the `PayByBandwidth` combination fails with
        `COMMODITY.INVALID_COMPONENT`. Billed by traffic, an idle EIP
        costs nothing."""
        self.assertRegex(
            self.eip,
            r'payment_type\s*=\s*"PayAsYouGo"',
            "the EIP must be pay-as-you-go — a subscription EIP cannot be "
            "created by the provider",
        )
        self.assertRegex(
            self.eip,
            r'internet_charge_type\s*=\s*"PayByTraffic"',
            "an international account can only buy an EIP billed by " "traffic",
        )

    def test_the_eip_id_is_published(self):
        self.assertIn(
            "packer_eip_id",
            self.outputs,
            "the operator (and the CI job) needs the EIP id to hand it " "to the bake",
        )
        self.assertRegex(
            self.outputs,
            r"alicloud_eip_address\.packer\.id",
            "the output must be the EIP's id",
        )

    def test_the_bake_takes_the_eip(self):
        self.assertIn(
            "eip_id",
            self.template,
            "the bake must pass the EIP to the builder — it is the "
            "instance's only public address",
        )

    def test_the_ci_job_passes_the_eip(self):
        self.assertIn(
            "ALICLOUD_EIP_ID",
            self.workflow,
            "the CI bake job must read the EIP id from a repo variable",
        )
        self.assertIn(
            '-var "eip_id=${EIP_ID}"',
            self.workflow,
            "the CI bake job must pass the EIP id to Packer",
        )


def _strip_hcl_comments(text):
    """Return `text` with the `#` comments removed.

    The bucket blocks CITE the resource names they do NOT use (e.g. the
    provider-docs quote lists alicloud_oss_bucket_versioning), so a
    naive substring check would match a comment. This strips comments
    so the checks are about the CONFIG, not about the prose.
    """
    lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out = []
        quote = None
        for ch in line:
            if quote:
                out.append(ch)
                if ch == quote:
                    quote = None
                continue
            if ch in ("'", '"'):
                quote = ch
                out.append(ch)
                continue
            if ch == "#":
                break
            out.append(ch)
        lines.append("".join(out))
    return "\n".join(lines)


class TestAlicloudBucketOneWayAttributesAreIgnored(unittest.TestCase):
    """OSS bucket settings that cannot be undone must be ignored by
    the bucket resource.

    The provider docs (alicloud_oss_bucket, "Using sub-resources with
    ignore_changes"):

      "When using standalone sub-resources (e.g.
       alicloud_oss_bucket_acl, alicloud_oss_bucket_server_side_encryption,
       alicloud_oss_bucket_versioning, ...) alongside
       alicloud_oss_bucket, you must add a lifecycle block with
       ignore_changes for the corresponding attribute on
       alicloud_oss_bucket. This prevents Terraform from detecting
       spurious diffs caused by the same configuration being managed by
       both the bucket resource and the standalone sub-resource.
       Without ignore_changes, Terraform may attempt to revert changes
       made by the sub-resource on every apply."

    Why this matters on Alicloud specifically — two of the settings are
    ONE-WAY DOORS:

      * versioning: "Once enabled, versioning cannot return to the
        Disabled state. It can only be suspended."
      * encryption: the SSE algorithm is fixed at bucket creation.

    So the bucket resource reading back `versioning = "Enabled"`
    against a config with no versioning block makes Terraform plan to
    set it to Disabled — a change the API refuses to apply. That is a
    perpetual diff, and the only way out is a `terraform taint`, which
    destroys the bucket (and, on Alicloud, burns the bucket NAME for
    4-8 hours).

    The fix is verified against the live account: a bucket whose
    versioning was enabled out-of-band still plans "No changes".
    """

    @classmethod
    def setUpClass(cls):
        cls.main_tf = _read(os.path.join(ALICLOUD_BOOTSTRAP, "main.tf"))
        cls.main_tf_code = _strip_hcl_comments(cls.main_tf)

    def _bucket_block(self, name):
        """Return the body of `resource "alicloud_oss_bucket" "<name>"`."""
        marker = f'resource "alicloud_oss_bucket" "{name}" {{'
        start = self.main_tf_code.find(marker)
        self.assertNotEqual(start, -1, f"the bucket {name} must exist")
        i = self.main_tf_code.index("{", start)
        depth = 0
        for j in range(i, len(self.main_tf_code)):
            if self.main_tf_code[j] == "{":
                depth += 1
            elif self.main_tf_code[j] == "}":
                depth -= 1
                if depth == 0:
                    return self.main_tf_code[i + 1 : j]
        return ""

    def _ignored_attributes(self, bucket_name):
        """Return the set of attributes in the bucket's
        `lifecycle.ignore_changes` list."""
        block = self._bucket_block(bucket_name)
        match = re.search(r"ignore_changes\s*=\s*\[(.*?)\]", block, flags=re.DOTALL)
        self.assertIsNotNone(
            match,
            f"the {bucket_name} bucket must have a lifecycle.ignore_changes "
            "block — the provider docs require it when a standalone "
            "sub-resource manages the same setting",
        )
        return {m.strip() for m in match.group(1).split(",") if m.strip()}

    def test_both_buckets_ignore_the_one_way_attributes(self):
        for bucket in ("agent_logs", "stt_models"):
            ignored = self._ignored_attributes(bucket)
            for attr in ("acl", "server_side_encryption_rule", "versioning"):
                self.assertIn(
                    attr,
                    ignored,
                    f"the {bucket} bucket must ignore `{attr}` — it is "
                    "managed by a standalone sub-resource, and OSS does "
                    "not let Terraform revert it",
                )

    def test_versioning_is_ignored_even_without_a_sub_resource(self):
        """`agent_logs` has NO versioning sub-resource — versioning is
        enabled on the STT-models bucket only.

        The bucket resource still READS the versioning attribute back
        from OSS. So if versioning is ever enabled on this bucket (an
        operator, a console click, a future sub-resource), the bucket
        resource plans to set it back to Disabled — which OSS refuses.

        The diff is identical whether or not a sub-resource exists, so
        the ignore must be unconditional.
        """
        main = self.main_tf_code
        # The agent_logs bucket must not have a versioning sub-resource.
        acl_pos = main.find('resource "alicloud_oss_bucket_acl" "agent_logs"')
        agent_bucket_block = self._bucket_block("agent_logs")
        self.assertNotIn(
            "alicloud_oss_bucket_versioning",
            agent_bucket_block,
            "the agent_logs bucket must not have a versioning sub-resource",
        )
        self.assertNotEqual(acl_pos, -1)

        self.assertIn(
            "versioning",
            self._ignored_attributes("agent_logs"),
            "agent_logs must ignore `versioning` even though it has no "
            "versioning sub-resource — the attribute is read back from "
            "OSS, and OSS refuses to un-enable it",
        )

    def test_ignore_lists_are_not_over_broad(self):
        """The docs say: "You only need to include the attributes in
        ignore_changes that correspond to the sub-resources you are
        actually using." An over-broad list would silently stop
        Terraform managing a setting it SHOULD manage."""
        # These attributes have no sub-resource in this stack, so they
        # must not be ignored — Terraform should keep reconciling them.
        for attr in ("policy", "logging", "website", "cors_rule"):
            for bucket in ("agent_logs", "stt_models"):
                self.assertNotIn(
                    attr,
                    self._ignored_attributes(bucket),
                    f"the {bucket} bucket must not ignore `{attr}` — there "
                    "is no sub-resource for it, so Terraform should keep "
                    "reconciling it",
                )

    def test_public_access_block_is_not_ignored(self):
        """`alicloud_oss_bucket_public_access_block` has NO inline
        equivalent on the bucket resource — there is no attribute to
        ignore, and the setting is freely togglable, so it converges
        normally."""
        for bucket in ("agent_logs", "stt_models"):
            self.assertNotIn(
                "public_access_block",
                self._ignored_attributes(bucket),
                "there is no `public_access_block` attribute on "
                "alicloud_oss_bucket — ignoring a non-existent attribute "
                "would be a config error",
            )


class TestAlicloudOssArnsUseTheBucketLocation(unittest.TestCase):
    """OSS matches a RAM grant against the bucket's OSS LOCATION
    ("oss-cn-hongkong"), NOT the region id ("cn-hongkong").

    A grant written with the region id silently never matches: the
    policy document looks right, the role is attached, the instance
    has valid credentials — and every object request still comes back
    403 AccessDenied ("The bucket you access does not belong to you").
    Verified against this account: the region-id form is denied and
    the location form is honoured (and `acs:oss:*:<account>:...`
    works too, but the location is the precise one).

    The ARNs are therefore built from the bucket's `location`
    attribute, not from `var.alicloud_region`.
    """

    @classmethod
    def setUpClass(cls):
        cls.core_oss = _read(
            os.path.join(REPO_ROOT, "infra/alicloud/modules/core/oss.tf")
        )
        cls.packer_role = _read(os.path.join(ALICLOUD_BOOTSTRAP, "packer-role.tf"))

    def test_core_arns_come_from_the_bucket_location(self):
        for bucket in ("agent_logs", "stt_models"):
            self.assertIn(
                f"{bucket}_bucket_location_resolved",
                self.core_oss,
                f"the {bucket} ARN must be built from the bucket's OSS " "location",
            )

    def test_no_oss_arn_is_built_from_the_region_id(self):
        """The region id in an OSS ARN is the failure this class
        exists for — it must not creep back in."""
        for text, what in (
            (self.core_oss, "core module"),
            (self.packer_role, "packer role"),
        ):
            self.assertNotRegex(
                text,
                r"acs:oss:\$\{var\.alicloud_region\}",
                f"the {what} must not build an OSS ARN from the region "
                "id — OSS matches grants against the bucket's location",
            )

    def test_the_packer_role_uses_the_bucket_location(self):
        self.assertIn(
            "alicloud_oss_bucket.stt_models.location",
            self.packer_role,
            "the packer role's STT-model grant must name the bucket's " "OSS location",
        )


class TestAlicloudUserPoolStack(unittest.TestCase):
    """The Alicloud mirror of `infra/aws/user-pool/`.

    The pool namespace is env-INDEPENDENT (`blitzlog/users/<login>/…`)
    and holds the Telegram bot tokens, the allowed user id, the
    per-user local-LLM config and the git author identity. Without
    it the Function Compute function's `acquire_bot_token` finds an
    empty pool and answers every webhook with 503.
    """

    @classmethod
    def setUpClass(cls):
        cls.root = os.path.join(REPO_ROOT, "infra/alicloud/user-pool")
        cls.main_tf = _read(os.path.join(cls.root, "main.tf"))
        cls.vars_tf = _read(os.path.join(cls.root, "variables.tf"))

    def test_the_stack_creates_oos_parameters(self):
        self.assertIn(
            'resource "alicloud_oos_secret_parameter" "telegram_bot_pool"',
            self.main_tf,
            "the pool must be OOS parameters — the Alicloud secret store",
        )

    def test_the_parameters_live_under_the_shared_user_namespace(self):
        """The names must match the SSM paths the AWS side uses (minus
        the leading slash) so `lambda/bot_pool.py` reads the same
        paths on both clouds."""
        self.assertIn(
            'user_root = "blitzlog/users/${var.owner_login}"',
            self.main_tf,
            "the pool namespace must be blitzlog/users/<owner_login> — the "
            "same env-independent root the AWS side uses",
        )
        for suffix in (
            "${local.user_root}/telegram/pool/",
            "${local.user_root}/telegram/allowed-user-id",
            "${local.user_root}/local-llm/",
            "${local.user_root}/git-author/",
        ):
            self.assertIn(
                suffix,
                self.main_tf,
                f"the pool parameters must be written under {suffix}",
            )

    def test_the_namespace_is_not_env_scoped(self):
        """The pool is shared across envs: dev and prod read the same
        parameters."""
        self.assertNotIn(
            'variable "environment"',
            self.vars_tf,
            "the user-pool variables must NOT declare an environment — the "
            "pool is shared across envs",
        )
        self.assertNotIn(
            "var.environment",
            self.main_tf,
            "the user-pool parameters must not be env-prefixed",
        )

    def test_the_pool_parameters_use_the_shared_cmk(self):
        """One CMK encrypts every parameter in the account.

        The pool namespace is env-independent, which used to rule out
        a CMK-encrypted parameter while the CMK was per-env (only one
        of dev/prod could have read it). The key is account-wide now,
        so the pool is encrypted with it like everything else — and
        the readers already decrypt it.
        """
        self.assertIn(
            "key_id                = var.kms_key_id",
            self.main_tf,
            "the pool parameters must be encrypted with the shared CMK",
        )

    def test_the_git_author_values_keep_their_live_value(self):
        """Mirrors the AWS sibling: the resolved default derives from
        owner_login, so an apply must not churn the parameter when only
        the derived default changed.

        The parameters read their live value back (with decryption)
        instead of using `ignore_changes`: the value follows what is
        in OOS, so a change to the derived default does not touch it
        — and a parameter that has to be re-created (its encryption
        key is fixed at creation) carries its value across rather
        than being re-created empty.
        """
        self.assertEqual(
            self.main_tf.count("ignore_changes = [value]"),
            0,
            "the pool parameters read their live value back; "
            "`ignore_changes` would swallow the change that carries "
            "the value across a re-creation",
        )
        self.assertIn(
            'data "alicloud_oos_secret_parameters" "current"',
            self.main_tf,
            "the pool parameters must read their live value back",
        )


class TestAlicloudServiceActivations(unittest.TestCase):
    """Two account-level products must be open before the per-env
    stacks can create anything in them:

      * API Gateway — `CreateApiGroup` fails with `ServiceUnOpen`
        ("Your Gateway service has not been opened."),
      * MNS — `CreateQueue` fails with `ServiceNotEnable` ("Service
        not open").

    Both activations live in the bootstrap stack: they are
    account-level, one-way (a service cannot be closed once opened)
    and accept the product's Terms of Service — so they belong to the
    operator's one-time setup, not to the CI deploy path.
    """

    @classmethod
    def setUpClass(cls):
        cls.services = _read(os.path.join(ALICLOUD_BOOTSTRAP, "services.tf"))

    def test_the_api_gateway_service_is_opened(self):
        self.assertIn(
            'data "alicloud_api_gateway_service" "open"',
            self.services,
            "the bootstrap must open the API Gateway service — the "
            "per-env stack's CreateApiGroup fails with ServiceUnOpen "
            "otherwise",
        )
        self.assertIn(
            'enable = "On"',
            self.services,
            "the API Gateway service data source must be read with "
            "enable = On — that is what opens the service",
        )

    def test_the_mns_service_is_subscribed(self):
        self.assertIn(
            'resource "alicloud_message_service_service" "open"',
            self.services,
            "the bootstrap must subscribe the account to the MNS "
            "(Simple Message Queue) service — the per-env stack's "
            "CreateQueue fails with ServiceNotEnable otherwise",
        )

    def test_the_mns_console_step_is_documented(self):
        """The subscription order alone does not open the service on
        the international site — Alicloud's own error DB says the
        service is opened from the MNS console ("激活" / 开通). The
        runbook must say so, or the operator hits an unexplained
        `ServiceNotEnable`."""
        runbook = _read(os.path.join(REPO_ROOT, "docs/ALICLOUD_DEPLOY.md"))
        self.assertIn(
            "mns.console.aliyun.com",
            runbook,
            "the runbook must point the operator at the MNS console — the "
            "service activation is a console step, not a Terraform one",
        )

    def test_the_api_gateway_resource_names_have_no_hyphens(self):
        """API Gateway names accept letters, digits and underscores
        only — a hyphen is rejected with `InvalidAppName` /
        `InvalidGroupName`."""
        apigateway = _read(
            os.path.join(REPO_ROOT, "infra/alicloud/modules/core/apigateway.tf")
        )
        for match in re.finditer(
            r'resource "alicloud_api?gateway_\w+" "\w+" \{.*?\n\}',
            apigateway,
            re.DOTALL,
        ):
            name_match = re.search(r'name\s*=\s*"([^"]+)"', match.group(0))
            self.assertIsNotNone(name_match, "the resource must set a name")
            self.assertNotIn(
                "-",
                name_match.group(1),
                f"the API Gateway name {name_match.group(1)!r} contains a "
                "hyphen — API Gateway names accept letters, digits and "
                "underscores only",
            )


class TestAlicloudWebhookFrontDoor(unittest.TestCase):
    """The webhook has to be reachable from the internet and it has
    to carry the GitHub signature.

    Three things the Alicloud side needs that the AWS side gets for
    free:

      * `auth_type = "ANONYMOUS"` — GitHub cannot present API
        Gateway app credentials, and the `APP` default answers every
        request with "Header `X-Ca-Key` is Required";
      * `stage_names = ["RELEASE"]` — an API that is not deployed to
        a stage has no route;
      * an HTTP trigger on the function — the `FCEvent` backend
        hands the function the bare body and drops the headers, so
        the `X-Hub-Signature-256` would never arrive.
    """

    @classmethod
    def setUpClass(cls):
        cls.apigateway = _read(
            os.path.join(REPO_ROOT, "infra/alicloud/modules/core/apigateway.tf")
        )
        cls.outputs = _read(
            os.path.join(REPO_ROOT, "infra/alicloud/modules/core/outputs.tf")
        )

    def test_the_api_is_anonymous(self):
        self.assertIn(
            'auth_type = "ANONYMOUS"',
            self.apigateway,
            "the API must be anonymous — GitHub cannot present API "
            "Gateway app credentials",
        )

    def test_the_api_is_deployed_to_a_stage(self):
        self.assertIn(
            'stage_names = ["RELEASE"]',
            self.apigateway,
            "the API must be deployed to a stage — an undeployed API "
            "has no route and the group's domain answers 404",
        )

    def test_the_function_has_an_http_trigger(self):
        self.assertIn(
            'resource "alicloud_fcv3_trigger" "webhook"',
            self.apigateway,
            "the function needs an HTTP trigger — the FCEvent backend "
            "drops the headers the signature check needs",
        )
        self.assertIn(
            'function_type     = "HttpTrigger"',
            self.apigateway,
            "the API's FC backend must be the HttpTrigger kind",
        )

    def test_the_webhook_url_is_the_group_domain(self):
        """The URL the GitHub App POSTs to is the API Gateway
        group's domain plus the API's path — not the API id."""
        self.assertIn(
            '"https://${alicloud_api_gateway_group.webhook.sub_domain}',
            self.outputs,
            "the webhook_url output must be the invocable URL, not the " "API id",
        )

    def test_the_role_may_be_assumed_by_the_api_gateway(self):
        """The API Gateway assumes the function role to invoke the
        function; without it in the trust policy the gateway's
        AssumeRole is rejected and every invocation 403s."""
        ram = _read(os.path.join(REPO_ROOT, "infra/alicloud/modules/core/ram.tf"))
        self.assertIn(
            "apigateway.aliyuncs.com",
            ram,
            "the function role's trust policy must allow the API "
            "Gateway to assume it",
        )
        trust_policy = ram[ram.index("assume_role_policy_document") : ram.index("})")]
        self.assertNotIn(
            "AWS",
            trust_policy,
            'Alicloud RAM only accepts the "Service" principal type — an '
            'account-root principal is rejected with "MalformedPolicyDocument '
            '— Unsupported principal type"',
        )

    def test_the_role_may_invoke_the_function(self):
        ram = _read(os.path.join(REPO_ROOT, "infra/alicloud/modules/core/ram.tf"))
        self.assertIn(
            '"fc:InvokeFunction"',
            ram,
            "the function role needs the invoke verb — the gateway "
            "calls the function with this role's credentials",
        )


class TestAlicloudOosParameterStore(unittest.TestCase):
    """The Alicloud secret store is the OOS Parameter Store.

    KMS Secrets Manager was the original choice, but on KMS 3.0 a
    secret can only be created inside a dedicated KMS instance —
    `CreateSecret` without a `DKMSInstanceId` is rejected with
    `UnsupportedOperation` — and a KMS instance is a billable
    resource (USD 500/month subscription, USD 4.50/day pay-as-you-go).
    The OOS Parameter Store is free and its API maps 1:1 onto the
    `SecretStore` interface.

    These tests pin the invariants that made the old shape fail:
    every parameter is an encrypted one, it is encrypted with the
    per-env CMK, and its name carries no leading slash (the RAM ARN
    is `acs:oos:<region>:<account>:secretparameter/<name>`, so a
    leading slash would render a double slash and the grants would
    stop matching).
    """

    @classmethod
    def setUpClass(cls):
        cls.secrets = _read(
            os.path.join(REPO_ROOT, "infra/alicloud/modules/core/secrets.tf")
        )
        cls.locals = _read(
            os.path.join(REPO_ROOT, "infra/alicloud/modules/core/locals.tf")
        )
        cls.ram = _read(os.path.join(REPO_ROOT, "infra/alicloud/modules/core/ram.tf"))

    def test_every_parameter_is_an_encrypted_oos_parameter(self):
        """No `alicloud_kms_secret` may survive — the resource is the
        KMS 3.0 shape that the apply rejects."""
        self.assertNotIn(
            "alicloud_kms_secret",
            self.secrets,
            "the Alicloud secrets must be OOS parameters — "
            "alicloud_kms_secret is rejected by KMS 3.0 with "
            "UnsupportedOperation",
        )
        parameters = re.findall(
            r'resource "alicloud_oos_secret_parameter" "(\w+)" \{(.*?)\n\}',
            self.secrets,
            re.DOTALL,
        )
        self.assertEqual(
            len(parameters),
            10,
            "every secret the core module used to create must have an "
            "OOS parameter counterpart",
        )
        for name, body in parameters:
            self.assertIn(
                'type                  = "Secret"',
                body,
                f"the {name} parameter must be an encrypted one — a plain "
                "parameter would store the GitHub App private key in the "
                "clear",
            )
            self.assertIn(
                "key_id                = var.kms_key_id",
                body,
                f"the {name} parameter must be encrypted with the per-env " "CMK",
            )

    def test_the_value_comes_from_the_deploy_time_set(self):
        """The provider's read does not decrypt, so terraform's own
        copy of a value is always empty. The value therefore comes
        from the deploy-time set (read with decryption), which is
        what makes two things work:

        * a rotation reaches the runtime's copy on the next apply,
        * a parameter can be moved onto another key — a re-creation
          carries its value instead of being re-created empty.

        `ignore_changes = [value]` would swallow exactly that: the
        planned value would be the empty state copy, and the
        re-creation would wipe the secret.
        """
        self.assertIn(
            'data "alicloud_oos_secret_parameters" "deploy"',
            self.secrets,
            "the parameters must read the deploy-time set with "
            "decryption — the provider's own read cannot",
        )
        self.assertIn(
            "with_decryption       = true",
            self.secrets,
            "the data source must decrypt — otherwise it reads the "
            "value as empty, like the resource's own read",
        )
        parameters = re.findall(
            r'resource "alicloud_oos_secret_parameter" "(\w+)" \{(.*?)\n\}',
            self.secrets,
            re.DOTALL,
        )
        self.assertEqual(len(parameters), 10)
        for name, body in parameters:
            self.assertIn(
                "data.alicloud_oos_secret_parameters.deploy[",
                body,
                f"the {name} parameter must take its value from the " "deploy-time set",
            )
            self.assertNotIn(
                "ignore_changes = [value]",
                body,
                f"the {name} parameter must not ignore value changes — "
                "that would make a re-creation wipe the secret",
            )

    def test_parameter_names_carry_no_leading_slash(self):
        """OOS names its parameters `blitzlog/<env>/...`. A leading
        slash would render the RAM ARN with two slashes after
        `secretparameter` and the grants would stop matching."""
        self.assertIn(
            'oos_root = "blitzlog/${var.environment}"',
            self.locals,
            "the OOS parameter root must not start with a slash",
        )
        self.assertNotIn(
            'oos_root = "/blitzlog',
            self.locals,
            "the OOS parameter root must not start with a slash",
        )
        for match in re.finditer(
            r"secret_parameter_name = local\.(oos_\w+)", self.secrets
        ):
            local_name = match.group(1)
            definition = re.search(
                rf"^\s*{local_name}\s*=\s*(.*)$", self.locals, re.MULTILINE
            )
            self.assertIsNotNone(
                definition, f"{local_name} must be declared in locals.tf"
            )
            self.assertIn(
                "${local.oos_root}",
                definition.group(1),
                f"{local_name} must be built from the slash-free OOS root",
            )

    def test_the_reader_roles_are_granted_the_oos_verbs(self):
        """The FC + ECS roles read parameters; the FC also lists them
        by path for the per-user bot pool."""
        for action in (
            "oos:GetSecretParameter",
            "oos:GetSecretParametersByPath",
            "oos:CreateSecretParameter",
            "oos:UpdateSecretParameter",
            "oos:DeleteSecretParameter",
        ):
            self.assertIn(
                action,
                self.ram,
                f"the RAM policies must grant {action} — the Alicloud "
                "secret store reads and writes OOS parameters",
            )
        # OOS implements its ENCRYPTED parameter kind on top of KMS
        # Secrets Manager: each parameter is a KMS secret named
        # `secret/oos/<parameter path>`, and the GetSecretParameter
        # API requires `kms:GetSecretValue` on it. A reader without
        # that grant gets `Kms.SecretFailed` /
        # `Forbidden.NoPermission` — which the deploy legs read as
        # "the parameter is missing or empty".
        #
        # The plain OOS parameter kind (`alicloud_oos_parameter`) has
        # no KMS dependency at all, but the secrets here are stored
        # encrypted, so the grant is needed.
        self.assertIn(
            "kms:GetSecretValue",
            self.ram,
            "the RAM policies must grant kms:GetSecretValue — an OOS "
            "encrypted parameter is a KMS secret, and reading one "
            "without the grant fails with Kms.SecretFailed",
        )

    def test_the_fc_role_can_read_the_published_agent_image_id(self):
        """The FC function reads the Packer-published agent image id
        at launch time (lambda/ec2.py::get_agent_ami). Without the
        grant the read is AccessDenied, and the launch fails — the
        AWS side grants the same parameter
        (iam.tf: `blitzlog/<env>/agent-ami-id-docker-ubuntu`)."""
        self.assertIn(
            "agent-ami-id-ecs-ubuntu",
            self.ram,
            "the FC role must be granted oos:GetSecretParameter on the "
            "Packer-published agent image id — the AWS side grants the "
            "equivalent SSM parameter",
        )

    def test_the_deploy_role_can_manage_the_parameters(self):
        """CI applies the per-env stack, so the deploy role needs the
        write verbs on the parameters the stack creates."""
        deploy_role = _read(os.path.join(ALICLOUD_BOOTSTRAP, "deploy-role.tf"))
        for action in (
            "oos:CreateSecretParameter",
            "oos:UpdateSecretParameter",
            "oos:GetSecretParameter",
            "oos:DeleteSecretParameter",
        ):
            self.assertIn(
                action,
                deploy_role,
                f"the deploy role must grant {action} — the apply creates "
                "the parameters",
            )
        self.assertNotIn(
            "kms:CreateSecret",
            deploy_role,
            "the KMS secret verbs are dead on the Alicloud path",
        )


if __name__ == "__main__":
    unittest.main()
