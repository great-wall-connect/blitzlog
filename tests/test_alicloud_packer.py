"""Tests for the Alicloud Packer tree + host bootstrap (issue #125).

Issue #125 adds a parallel host-image pipeline for Alicloud:

    infra/alicloud/packer/agent-ecs-ubuntu.pkr.hcl
    infra/alicloud/packer/scripts-ecs-ubuntu/

It is the Alicloud analogue of:

    infra/aws/packer/agent-docker-ubuntu.pkr.hcl
    infra/aws/packer/scripts-docker-ubuntu/

The point of the split is that the HOST IMAGE IS SINGLE-CLOUD BY
CONSTRUCTION — the AWS AMI bakes `aws s3` into the watchdog and the
Alicloud image bakes `ossutil`. So the two trees are near-duplicates
that differ in a small, enumerable set of places. These tests pin that
difference set so the two trees cannot drift apart silently:

  * the object-storage backend (`aws s3` vs `ossutil`),
  * the bucket env-var names (`S3_LOGS_BUCKET` vs `OSS_LOGS_BUCKET`),
  * the metadata endpoint (IMDSv2 at 169.254.169.254 vs the Aliyun
    metadata service at 100.100.100.200),
  * the secret backend (`aws ssm` vs `aliyun kms`),
  * the instance teardown (`aws ec2 terminate-instances` vs
    `aliyun ecs DeleteInstances`),
  * the metadata egress block (the AWS watchdog blocks 169.254.169.254;
    the Alicloud watchdog must block 100.100.100.200).

They also pin the fail-closed metadata block (issue #175): if the
`DOCKER-USER` rule cannot be installed, the agent container must NOT
start. A boundary that was not in force looked exactly like one that
was, and the gap went unnoticed for a release cycle.

Finally they pin the agent-image-id contract: the Alicloud Packer
publishes the image id to KMS Secrets Manager at
/blitzlog/<env>/agent-ami-id-ecs-ubuntu (the Alicloud `SecretStore`
implementation), and the compute provider reads it from there. If the
two sides disagree on the suffix, the Alicloud path silently falls
back to the public Ubuntu image and the baked agent image is never
used — the failure is invisible, so it is pinned here.
"""

import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ALICLOUD_PACKER_DIR = os.path.join(REPO_ROOT, "infra/alicloud/packer")
ALICLOUD_TEMPLATE = os.path.join(ALICLOUD_PACKER_DIR, "agent-ecs-ubuntu.pkr.hcl")
ALICLOUD_SCRIPTS_DIR = os.path.join(ALICLOUD_PACKER_DIR, "scripts-ecs-ubuntu")

AWS_SCRIPTS_DIR = os.path.join(REPO_ROOT, "infra/aws/packer/scripts-docker-ubuntu")
AWS_WATCHDOG = os.path.join(AWS_SCRIPTS_DIR, "watchdog.sh")

ALICLOUD_WATCHDOG = os.path.join(ALICLOUD_SCRIPTS_DIR, "watchdog.sh")
ALICLOUD_OSS_UPLOADER = os.path.join(ALICLOUD_SCRIPTS_DIR, "blitzlog_oss_uploader.sh")
ALICLOUD_01_SYSTEM = os.path.join(ALICLOUD_SCRIPTS_DIR, "01-system.sh")
ALICLOUD_02_SYSTEMD = os.path.join(ALICLOUD_SCRIPTS_DIR, "02-systemd.sh")
ALICLOUD_03_BAKE = os.path.join(ALICLOUD_SCRIPTS_DIR, "03-bake-images.sh")
ALICLOUD_05_PUBLISH = os.path.join(ALICLOUD_SCRIPTS_DIR, "05-publish-ami-id.sh")

AWS_OSS_UPLOADER = os.path.join(AWS_SCRIPTS_DIR, "blitzlog_uploader.sh")

