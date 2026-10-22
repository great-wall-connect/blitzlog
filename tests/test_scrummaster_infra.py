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
CORE = REPO_ROOT / "infra" / "aws" / "modules" / "core"
# Scrummaster assets now live inside the core module (epic #134). The
# standalone stack they came from was `infra/scrummaster/`; that directory
# survives only until its state is migrated into the dev stack and is
# deleted. See CORE_SCRUMMASTER below for the module that replaced it.
CORE_SCRUMMASTER = CORE / "scrummaster.tf"
SCRUM_ASSETS = CORE / "scrummaster"
# Legacy standalone stack. Retained in TRACKED_FILES until its state is
# migrated and the directory is removed.
LEGACY_SCRUM = REPO_ROOT / "infra" / "scrummaster"
# Packer templates live under the per-cloud tree. The flat `infra/packer/`
# that this test originally referenced was moved to `infra/aws/packer/` by
# the per-cloud split (issue #126); the scrummaster AMI is an AWS artifact
# (t4g.nano + Amazon Linux), so it lands in the AWS tree.
PACKER = REPO_ROOT / "infra" / "aws" / "packer"
PKR_HCL = PACKER / "scrummaster-zeroclaw-ubuntu.pkr.hcl"
PKR_SCRIPTS = PACKER / "scripts-scrummaster-ubuntu"

CORE_VARIABLES = CORE / "variables.tf"
CORE_OUTPUTS = CORE / "outputs.tf"
DEV_MAIN = REPO_ROOT / "infra" / "aws" / "dev" / "main.tf"
DEV_TFVARS_EXAMPLE = REPO_ROOT / "infra" / "aws" / "dev" / "terraform.tfvars.example"
PROD_MAIN = REPO_ROOT / "infra" / "aws" / "prod" / "main.tf"
PROD_TFVARS_EXAMPLE = REPO_ROOT / "infra" / "aws" / "prod" / "terraform.tfvars.example"

TRACKED_FILES = [
    PKR_HCL,
    PKR_SCRIPTS / "01-system.sh",
    PKR_SCRIPTS / "02-publish-ami-id.sh",
    CORE_SCRUMMASTER,
    SCRUM_ASSETS / "user-data.sh",
    SCRUM_ASSETS / "systemd" / "zeroclaw-image-pull.service",
    SCRUM_ASSETS / "systemd" / "zeroclaw@.service",
    SCRUM_ASSETS / "tests" / "smoketest.sh",
    SCRUM_ASSETS / "README.md",
]

