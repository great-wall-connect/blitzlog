"""Regression tests for `scripts.alicloud.autonomous.build_autonomous_user_data`
(issue #126).

The Alicloud user-data script must differ from the AWS-side script
in five cloud-specific ways:

  1. **CLI install block**: `ossutil` and `aliyun` are installed
     on first boot (the Packer image is still AWS-only — Phase 5
     brings a per-cloud Packer image).
  2. **Inline `blitzlog_oss_*` library**: parallel to the AWS-side
     `blitzlog_s3_*` from
     `infra/aws/packer/scripts-docker-ubuntu/blitzlog_uploader.sh`.
  3. **KMS read via `aliyun kms GetSecretValue`** instead of
     `aws ssm get-parameter`. The secret paths are byte-for-byte
     identical.
  4. **Aliyun IMDS** at `100.100.100.200` (not AWS's `169.254.169.254`).
  5. **`OSS_LOGS_BUCKET` / `OSS_LOG_PREFIX`** env-var names instead
     of `S3_LOGS_BUCKET` / `S3_LOG_PREFIX`.

These tests are static — they import the builder, render the
user-data string, and regex-assert each of the five invariants.
No real AWS / Alicloud credentials are needed.
"""

import re
import unittest

from scripts.alicloud.autonomous import build_autonomous_user_data


