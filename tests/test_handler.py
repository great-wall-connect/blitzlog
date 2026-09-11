"""Lambda entrypoint + Terraform/mise file content regressions.

The entrypoint (`lambda_handler`) and its `extract_event_data` helper
live in `lambda/handler.py`. The handler dispatches into `scripts/`,
`auth`, `bot_pool`, and `ec2` — those modules have their own test
files (test_scripts_*, test_auth.py, test_bot_pool.py, test_ec2.py).

Tests in this file:
    - `TestLambdaHandlerBotPool`: webhook → launch flow (entrypoint)
    - `TestLambdaBuildConfiguration`: infra/modules/core/lambda.tf content
    - `TestMiseToml`: top-level mise.toml Node/Terraform pinning regressions
"""

import json
import os
import unittest
from unittest.mock import patch


class TestLambdaHandlerBotPool(unittest.TestCase):
    @patch("handler._update_lock_instance_id")
    @patch("handler.launch_ec2_spot_instance", return_value="i-123")
    @patch("handler.acquire_bot_token")
    @patch("handler.get_telegram_user_id", return_value="99999")
    @patch("handler.get_github_app_token", return_value="ghp_test")
    @patch("handler.verify_github_signature", return_value=True)
    @patch("handler.get_ssm_param", return_value="secret")
    @patch("handler.get_local_llm_config", return_value=None)
    def test_assisted_calls_acquire_bot_token_with_sender(
        self,
        mock_local_llm,
        mock_ssm,
        mock_sig,
        mock_gh,
        mock_tg_user,
        mock_acquire,
        mock_launch,
        mock_update_lock,
    ):
        from handler import lambda_handler

        mock_acquire.return_value = ("escobar", "token123")

        event = {
            "body": json.dumps(
                {
                    "action": "labeled",
                    "issue": {"number": 42, "labels": [{"name": "assisted"}]},
                    "repository": {"full_name": "org/repo"},
                    "sender": {"login": "octocat", "id": "123"},
                }
            ),
            "headers": {"x-hub-signature-256": "sha256=abc"},
        }

        result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        mock_tg_user.assert_called_once_with("octocat")
        mock_acquire.assert_called_once()
        acquire_args = mock_acquire.call_args
        self.assertEqual(acquire_args[0][0], "octocat")
        update_args = mock_update_lock.call_args
        self.assertEqual(update_args[0][0], "octocat")
        self.assertEqual(update_args[0][1], "escobar")

    @patch("handler.get_telegram_user_id", return_value="99999")
    @patch("handler.get_github_app_token", return_value="ghp_test")
    @patch("handler.verify_github_signature", return_value=True)
    @patch("handler.get_ssm_param", return_value="secret")
    @patch("handler.acquire_bot_token", return_value=None)
    def test_pool_exhausted_returns_503(
        self, mock_acquire, mock_ssm, mock_sig, mock_gh, mock_tg_user
    ):
        from handler import lambda_handler

        event = {
            "body": json.dumps(
                {
                    "action": "labeled",
                    "issue": {"number": 42, "labels": [{"name": "assisted"}]},
                    "repository": {"full_name": "org/repo"},
                    "sender": {"login": "octocat", "id": "123"},
                }
            ),
            "headers": {"x-hub-signature-256": "sha256=abc"},
        }

        result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 503)
        self.assertIn("octocat", result["body"])

    @patch("handler.get_github_app_token", return_value="ghp_test")
    @patch("handler.verify_github_signature", return_value=True)
    @patch("handler.get_ssm_param", return_value="secret")
    @patch("handler.get_telegram_user_id", return_value=None)
    def test_user_without_telegram_id_returns_503(
        self, mock_tg_user, mock_ssm, mock_sig, mock_gh
    ):
        from handler import lambda_handler

        event = {
            "body": json.dumps(
                {
                    "action": "labeled",
                    "issue": {"number": 42, "labels": [{"name": "assisted"}]},
                    "repository": {"full_name": "org/repo"},
                    "sender": {"login": "lonely", "id": "9"},
                }
            ),
            "headers": {"x-hub-signature-256": "sha256=abc"},
        }

        result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 503)
        self.assertIn("lonely", result["body"])

    @patch("handler.launch_ec2_spot_instance", return_value="i-123")
    @patch("handler.get_github_app_token", return_value="ghp_test")
    @patch("handler.verify_github_signature", return_value=True)
    @patch("handler.get_ssm_param", return_value="secret")
    @patch("handler.list_bot_pool")
    @patch("handler.get_local_llm_config", return_value=None)
    def test_autonomous_does_not_call_acquire(
        self, mock_local_llm, mock_pool, mock_ssm, mock_sig, mock_gh, mock_launch
    ):
        from handler import lambda_handler

        event = {
            "body": json.dumps(
                {
                    "action": "labeled",
                    "issue": {"number": 42, "labels": [{"name": "autonomous"}]},
                    "repository": {"full_name": "org/repo"},
                    "sender": {"login": "octocat", "id": "123"},
                }
            ),
            "headers": {"x-hub-signature-256": "sha256=abc"},
        }

        result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        mock_pool.assert_not_called()


