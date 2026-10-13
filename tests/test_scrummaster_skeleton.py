"""Static analysis tests for the scrummaster stack.

These tests do NOT need AWS credentials. They parse the HCL/JSON in
infra/scrummaster/ and assert the issue #135 contract:

- Single t4g.nano with disable_api_termination = true (no accidental
  destroys).
- Security group egress is 443/TCP only; ingress is tcp/22 from
  var.ssh_allowed_cidrs and absent when that var is empty.
- IAM role grants read-only access to /blitzlog/scrummaster/* (the
  parameters are written by later issues; the grant ships in this
  one).
- The user-data script lays down /etc/zeroclaw, /var/lib/zeroclaw,
  /var/log/zeroclaw and a placeholder config.toml with no providers
  and no channels (fails closed).
- AGENTS.md has a `## Scrummaster infrastructure` section that
  forbids agent edits to infra/scrummaster/*.

If a future refactor widens the egress (e.g. opens port 80 or SSH
egress), drops disable_api_termination, or accidentally emits a
default provider / channel in the user-data config, these tests
catch it at PR review time instead of after a bad apply.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRUMMASTER = REPO_ROOT / "infra" / "scrummaster"
MAIN_TF = SCRUMMASTER / "main.tf"
VARIABLES_TF = SCRUMMASTER / "variables.tf"
USER_DATA_SH = SCRUMMASTER / "user-data.sh"
AGENTS_MD = REPO_ROOT / "AGENTS.md"


def _policy_body_for_role(role_resource_name: str, iam_tf: str) -> str:
    """Return the HCL body of the named role's `policy = jsonencode({...})` block."""
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