# The metadata endpoints. The Aliyun metadata service is the
# IMDSv2 equivalent; the two MUST NOT be mixed up — a watchdog that
# reads the AWS endpoint on an ECS instance gets nothing, and one that
# blocks the wrong endpoint leaves the real one reachable from the
# container (issue #175).
AWS_METADATA_IP = "169.254.169.254"
ALICLOUD_METADATA_IP = "100.100.100.200"


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _strip_comments(src):
    """Return `src` with the bash comments removed.

    The Alicloud scripts DOCUMENT their differences from the AWS
    scripts in comments (e.g. "`aws s3 cp` → the `blitzlog_oss_*`
    library"), so a naive substring check for "aws s3" would flag
    the very documentation that keeps the two trees in sync. The
    checks that matter are about CODE — a stray `aws s3` CALL is a
    bug; a comment naming it is a feature.
    """
    lines = []
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Strip a trailing comment, respecting quotes so a '#' inside
        # a string is not mistaken for a comment start.
        out = []
        quote = None
        for ch in line:
            if quote:
                out.append(ch)
                if ch == quote:
                    quote = None
                continue
            if ch in ("'", '"'):
                quote = ch
                out.append(ch)
                continue
            if ch == "#":
                break
            out.append(ch)
        lines.append("".join(out))
    return "\n".join(lines)


class TestAlicloudPackerTreeExists(unittest.TestCase):
    """The tree itself must exist, with the same file names as the
    AWS tree (minus the token-poll loop, which has no Aliyun
    scheduler equivalent — see `TestAlicloudTreeOmitsAwsOnlyPieces`)."""

    def test_template_exists(self):
        self.assertTrue(
            os.path.isfile(ALICLOUD_TEMPLATE),
            f"{ALICLOUD_TEMPLATE} must exist",
        )

    def test_script_tree_exists(self):
        for path in (
            ALICLOUD_WATCHDOG,
            ALICLOUD_OSS_UPLOADER,
            ALICLOUD_01_SYSTEM,
            ALICLOUD_02_SYSTEMD,
            ALICLOUD_03_BAKE,
            ALICLOUD_05_PUBLISH,
        ):
            self.assertTrue(os.path.isfile(path), f"{path} must exist")

    def test_template_declares_the_alicloud_builder(self):
        src = _read(ALICLOUD_TEMPLATE)
        self.assertIn(
            'source "alicloud-ecs"',
            src,
            "the Alicloud template must use the alicloud-ecs builder, not "
            "the amazon-ebs one",
        )
        self.assertIn(
            "github.com/hashicorp/alicloud",
            src,
            "the required_plugins block must point at the alicloud plugin",
        )

    def test_template_provisions_every_script(self):
        """Every script in the tree must be wired into the build, or
        it never runs."""
        src = _read(ALICLOUD_TEMPLATE)
        for name in (
            "01-system.sh",
            "02-systemd.sh",
            "03-bake-images.sh",
            "05-publish-ami-id.sh",
            "watchdog.sh",
            "blitzlog_oss_uploader.sh",
        ):
            self.assertIn(
                name,
                src,
                f"{name} must be referenced by the Packer template — a "
                "script that is not provisioned never runs",
            )

    def test_template_uploads_the_container_hardening_contract(self):
        """The hardening contract is shared with the AWS tree (it
        lives next to the agent image it describes). The Alicloud
        build must bake it too, or the watchdog's `source` of it
        fails and the agent never starts."""
        src = _read(ALICLOUD_TEMPLATE)
        self.assertIn(
            "container-hardening.env",
            src,
            "the Alicloud Packer build must upload the shared "
            "container-hardening.env contract",
        )
        self.assertIn(
            "packages/images/agent/container-hardening.env",
            src,
            "the contract must be read from its canonical location "
            "(packages/images/agent/), not copied into the packer tree",
        )


