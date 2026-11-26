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
MIGRATE_SCRIPT = REPO_ROOT / "scripts/migrate_oos_parameters_to_cmk.sh"


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

    def test_both_refuse_to_replace_a_parameter(self):
        """A replacement re-creates a parameter with an EMPTY value —
        the provider reads it back without decryption — so the apply
        must stop before that happens."""
        for path in self.WORKFLOWS:
            text = path.read_text()
            self.assertIn(
                "Fail if an OOS parameter would be replaced",
                text,
                f"{path.name} must refuse an apply that would replace an "
                "OOS parameter",
            )
            self.assertIn(
                "migrate_oos_parameters_to_cmk.sh",
                text,
                f"{path.name} must point at the migration script",
            )


class TestTheMigrationScript(unittest.TestCase):
    """The re-key has to happen in OOS: the values only exist there,
    and terraform cannot write them back (its copy is always empty)."""

    def test_it_exists_and_is_executable(self):
        self.assertTrue(
            MIGRATE_SCRIPT.exists(),
            "the migration script must exist — the values live in OOS, "
            "so the re-key cannot be a terraform apply",
        )
        self.assertTrue(
            MIGRATE_SCRIPT.stat().st_mode & 0o111,
            "the migration script must be executable",
        )

    def test_it_is_a_dry_run_by_default(self):
        text = MIGRATE_SCRIPT.read_text()
        self.assertIn(
            "--apply",
            text,
            "the migration must be a dry run unless --apply is passed",
        )
        self.assertIn(
            "APPLY=0",
            text,
            "the default must be a dry run — the script deletes and "
            "recreates parameters",
        )

    def test_it_reads_and_rewrites_the_value(self):
        text = MIGRATE_SCRIPT.read_text()
        for expected in (
            "GetSecretParameter",
            "DeleteSecretParameter",
            "CreateSecretParameter",
            "--KeyId",
        ):
            self.assertIn(
                expected,
                text,
                f"the migration script must use {expected} — a parameter "
                "cannot be re-keyed without deleting and recreating it",
            )

    def test_it_verifies_the_value_survived(self):
        text = MIGRATE_SCRIPT.read_text()
        self.assertIn(
            "does not read back the same value",
            text,
            "the migration must verify the value survived — the whole "
            "point is that no secret is lost",
        )


if __name__ == "__main__":
    unittest.main()
