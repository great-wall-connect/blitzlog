"""Regression tests for `scripts.alicloud.assisted.build_assisted_user_data`
(issue #126).

Parallel to `tests/test_scripts_autonomous_alicloud.py` — same set
of invariants, applied to the assisted-mode user-data. The
differences from the autonomous test are:

  - the `TELEGRAM_BOT_TOKEN` / `TELEGRAM_USER_ID` exports must be
    present (assisted mode uses a Telegram bot pool)
  - the `MODE="assisted"` env file line (vs autonomous)
  - the `S3_RESTORE_*` env-var names in the session-restore block
    are now `OSS_RESTORE_*` (parallel to the AWS-side S3_RESTORE_*)
  - the `_session_restore_script` body uses `blitzlog_oss_ls` and
    `blitzlog_oss_get` (vs the AWS-side `blitzlog_s3_ls` and
    `blitzlog_s3_get`)
"""

import unittest

from scripts.alicloud.assisted import build_assisted_user_data


class TestAlicloudAssistedUserData(unittest.TestCase):
    """Static shape checks for the Alicloud assisted user-data script."""

    def _render(self, **kwargs) -> str:
        defaults = {
            "repo": "great-wall-connect/blitzlog",
            "issue_number": 126,
            "sender_login": "daniel-sarosi-gwc",
            "sender_id": "12345",
            "bot_name": "blitzlog-dev-bot",
            "bot_token": "REPLACE_ME_token",
            "telegram_user_id": "987654321",
            "local_llm": None,
        }
        defaults.update(kwargs)
        return build_assisted_user_data(**defaults)

    def test_ossutil_install_block_present(self):
        """ossutil install on first boot (Packer image is AWS-only)."""
        ud = self._render()
        self.assertIn(
            "https://gosspublic.alicdn.com/ossutil",
            ud,
            "Alibaba CDN URL for ossutil must be present",
        )

    def test_aliyun_cli_install_block_present(self):
        """aliyun CLI install for KMS read."""
        ud = self._render()
        self.assertIn(
            "https://aliyuncli.alicdn.com/aliyun-cli-linux",
            ud,
            "Aliyun CLI must be installed for KMS secret fetch",
        )

    def test_inline_oss_helper_library_present(self):
        """The `blitzlog_oss_*` library is inlined (parallel to
        AWS-side `blitzlog_s3_*`).
        """
        ud = self._render()
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
        """The library must call `ossutil` under the hood."""
        ud = self._render()
        for verb in ("ossutil cp", "ossutil ls", "ossutil rm"):
            self.assertIn(
                verb,
                ud,
                f"Inline library must call `{verb}`",
            )

    def test_aliyun_imds_endpoint(self):
        """Aliyun IMDS at 100.100.100.200, not AWS's 169.254.169.254."""
        ud = self._render()
        self.assertIn("100.100.100.200", ud)
        self.assertIn("X-aliyun-ecs-metadata-token", ud)
        self.assertNotIn("169.254.169.254", ud)

    def test_kms_get_secret_value_present(self):
        """KMS read via `aliyun kms GetSecretValue` (not SSM)."""
        ud = self._render()
        self.assertIn("aliyun kms GetSecretValue", ud)
        self.assertNotIn("aws ssm get-parameter", ud)

    def test_oss_log_prefix_env_var(self):
        """`OSS_LOG_PREFIX` is set; `S3_LOG_PREFIX` is not."""
        ud = self._render()
        self.assertIn('OSS_LOG_PREFIX="', ud)
        self.assertNotIn("S3_LOG_PREFIX=", ud)

    def test_oss_logs_bucket_env_var(self):
        """`OSS_LOGS_BUCKET` is set + passed through to /etc/blitzlog.env."""
        ud = self._render()
        self.assertIn(
            'OSS_LOGS_BUCKET="<your-agent-logs-bucket>"',
            ud,
            "OSS_LOGS_BUCKET must be exported in the prologue",
        )
        self.assertIn(
            'OSS_LOGS_BUCKET="${OSS_LOGS_BUCKET}"',
            ud,
            "OSS_LOGS_BUCKET must be passed through to /etc/blitzlog.env",
        )

    def test_session_restore_block_uses_oss_helpers(self):
        """The session-restore block uses `blitzlog_oss_ls` /
        `blitzlog_oss_get` and `OSS_RESTORE_BUCKET` (not the
        AWS-side `S3_*`).
        """
        ud = self._render()
        # The restore-bucket env-var.
        self.assertIn("OSS_RESTORE_BUCKET=", ud)
        # The helpers used in the body.
        self.assertIn(
            'blitzlog_oss_ls "${OSS_RESTORE_BUCKET}"',
            ud,
            "Session-restore block must use blitzlog_oss_ls with "
            "OSS_RESTORE_BUCKET (parallel to AWS-side blitzlog_s3_ls)",
        )
        self.assertIn(
            'blitzlog_oss_get "${OSS_RESTORE_BUCKET}"',
            ud,
            "Session-restore block must use blitzlog_oss_get with "
            "OSS_RESTORE_BUCKET",
        )
        # And the AWS-side names must not appear.
        self.assertNotIn("S3_RESTORE_BUCKET=", ud)
        self.assertNotIn("blitzlog_s3_ls", ud)
        self.assertNotIn("blitzlog_s3_get", ud)

    def test_telegram_env_vars_set(self):
        """Assisted mode requires Telegram bot creds in /etc/blitzlog.env."""
        ud = self._render()
        # The function arg is substituted into the env-export line
        # (the literal token value) AND into the /etc/blitzlog.env
        # heredoc (the `${TELEGRAM_BOT_TOKEN}` env-var ref that
        # forwards the value to the watchdog).
        self.assertIn(
            'TELEGRAM_BOT_TOKEN="REPLACE_ME_token"',
            ud,
            "TELEGRAM_BOT_TOKEN must be exported in the prologue with "
            "the literal token value (the function arg)",
        )
        self.assertIn(
            'TELEGRAM_USER_ID="987654321"',
            ud,
            "TELEGRAM_USER_ID must be exported with the literal value",
        )
        # /etc/blitzlog.env forwards the env vars.
        self.assertIn(
            'TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN}"',
            ud,
            "TELEGRAM_BOT_TOKEN must be passed through to /etc/blitzlog.env",
        )
        self.assertIn(
            'TELEGRAM_USER_ID="${TELEGRAM_USER_ID}"',
            ud,
            "TELEGRAM_USER_ID must be passed through to /etc/blitzlog.env",
        )

    def test_mode_env_var_is_assisted(self):
        """`MODE=\"assisted\"` in /etc/blitzlog.env (vs autonomous)."""
        ud = self._render()
        self.assertIn('MODE="assisted"', ud)
        # Autonomous mode's literal must not appear.
        self.assertNotIn('MODE="autonomous"', ud)

    def test_secret_paths_byte_identical_to_aws(self):
        """KMS secret paths match the AWS-side SSM paths byte-for-byte."""
        ud = self._render()
        # The github-app token path (per-issue ephemeral).
        self.assertRegex(
            ud,
            r"/blitzlog/(prod|dev)/ephemeral/gh/[^/]+/126",
            "Per-issue GitHub token path must match the AWS-side SSM path",
        )
        # The OPENCODE_API_KEY.
        self.assertIn(
            "/blitzlog/prod/opencode/api-key",
            ud,
        )
        # The STT_*
        self.assertIn(
            "/blitzlog/prod/stt/api-url",
            ud,
        )

    def test_no_aws_s3_helper_calls(self):
        """The Alicloud user-data must not contain any of the
        `blitzlog_s3_*` helper calls (the AWS-side name set). A
        copy-paste regression would silently make the bootstrap
        call a non-existent function and fail at boot.
        """
        ud = self._render()
        for helper in (
            "blitzlog_s3_get",
            "blitzlog_s3_put",
            "blitzlog_s3_ls",
            "blitzlog_s3_rm",
        ):
            self.assertNotIn(
                f"{helper} ",
                ud,
                f"{helper} must not appear on the Alicloud path",
            )
            self.assertNotIn(
                f"{helper}(",
                ud,
                f"{helper} must not appear on the Alicloud path",
            )


if __name__ == "__main__":
    unittest.main()
