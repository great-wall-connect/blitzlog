"""Autonomous-mode user-data bootstrap builder."""

import os
import unittest
from unittest.mock import patch

from plugins import (
    _write_periodic_autosave_plugin_script,
    _write_spot_watchdog_plugin_script,
)
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
        self.assertIn("${_CC_GITHUB_TOKEN}", script)
        self.assertIn('user.name "octocat"', script)
        self.assertIn('user.email "12345+octocat@users.noreply.github.com"', script)

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
        self.assertIn("Effective opencode config", user_data)
        self.assertIn("api_key_prefix", user_data)

    def test_default_opencode_model_is_minimax(self):
        with patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"}, clear=True):
            self.assertIn(
                "minimax-coding-plan/MiniMax-M3",
                build_autonomous_user_data("owner/repo", 1),
            )


class TestAutonomousToolchainInUserData(unittest.TestCase):
    def test_includes_toolchain(self):
        user_data = _with_env(lambda: build_autonomous_user_data("owner/repo", 42))
        self.assertIn("mise install", user_data)
        self.assertIn("mise.toml", user_data)

    def test_toolchain_runs_after_clone(self):
        user_data = _with_env(lambda: build_autonomous_user_data("owner/repo", 42))
        clone_pos = user_data.index("git clone")
        mise_pos = user_data.index("mise install")
        config_pos = user_data.index("Writing opencode config")
        self.assertGreater(mise_pos, clone_pos)
        self.assertLess(mise_pos, config_pos)


class TestAutonomousShutdownExclusion(unittest.TestCase):
    def test_user_data_excludes_shutdown_tool(self):
        user_data = _with_env(lambda: build_autonomous_user_data("owner/repo", 42))
        self.assertNotIn("shutdown.js", user_data)
        self.assertNotIn("SHUTDOWN_TOOL_JS", user_data)


class TestAutonomousPluginOrdering(unittest.TestCase):
    def test_user_data_contains_spot_watchdog_plugin(self):
        user_data = _with_env(lambda: build_autonomous_user_data("owner/repo", 42))
        self.assertIn("spot-watchdog.js", user_data)
        self.assertIn("SpotWatchdog", user_data)

    def test_user_data_contains_periodic_autosave_plugin(self):
        user_data = _with_env(lambda: build_autonomous_user_data("owner/repo", 42))
        self.assertIn("periodic-autosave.js", user_data)
        self.assertIn("PeriodicAutosave", user_data)

    def test_spot_watchdog_uses_global_directory(self):
        script = _write_spot_watchdog_plugin_script()
        self.assertIn("/root/.config/opencode/plugins/spot-watchdog.js", script)
        self.assertNotIn("/workspace/repo/.opencode/plugins", script)

    def test_periodic_autosave_uses_global_directory(self):
        script = _write_periodic_autosave_plugin_script()
        self.assertIn("/root/.config/opencode/plugins/periodic-autosave.js", script)
        self.assertNotIn("/workspace/repo/.opencode/plugins", script)

    def test_plugins_after_session_archive(self):
        user_data = _with_env(lambda: build_autonomous_user_data("owner/repo", 42))
        archive_pos = user_data.index("session-archive.js")
        spot_pos = user_data.index("spot-watchdog.js")
        periodic_pos = user_data.index("periodic-autosave.js")
        self.assertGreater(spot_pos, archive_pos)
        self.assertGreater(periodic_pos, archive_pos)


class TestAutonomousLocalLlm(unittest.TestCase):
    LOCAL_LLM = {  # noqa: RUF012 - intentional class-level test fixture
        "endpoint": "http://100.64.0.5:11434",
        "model": "qwen2.5-coder:32b",
        "api_key": "",
        "allow_private": True,
        "fallback": "closed",
    }

    def test_no_local_llm_keeps_cloud_block(self):
        user_data = _with_env(lambda: build_autonomous_user_data("owner/repo", 42))
        self.assertIn("minimax-coding-plan", user_data)
        self.assertIn('OPENCODE_MODEL="test/model"', user_data)
        self.assertIn("export OPENCODE_API_KEY", user_data)

    def test_local_llm_switches_model(self):
        user_data = _with_env(
            lambda: build_autonomous_user_data(
                "owner/repo", 42, local_llm=self.LOCAL_LLM
            )
        )
        self.assertIn('OPENCODE_MODEL="qwen2.5-coder:32b"', user_data)
        self.assertIn("LOCAL_LLM_ENDPOINT=", user_data)
        self.assertIn("LOCAL_LLM_MODEL=", user_data)
        self.assertNotIn('"minimax-coding-plan":', user_data)
        self.assertIn('"local":', user_data)

    def test_local_llm_includes_preflight(self):
        user_data = _with_env(
            lambda: build_autonomous_user_data(
                "owner/repo", 42, local_llm=self.LOCAL_LLM
            )
        )
        self.assertIn("preflight_local_llm()", user_data)
        self.assertIn("MODE=autonomous", user_data)

    def test_no_local_llm_no_preflight(self):
        user_data = _with_env(lambda: build_autonomous_user_data("owner/repo", 42))
        self.assertNotIn("preflight_local_llm", user_data)

    def test_defensive_unset_always_present(self):
        user_data = _with_env(lambda: build_autonomous_user_data("owner/repo", 42))
        self.assertIn("unset OPENCODE_API_KEY HTTPS_PROXY HTTP_PROXY", user_data)

    def test_local_llm_includes_tailscale_up_when_key_set(self):
        llm = dict(self.LOCAL_LLM, tailscale_auth_key="tskey-auth-foobar")
        user_data = _with_env(
            lambda: build_autonomous_user_data("owner/repo", 42, local_llm=llm)
        )
        self.assertIn("TAILSCALE_AUTH_KEY=", user_data)
        self.assertIn("tailscale up", user_data)
        self.assertIn("--accept-routes=false", user_data)
        self.assertIn("blitzlog-agent-${ISSUE_NUMBER}", user_data)
        # Regression: --ephemeral is NOT a tailscale up flag. Ephemeral-ness
        # is a property of the auth key itself (set when the key is
        # generated at https://login.tailscale.com/admin/settings/keys).
        # Passing --ephemeral makes tailscale up exit with
        # "flag provided but not defined: -ephemeral", leaving the node
        # unauthenticated and the preflight probe timing out.
        self.assertNotIn("--ephemeral", user_data)
        self.assertNotIn(" -ephemeral", user_data)

    def test_local_llm_omits_tailscale_when_key_empty(self):
        user_data = _with_env(
            lambda: build_autonomous_user_data(
                "owner/repo", 42, local_llm=self.LOCAL_LLM
            )
        )
        self.assertNotIn("TAILSCALE_AUTH_KEY=", user_data)
        self.assertNotIn("tailscale up", user_data)


if __name__ == "__main__":
    unittest.main()
