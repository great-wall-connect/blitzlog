"""Static analysis tests for the infra/scrummaster/ skeleton (issue #135).

These tests do NOT need AWS credentials, Terraform, or Packer. They read
the HCL / shell / systemd files as text and assert the invariants the
issue's acceptance criteria depend on:

- the t4g.nano is protected from `terraform destroy` and API termination
- the instance role is scoped to /blitzlog/users/* only
- the image is pinned by digest, never by tag, in the template unit
- user-data creates only the three parent dirs and enables (does not
  start) the one-shot image-pull unit; per-user accounts and config.toml
  are out of scope (#136 / #137)
- the Packer bake creates `zeroclaw-host` in the `docker` group and
  publishes the AMI id to /blitzlog/scrummaster/agent-ami-id-zeroclaw-ubuntu
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRUM = REPO_ROOT / "infra" / "scrummaster"
# Packer templates live under the per-cloud tree. The flat `infra/packer/`
# that this test originally referenced was moved to `infra/aws/packer/` by
# the per-cloud split (issue #126); the scrummaster AMI is an AWS artifact
# (t4g.nano + Amazon Linux), so it lands in the AWS tree. `infra/scrummaster/`
# itself stays at the top level — AGENTS.md rule 7 marks it operator-managed.
PACKER = REPO_ROOT / "infra" / "aws" / "packer"
PKR_HCL = PACKER / "scrummaster-zeroclaw-ubuntu.pkr.hcl"
PKR_SCRIPTS = PACKER / "scripts-scrummaster-ubuntu"

TRACKED_FILES = [
    PKR_HCL,
    PKR_SCRIPTS / "01-system.sh",
    PKR_SCRIPTS / "02-publish-ami-id.sh",
    SCRUM / "main.tf",
    SCRUM / "variables.tf",
    SCRUM / "outputs.tf",
    SCRUM / "user-data.sh",
    SCRUM / "terraform.tfvars.example",
    SCRUM / "systemd" / "zeroclaw-image-pull.service",
    SCRUM / "systemd" / "zeroclaw@.service",
    SCRUM / "zeroclaw" / "README.md",
    SCRUM / "tests" / "smoketest.sh",
]

SHELL_SCRIPTS = [
    PKR_SCRIPTS / "01-system.sh",
    PKR_SCRIPTS / "02-publish-ami-id.sh",
    SCRUM / "user-data.sh",
    SCRUM / "tests" / "smoketest.sh",
]

DEFAULT_TAG = "default-features-v0.8.6"


def _strip_comments(text: str) -> str:
    """Drop `#` comment lines so assertions only look at live config."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


class TestScrummasterFilesExist(unittest.TestCase):
    def test_all_tracked_files_exist(self):
        missing = [
            str(p.relative_to(REPO_ROOT)) for p in TRACKED_FILES if not p.is_file()
        ]
        self.assertEqual(missing, [], f"missing scrummaster files: {missing}")

    def test_shell_scripts_have_valid_syntax(self):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash not available")
        for script in SHELL_SCRIPTS:
            result = subprocess.run(
                [bash, "-n", str(script)], capture_output=True, text=True, check=False
            )
            self.assertEqual(
                result.returncode, 0, f"{script.name}: bash -n failed: {result.stderr}"
            )

    def test_shell_scripts_use_strict_mode(self):
        for script in SHELL_SCRIPTS:
            self.assertIn(
                "set -euo pipefail",
                script.read_text(),
                f"{script.name} lacks strict mode",
            )