class TestLambdaBuildConfiguration(unittest.TestCase):
    """Regression tests for infra/modules/core/lambda.tf build-time configuration.

    A mistake here silently produces a broken Lambda zip at runtime
    (e.g., 1-byte server.py from a stale cp reference). These tests
    assert the file content directly so we catch the bug at PR review
    time, not on the EC2 instance.
    """

    @staticmethod
    def _read_lambda_tf():
        with open("infra/modules/core/lambda.tf", "r", encoding="utf-8") as f:
            return f.read()

    def test_lambda_build_copies_python_shim(self):
        """Regression: the Lambda build's cp must reference server.py
        (the current canonical shim name), not the obsolete server.js.
        Otherwise the Lambda zip is missing server.py, the bootstrap's
        heredoc writes a 1-byte stub (server.py comes back empty), and
        the shim is empty on the EC2 instance."""
        content = self._read_lambda_tf()
        self.assertRegex(
            content,
            r"cp\s+\$\{path\.module\}/(?:\.\./)+packages/whisper-stt-shim/server\.py",
        )
        self.assertNotRegex(
            content,
            r"cp\s+\$\{path\.module\}/(?:\.\./)+packages/whisper-stt-shim/server\.js",
        )

    def test_lambda_build_local_exec_uses_set_e(self):
        """Regression: the local-exec build must `set -e` so a missing
        file (cp fails) aborts the build instead of silently producing
        a broken zip. Without this, future renames (like server.js ->
        server.py) produce a zip that looks fine but is missing the
        renamed file, and the EC2 instance gets a 1-byte stub."""
        content = self._read_lambda_tf()
        self.assertRegex(
            content,
            r'provisioner\s+"local-exec"\s*\{\s*command\s*=\s*<<-EOT\s*\n\s*set\s+-e',
        )


class TestMiseToml(unittest.TestCase):
    @staticmethod
    def _read_mise_toml():
        repo_root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(repo_root, "mise.toml"), "r", encoding="utf-8") as f:
            return f.read()

    def test_mise_toml_does_not_pin_node(self):
        # blitzlog itself is Python; Node is brought in at runtime by the
        # user-data's `dnf install nodejs24` plus the bot's `npx`. Pinning
        # Node in mise.toml reinstalls Node (typically v20) and shims it ahead
        # of /usr/bin/node, masking dnf's install — that's what made the bot
        # refuse to start with "requires Node.js 22.14+, 23.6+, or 24+".
        content = self._read_mise_toml()
        self.assertNotRegex(content, r"^\s*node\s*=", msg=content)

    def test_mise_toml_does_not_pin_terraform(self):
        # The bootstrap doesn't invoke the Terraform CLI; pinning it just
        # adds a needless install on the EC2 instance.
        content = self._read_mise_toml()
        self.assertNotRegex(content, r"^\s*terraform\s*=", msg=content)


if __name__ == "__main__":
    unittest.main()