class TestAlicloudWatchdogUsesOssNotS3(unittest.TestCase):
    """The Alicloud watchdog must not speak S3/AWS at all. A stray
    `aws s3` call would fail at runtime with a swallowed `|| true`
    and the logs would silently never reach OSS."""

    @classmethod
    def setUpClass(cls):
        cls.aws = _read(AWS_WATCHDOG)
        cls.watchdog = _read(ALICLOUD_WATCHDOG)
        cls.watchdog_code = _strip_comments(cls.watchdog)
        cls.oss_uploader = _read(ALICLOUD_OSS_UPLOADER)
        cls.oss_uploader_code = _strip_comments(cls.oss_uploader)

    def test_watchdog_uses_the_oss_helpers(self):
        """The Alicloud watchdog must use the OSS verbs for the same
        operations the AWS watchdog performs with the S3 ones.

        Note the asymmetry, which is inherited from the AWS watchdog:
        the watchdog uses `put` (log + artifact upload) and `rm` (the
        bot-pool lock release), but NOT `get` or `ls` — the whisper
        model is downloaded at BAKE time (03-bake-images.sh) and the
        session-restore probe lives in the user-data bootstrap, not in
        the watchdog. The Alicloud watchdog must not drift into using
        extra verbs the AWS one does not, because each verb is a
        separate RAM grant.
        """
        for fn in ("blitzlog_oss_put", "blitzlog_oss_rm"):
            self.assertIn(fn, self.watchdog, f"{fn} must be used")

        # Parity with the AWS watchdog's verb usage. The regex is
        # anchored on a CALL (`blitzlog_s3_put `) so the library
        # FILENAME (`blitzlog_s3_uploader.sh`) does not count as a verb.
        aws_verbs = set(re.findall(r"\bblitzlog_s3_(put|get|rm|ls)\b", self.aws))
        alicloud_verbs = set(
            re.findall(r"\bblitzlog_oss_(put|get|rm|ls)\b", self.watchdog)
        )
        self.assertEqual(
            alicloud_verbs,
            aws_verbs,
            "the Alicloud watchdog must use exactly the OSS verbs the "
            "AWS watchdog uses — each verb is a separate RAM grant",
        )

    def test_watchdog_sources_the_baked_oss_library(self):
        self.assertIn(
            "source /usr/local/bin/blitzlog_oss_uploader.sh",
            self.watchdog,
            "the watchdog must source the AMI-baked OSS library",
        )

    def test_watchdog_has_no_aws_calls(self):
        """No `aws ` invocation and no `aws s3` URL may appear in the
        CODE. Comments naming the AWS equivalents are fine — they
        document the substitution."""
        for pattern in (r"\baws s3\b", r"\baws ssm\b", r"\baws ec2\b"):
            self.assertIsNone(
                re.search(pattern, self.watchdog_code),
                f"the Alicloud watchdog must not CALL {pattern!r} — it "
                "speaks OSS, not S3",
            )
        # The AWS metadata endpoint must not appear in code either.
        self.assertNotIn(
            AWS_METADATA_IP,
            self.watchdog_code,
            "the Alicloud watchdog must use the Aliyun metadata service, " "not IMDSv2",
        )

    def test_oss_uploader_has_no_aws_calls(self):
        for pattern in (r"\baws s3\b", r"\baws ssm\b", r"\baws ec2\b"):
            self.assertIsNone(
                re.search(pattern, self.oss_uploader_code),
                f"the Alicloud OSS library must not CALL {pattern!r}",
            )

    def test_bucket_env_vars_use_the_oss_names(self):
        """The env-var names must match what
        `lambda/scripts/alicloud/*` writes into /etc/blitzlog.env."""
        for var in ("OSS_LOGS_BUCKET", "OSS_LOG_PREFIX"):
            self.assertIn(
                var,
                self.watchdog_code,
                f"{var} must be read by the Alicloud watchdog — the "
                "Alicloud bootstrap writes that name, not the S3 one",
            )
        for var in ("S3_LOGS_BUCKET", "S3_LOG_PREFIX"):
            self.assertNotIn(
                var,
                self.watchdog_code,
                f"{var} is the AWS-side name; the Alicloud watchdog must "
                "use the OSS_ prefix",
            )
        # SESSION_ARCHIVE_* keep their name on both sides.
        self.assertIn("SESSION_ARCHIVE_BUCKET", self.watchdog_code)
        self.assertIn("SESSION_ARCHIVE_PREFIX", self.watchdog_code)

    def test_metadata_endpoint_is_the_aliyun_one(self):
        self.assertIn(
            ALICLOUD_METADATA_IP,
            self.watchdog,
            "the Alicloud watchdog must read the Aliyun metadata service",
        )
        self.assertIn(
            "X-aliyun-ecs-metadata-token",
            self.watchdog,
            "the Aliyun metadata token header has a different name from "
            "AWS's X-aws-ec2-metadata-token",
        )

    def test_secret_read_uses_kms(self):
        self.assertIn(
            "aliyun kms GetSecretValue",
            self.watchdog,
            "the Alicloud watchdog reads secrets from KMS, not SSM",
        )

    def test_terminate_uses_the_ecs_api(self):
        self.assertIn(
            "aliyun ecs DeleteInstances",
            self.watchdog,
            "the Alicloud watchdog must terminate the ECS instance through "
            "the ECS API",
        )


