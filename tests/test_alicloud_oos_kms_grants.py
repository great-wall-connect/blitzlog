"""Guards for the OOS parameter reads' KMS grants.

OOS encrypts every parameter with its OWN service key
(`acs:kms:<region>:<account>:secret/oos...`), not only with the CMK
named on the parameter. A caller that can decrypt the per-env CMK but
not that service key gets:

    Kms.SecretFailed / Forbidden.NoPermission
    This operation for acs:kms:cn-hongkong:<account>:secret/oos... is
    not allowed

which the deploy legs read as "the parameter is missing or empty" —
the parameter is there, the caller just cannot decrypt it.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_RAM = REPO_ROOT / "infra/alicloud/modules/core/ram.tf"
BOOTSTRAP_DEPLOY = REPO_ROOT / "infra/alicloud/bootstrap/deploy-role.tf"


class TestAlicloudOosReadersCanDecryptTheServiceKey(unittest.TestCase):
    """Every role that reads an OOS parameter needs
    `kms:Decrypt` on the OOS service key."""

    # The roles that read parameters, and where they live.
    ROLES = (
        ("FC execution role", CORE_RAM),
        ("ECS agent role", CORE_RAM),
        ("CI deploy role", BOOTSTRAP_DEPLOY),
    )

    def test_the_service_key_grant_is_present(self):
        for name, path in self.ROLES:
            text = path.read_text()
            self.assertIn(
                "secret/oos*",
                text,
                f"the {name} must be able to decrypt the OOS service "
                "key (`secret/oos*`) — without it every parameter read "
                "fails with Kms.SecretFailed",
            )

    def test_the_grants_are_decrypt_only(self):
        """The grant is a decrypt, nothing more — the OOS service key
        is service-managed, so there is no id to scope to, but there
        is also nothing to manage."""
        for name, path in self.ROLES:
            text = path.read_text()
            for match in re.finditer(r"secret/oos\*", text):
                window = text[max(0, match.start() - 600) : match.end() + 200]
                self.assertIn(
                    '"kms:Decrypt"',
                    window,
                    f"the {name}'s OOS service-key grant must be "
                    "kms:Decrypt and nothing else",
                )
                for verb in ("kms:CreateKey", "kms:DeleteKey", "kms:PutKey"):
                    self.assertNotIn(
                        verb,
                        window,
                        f"the {name} must not be able to manage the OOS " "service key",
                    )


if __name__ == "__main__":
    unittest.main()
