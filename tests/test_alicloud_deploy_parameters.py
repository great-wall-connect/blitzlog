"""Guards for the Alicloud deploy-time parameter set.

The bootstrap stack owns the parameters the deploy legs read
(`infra/alicloud/bootstrap/secrets.tf`), so a deploy can run on an
account whose per-env stacks have never been applied. The per-env
stacks keep their own parameters — the runtime's copy — and the two
sets are kept in step by `scripts/sync_oos_parameters.sh`.

The invariant that matters: the deploy legs read exactly the
parameters the bootstrap creates. A leaf added to one and not the
other shows up as a deploy that reads a parameter nobody creates (or
creates a parameter nobody reads).
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_SECRETS = REPO_ROOT / "infra/alicloud/bootstrap/secrets.tf"
CORE_SECRETS = REPO_ROOT / "infra/alicloud/modules/core/secrets.tf"
CORE_VARIABLES = REPO_ROOT / "infra/alicloud/modules/core/variables.tf"
RELEASE_YML = REPO_ROOT / ".github/workflows/release.yml"
APPLY_DEV_YML = REPO_ROOT / ".github/workflows/terraform-apply-dev.yml"


def _leaf_list(text: str) -> list[str]:
    """The leaves of a bash array literal."""
    match = re.search(r"declare -a OOS_LEAVES=\(\s*(.*?)\s*\)", text, re.DOTALL)
    if match is None:
        raise AssertionError("no OOS_LEAVES array in the file")
    return re.findall(r'"([^"]+)"', match.group(1))


class TestTheDeployTimeParameterSet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap = BOOTSTRAP_SECRETS.read_text()
        cls.core = CORE_SECRETS.read_text()
        cls.core_variables = CORE_VARIABLES.read_text()
        cls.release = RELEASE_YML.read_text()
        cls.apply_dev = APPLY_DEV_YML.read_text()

    def test_the_bootstrap_creates_the_deploy_time_parameters(self):
        self.assertIn(
            'resource "alicloud_oos_secret_parameter" "deploy"',
            self.bootstrap,
            "the bootstrap must create the parameters the deploy legs "
            "read — otherwise the first deploy on a fresh account can "
            "never happen (the CI is what creates the per-env ones)",
        )

    def _bootstrap_leaves(self) -> list[str]:
        """The leaves the bootstrap's deploy-time set is built from."""
        match = re.search(
            r"deploy_parameter_leaves\s*=\s*\[(.*?)\]", self.bootstrap, re.DOTALL
        )
        self.assertIsNotNone(
            match, "the bootstrap must declare local.deploy_parameter_leaves"
        )
        return re.findall(r'"([^"]+)"', match.group(1))

    def test_the_deploy_time_parameters_are_encrypted(self):
        self.assertIn(
            "key_id                = alicloud_kms_key.secrets.id",
            self.bootstrap,
            "the deploy-time parameters must be encrypted with the " "shared CMK",
        )

    def test_the_deploy_time_values_are_operator_owned(self):
        """The provider reads an OOS parameter back without decryption,
        so its copy of the value is always empty. Without
        `ignore_changes` every apply would plan an update that writes
        that empty value back and wipes the secret."""
        self.assertIn(
            "ignore_changes = [value]",
            self.bootstrap,
            "the deploy-time values are operator-owned — an apply must "
            "not rewrite them",
        )

    def test_both_deploy_legs_read_the_same_leaves(self):
        release = _leaf_list(self.release)
        apply_dev = _leaf_list(self.apply_dev)
        self.assertEqual(
            release,
            apply_dev,
            "the two deploy legs must read the same parameters — one of "
            "them would otherwise render a variable the other does not",
        )

    def test_the_legs_read_exactly_what_the_bootstrap_creates(self):
        """The invariant: the deploy legs read the bootstrap's
        parameters, no more and no less."""
        created = self._bootstrap_leaves()
        self.assertEqual(
            sorted(set(created)),
            sorted(_leaf_list(self.release)),
            "the deploy legs and the bootstrap disagree about the "
            "deploy-time parameters",
        )

    def test_every_leaf_is_a_tf_variable(self):
        """The leaf name IS the variable name, with hyphens replaced by
        underscores — that is what lets the deploy legs render them
        without a mapping table."""
        for leaf in _leaf_list(self.release):
            variable = leaf.replace("-", "_")
            self.assertIn(
                f'variable "{variable}"',
                self.core_variables,
                f"the deploy-time leaf {leaf} maps to the TF variable "
                f"{variable}, which the core module must declare",
            )

    def test_the_per_env_stacks_keep_their_own_parameters(self):
        """The runtime reads the per-env set; the deploy legs read the
        deploy-time set. Two sets, two jobs — the AWS side has the
        same split."""
        self.assertIn(
            'resource "alicloud_oos_secret_parameter"',
            self.core,
            "the per-env stack must keep the parameters the runtime " "reads",
        )
        for path in (
            "blitzlog/dev/github-app/id",
            "blitzlog/dev/opencode/api-key",
            "blitzlog/dev/stt/model",
        ):
            self.assertNotIn(
                path,
                self.release,
                f"the deploy legs must not read the per-env parameter "
                f"{path} — that is the runtime's copy",
            )


if __name__ == "__main__":
    unittest.main()
