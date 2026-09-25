"""Assisted-mode user-data bootstrap builder."""

import os
import unittest
from unittest.mock import patch

from scripts.assisted import build_assisted_user_data


def _with_env(fn):
    with patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    ):
        return fn()


def _build_assisted(**kwargs):
    defaults = {
        "repo": "owner/repo",
        "issue_number": 42,
        "sender_login": "octocat",
        "sender_id": "12345",
        "bot_name": "escobar",
        "bot_token": "123:ABC",
        "telegram_user_id": "99999",
    }
    defaults.update(kwargs)
    return build_assisted_user_data(**defaults)


class TestAssistedNoSecrets(unittest.TestCase):
    def test_no_embedded_token(self):
        script = _build_assisted()
        self.assertNotIn("ghp_", script)
        self.assertNotIn("x-access-token:ghp_", script)
        # The GitHub token is set via SSM and passed to the container via
        # the GITHUB_TOKEN env var; the host bootstrap doesn't configure
        # git identity (the container does, in entrypoint.sh).
        self.assertIn("GITHUB_TOKEN=", script)
        self.assertIn("escobar", script)

    @patch.dict(
        os.environ,
        {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"},
    )
    def test_no_embedded_stt_api_key(self):
        # STT_API_KEY is a SecureString; the bootstrap must reference the
        # runtime-fetched shell variable rather than bake a literal value.
        # In the new architecture, the bootstrap writes STT_API_KEY to
        # /etc/blitzlog.env (which the host's watchdog reads and passes to
        # the container as an env var). The bootstrap also fetches the
        # value from SSM in the standard secrets block.
        script = _build_assisted()
        self.assertIn("STT_API_KEY=$(aws ssm get-parameter", script)
        # The /etc/blitzlog.env block must reference the runtime-fetched
        # shell variable, not a literal value.
        env_start = script.index("cat > /etc/blitzlog.env")
        env_section = script[env_start : env_start + 500]
        self.assertIn("STT_API_KEY=${STT_API_KEY}", env_section)
        # No literal STT_API_KEY=xxx should appear anywhere.
        self.assertNotRegex(script, r"STT_API_KEY=[^$\n][^\n]*")


class TestAssistedModelAndDiagnostics(unittest.TestCase):
    @patch.dict(
        os.environ,
        {
            "S3_LOGS_BUCKET": "test-bucket",
            "OPENCODE_MODEL": "minimax-coding-plan/MiniMax-M3",
        },
    )
    def test_user_data_uses_minimax_model(self):
        # In the new architecture, the bootstrap writes OPENCODE_MODEL
        # (slash-form "provider/model") to /etc/blitzlog.env — the host's
        # watchdog reads this and passes it to the container. The provider
        # is derived from the slash prefix inside the container.
        user_data = _build_assisted()
        env_start = user_data.index("cat > /etc/blitzlog.env")
        env_section = user_data[env_start : env_start + 500]
        self.assertIn("minimax-coding-plan", env_section)
        # The slash-form model id is what the container receives.
        self.assertIn('OPENCODE_MODEL=minimax-coding-plan/MiniMax-M3', env_section)

    @patch.dict(
        os.environ,
        {
            "S3_LOGS_BUCKET": "test-bucket",
            "OPENCODE_MODEL": "minimax-coding-plan/MiniMax-M3",
        },
    )
    def test_logs_config_diagnostic(self):
        user_data = _build_assisted()
        self.assertIn("Effective opencode config", user_data)
        self.assertIn("api_key_prefix", user_data)

    def test_default_opencode_model_is_minimax(self):
        with patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"}, clear=True):
            self.assertIn(
                "minimax-coding-plan/MiniMax-M3",
                build_assisted_user_data(
                    "owner/repo", 1, bot_name="b", bot_token="t", telegram_user_id="9"
                ),
            )




if __name__ == "__main__":
    unittest.main()
