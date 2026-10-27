"""Regression guards for the blitzlog-<env>-ec2-agent-role IAM policy.

The EC2 instance role is the identity the AGENT runs under: the
watchdog reads a few SSM parameters with it, and the container shares
the instance's network. If the role can read more than it needs, the
container inherits that reach — and the container is where untrusted
code (the agent, the target repo's build, a malicious dependency)
runs.

Issue #175 is the concrete case. The container could reach the
instance metadata service, assume the instance role, and read:

  * `/blitzlog/{dev,prod}/github-app/private-key` — the GitHub App's
    private key, which mints installation tokens for EVERY repository
    the App is installed on (the platform scopes its per-run token to
    the target repository; a private-key holder is not scoped at all);
  * `/blitzlog/dev/github-webhook-secret` — the HMAC secret that
    authenticates webhook deliveries, i.e. the ability to forge them.

The Terraform in this repo does NOT grant the EC2 role either of
those: the GitHub App's key and the webhook secret belong to the
Lambda, which is the only component that mints tokens. The live role
had drifted (prod was last applied before the policy was split), so
this file pins the Terraform side and the drift cannot come back
silently.

The tests read the HCL as a string and scan the role's inline policy
with regex — the same pattern as `tests/test_lambda_iam.py`, for the
same reason: a grant that is too broad is invisible at runtime until
something abuses it.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
IAM_TF = REPO_ROOT / "infra" / "aws" / "modules" / "core" / "iam.tf"

# Parameters the EC2 role must NEVER read: they are the Lambda's, not
# the agent host's. Each entry is (parameter-name-fragment, why).
FORBIDDEN_FOR_EC2 = (
    (
        "github_app_private_key",
        (
            "the GitHub App's private key mints installation tokens for every "
            "repository the App is installed on — the EC2 role has no business "
            "with it (issue #175)"
        ),
    ),
    (
        "github_app_id",
        "the App id belongs to the Lambda's token minting (issue #175)",
    ),
    (
        "github_app_installation_id",
        "the installation id belongs to the Lambda's token minting (issue #175)",
    ),
    (
        "github_webhook_secret",
        (
            "the webhook secret authenticates inbound deliveries; readable by "
            "the agent, it can be forged (issue #175)"
        ),
    ),
)


def _read(path):
    return path.read_text(encoding="utf-8")


def _inline_policy_body(iam_tf: str, resource_marker: str) -> str:
    """Return the HCL body of an inline role policy resource.

    `resource_marker` is the full resource header, e.g.
    `resource "aws_iam_role_policy" "ec2_agent_policy" {`. The body is
    the text between the outermost braces of that resource.
    """
    start_idx = iam_tf.find(resource_marker)
    assert start_idx >= 0, f"could not find {resource_marker} in {IAM_TF}"
    body_open = iam_tf.find("{", start_idx)
    depth = 0
    index = body_open
    while index < len(iam_tf):
        char = iam_tf[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                break
        index += 1
    return iam_tf[body_open + 1 : index]


def _ec2_policy_body(iam_tf: str) -> str:
    return _inline_policy_body(
        iam_tf, 'resource "aws_iam_role_policy" "ec2_agent_policy" {'
    )


def _lambda_policy_body(iam_tf: str) -> str:
    return _inline_policy_body(
        iam_tf, 'resource "aws_iam_role_policy" "lambda_policy" {'
    )


class TestEc2AgentRoleCannotReadTheLambdasSecrets(unittest.TestCase):
    """The EC2 role's SSM grants must stay narrow."""

    @classmethod
    def setUpClass(cls):
        cls.iam_tf = _read(IAM_TF)
        cls.ec2_policy = _ec2_policy_body(cls.iam_tf)

    def test_the_ec2_policy_block_was_found(self):
        """A parser change that silently returns "" must not make the
        assertions below vacuously true."""
        self.assertIn(
            "ssm:GetParameter",
            self.ec2_policy,
            "the EC2 agent policy must grant ssm:GetParameter for the "
            "watchdog's own reads — if this fails, the policy block "
            "moved and this test needs updating",
        )

    def test_ec2_policy_grants_no_read_on_the_app_credentials(self):
        for fragment, why in FORBIDDEN_FOR_EC2:
            with self.subTest(parameter=fragment):
                self.assertNotIn(
                    fragment,
                    self.ec2_policy,
                    f"the EC2 agent policy must not reference {fragment} — {why}",
                )

    def test_ec2_policy_grants_no_wildcard_ssm_read(self):
        """A blanket `parameter/blitzlog/*` grant would re-open the
        whole namespace, including every future parameter."""
        self.assertNotIn(
            'parameter/blitzlog/*"',
            self.ec2_policy,
            "the EC2 agent policy must not grant a blanket read on "
            "/blitzlog/* — scope every grant to the exact parameter "
            "(issue #175)",
        )
        self.assertNotIn(
            'parameter/blitzlog/*/*"',
            self.ec2_policy,
            "the EC2 agent policy must not grant a blanket read on "
            "/blitzlog/*/* — scope every grant to the exact parameter "
            "(issue #175)",
        )

    def test_ec2_policy_reads_only_the_ephemeral_token_namespace(self):
        """The one SSM path the agent host needs is the per-issue
        GitHub token it refreshes for the container."""
        reads = re.findall(r'arn:aws:ssm:[^"]*parameter/([^"]*)"', self.ec2_policy)
        for path in reads:
            with self.subTest(path=path):
                self.assertNotIn("github-app", path)
                self.assertNotIn("github-webhook", path)

    def test_the_lambda_policy_still_reads_the_app_credentials(self):
        """The mirror image of the rule above: the Lambda IS the
        component that mints tokens, so it must keep the grant. If
        this fails, the Lambda cannot mint a token any more — a
        different bug, but one this file should notice."""
        lambda_policy = _lambda_policy_body(self.iam_tf)
        self.assertIn(
            "aws_ssm_parameter.github_app_private_key.arn",
            lambda_policy,
            "the Lambda policy must keep reading the GitHub App's private "
            "key — it is what mints the per-run installation token",
        )


if __name__ == "__main__":
    unittest.main()
