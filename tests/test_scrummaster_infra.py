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
PKR_HCL = PACKER / "scrummaster-zeroclaw-docker-ubuntu.pkr.hcl"
PKR_SCRIPTS = PACKER / "scripts-scrummaster-docker-ubuntu"

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
            "/blitzlog/${var.environment}/agent-ami-id-zeroclaw-docker-ubuntu",
            self.main_tf,
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

    def test_uses_the_core_environment_variable(self):
        """The scrummaster reads var.environment like the rest of core.

        An earlier revision asserted the opposite (env-independent). The
        module is now uniformly env-scoped; see
        test_names_embed_environment.
        """
        self.assertIn("var.environment", _strip_comments(self.main_tf))

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

    def test_packer_declares_environment_variable(self):
        """The template must take `environment`, like agent-docker-ubuntu.pkr.hcl."""
        self.assertRegex(
            self.pkr,
            r'variable\s+"environment"\s*\{[^}]*type\s*=\s*string',
            "the scrummaster Packer template must declare variable "
            '"environment" — without it the AMI cannot be tagged or '
            "scoped to an environment",
        )

    def test_packer_env_scopes_ami_name_and_tags(self):
        live = _strip_comments(self.pkr)
        self.assertRegex(
            live,
            r'ami_name\s*=\s*"blitzlog-\$\{var\.environment\}-scrummaster-ami-zeroclaw-docker-ubuntu-',
            "ami_name must be env-scoped",
        )
        self.assertIn(
            "Environment = var.environment",
            live,
            "AMI tags must carry Environment, matching agent-docker-ubuntu.pkr.hcl",
        )
        self.assertNotRegex(
            live,
            r'ami_name\s*=\s*"blitzlog-scrummaster-',
            "the un-scoped ami_name is retired",
        )

    def test_packer_uploads_systemd_units_from_the_core_module(self):
        """The `file` provisioners must point at the units' real home.

        They referenced `${path.root}/../scrummaster/systemd/` until the
        fold into modules/core moved the units to
        `infra/aws/modules/core/scrummaster/systemd/`. A stale path here
        fails the bake at the first provisioner — and nothing else in CI
        exercises this template, so it would rot silently.
        """
        live = _strip_comments(self.pkr)
        self.assertIn(
            "${path.root}/../modules/core/scrummaster/systemd/zeroclaw-image-pull.service",
            live,
            "the image-pull unit must be uploaded from the core module path",
        )
        self.assertIn(
            "${path.root}/../modules/core/scrummaster/systemd/zeroclaw@.service",
            live,
            "the template unit must be uploaded from the core module path",
        )
        self.assertNotIn(
            "${path.root}/../scrummaster/systemd/",
            live,
            "the pre-fold `infra/aws/scrummaster/systemd/` path does not exist; "
            "this silently broke the bake once the units moved into modules/core",
        )

    def test_packer_scripts_use_the_renamed_dir(self):
        live = _strip_comments(self.pkr)
        self.assertIn(
            "${path.root}/scripts-scrummaster-docker-ubuntu/01-system.sh", live
        )
        self.assertIn(
            "${path.root}/scripts-scrummaster-docker-ubuntu/02-publish-ami-id.sh", live
        )

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
            'PARAM="/blitzlog/${BLITZLOG_ENV}/agent-ami-id-zeroclaw-docker-ubuntu"',
            self.publish,
        )
        self.assertIn("manifest", self.publish)

    def test_publish_requires_blitzlog_env(self):
        """The env must be a required input, not a defaulted one.

        Publishing to an un-scoped path would hand dev and prod the same
        AMI, which is exactly what the env-scoped naming prevents.
        """
        self.assertIn(
            ': "${BLITZLOG_ENV:?BLITZLOG_ENV must be set}"',
            self.publish,
        )


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

    def test_names_embed_environment(self):
        """Every scrummaster resource must be `blitzlog-${var.environment}-scrummaster*`.

        An earlier revision kept these env-independent (one host per
        account). That was reversed: env-scoped naming is the uniform
        convention for this module, it lets dev and prod run their own
        scrummaster from their own AMI, and it puts the AMI id in the
        same env-scoped SSM namespace as the agent AMI. Pin it so a
        contributor doesn't half-revert and reintroduce a collision
        between dev and prod.
        """
        expected = [
            'name = "blitzlog-${var.environment}-scrummaster-role"',
            'name = "blitzlog-${var.environment}-scrummaster-ssm-read"',
            'name = "blitzlog-${var.environment}-scrummaster-profile"',
            'name        = "blitzlog-${var.environment}-scrummaster-sg"',
            'Name        = "blitzlog-${var.environment}-scrummaster-eip"',
            'Name        = "blitzlog-${var.environment}-scrummaster"',
        ]
        for snippet in expected:
            self.assertIn(
                snippet,
                self.live,
                f"scrummaster.tf must name this resource env-scoped: {snippet!r}",
            )

        # No bare un-scoped leftovers from the old convention.
        for stale in (
            'name = "blitzlog-scrummaster-role"',
            'name = "blitzlog-scrummaster-profile"',
            'name        = "blitzlog-scrummaster-sg"',
        ):
            self.assertNotIn(stale, self.live, f"stale env-independent name: {stale!r}")

    def test_tags_carry_environment(self):
        """The SG / instance / EIP tags must carry `Environment`.

        Core tags every env-scoped resource this way (see ec2.tf). It is
        how `terraform console` and the AWS console can tell a dev
        scrummaster from a prod one at a glance.
        """
        self.assertGreaterEqual(
            self.live.count("Environment = var.environment"),
            3,
            "scrummaster SG, instance and EIP must all tag Environment",
        )

    def test_ami_ssm_param_is_env_scoped(self):
        """The AMI id must be read from the env-scoped parameter.

        `agent-ami-id-zeroclaw-docker-ubuntu` under
        `/blitzlog/${var.environment}/` — the same shape
        agent-docker-ubuntu.pkr.hcl publishes. The old un-scoped
        `/blitzlog/scrummaster/agent-ami-id-zeroclaw-ubuntu` path would
        make dev and prod share one AMI, which is the thing the
        env-scoping exists to prevent.
        """
        self.assertIn(
            'name = "/blitzlog/${var.environment}/agent-ami-id-zeroclaw-docker-ubuntu"',
            self.live,
            "scrummaster.tf must read the env-scoped AMI parameter",
        )
        self.assertNotIn(
            "/blitzlog/scrummaster/agent-ami-id-zeroclaw-ubuntu",
            self.live,
            "the un-scoped AMI parameter path is retired; dev and prod each "
            "publish their own",
        )

    def test_both_envs_wire_the_scrummaster(self):
        """dev and prod both pass the flags through to module.core.

        Both are wired because the resources are env-scoped: each env
        gets its own host, IAM objects and AMI with no collision. Both
        default off (asserted separately) so a fresh install stays a
        single command.
        """
        for label, path in (("dev", DEV_MAIN), ("prod", PROD_MAIN)):
            text = path.read_text()
            for flag in (
                "enable_scrummaster",
                "zeroclaw_image_tag",
                "ssh_key_name",
                "enable_scrummaster_eip",
            ):
                self.assertRegex(
                    text,
                    rf"\b{re.escape(flag)}\s*=\s*var\.{re.escape(flag)}\b",
                    f"infra/aws/{label}/main.tf must pass {flag} through to module.core",
                )

    def test_both_tfvars_examples_keep_it_off(self):
        """Both tfvars examples must state enable_scrummaster = false.

        The default is already false in the module and the wrappers, but
        an operator reading only the .example must see it too — turning
        it on before the AMI is baked fails the plan at the
        data-source read.
        """
        for label, path in (
            ("dev", DEV_TFVARS_EXAMPLE),
            ("prod", PROD_TFVARS_EXAMPLE),
        ):
            example = path.read_text()
            self.assertIn(
                "enable_scrummaster",
                example,
                f"infra/aws/{label}/terraform.tfvars.example must state the "
                f"scrummaster position explicitly",
            )
            self.assertRegex(
                example,
                r"enable_scrummaster\s*=\s*false",
                f"infra/aws/{label}/terraform.tfvars.example must pin "
                f"enable_scrummaster = false",
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


class TestScrummasterPackerInRelease(unittest.TestCase):
    """Guards for the parallel scrummaster AMI bake in release.yml.

    The scrummaster AMI bake runs as its own job, concurrent with the
    agent AMI bake, instead of as extra steps inside the `release` job.
    That only works if both jobs derive their environment from the SAME
    gate — hence the shared `.github/actions/ref-gate` composite action.
    """

    WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
    ACTION = REPO_ROOT / ".github" / "actions" / "ref-gate" / "action.yml"

    @classmethod
    def setUpClass(cls):
        cls.wf = cls.WORKFLOW.read_text()
        cls.action = cls.ACTION.read_text()

    def test_release_workflow_parses(self):
        """Guard against a YAML syntax error in a 1000-line workflow."""
        import yaml

        data = yaml.safe_load(self.wf)
        self.assertIn("scrummaster-packer", data["jobs"])

    def test_scrummaster_packer_is_a_separate_job(self):
        """It must be its own job — steps inside one job are sequential."""
        import yaml

        data = yaml.safe_load(self.wf)
        job = data["jobs"]["scrummaster-packer"]
        self.assertEqual(
            list(job.keys()) and job.get("runs-on"),
            "ubuntu-26.04-arm",
            "the bake must run on arm64 (Graviton AMIs), like the agent bake",
        )
        self.assertNotIn(
            "needs",
            job,
            "the scrummaster job must NOT need `release` — that "
            "would serialise it instead of running in parallel",
        )

    def test_both_jobs_call_the_same_ref_gate(self):
        """Single source of truth for the ref -> env decision.

        If the two jobs ever diverge, the agent AMI lands in one env's
        SSM param and the scrummaster AMI in the other's — two AMIs, two
        environments, no error anywhere.
        """
        uses = self.wf.count("uses: ./.github/actions/ref-gate")
        self.assertEqual(
            uses,
            2,
            "both the release job and the scrummaster-packer job must call "
            "the shared ref-gate action",
        )
        self.assertNotIn(
            "gh pr list --state open",
            self.wf,
            "the ref-gate logic must live in the composite action, not be "
            "re-inlined in the workflow",
        )

    def test_ref_gate_action_exposes_env(self):
        import yaml

        data = yaml.safe_load(self.action)
        self.assertEqual(data["runs"]["using"], "composite")
        self.assertIn("build-ref", data["inputs"])
        for out in ("mode", "pr_number", "blitzlog_env"):
            self.assertIn(out, data["outputs"])
        # The gate must still refuse non-release refs with no open PR —
        # that refusal is the issue #131 regression guard.
        body = data["runs"]["steps"][0]["run"]
        self.assertIn("Refusing to run", body)
        self.assertIn("release/*)", body)

    def test_deploy_waits_for_both_bakes(self):
        import yaml

        data = yaml.safe_load(self.wf)
        needs = data["jobs"]["deploy"]["needs"]
        self.assertEqual(
            sorted(needs),
            ["release", "scrummaster-packer"],
            "deploy must wait for both AMI bakes: the terraform plan reads "
            "the scrummaster AMI SSM param whenever enable_scrummaster is true",
        )

    def test_packer_role_grants_the_scrummaster_param(self):
        """CI baking this AMI over OIDC needs write access to its SSM param."""
        role = (
            REPO_ROOT / "infra" / "aws" / "bootstrap" / "packer-role.tf"
        ).read_text()
        self.assertIn(
            "parameter/blitzlog/*/agent-ami-id-zeroclaw-docker-ubuntu",
            role,
            "packer-role.tf must grant the OIDC bake write access to the "
            "env-scoped scrummaster AMI parameter, or the post-processor "
            "fails with AccessDenied",
        )

    def test_packer_role_drops_the_dangling_workflow_path(self):
        """`workflow_path` pointed at .github/workflows/packer-build.yml,
        which does not exist. It was a dead local — the trust policy
        matches the OIDC `sub` claim instead."""
        role = (
            REPO_ROOT / "infra" / "aws" / "bootstrap" / "packer-role.tf"
        ).read_text()
        self.assertNotIn(
            "packer-build.yml",
            role,
            "packer-role.tf must not reference the non-existent "
            "packer-build.yml workflow",
        )


if __name__ == "__main__":
    unittest.main()