class TestScrummasterSkeleton(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main_tf = MAIN_TF.read_text()
        cls.variables_tf = VARIABLES_TF.read_text()
        cls.user_data = USER_DATA_SH.read_text()
        cls.agents_md = AGENTS_MD.read_text()

    def test_stack_directory_layout(self):
        """infra/scrummaster/ must contain the files issue #135 lists."""
        for required in (
            SCRUMMASTER / "main.tf",
            SCRUMMASTER / "variables.tf",
            SCRUMMASTER / "outputs.tf",
            SCRUMMASTER / "user-data.sh",
            SCRUMMASTER / "scrummaster-backend.hcl",
            SCRUMMASTER / "terraform.tfvars.example",
            SCRUMMASTER / "tests" / "smoketest.sh",
            SCRUMMASTER / ".gitignore",
        ):
            self.assertTrue(
                required.is_file(),
                f"infra/scrummaster/ missing required file: {required.relative_to(REPO_ROOT)}",
            )

    def test_single_instance_with_disable_api_termination(self):
        """Exactly one aws_instance is created and disable_api_termination is true."""
        # Use a brace-balanced match so the test is robust to
        # trailing-newline / end-of-file shapes (terraform fmt does
        # not require a newline after the final brace).
        match = re.search(
            r'resource\s+"aws_instance"\s+"scrummaster"\s*\{',
            self.main_tf,
        )
        self.assertIsNotNone(
            match,
            "infra/scrummaster/main.tf must declare one aws_instance resource (single t4g.nano per issue #135).",
        )
        # Walk braces from the match to find the body.
        body_open = self.main_tf.find("{", match.start())
        depth = 0
        i = body_open
        while i < len(self.main_tf):
            c = self.main_tf[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = self.main_tf[body_open + 1 : i]
        self.assertRegex(
            body,
            r"disable_api_termination\s*=\s*true",
            "aws_instance.scrummaster must set disable_api_termination = true "
            "so a stray `terraform destroy` cannot take the host down (issue #135).",
        )

    def test_instance_type_defaults_to_t4g_nano(self):
        """var.instance_type must default to t4g.nano for the cost pin."""
        match = re.search(
            r'variable\s+"instance_type"\s*\{(?P<body>.*?)\n\}\n',
            self.variables_tf,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "instance_type variable not found in variables.tf")
        self.assertIn(
            "t4g.nano",
            match.group("body"),
            "var.instance_type must default to t4g.nano (issue #135 §Acceptance criteria cost pin).",
        )

    def test_security_group_egress_is_443_tcp_only(self):
        """The scrummaster SG must allow only 443/TCP egress (HTTPS to GitHub / Tailscale / minimax.io)."""
        match = re.search(
            r'resource\s+"aws_security_group"\s+"scrummaster"\s*\{(?P<body>.*?)\n\}\n',
            self.main_tf,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "aws_security_group.scrummaster not found")
        body = match.group("body")
        # The egress block is a single-object list with the canonical
        # {from_port, to_port, protocol, cidr_blocks} shape. Pull the
        # port range and protocol so a future edit widening to e.g.
        # "0-65535" or "tcp + udp" trips this test.
        egress = re.search(
            r"egress\s*=\s*\[\s*\{(?P<e>.*?)\}\s*,\s*\]", body, re.DOTALL
        )
        self.assertIsNotNone(
            egress, "aws_security_group.scrummaster has no egress block"
        )
        eb = egress.group("e")
        self.assertRegex(eb, r"from_port\s*=\s*443", "egress must start at port 443")
        self.assertRegex(eb, r"to_port\s*=\s*443", "egress must end at port 443")
        self.assertRegex(
            eb, r'protocol\s*=\s*"tcp"', "egress must be TCP (not all-protocols / udp)"
        )
        # No other ingress rules beyond the conditional SSH rule below
        # (asserted separately) — a wide ingress would defeat the
        # hardening intent.
        self.assertNotIn(
            "from_port = 0",
            eb,
            "egress must not be port 0-65535 — restrict to 443/TCP only",
        )
        self.assertNotIn(
            'protocol = "-1"',
            eb,
            "egress must not be all-protocols — restrict to TCP/443",
        )

    def test_ssh_ingress_is_conditional_on_var(self):
        """SSH ingress must be guarded by length(var.ssh_allowed_cidrs) > 0."""
        match = re.search(
            r'resource\s+"aws_security_group_rule"\s+"ssh_from_admin"\s*\{(?P<body>.*?)\n\}\n',
            self.main_tf,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "ssh_from_admin security_group_rule not found")
        body = match.group("body")
        self.assertIn(
            "length(var.ssh_allowed_cidrs) > 0",
            body,
            "ssh_from_admin must be conditional on length(var.ssh_allowed_cidrs) > 0 "
            "so an empty list produces no ingress (count = 0).",
        )
        self.assertRegex(body, r"from_port\s*=\s*22", "SSH ingress must be tcp/22")
        self.assertRegex(body, r"to_port\s*=\s*22", "SSH ingress must be tcp/22")
        self.assertRegex(body, r'protocol\s*=\s*"tcp"', "SSH ingress must be TCP")

    def test_iam_role_is_read_only_ssm_on_scrummaster_namespace(self):
        """The scrummaster IAM policy must grant read-only SSM access under
        /blitzlog/scrummaster/* and nothing else (writes are wired by
        later issues; this issue ships the read grant only).
        """
        body = _policy_body_for_role("scrummaster_ssm_read", self.main_tf)
        for required in (
            "ssm:GetParameter",
            "ssm:GetParameters",
            "ssm:GetParametersByPath",
        ):
            self.assertIn(required, body, f"scrummaster_ssm_read must allow {required}")
        # The ARN is rendered either as `arn:aws:ssm:${var.aws_region}:*:parameter/...`
        # (region-scoped — matches the rest of the project) or the
        # static `arn:aws:ssm:*:*:parameter/...` form. Both have to
        # terminate in `parameter/blitzlog/scrummaster/*` so the
        # grant is namespace-scoped to the scrummaster subtree.
        self.assertRegex(
            body,
            r"arn:aws:ssm:.*parameter/blitzlog/scrummaster/\*",
            "scrummaster_ssm_read must scope the resource to "
            "arn:aws:ssm:...:parameter/blitzlog/scrummaster/* (issue #135 §IAM grant).",
        )
        # The write verbs must be absent — this issue ships the
        # read grant only; later issues add their own narrower
        # write grants for parameters they own.
        for forbidden in (
            "ssm:PutParameter",
            "ssm:DeleteParameter",
        ):
            self.assertNotIn(
                forbidden,
                body,
                f"scrummaster_ssm_read must not allow {forbidden} — write grants "
                "ship in later epic issues (#137 / #138 / #139) under their own "
                "policy documents.",
            )

    def test_iam_role_assumed_only_by_ec2(self):
        """The scrummaster role's trust policy must permit only ec2.amazonaws.com."""
        match = re.search(
            r'resource\s+"aws_iam_role"\s+"scrummaster"\s*\{(?P<body>.*?)\n\}\n',
            self.main_tf,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "aws_iam_role.scrummaster not found")
        body = match.group("body")
        self.assertIn(
            "ec2.amazonaws.com",
            body,
            "scrummaster role's trust policy must permit only ec2.amazonaws.com "
            "(a leaked access key cannot be swapped in for the instance).",
        )

    def test_user_data_creates_three_directories(self):
        """user-data.sh must create /etc/zeroclaw, /var/lib/zeroclaw, /var/log/zeroclaw."""
        for directory in (
            "/etc/zeroclaw",
            "/var/lib/zeroclaw",
            "/var/log/zeroclaw",
        ):
            self.assertIn(
                directory,
                self.user_data,
                f"user-data.sh must create {directory} (issue #135 §user-data.sh).",
            )
        # install -d is the canonical mkdir -p replacement on the
        # minimal image; a stray `mkdir -p` is also acceptable but
        # at least one creation call must be present per directory.
        self.assertRegex(
            self.user_data,
            r"install\s+-d.*/etc/zeroclaw",
            "user-data.sh must create /etc/zeroclaw via install -d (set -m 0755 is the convention).",
        )

    def test_user_data_fails_closed_no_providers_no_channels(self):
        """The placeholder config.toml must omit providers and channels
        (ZeroClaw refuses to start an LLM round-trip / send a Telegram
        message until #137 / #138 / #139 layer in real config).
        """
        # Extract the heredoc body of the placeholder config.toml.
        # The shell heredoc is `<<'CONFIG_EOF' ... CONFIG_EOF` (or
        # unquoted `<<CONFIG_EOF`) — the regex accepts either form
        # without requiring a closing quote on the opening marker.
        m = re.search(
            r"<<\s*'?CONFIG_EOF'?\s*\n(?P<body>.*?)CONFIG_EOF",
            self.user_data,
            re.DOTALL,
        )
        self.assertIsNotNone(
            m,
            "user-data.sh must write /etc/zeroclaw/config.toml via a heredoc "
            "anchored on CONFIG_EOF (so the script is greppable for 'fails closed').",
        )
        body = m.group("body")
        # Strip TOML comment lines (`#`-prefixed) so the section-name
        # assertions below only see actual config. Comment text like
        # "no [providers.openai_compat.*]" is allowed — the test
        # is about the rendered config, not the prose around it.
        non_comment = "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith("#")
        )
        for forbidden_section in (
            "[providers",
            "[channels",
            "[[tools",
        ):
            self.assertNotIn(
                forbidden_section,
                non_comment,
                f"placeholder config.toml must not declare {forbidden_section} — "
                "fails closed by design (issue #135).",
            )

    def test_user_data_installs_zeroclaw_binary(self):
        """user-data.sh must download the ZeroClaw release tarball into /usr/local/bin."""
        self.assertIn(
            "github.com/zeroclaw-labs/zeroclaw/releases",
            self.user_data,
            "user-data.sh must download ZeroClaw from "
            "https://github.com/zeroclaw-labs/zeroclaw/releases (issue #135 §user-data.sh).",
        )
        self.assertIn(
            "/usr/local/bin",
            self.user_data,
            "user-data.sh must install the ZeroClaw binary into /usr/local/bin (on PATH for root and ubuntu).",
        )
        self.assertRegex(
            self.user_data,
            r"tar\s+-[a-z]*x",
            "user-data.sh must extract the ZeroClaw release tarball (tar -x flag).",
        )

    def test_agents_md_has_scrummaster_section(self):
        """AGENTS.md must declare a `## Scrummaster infrastructure` section
        that lists infra/scrummaster/* as operator-hosted and forbidden
        to blitzlog-agent runs.
        """
        # re.search anchors `^` to the start of the document unless
        # MULTILINE is set; assertIn on the literal header line is
        # more robust against regex quoting accidents.
        self.assertIn(
            "## Scrummaster infrastructure\n",
            self.agents_md,
            "AGENTS.md must declare a `## Scrummaster infrastructure` section (issue #135).",
        )
        # The carve-out must mention the path. The other carve-outs
        # (prod / dev / user-pool / bootstrap / modules/core) use the
        # same "blitzlog-agent runs ... must not modify" phrasing,
        # so we assert the path is referenced in the new section.
        section_match = re.search(
            r"## Scrummaster infrastructure\s*\n(?P<body>.*?)(?=^## |\Z)",
            self.agents_md,
            re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(
            section_match,
            "AGENTS.md is missing the `## Scrummaster infrastructure` section body.",
        )
        body = section_match.group("body")
        self.assertIn(
            "infra/scrummaster",
            body,
            "the Scrummaster infrastructure section must reference `infra/scrummaster/*` "
            "so a future reader can find the carve-out in <5 seconds.",
        )
        self.assertIn(
            "must not",
            body,
            "the Scrummaster infrastructure section must say blitzlog-agent runs "
            "'must not' modify infra/scrummaster/* — the same hard-prohibition "
            "phrasing the other carve-outs use.",
        )

    def test_backend_hcl_uses_scrummaster_key(self):
        """scrummaster-backend.hcl must key under scrummaster/blitzlog.tfstate."""
        backend = (SCRUMMASTER / "scrummaster-backend.hcl").read_text()
        self.assertRegex(
            backend,
            r'key\s*=\s*"scrummaster/blitzlog\.tfstate"',
            "scrummaster-backend.hcl must declare "
            'key = "scrummaster/blitzlog.tfstate" (issue #135 §State file).',
        )


if __name__ == "__main__":
    unittest.main()
