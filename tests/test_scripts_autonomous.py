"""Autonomous-mode user-data bootstrap builder."""

import os
import unittest
from unittest.mock import patch

from scripts.autonomous import build_autonomous_user_data


def _with_env(fn):
    """Build an autonomous user-data string under the standard test
    environment (S3_LOGS_BUCKET + OPENCODE_MODEL set).
    """
    with patch.dict(
        os.environ,
        {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"},
    ):
        return fn()


class TestAutonomousNoSecrets(unittest.TestCase):
    def test_no_embedded_token(self):
        script = build_autonomous_user_data("org/repo", 42, "octocat", "12345")
        self.assertNotIn("ghp_", script)
        self.assertNotIn("x-access-token:ghp_", script)
        # The GitHub token is set via SSM and passed to the container via
        # the GITHUB_TOKEN env var; the host bootstrap doesn't configure
        # git identity (the container does, in entrypoint.sh).
        self.assertIn("GITHUB_TOKEN=", script)

    def test_no_identity_without_sender(self):
        script = build_autonomous_user_data("org/repo", 42)
        self.assertNotIn("user.name", script)
        self.assertNotIn("user.email", script)


class TestAutonomousModelAndDiagnostics(unittest.TestCase):
    def test_user_data_uses_minimax_model(self):
        with patch.dict(
            os.environ,
            {
                "S3_LOGS_BUCKET": "test-bucket",
                "OPENCODE_MODEL": "minimax-coding-plan/MiniMax-M3",
            },
        ):
            user_data = build_autonomous_user_data("owner/repo", 42)
        self.assertIn("minimax-coding-plan", user_data)
        self.assertIn("OPENCODE_MODEL", user_data)

    def test_logs_config_diagnostic(self):
        with patch.dict(
            os.environ,
            {
                "S3_LOGS_BUCKET": "test-bucket",
                "OPENCODE_MODEL": "minimax-coding-plan/MiniMax-M3",
            },
        ):
            user_data = build_autonomous_user_data("owner/repo", 42)
        # The bootstrap logs an "Effective opencode config: model=$X" line
        # right before starting the watchdog. The container's entrypoint
        # writes the actual opencode.json; the bootstrap is just a
        # diagnostic line.
        self.assertIn("Effective opencode config", user_data)
        self.assertIn('model=$OPENCODE_MODEL', user_data)

    def test_default_opencode_model_is_minimax(self):
        with patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"}, clear=True):
            self.assertIn(
                "minimax-coding-plan/MiniMax-M3",
                build_autonomous_user_data("owner/repo", 1),
            )


class TestAutonomousShutdownExclusion(unittest.TestCase):
    """Autonomous mode runs unattended and has no interactive shutdown
    surface (no Telegram bot, no agent tool call). The shutdown tool
    is still installed in the image (the Dockerfile copies it), but the
    Lambda user-data should NOT include any shutdown-tool wiring — the
    watchdog terminates the instance on container exit."""

    def test_user_data_excludes_shutdown_tool(self):
        user_data = _with_env(lambda: build_autonomous_user_data("owner/repo", 42))
        self.assertNotIn("shutdown.js", user_data)
        self.assertNotIn("SHUTDOWN_TOOL_JS", user_data)
if __name__ == "__main__":
    unittest.main()