class TestAlicloudMetadataBlockIsFailClosed(unittest.TestCase):
    """Issue #175: the container must not be able to reach the
    instance metadata service and assume the instance role.

    The AWS watchdog blocks 169.254.169.254 and REFUSES to start the
    agent if the rule cannot be installed. The Alicloud watchdog must
    block 100.100.100.200 and behave the same way — the best-effort
    version of this boundary was worse than useless, because a
    boundary that was not in force looked exactly like one that was.
    """

    @classmethod
    def setUpClass(cls):
        cls.watchdog = _read(ALICLOUD_WATCHDOG)

    def test_blocks_the_aliyun_metadata_ip(self):
        self.assertIn(
            f"DOCKER-USER -d {ALICLOUD_METADATA_IP} -j DROP",
            self.watchdog,
            "the DOCKER-USER rule must target the Aliyun metadata IP",
        )

    def test_does_not_block_the_aws_metadata_ip(self):
        self.assertNotIn(
            f"DOCKER-USER -d {AWS_METADATA_IP}",
            self.watchdog,
            "the Alicloud watchdog must not carry the AWS IMDS rule",
        )

    def test_refuses_to_start_when_the_block_fails(self):
        self.assertIn(
            "refusing to start the agent container",
            self.watchdog,
            "the watchdog must refuse to start the agent when the "
            "metadata block cannot be installed",
        )
        # The refusal must be wired to the block function's exit
        # status, not merely logged.
        self.assertRegex(
            self.watchdog,
            r"if block_metadata_from_container; then\s*\n\s*run_agent_container",
            "the container start must be guarded by the block function's "
            "exit status",
        )

    def test_disables_metadata_in_the_sdk_too(self):
        """Defense in depth: the network rule blocks the path, and the
        SDK-level switch stops the client falling back to the instance
        role even if the path were open."""
        self.assertIn(
            "ALIBABA_CLOUD_ECS_METADATA_DISABLED=true",
            self.watchdog,
            "the container must get an SDK-level metadata-disable switch, "
            "the Alicloud analogue of AWS_EC2_METADATA_DISABLED",
        )
        self.assertIn(
            "ALIBABA_CLOUD_IMDSV1_DISABLED=true",
            self.watchdog,
            "IMDSv1 must be disabled too — the Aliyun SDK falls back to "
            "it when the v2 token path is unavailable",
        )


class TestAlicloudWatchdogMirrorsAwsStructure(unittest.TestCase):
    """The two watchdogs must stay structurally parallel. The host
    image is single-cloud by construction, so the difference between
    the two trees is a small enumerable set of substitutions — a
    structural drift means one of them missed a change."""

    @classmethod
    def setUpClass(cls):
        cls.aws = _read(AWS_WATCHDOG)
        cls.alicloud = _read(ALICLOUD_WATCHDOG)
        cls.alicloud_code = _strip_comments(cls.alicloud)

    def test_both_have_the_same_lifecycle_steps(self):
        """Same ordering: load image → start container → wait → upload
        logs → upload artifacts → release lock → cleanup → terminate."""
        for marker in (
            "docker load -i /opt/blitzlog/images/blitzlog-agent.tar.gz",
            "rm -f /workspace/.shutdown",
            "blitzlog_oss_put",
            "blitzlog_oss_rm",
            "docker rm -f blitzlog-agent",
        ):
            self.assertIn(marker, self.alicloud, f"{marker} must be present")

        # The order matters: the uploads and the lock release must come
        # BEFORE the instance teardown, or the artifacts are lost.
        put_at = self.alicloud_code.index("blitzlog_oss_put")
        rm_at = self.alicloud_code.index("blitzlog_oss_rm")
        term_at = self.alicloud_code.index("aliyun ecs DeleteInstances")
        self.assertLess(put_at, rm_at, "uploads must precede the lock release")
        self.assertLess(rm_at, term_at, "the lock release must precede teardown")

    def test_both_write_the_etc_blitzlog_env_file(self):
        """The bootstrap writes /etc/blitzlog.env and the watchdog
        re-writes it. Both trees must do this, or the container gets
        no env at all."""
        self.assertIn("cat > /etc/blitzlog.env <<ENVEOF", self.aws)
        self.assertIn("cat > /etc/blitzlog.env <<ENVEOF", self.alicloud)

    def test_both_source_the_hardening_contract(self):
        self.assertIn("source /usr/local/bin/container-hardening.env", self.aws)
        self.assertIn("source /usr/local/bin/container-hardening.env", self.alicloud)

    def test_both_load_the_same_baked_image_path(self):
        self.assertIn(
            "source /root/blitzlog.env",
            self.alicloud,
            "the watchdog must source the Packer-baked agent-image spec",
        )


