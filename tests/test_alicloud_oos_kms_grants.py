"""Guards for the OOS parameter reads' KMS grants.

OOS implements its *encrypted* parameter kind on top of KMS Secrets
Manager: each encrypted parameter is a KMS secret named
`secret/oos/<parameter path>`, and the API docs require
`kms:GetSecretValue` on that secret. A caller without it gets:

    Kms.SecretFailed / Forbidden.NoPermission
    This operation for acs:kms:<region>:<account>:secret/oos/... is
    not allowed

which the deploy legs read as "the parameter is missing or empty" —
the parameter is there.

The trap this file pins: `kms:Decrypt` is the action for a CMK
(`key/...`), NOT for a secret (`secret/...`). Granting Decrypt on
`secret/oos*` looks right and does nothing.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_RAM = REPO_ROOT / "infra/alicloud/modules/core/ram.tf"
BOOTSTRAP_DEPLOY = REPO_ROOT / "infra/alicloud/bootstrap/deploy-role.tf"

# The action that unlocks a KMS secret, and the one that unlocks a CMK.
GET_SECRET_VALUE = "kms:GetSecretValue"
DECRYPT = "kms:Decrypt"


def _statements(text: str) -> list[tuple[list[str], list[str]]]:
    """The (actions, resources) pairs of every policy statement.

    The RAM policies in these files are a flat list of statements,
    each with an `Action` list followed by a `Resource` list (or a
    single resource string).
    """
    pairs = []
    for match in re.finditer(r"Action\s*=\s*\[(?P<actions>.*?)\]", text, re.DOTALL):
        actions = re.findall(r'"([^"]+)"', match.group("actions"))
        tail = text[match.end() : match.end() + 2000]
        resource_match = re.search(
            r"""Resource\s*=\s*(?:\[(?P<list>.*?)\]|"(?P<one>[^"]+)")""",
            tail,
            re.DOTALL,
        )
        if resource_match is None:
            continue
        if resource_match.group("list") is not None:
            resources = re.findall(r'"([^"]+)"', resource_match.group("list"))
        else:
            resources = [resource_match.group("one") or ""]
        pairs.append((actions, resources))
    return pairs


class TestAlicloudOosReadersCanDecryptTheServiceKey(unittest.TestCase):
    """Every role that reads an OOS parameter needs
    `kms:GetSecretValue` on the OOS secret."""

    # The roles that read parameters, and where they live.
    ROLES = (
        ("FC execution role", CORE_RAM),
        ("ECS agent role", CORE_RAM),
        ("CI deploy role", BOOTSTRAP_DEPLOY),
    )

    @staticmethod
    def _statements_touching(text: str, resource_prefix: str):
        """The statements that grant access to a matching resource."""
        return [
            (actions, resources)
            for actions, resources in _statements(text)
            if any(resource_prefix in resource for resource in resources)
        ]

    def test_the_oos_secret_grant_is_present(self):
        for name, path in self.ROLES:
            text = path.read_text()
            matching = self._statements_touching(text, "secret/oos")
            self.assertTrue(
                matching,
                f"the {name} must be able to read the OOS secret "
                "(`secret/oos/...`) — without it every parameter read "
                "fails with Kms.SecretFailed",
            )

    def test_the_read_grant_is_get_secret_value(self):
        """`kms:Decrypt` is the action for a CMK (`key/...`), not for
        a secret (`secret/...`) — the two are not interchangeable."""
        for name, path in self.ROLES:
            text = path.read_text()
            for actions, resources in self._statements_touching(text, "secret/oos"):
                self.assertIn(
                    GET_SECRET_VALUE,
                    actions,
                    f"the {name}'s OOS grant {actions} on {resources} must "
                    f"include {GET_SECRET_VALUE} — {DECRYPT} applies to "
                    "CMKs (`key/...`), not to the KMS secrets OOS keeps "
                    "the parameters in",
                )

    def test_no_role_decrypts_a_secret_resource(self):
        for name, path in self.ROLES:
            text = path.read_text()
            for actions, resources in self._statements_touching(text, "secret/oos"):
                self.assertNotIn(
                    DECRYPT,
                    actions,
                    f"the {name} must not {DECRYPT} a secret resource "
                    f"({resources}) — the action for a KMS secret is "
                    f"{GET_SECRET_VALUE}",
                )

    def test_the_cmk_grant_is_decrypt_on_a_key(self):
        """The customer CMK is a `key/...` resource, so Decrypt on it
        is right — that is the one place Decrypt belongs."""
        for name, path in self.ROLES:
            text = path.read_text()
            matching = self._statements_touching(text, ":key/")
            self.assertTrue(
                matching,
                f"the {name} must be able to decrypt the customer CMK the "
                "parameters are encrypted with",
            )
            decrypting = [actions for actions, _ in matching if DECRYPT in actions]
            self.assertTrue(
                decrypting,
                f"the {name} holds statements on a CMK ({matching}) but "
                f"none of them grants {DECRYPT}",
            )

    def test_the_ci_role_can_list_the_parameters(self):
        """The per-env stack reads its values through a data source,
        and the RAM table for `oos:ListSecretParameters` authorizes
        the action against the whole namespace — the list is not
        authorized against the name it is filtered by.

        Without the grant the plan fails with "User has no permission
        to do the action: (ListSecretParameters)"."""
        text = BOOTSTRAP_DEPLOY.read_text()
        listing = [
            (actions, resources)
            for actions, resources in _statements(text)
            if "oos:ListSecretParameters" in actions
        ]
        self.assertTrue(
            listing,
            "the CI deploy role must be able to list the OOS "
            "parameters — the per-env stack reads them through a data "
            "source",
        )
        for actions, resources in listing:
            self.assertTrue(
                any(resource.endswith("secretparameter/*") for resource in resources),
                f"the list grant must cover the namespace "
                f"({resources}) — RAM authorizes it against the whole "
                "namespace, not against the name the list is filtered by",
            )

    def test_the_ci_role_can_read_what_the_provider_refreshes(self):
        """The provider reads a resource's attributes and its tags on
        every refresh — so a plan needs those verbs, and each one
        failed the deploy until it was granted.

        The resource the check ran against is in the API's
        `AccessDeniedDetail.EncodedDiagnosticMessage`; `ram
        DecodeDiagnosticMessage` decodes it, and
        scripts/decode_ram_diagnostics.sh does that for a failed
        plan.
        """
        text = BOOTSTRAP_DEPLOY.read_text()
        for action, resource in (
            ("oos:ListTagResources", "secretparameter/blitzlog/*"),
            ("ram:ListTagResources", None),
            ("mns:GetQueueAttributes", None),
            ("ecs:DescribeSecurityGroupAttribute", None),
            ("oos:ListSecretParameters", "secretparameter/*"),
        ):
            self.assertIn(
                action,
                text,
                f"the CI deploy role must hold {action} — the provider "
                "reads it on every refresh, and the plan fails without it",
            )
            if resource is not None:
                self.assertIn(
                    resource,
                    text,
                    f"{action} must be scoped to {resource}",
                )

    def test_the_ci_role_cannot_manage_the_oos_secrets(self):
        """The CI role reads parameters; it does not manage them."""
        text = BOOTSTRAP_DEPLOY.read_text()
        for verb in ("kms:CreateSecret", "kms:DeleteSecret", "kms:UpdateSecret"):
            self.assertNotIn(
                verb,
                text,
                f"the CI deploy role must not hold {verb} — it reads the "
                "OOS parameters, it does not manage them",
            )


if __name__ == "__main__":
    unittest.main()