class TestScrummasterTerraform(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main_tf = (SCRUM / "main.tf").read_text()
        cls.vars_tf = (SCRUM / "variables.tf").read_text()
        cls.outputs_tf = (SCRUM / "outputs.tf").read_text()

    def test_instance_is_t4g_nano(self):
        self.assertRegex(self.main_tf, r'instance_type\s*=\s*"t4g\.nano"')

    def test_instance_has_api_termination_protection(self):
        self.assertRegex(self.main_tf, r"disable_api_termination\s*=\s*true")

    def test_instance_rejects_terraform_destroy(self):
        """`terraform destroy` must fail unless the operator edits the guard."""
        self.assertRegex(self.main_tf, r"prevent_destroy\s*=\s*true")

    def test_state_key_is_scrummaster(self):
        self.assertIn('key    = "scrummaster/blitzlog.tfstate"', self.main_tf)

    def test_iam_role_scoped_to_user_params(self):
        live = _strip_comments(self.main_tf)
        self.assertIn("arn:aws:ssm:*:*:parameter/blitzlog/users/*", live)
        arns = re.findall(r'"(arn:aws:ssm:[^"]*)"', live)
        for arn in arns:
            self.assertTrue(
                arn.startswith("arn:aws:ssm:*:*:parameter/blitzlog/users/"),
                f"scrummaster role grants SSM outside /blitzlog/users/*: {arn!r}",
            )

    def test_iam_role_is_read_only_on_ssm(self):
        live = _strip_comments(self.main_tf)
        for forbidden in ('"ssm:PutParameter"', '"ssm:DeleteParameter"', '"ssm:*"'):
            self.assertNotIn(forbidden, live)

    def test_ssh_ingress_uses_allowed_cidrs(self):
        self.assertIn("var.ssh_allowed_cidrs", self.main_tf)
        self.assertRegex(self.main_tf, r"from_port\s*=\s*22")

    def test_egress_has_no_allow_all_protocol(self):
        """Egress must be port-scoped (443/TCP + Tailscale), not protocol -1."""
        self.assertNotRegex(_strip_comments(self.main_tf), r'protocol\s*=\s*"-1"')

    def test_ami_read_from_scrummaster_ssm_param(self):
        self.assertIn(
            "/blitzlog/scrummaster/agent-ami-id-zeroclaw-ubuntu", self.main_tf
        )

    def test_user_data_is_wired(self):
        self.assertIn("user-data.sh", self.main_tf)

    def test_variables_declared(self):
        self.assertIn('variable "zeroclaw_image_tag"', self.vars_tf)
        self.assertIn('variable "ssh_allowed_cidrs"', self.vars_tf)
        self.assertIn('variable "enable_eip"', self.vars_tf)
        self.assertIn(f'default     = "{DEFAULT_TAG}"', self.vars_tf)

    def test_eip_is_optional_and_disabled_by_default(self):
        """Public IP (EIP + subnet auto-assign) must be disabled by default."""
        self.assertRegex(
            self.vars_tf,
            r'variable\s+"enable_eip"\s*\{[^}]*default\s*=\s*false',
        )
        self.assertRegex(
            self.main_tf,
            r'resource\s+"aws_eip"\s+"scrummaster"\s*\{[^}]*count\s*=\s*var\.enable_eip\s*\?\s*1\s*:\s*0',
        )
        self.assertRegex(
            self.main_tf,
            r"associate_public_ip_address\s*=\s*false",
        )

    def test_no_environment_variable(self):
        """Scrummaster is operator hosting, shared across envs like user-pool."""
        self.assertNotIn('variable "environment"', self.vars_tf)
        self.assertNotIn("var.environment", _strip_comments(self.main_tf))

    def test_outputs_expose_eip(self):
        self.assertRegex(self.outputs_tf, r'output\s+"public_ip"')


class TestScrummasterUserData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _strip_comments((SCRUM / "user-data.sh").read_text())

    def test_creates_three_parent_dirs(self):
        for d in ("/var/lib/zeroclaw", "/etc/zeroclaw", "/var/log/zeroclaw"):
            self.assertIn(d, self.text)
        self.assertIn("0755", self.text)

    def test_enables_image_pull_unit(self):
        self.assertIn("systemctl enable zeroclaw-image-pull.service", self.text)

    def test_does_not_start_anything(self):
        self.assertNotRegex(self.text, r"systemctl\s+(start|restart)\b")
        self.assertNotIn("--now", self.text)
        self.assertNotIn("zeroclaw@", self.text)

    def test_out_of_scope_work_absent(self):
        for forbidden in ("useradd", "adduser", "config.toml", "docker run"):
            self.assertNotIn(forbidden, self.text, f"user-data must not {forbidden!r}")


class TestScrummasterSystemdUnits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pull = (SCRUM / "systemd" / "zeroclaw-image-pull.service").read_text()
        cls.tmpl = (SCRUM / "systemd" / "zeroclaw@.service").read_text()

    def test_image_pull_is_oneshot(self):
        self.assertIn("Type=oneshot", self.pull)
        self.assertIn("RemainAfterExit=yes", self.pull)
        self.assertIn("WantedBy=multi-user.target", self.pull)
        self.assertIn("docker load -i /opt/zeroclaw/images/zeroclaw.tar.gz", self.pull)

    def test_template_runs_as_zeroclaw_host(self):
        self.assertIn("User=zeroclaw-host", self.tmpl)
        self.assertIn("Group=zeroclaw-host", self.tmpl)

    def test_template_has_no_exec_start_pre(self):
        """ExecStartPre= lands in #138 with the first plugin install."""
        self.assertNotIn("ExecStartPre=", self.tmpl)

    def test_template_hardening_flags(self):
        for flag in (
            "--read-only",
            "--tmpfs /tmp:size=64m",
            "--security-opt no-new-privileges",
            "--rm",
            "--name zeroclaw-%i",
            "--env-file /etc/zeroclaw/%i/env",
            "-v /var/lib/zeroclaw/%i:/var/lib/zeroclaw",
            "-v /etc/zeroclaw/%i:/etc/zeroclaw:ro",
            "-v /var/log/zeroclaw/%i:/var/log/zeroclaw",
        ):
            self.assertIn(flag, self.tmpl)

    def test_template_passes_per_user_uid_gid(self):
        self.assertIn("--user", self.tmpl)
        self.assertIn("id -u zeroclaw-%i", self.tmpl)
        self.assertIn("id -g zeroclaw-%i", self.tmpl)

    def test_template_pins_image_by_digest(self):
        self.assertIn("ghcr.io/zeroclaw-labs/zeroclaw@", self.tmpl)
        self.assertNotRegex(self.tmpl, r"ghcr\.io/zeroclaw-labs/zeroclaw:")


class TestScrummasterPacker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkr = PKR_HCL.read_text()
        cls.system = (PKR_SCRIPTS / "01-system.sh").read_text()
        cls.publish = (PKR_SCRIPTS / "02-publish-ami-id.sh").read_text()

    def test_packer_uses_canonical_arm64_ubuntu(self):
        self.assertIn("ubuntu-resolute-26.04-arm64-server-*", self.pkr)
        self.assertIn('"099720109477"', self.pkr)

    def test_packer_image_tag_variable(self):
        self.assertIn('variable "zeroclaw_image_tag"', self.pkr)
        self.assertIn(f'default     = "{DEFAULT_TAG}"', self.pkr)

    def test_packer_installs_systemd_units(self):
        self.assertIn("zeroclaw-image-pull.service", self.pkr)
        self.assertIn("zeroclaw@.service", self.pkr)

    def test_system_script_installs_docker_and_tools(self):
        self.assertIn("download.docker.com", self.system)
        self.assertIn("docker-ce", self.system)
        for pkg in ("awscli", "git", "jq"):
            self.assertRegex(self.system, rf"\b{pkg}\b")

    def test_system_script_creates_zeroclaw_host_in_docker_group(self):
        self.assertIn("useradd", self.system)
        self.assertIn("zeroclaw-host", self.system)
        self.assertRegex(self.system, r"usermod\s+-aG\s+docker\s+zeroclaw-host")

    def test_system_script_pins_image_by_digest(self):
        self.assertIn("docker buildx imagetools inspect", self.system)
        self.assertIn("linux/arm64", self.system)
        self.assertIn("/opt/zeroclaw/images/zeroclaw.tar.gz", self.system)

    def test_system_script_has_no_spot_machinery(self):
        live = _strip_comments(self.system)
        for forbidden in ("watchdog", "load-image", "spot/instance-action"):
            self.assertNotIn(forbidden, live)

    def test_publish_writes_scrummaster_param(self):
        self.assertIn(
            "/blitzlog/scrummaster/agent-ami-id-zeroclaw-ubuntu", self.publish
        )
        self.assertIn("manifest", self.publish)


class TestScrummasterAgentsRule(unittest.TestCase):
    def test_agents_md_forbids_agent_edits_to_scrummaster(self):
        agents = (REPO_ROOT / "AGENTS.md").read_text()
        self.assertIn("infra/scrummaster/", agents)


if __name__ == "__main__":
    unittest.main()