class TestAlicloudOssUploaderParity(unittest.TestCase):
    """The OSS library is the analogue of `blitzlog_uploader.sh`
    (issue #123). Same four verbs, same return-code contract."""

    @classmethod
    def setUpClass(cls):
        cls.aws = _read(AWS_OSS_UPLOADER)
        cls.oss = _read(ALICLOUD_OSS_UPLOADER)

    def test_same_function_names(self):
        for fn in (
            "blitzlog_s3_put",
            "blitzlog_s3_get",
            "blitzlog_s3_rm",
            "blitzlog_s3_ls",
        ):
            self.assertIn(fn, self.aws, f"the AWS library must define {fn}")
        for fn in (
            "blitzlog_oss_put",
            "blitzlog_oss_get",
            "blitzlog_oss_rm",
            "blitzlog_oss_ls",
        ):
            self.assertIn(fn, self.oss, f"the Alicloud library must define {fn}")

    def test_uses_ossutil(self):
        self.assertIn("ossutil cp", self.oss)
        self.assertIn("ossutil rm", self.oss)
        self.assertIn("ossutil ls", self.oss)

    def test_region_fallback_chain_matches_the_aws_shape(self):
        """Both libraries resolve the region as
        explicit arg > $REGION > provider-specific default."""
        self.assertIn("_bl_uploader_region", self.aws)
        self.assertIn("_bl_oss_uploader_region", self.oss)
        self.assertIn("ALIBABA_CLOUD_REGION", self.oss)

    def test_error_lines_use_the_oss_wording(self):
        self.assertIn("ERROR: OSS put failed", self.oss)
        self.assertIn("ERROR: OSS get failed", self.oss)
        self.assertIn("ERROR: OSS rm failed", self.oss)
        self.assertIn("ERROR: OSS ls failed", self.oss)


class TestAlicloudSystemAndSystemdScripts(unittest.TestCase):
    """01-system.sh installs the Alicloud CLIs; 02-systemd.sh
    installs the unit + the baked libraries."""

    @classmethod
    def setUpClass(cls):
        cls.system = _read(ALICLOUD_01_SYSTEM)
        cls.systemd = _read(ALICLOUD_02_SYSTEMD)

    def test_system_installs_the_aliyun_cli_and_ossutil(self):
        self.assertIn("aliyun-cli-linux", self.system)
        self.assertIn("ossutil", self.system)
        self.assertIn("/usr/local/bin/aliyun", self.system)
        self.assertIn("/usr/local/bin/ossutil", self.system)

    def test_system_installs_iptables(self):
        """The watchdog's DOCKER-USER rule needs iptables, and it
        REFUSES to start the agent without it. It must be an explicit
        dependency, not a transitive one."""
        self.assertIn("iptables", self.system)

    def test_system_installs_docker(self):
        self.assertIn("docker-ce", self.system)

    def test_systemd_installs_the_agent_service_unit(self):
        self.assertIn(
            "/etc/systemd/system/blitzlog-agent.service",
            self.systemd,
            "the Alicloud image needs the blitzlog-agent.service unit — "
            "without it `systemctl start blitzlog-agent.service` in the "
            "bootstrap fails and the agent never runs (issue #125)",
        )
        self.assertIn("ExecStart=/usr/local/bin/watchdog.sh", self.systemd)
        self.assertIn(
            "sudo systemctl enable blitzlog-agent.service",
            self.systemd,
            "the unit must be ENABLED so it starts on boot, not only when "
            "started by hand",
        )

    def test_systemd_installs_the_oss_library(self):
        self.assertIn(
            "/usr/local/bin/blitzlog_oss_uploader.sh",
            self.systemd,
            "the OSS library must be installed at the path the watchdog "
            "and the user-data bootstrap source it from",
        )

    def test_systemd_installs_the_hardening_contract(self):
        self.assertIn("/usr/local/bin/container-hardening.env", self.systemd)