class TestAlicloudAutonomousUserData(unittest.TestCase):
    """Static shape checks for the Alicloud autonomous user-data script."""

    def _render(self, **kwargs) -> str:
        """Render the user-data with sensible defaults so each test
        can override one or two fields.
        """
        defaults = {
            "repo": "great-wall-connect/blitzlog",
            "issue_number": 126,
            "sender_login": "daniel-sarosi-gwc",
            "sender_id": "12345",
            "local_llm": None,
        }
        defaults.update(kwargs)
        return build_autonomous_user_data(**defaults)

    def test_ossutil_install_block_present(self):
        """The CLI install block must curl ossutil from the
        Aliyun CDN and place it on PATH.
        """
        ud = self._render()
        self.assertIn(
            "https://gosspublic.alicdn.com/ossutil",
            ud,
            "Alibaba CDN URL for ossutil must be present in the user-data",
        )
        self.assertIn(
            "chmod +x ossutil64",
            ud,
            "ossutil64 must be made executable before being moved to PATH",
        )
        self.assertIn(
            "mv ossutil64 /usr/local/bin/ossutil",
            ud,
            "ossutil must be moved onto PATH",
        )

    def test_aliyun_cli_install_block_present(self):
        """The CLI install block must curl the aliyun CLI for KMS read."""
        ud = self._render()
        self.assertIn(
            "https://aliyuncli.alicdn.com/aliyun-cli-linux",
            ud,
            "Aliyun CLI must be downloaded from the Alibaba CDN for KMS read",
        )
        self.assertIn(
            "mv aliyun /usr/local/bin/aliyun",
            ud,
            "aliyun CLI must be moved onto PATH",
        )

    def test_inline_oss_helper_library_present(self):
        """The user-data must inline the `blitzlog_oss_*` library
        (parallel to the AWS-side `blitzlog_s3_*`).
        """
        ud = self._render()
        # The four canonical helper names, parallel to the AWS
        # `blitzlog_s3_*` set.
        for helper in (
            "blitzlog_oss_get",
            "blitzlog_oss_put",
            "blitzlog_oss_ls",
            "blitzlog_oss_rm",
        ):
            self.assertIn(
                f"{helper}()",
                ud,
                f"{helper} must be defined in the inlined library",
            )

    def test_oss_helper_wraps_ossutil(self):
        """The inline library must call `ossutil` under the hood
        (it's the OSS analogue of `aws s3`).
        """
        ud = self._render()
        # The library uses `ossutil cp` / `ossutil ls` / `ossutil rm`.
        for verb in ("ossutil cp", "ossutil ls", "ossutil rm"):
            self.assertIn(
                verb,
                ud,
                f"Inline library must call `{verb}` (OSS analogue of `aws s3`)",
            )

    def test_aliyun_imds_endpoint(self):
        """The user-data must query the Aliyun metadata server at
        100.100.100.200 (not AWS's 169.254.169.254).
        """
        ud = self._render()
        self.assertIn(
            "100.100.100.200",
            ud,
            "Aliyun IMDS endpoint must be present",
        )
        self.assertIn(
            "X-aliyun-ecs-metadata-token",
            ud,
            "Aliyun IMDS token header must be present",
        )
        # And the AWS IMDS endpoint must NOT be present.
        self.assertNotIn(
            "169.254.169.254",
            ud,
            "AWS IMDS endpoint must not appear on the Alicloud path",
        )

    def test_kms_get_secret_value_present(self):
        """The user-data must use `aliyun kms GetSecretValue` for
        secret fetching (the KMS analogue of `aws ssm get-parameter`).
        """
        ud = self._render()
        # The literal API call shape.
        self.assertIn(
            "aliyun kms GetSecretValue",
            ud,
            "`aliyun kms GetSecretValue` must be used for secret fetching",
        )
        # And the AWS SSM API must NOT be present.
        self.assertNotIn(
            "aws ssm get-parameter",
            ud,
            "`aws ssm get-parameter` must not appear on the Alicloud path",
        )

    def test_oss_log_prefix_env_var(self):
        """The user-data must set `OSS_LOG_PREFIX` (not `S3_LOG_PREFIX`)
        and the /etc/blitzlog.env heredoc must use the same name.
        """
        ud = self._render()
        self.assertIn(
            'OSS_LOG_PREFIX="',
            ud,
            "OSS_LOG_PREFIX must be set in the script",
        )
        # AWS-side name must not appear in the Alicloud user-data.
        self.assertNotIn(
            "S3_LOG_PREFIX=",
            ud,
            "S3_LOG_PREFIX must not appear on the Alicloud path",
        )

    def test_oss_logs_bucket_env_var(self):
        """OSS_LOGS_BUCKET is set both as an env export and in the
        /etc/blitzlog.env heredoc.
        """
        ud = self._render()
        # The export line (the f-string substitutes the literal
        # `<your-agent-logs-bucket>` placeholder when the env var
        # isn't set). We use `re.search` semantics — no anchors, just
        # assert the substring `OSS_LOGS_BUCKET="<value>"` appears
        # in the export block (the export block is the contiguous
        # run of `OSS_LOGS_BUCKET=...` followed by the
        # `SESSION_ARCHIVE_*` lines).
        self.assertIn(
            'OSS_LOGS_BUCKET="<your-agent-logs-bucket>"',
            ud,
            "OSS_LOGS_BUCKET must be exported in the prologue (default "
            "placeholder is fine — the operator overrides via the env "
            "var at Lambda run time)",
        )
        # The /etc/blitzlog.env line (uses `${OSS_LOGS_BUCKET}` to
        # forward the env-var value to the watchdog).
        self.assertIn(
            'OSS_LOGS_BUCKET="${OSS_LOGS_BUCKET}"',
            ud,
            "OSS_LOGS_BUCKET must be passed through to /etc/blitzlog.env",
        )

    def test_secret_paths_byte_identical_to_aws(self):
        """The KMS secret paths are byte-for-byte identical to the
        AWS SSM paths (`/blitzlog/<env>/<service>/<name>`). This
        guarantees the FC-side RAM grant in
        `infra/alicloud/modules/core/ram.tf` is the only ACL the
        host needs.

        The user-data builder substitutes `$BLITZLOG_ENV` for the
        literal env value at render time (via `_blitzlog_env()`),
        so the rendered path looks like `/blitzlog/prod/...` not
        `/blitzlog/${BLITZLOG_ENV}/...`. The structural shape
        (per-env prefix + service + leaf) is what we assert.
        """
        ud = self._render()
        # The github-app token path (per-issue ephemeral). The
        # FC substitutes `${BLITZLOG_ENV}` → `prod` in the test env.
        # We assert the structural shape with a flexible regex.
        self.assertRegex(
            ud,
            r"/blitzlog/(prod|dev)/ephemeral/gh/[^/]+/126",
            "Per-issue GitHub token path must match the AWS-side SSM path",
        )
        # The OPENCODE_API_KEY.
        self.assertIn(
            "/blitzlog/prod/opencode/api-key",
            ud,
            "OPENCODE_API_KEY secret path must match the AWS-side SSM path",
        )
        # The STT_*
        self.assertIn(
            "/blitzlog/prod/stt/api-url",
            ud,
            "STT_API_URL secret path must match the AWS-side SSM path",
        )

    def test_known_unbound_var_does_not_appear_in_unconditional(self):
        """Same byte-layout invariant as the AWS-side test in
        `tests/test_scripts_autonomous.py`: the helper library
        references `$STT_MODEL` and `$STT_MODELS_BUCKET` only
        inside the conditional whisper-model-download block
        (guarded by `:-base.en`). The unconditional env-export
        preamble must NOT contain them as bare `$STT_*`
        expansions (which would trip `set -u` before the
        `_read_secrets_from_kms_script` block runs).
        """
        ud = self._render()
        # Find the env-export line (the one between the bucket
        # exports and the `log "=== Cloud-coder` line).
        env_export_line = re.search(r"export ISSUE_NUMBER[^\n]*", ud)
        self.assertIsNotNone(
            env_export_line,
            "Env-export preamble must contain ISSUE_NUMBER",
        )
        self.assertNotIn(
            "STT_MODEL",
            env_export_line.group(0),
            "Env-export preamble must not reference $STT_MODEL (set "
            "by _read_secrets_from_kms_script, AFTER $REGION)",
        )
        self.assertNotIn(
            "STT_MODELS_BUCKET",
            env_export_line.group(0),
            "Env-export preamble must not reference $STT_MODELS_BUCKET "
            "(set by _read_secrets_from_kms_script, AFTER $REGION)",
        )


if __name__ == "__main__":
    unittest.main()
