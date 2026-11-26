"""Guards for who owns the Alicloud CMK.

The CMK that encrypts the OOS parameters lives in the BOOTSTRAP
stack, not in the per-env stack, for two reasons:

* One key for the whole account. The bootstrap creates the parameters
  the deploy legs read and the per-env stacks create the ones the
  runtime reads; both must be under the same key, or every reader
  needs a grant per key.
* The bootstrap can NAME the key, so the CI deploy role's grant is
  `kms:Decrypt` on `key/<id>` rather than on `key/*`. A role cannot
  be granted access to a key that does not exist yet, so the per-env
  stack — whose apply the CI role runs — could never name it.

The per-env stacks get the key's id from the OOS parameter the
bootstrap publishes (`blitzlog/shared/kms-key-id`): publishing it
rather than hard-coding it is what keeps the grant honest, because a
key that is recreated would otherwise leave every reader pointing at
a key that no longer exists.
"""

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO_ROOT / "infra/alicloud/bootstrap"
CORE = REPO_ROOT / "infra/alicloud/modules/core"
DEV = REPO_ROOT / "infra/alicloud/dev"
PROD = REPO_ROOT / "infra/alicloud/prod"
RELEASE_YML = REPO_ROOT / ".github/workflows/release.yml"
APPLY_DEV_YML = REPO_ROOT / ".github/workflows/terraform-apply-dev.yml"


class TestTheCmkLivesInTheBootstrap(unittest.TestCase):
    """The bootstrap creates the key and publishes its id."""

    def test_the_bootstrap_creates_the_cmk(self):
        kms = (BOOTSTRAP / "kms.tf").read_text()
        self.assertIn(
            'resource "alicloud_kms_key" "secrets"',
            kms,
            "the bootstrap stack must create the CMK — the per-env "
            "stack's apply (the CI role) cannot grant access to a key "
            "that does not exist yet",
        )
        self.assertIn(
            'status                 = "Enabled"',
            kms,
            "the CMK must be enabled — a disabled key cannot encrypt",
        )

    def test_the_core_module_does_not_create_a_cmk(self):
        self.assertFalse(
            (CORE / "kms.tf").exists(),
            "the per-env stack must not create the CMK — the bootstrap "
            "owns it, so there is one key for the whole account and "
            "the CI role can be granted it by id",
        )
        for path in CORE.glob("*.tf"):
            self.assertNotIn(
                'resource "alicloud_kms_key"',
                path.read_text(),
                f"{path.name} must not create a KMS key",
            )

    def test_the_key_id_is_published_as_a_plain_parameter(self):
        kms = (BOOTSTRAP / "kms.tf").read_text()
        self.assertIn(
            'resource "alicloud_oos_parameter" "kms_key_id"',
            kms,
            "the bootstrap must publish the key's id so the per-env "
            "stacks can name it",
        )
        self.assertIn(
            'parameter_name = "blitzlog/shared/kms-key-id"',
            kms,
            "the published parameter must have the name the deploy " "legs read",
        )

    def test_the_bootstrap_publishes_the_key_id_as_an_output(self):
        outputs = (BOOTSTRAP / "outputs.tf").read_text()
        self.assertIn(
            'output "kms_key_id"',
            outputs,
            "a local apply of a per-env stack needs the key id — the "
            "bootstrap must publish it",
        )


class TestThePerEnvStacksTakeTheKeyId(unittest.TestCase):
    """The key id reaches the core module as a variable."""

    def test_the_core_module_declares_it(self):
        variables = (CORE / "variables.tf").read_text()
        self.assertIn(
            'variable "kms_key_id"',
            variables,
            "the core module must take the key id as a variable",
        )

    def test_the_core_module_uses_it_everywhere_the_key_is_named(self):
        """Every place that named the CMK must name the variable —
        a leftover `alicloud_kms_key.secrets.id` would be a reference
        to a resource that no longer exists in this module."""
        for path in CORE.glob("*.tf"):
            text = path.read_text()
            self.assertNotIn(
                "alicloud_kms_key.secrets",
                text,
                f"{path.name} still references the CMK resource directly — "
                "the key is owned by the bootstrap stack now",
            )

    def test_the_parameters_are_encrypted_with_it(self):
        secrets = (CORE / "secrets.tf").read_text()
        self.assertIn(
            "key_id                = var.kms_key_id",
            secrets,
            "every parameter must be encrypted with the shared CMK",
        )
        self.assertEqual(
            secrets.count("key_id                = var.kms_key_id"),
            secrets.count('resource "alicloud_oos_secret_parameter"'),
            "every secret parameter must carry the key id",
        )

    def test_the_fc_runtime_gets_it(self):
        """The function creates parameters at run time (the per-issue
        ephemeral GitHub token), so it needs the key id too."""
        fc = (CORE / "function_compute.tf").read_text()
        self.assertIn(
            "ALIYUN_KMS_KEY_ID = var.kms_key_id",
            fc,
            "the function's ALIYUN_KMS_KEY_ID must come from the "
            "variable — the runtime creates parameters of its own",
        )

    def test_both_stacks_pass_it_through(self):
        for stack in (DEV, PROD):
            variables = (stack / "variables.tf").read_text()
            main = (stack / "main.tf").read_text()
            self.assertIn(
                'variable "kms_key_id"',
                variables,
                f"{stack.name}/variables.tf must declare kms_key_id",
            )
            self.assertIn(
                "kms_key_id = var.kms_key_id",
                main,
                f"{stack.name}/main.tf must pass kms_key_id to the core " "module",
            )