class TestAlicloudBakeAndPublishScripts(unittest.TestCase):
    """03-bake-images.sh bakes the agent image + whisper model;
    05-publish-ami-id.sh publishes the image id to KMS."""

    @classmethod
    def setUpClass(cls):
        cls.bake = _read(ALICLOUD_03_BAKE)
        cls.publish = _read(ALICLOUD_05_PUBLISH)

    def test_bake_pulls_the_agent_image(self):
        self.assertIn("ghcr.io/great-wall-connect/blitzlog-agent", self.bake)
        self.assertIn("docker pull", self.bake)
        self.assertIn("docker save", self.bake)

    def test_bake_writes_the_agent_image_spec(self):
        """The watchdog sources /root/blitzlog.env for the repo + tag."""
        self.assertIn("/root/blitzlog.env", self.bake)
        self.assertIn("AGENT_IMAGE_REPO", self.bake)
        self.assertIn("AGENT_IMAGE_TAG", self.bake)

    def test_bake_downloads_the_whisper_model_from_oss(self):
        self.assertIn("blitzlog_oss_get", self.bake)
        self.assertIn("/opt/whisper-stt/models", self.bake)

    def test_publish_writes_to_kms(self):
        """The Alicloud `SecretStore` is KMS Secrets Manager, so the
        image id must be published there — not to SSM (invisible to
        the Alicloud path) and not to Parameter Store."""
        self.assertIn("aliyun kms CreateSecret", self.publish)
        self.assertIn("aliyun kms PutSecretValue", self.publish)

    def test_publish_uses_the_aliyun_parameter_path(self):
        self.assertIn(
            "agent-ami-id-ecs-ubuntu",
            self.publish,
            "the published secret name must match the suffix the "
            "AlicloudCompute provider reads",
        )
        self.assertIn("/blitzlog/${BLITZLOG_ENV}", self.publish)

    def test_publish_does_not_write_to_ssm(self):
        self.assertNotIn(
            "aws ssm put-parameter",
            self.publish,
            "the Alicloud publish path must not write to SSM — the "
            "Alicloud SecretStore reads KMS",
        )


class TestAlicloudImageIdContractIsConsistent(unittest.TestCase):
    """The publish path and the read path must agree on the secret
    name. If they disagree, the Alicloud path silently falls back to
    the public Ubuntu image and the baked agent image is never used —
    an invisible failure, which is why it is pinned here."""

    def test_publish_suffix_matches_the_provider_suffix(self):
        provider_src = _read(os.path.join(REPO_ROOT, "lambda/provider/alicloud.py"))
        self.assertIn(
            'agent_image_param_suffix = "agent-ami-id-ecs-ubuntu"',
            provider_src,
            "the Alicloud compute provider must read the same suffix the "
            "Packer post-processor publishes",
        )
        publish_src = _read(ALICLOUD_05_PUBLISH)
        self.assertIn("agent-ami-id-ecs-ubuntu", publish_src)

    def test_the_two_clouds_use_different_suffixes(self):
        """The AWS suffix would point at an SSM parameter the
        KMS-backed Alicloud store cannot read, and vice versa."""
        aws_provider = _read(os.path.join(REPO_ROOT, "lambda/provider/aws.py"))
        alicloud_provider = _read(
            os.path.join(REPO_ROOT, "lambda/provider/alicloud.py")
        )
        self.assertIn("agent-ami-id-docker-ubuntu", aws_provider)
        self.assertIn("agent-ami-id-ecs-ubuntu", alicloud_provider)


if __name__ == "__main__":
    unittest.main()