SHELL_SCRIPTS = [
    PKR_SCRIPTS / "01-system.sh",
    PKR_SCRIPTS / "02-publish-ami-id.sh",
    SCRUM_ASSETS / "user-data.sh",
    SCRUM_ASSETS / "tests" / "smoketest.sh",
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
        cls.main_tf = CORE_SCRUMMASTER.read_text()
        cls.vars_tf = CORE_VARIABLES.read_text()
        cls.outputs_tf = CORE_OUTPUTS.read_text()

    def test_instance_is_t4g_nano(self):
        self.assertRegex(self.main_tf, r'instance_type\s*=\s*"t4g\.nano"')

    def test_instance_has_api_termination_protection(self):
        self.assertRegex(self.main_tf, r"disable_api_termination\s*=\s*true")

    def test_instance_rejects_terraform_destroy(self):
        """`terraform destroy` must fail unless the operator edits the guard."""
        self.assertRegex(self.main_tf, r"prevent_destroy\s*=\s*true")

    def test_no_backend_block_in_module(self):
        """The core module must declare no backend of its own.

        It inherits the caller's, which is the whole point of the fold —
        one lock file, one state file, one `terraform init`. A `backend`
        block here would reintroduce the separate-stack setup friction.
        """
        self.assertNotIn(
            "backend ",
            (CORE / "main.tf").read_text(),
            "infra/aws/modules/core/main.tf must not declare a backend; "
            "the module inherits the calling stack's",
        )

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
        self.assertIn('variable "enable_scrummaster_eip"', self.vars_tf)
        self.assertIn(f'default     = "{DEFAULT_TAG}"', self.vars_tf)

    def test_eip_is_optional_and_disabled_by_default(self):
        """Public IP (EIP + subnet auto-assign) must be disabled by default."""
        self.assertRegex(
            self.vars_tf,
            r'variable\s+"enable_scrummaster_eip"\s*\{[^}]*default\s*=\s*false',
        )
        self.assertRegex(
            self.main_tf,
            r'resource\s+"aws_eip"\s+"scrummaster"\s*\{[^}]*'
            r"count\s*=\s*var\.enable_scrummaster\s*&&\s*var\.enable_scrummaster_eip"
            r"\s*\?\s*1\s*:\s*0",
        )
        self.assertRegex(
            self.main_tf,
            r"associate_public_ip_address\s*=\s*false",
        )

    def test_no_environment_variable(self):
        """Scrummaster is env-independent, shared across envs like user-pool.

        It reads one AMI id from `/blitzlog/scrummaster/...` and one set
        of `/blitzlog/users/*` params, with no env infix anywhere. Note
        this checks *usage* in scrummaster.tf — the core module as a whole
        does declare `variable "environment"` for the Lambda / ECS side.
        """
        self.assertNotIn("var.environment", _strip_comments(self.main_tf))

    def test_outputs_expose_eip(self):
        self.assertRegex(self.outputs_tf, r'output\s+"scrummaster_public_ip"')


class TestScrummasterUserData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _strip_comments((SCRUM_ASSETS / "user-data.sh").read_text())

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
        cls.pull = (
            SCRUM_ASSETS / "systemd" / "zeroclaw-image-pull.service"
        ).read_text()
        cls.tmpl = (SCRUM_ASSETS / "systemd" / "zeroclaw@.service").read_text()

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


class TestScrummasterInCoreModule(unittest.TestCase):
    """Guards for the fold of the scrummaster into the core module (epic #134).

    The scrummaster used to be a standalone stack at `infra/scrummaster/`
    with its own provider download, lock file, variable set, and a
    hand-run apply CI never touched. It now lives in
    `infra/aws/modules/core/scrummaster.tf` behind `enable_scrummaster`,
    so "apply the whole system" is a single CI-owned plan/apply.

    These tests pin the three properties that make that safe.
    """

    def setUp(self):
        self.scrum = CORE_SCRUMMASTER.read_text()
        self.live = _strip_comments(self.scrum)

    def test_every_scrummaster_resource_is_count_gated(self):
        """Each resource and data source must carry a `count` gate.

        Without it, `enable_scrummaster = false` would still create the
        IAM role / SG / instance, and prod — where the flag is off —
        would plan the AMI data-source reads too, failing on a missing
        /blitzlog/scrummaster/... SSM parameter.
        """
        resources = re.findall(
            r'^resource\s+"([^"]+)"\s+"([^"]+)"', self.live, re.MULTILINE
        )
        data_sources = re.findall(
            r'^data\s+"([^"]+)"\s+"([^"]+)"', self.live, re.MULTILINE
        )
        self.assertTrue(resources, "scrummaster.tf must define resources")
        self.assertTrue(data_sources, "scrummaster.tf must define data sources")

        blocks = re.split(r"^(?=resource |data )", self.live, flags=re.MULTILINE)[1:]
        for block, label in zip(
            blocks,
            [f"{t}.{n}" for t, n in resources + data_sources],
        ):
            self.assertIn(
                "var.enable_scrummaster",
                block.split("}")[0] + "}",
                f"{label} must be gated on var.enable_scrummaster",
            )

    def test_names_are_deliberately_env_independent(self):
        """The scrummaster's names carry NO `var.environment` infix.

        This is load-bearing, not an oversight. Renaming
        `blitzlog-scrummaster-profile` to
        `blitzlog-${var.environment}-scrummaster-profile` changes
        `iam_instance_profile`, which forces replacement of an instance
        carrying `disable_api_termination = true`. A future contributor
        "fixing" the naming convention would silently destroy and
        recreate the scrummaster host, so pin the deviation.
        """
        self.assertNotIn(
            "${var.environment}",
            self.live,
            "scrummaster resources must stay env-independent; adding a "
            "${var.environment} infix to their names changes "
            "iam_instance_profile and forces replacement of an instance "
            "with disable_api_termination = true",
        )
        self.assertIn(
            'name = "blitzlog-scrummaster-role"',
            self.live,
            "the env-independent naming convention is the documented one",
        )

    def test_only_dev_wires_the_scrummaster(self):
        """dev passes the flags through; prod must not.

        Because the names are env-independent, prod enabling the flag
        would collide with dev's `blitzlog-scrummaster-role` in the
        shared AWS account. This is the structural guard against that.
        """
        dev = DEV_MAIN.read_text()
        for flag in (
            "enable_scrummaster",
            "zeroclaw_image_tag",
            "ssh_key_name",
            "enable_scrummaster_eip",
        ):
            self.assertRegex(
                dev,
                rf"\b{re.escape(flag)}\s*=\s*var\.{re.escape(flag)}\b",
                f"infra/aws/dev/main.tf must pass {flag} through to module.core",
            )

        prod = PROD_MAIN.read_text()
        self.assertNotIn(
            "enable_scrummaster",
            prod,
            "infra/aws/prod/main.tf must NOT enable the scrummaster — its "
            "resources are env-independent and would collide with dev's",
        )

    def test_prod_tfvars_example_keeps_it_off(self):
        example = PROD_TFVARS_EXAMPLE.read_text()
        self.assertIn(
            "enable_scrummaster",
            example,
            "infra/aws/prod/terraform.tfvars.example must state the scrummaster "
            "position explicitly so an operator does not enable it",
        )
        self.assertIn(
            "enable_scrummaster        = false",
            example,
            "infra/aws/prod/terraform.tfvars.example must pin "
            "enable_scrummaster = false",
        )

    def test_defaults_off_in_core(self):
        variables = CORE_VARIABLES.read_text()
        self.assertIn(
            'variable "enable_scrummaster"',
            variables,
            "core/variables.tf must declare enable_scrummaster",
        )
        self.assertIn(
            "default     = false",
            variables.split('variable "enable_scrummaster"')[1].split("}")[0],
            "enable_scrummaster must default to false — enabling it requires a "
            "manual Packer bake of the ZeroClaw AMI, and a default-on module "
            "would gate the core webhook deploy behind that manual step",
        )

    def test_outputs_are_try_guarded(self):
        """Count-gated resources are absent from state when disabled.

        Without try(), `terraform output` errors on a stack where the
        scrummaster is off instead of returning null.
        """
        outputs = CORE_OUTPUTS.read_text()
        for name in (
            "scrummaster_instance_id",
            "scrummaster_public_ip",
            "scrummaster_ami_id",
            "scrummaster_zeroclaw_image_tag",
        ):
            self.assertIn(
                f'output "{name}"',
                outputs,
                f"core/outputs.tf must declare {name}",
            )
        for body in (
            "aws_instance.scrummaster[0].id",
            "aws_eip.scrummaster[0].public_ip",
            "data.aws_ami.scrummaster[0].id",
        ):
            self.assertIn(
                f"try({body}, null)",
                outputs,
                f"{body} must be wrapped in try() — it is absent from state "
                f"when enable_scrummaster is false",
            )

    def test_dev_reexports_for_smoketest(self):
        """dev must re-export the two outputs smoketest.sh reads.

        `infra/aws/modules/core/scrummaster/tests/smoketest.sh` runs
        `terraform output -raw public_ip` from the stack that owns the
        resources, which is now infra/aws/dev.
        """
        dev_outputs = (REPO_ROOT / "infra" / "aws" / "dev" / "outputs.tf").read_text()
        self.assertIn('output "scrummaster_public_ip"', dev_outputs)
        self.assertIn('output "scrummaster_zeroclaw_image_tag"', dev_outputs)

    def test_user_data_reference_points_at_new_location(self):
        self.assertIn(
            'file("${path.module}/scrummaster/user-data.sh")',
            self.live,
            "scrummaster.tf must reference the user-data.sh under the "
            "scrummaster/ subdirectory of the core module",
        )

    def test_agents_md_no_longer_carves_out_scrummaster(self):
        """AGENTS.md rule 7 (`infra/scrummaster/*` is operator hosting) is retired.

        The scrummaster is core deployment surface now — CI owns it and
        agents maintain it like any other Terraform. The separate
        *Trigger Labels* carve-out for applying `autonomous` / `assisted`
        is a different rule and is unaffected.
        """
        agents = (REPO_ROOT / "AGENTS.md").read_text()
        self.assertNotRegex(
            agents,
            r"^\s*\d+\.\s+.*is operator hosting",
            "AGENTS.md must not still carry an active numbered rule marking "
            "the scrummaster as operator hosting",
        )
        self.assertNotRegex(
            agents,
            r"^\s*\d+\.\s+.*must not modify anything under `infra/scrummaster/`",
            "AGENTS.md must not still forbid edits to the scrummaster tree",
        )
        self.assertIn(
            "was **removed** when the Scrummaster moved",
            agents,
            "AGENTS.md should record why rule 7 was retired so the next agent "
            "does not restore it",
        )


if __name__ == "__main__":
    unittest.main()
