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
        exactly like "the state was lost"."""
        text = _read(RELEASE_YML)
        self.assertIn(
            "'prod/blitzlog.tfstate' || 'dev/blitzlog.tfstate'",
            text,
            "the workflow's STATE_KEY expression must produce the same "
            "keys the backend .hcl examples use",
        )
        for path, key in (
            (
                os.path.join(ALICLOUD_DEV, "dev-backend.hcl.example"),
                "dev/blitzlog.tfstate",
            ),
            (
                os.path.join(ALICLOUD_PROD, "prod-backend.hcl.example"),
                "prod/blitzlog.tfstate",
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
            "oidc:token.actions.githubusercontent.com:aud",
            self.text,
            "the trust policy must condition on the OIDC audience",
        )
        self.assertIn(
            "sts.aliyuncs.com",
            self.text,
            "the trust policy's audience must be sts.aliyuncs.com",
        )
        self.assertIn(
            "repo:${local.github_org}*/${local.github_repo}*",
            self.text,
            "the trust policy must match the same branch wildcard as the "
            "packer role",
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
            "kms:CreateSecret",
            "kms:PutSecretValue",
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


class TestAlicloudKmsSecretFetchReplacesTfvars(unittest.TestCase):
    """CI must not need a gitignored `alicloud.tfvars`.

    `<provider>.tfvars` is gitignored, so it can never exist in a
    fresh CI checkout. The AWS path solved this by rendering TF_VAR_*
    from SSM; the Alicloud path needs the same from KMS.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = _read(RELEASE_YML)

    def test_kms_render_step_exists(self):
        self.assertIn(
            "Render TF_VAR_* env from KMS",
            self.text,
            "the Alicloud path needs a KMS-backed TF_VAR render — the "
            "analogue of the AWS SSM fetch",
        )

    def test_kms_step_reads_the_secret_paths(self):
        """The KMS secret paths mirror the SSM paths byte-for-byte, so
        the mapping from leaf name to TF variable is the same."""
        for secret in (
            "github-app/id",
            "github-app/private-key",
            "github-app/installation-id",
            "github-webhook/secret",
            "opencode/api-key",
            "stt/api-key",
            "stt/model",
            "stt/language",
            "stt/models-bucket",
        ):
            self.assertIn(
                f'"{secret}"',
                self.text,
                f"the KMS render step must read /blitzlog/<env>/{secret}",
            )

    def test_kms_step_covers_every_required_secret_variable(self):
        """Each secret maps to a TF variable the stack declares. A
        missing mapping means the plan prompts for a value that CI
        cannot supply."""
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
                f"var={var}",
                self.text,
                f"the KMS render step must map a secret to {var}",
            )

    def test_plan_does_not_hard_fail_on_a_missing_tfvars_file(self):
        """The pre-#125 Alicloud plan FATAL'd when `alicloud.tfvars`
        was absent — which is ALWAYS the case in CI. It must now fall
        through to the KMS-supplied values."""
        self.assertNotIn(
            "FATAL: ${TFVARS_FILE} not found",
            self.text,
            "the plan must not hard-fail on a missing tfvars file — in CI "
            "the file is always missing (it is gitignored) and the values "
            "come from KMS",
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


if __name__ == "__main__":
    unittest.main()