class TestTheCiRoleCanDecryptTheOneKey(unittest.TestCase):
    """The CI role gets `kms:Decrypt` on the one key — not on
    `key/*`, and with no key-management verbs left over."""

    def test_no_key_management_verbs(self):
        text = (BOOTSTRAP / "deploy-role.tf").read_text()
        for verb in (
            "kms:CreateKey",
            "kms:DeleteKey",
            "kms:CreateGrant",
            "kms:ListGrants",
        ):
            self.assertNotIn(
                verb,
                text,
                f"the CI deploy role must not hold {verb} — the "
                "per-env stacks create no keys, and a CI credential "
                "that can rotate or delete any key in the account is a "
                "blast radius nobody needs",
            )

    def test_the_grant_names_the_one_key(self):
        text = (BOOTSTRAP / "deploy-role.tf").read_text()
        self.assertIn(
            "key/${alicloud_kms_key.secrets.id}",
            text,
            "the CI role's decrypt grant must name the one CMK, not " "`key/*`",
        )
        self.assertNotIn(
            ":key/*",
            text,
            "the CI role must not be able to decrypt every key in the " "account",
        )

    def test_the_role_may_read_the_published_key_id(self):
        text = (BOOTSTRAP / "deploy-role.tf").read_text()
        self.assertIn(
            '"oos:GetParameter"',
            text,
            "the CI reads the published key id — it is a PLAIN OOS "
            "parameter, so the verb is oos:GetParameter",
        )
        self.assertIn(
            "parameter/blitzlog/shared/*",
            text,
            "the read must be scoped to the shared parameters",
        )


class TestTheDeployLegsUseThePublishedKeyId(unittest.TestCase):
    """Both deploy legs read the key id from OOS."""

    WORKFLOWS = (RELEASE_YML, APPLY_DEV_YML)

    def test_both_read_it(self):
        for path in self.WORKFLOWS:
            text = path.read_text()
            self.assertIn(
                "blitzlog/shared/kms-key-id",
                text,
                f"{path.name} must read the published CMK id — a "
                "hard-coded id would point at a key that no longer "
                "exists after a rotation",
            )
            self.assertIn(
                "TF_VAR_kms_key_id=",
                text,
                f"{path.name} must hand the key id to terraform",
            )

    def test_both_refuse_to_destroy_a_kms_key(self):
        """Every OOS parameter is encrypted with the account's CMK.
        Destroying the key schedules its deletion, and a week later
        the parameters it encrypts cannot be read any more."""
        for path in self.WORKFLOWS:
            text = path.read_text()
            self.assertIn(
                "Fail if the plan would destroy a KMS key",
                text,
                f"{path.name} must refuse an apply that would destroy a " "KMS key",
            )
            self.assertIn(
                'select(.type == "alicloud_kms_key")',
                text,
                f"{path.name} must check for a KMS key in the plan",
            )


class TestTheRekeyIsATerraformReplacement(unittest.TestCase):
    """A re-key is a replacement, and it must carry the value.

    A parameter's encryption key is fixed at creation, so moving one
    onto another key means recreating it — and the provider reads an
    OOS secret parameter back WITHOUT decryption, so terraform's own
    copy of a value is always empty. A replacement whose value came
    from the state would re-create the parameter EMPTY and wipe the
    secret.

    The value therefore comes from a data source that decrypts, and
    `ignore_changes = [value]` is gone: it would pin the planned
    value to that empty state copy.
    """

    CORE_SECRETS = REPO_ROOT / "infra/alicloud/modules/core/secrets.tf"
    USER_POOL = REPO_ROOT / "infra/alicloud/user-pool/main.tf"

    def test_the_core_module_reads_the_value_with_decryption(self):
        text = self.CORE_SECRETS.read_text()
        self.assertIn(
            'data "alicloud_oos_secret_parameters" "deploy"',
            text,
            "the runtime's copy must read the deploy-time set with "
            "decryption — the provider's own read cannot",
        )
        self.assertIn("with_decryption       = true", text)
        self.assertIn("enable_details        = true", text)

    def test_the_user_pool_reads_its_own_value_with_decryption(self):
        text = self.USER_POOL.read_text()
        self.assertIn(
            'data "alicloud_oos_secret_parameters" "current"',
            text,
            "the pool's parameters must read their live value back — "
            "they are not in the deploy-time set, and a re-creation "
            "must still carry their value",
        )
        self.assertIn("with_decryption       = true", text)

    def test_nothing_ignores_the_value(self):
        """`ignore_changes = [value]` pins the planned value to the
        empty state copy — which is exactly what a re-creation would
        write."""
        for path in (self.CORE_SECRETS, self.USER_POOL):
            self.assertNotIn(
                "ignore_changes = [value]",
                path.read_text(),
                f"{path.name} must not ignore value changes — a "
                "re-creation would re-create the parameter empty",
            )

    def test_no_migration_script_is_left_behind(self):
        """The scripts existed because terraform could not carry a
        value across a re-creation. Now it can, so they are gone."""
        for name in ("migrate_oos_parameters_to_cmk.sh", "sync_oos_parameters.sh"):
            self.assertFalse(
                (REPO_ROOT / "scripts" / name).exists(),
                f"scripts/{name} must be gone — the re-key is a "
                "terraform replacement now",
            )


if __name__ == "__main__":
    unittest.main()
