"""Shared bootstrap-script helpers used by both autonomous and assisted
builders: SSM-secret reads, opencode config, git identity, toolchain
install, decode_api_errors_script, preflight probe, and opencode-config
rendering for both cloud and local providers."""

import unittest

from _common import (
    _configure_git_script,
    _decode_api_errors_script,
    _install_toolchain_script,
    _install_whisper_stt_script,
    _preflight_local_llm_script,
    _read_secrets_from_ssm_script,
    _write_opencode_config_script,
)
from _env import BLITZLOG_ENV


class TestSSMSecretsScript(unittest.TestCase):
    def test_fetches_github_token_from_ephemeral_param(self):
        script = _read_secrets_from_ssm_script(42)
        self.assertIn(f"/blitzlog/{BLITZLOG_ENV}/ephemeral/github-token-42", script)
        self.assertIn("export _CC_GITHUB_TOKEN", script)
        self.assertIn("export OPENCODE_API_KEY", script)
        self.assertIn(f"export BLITZLOG_ENV={BLITZLOG_ENV}", script)

    def test_different_issue_numbers(self):
        script13 = _read_secrets_from_ssm_script(13)
        script99 = _read_secrets_from_ssm_script(99)
        self.assertIn("github-token-13", script13)
        self.assertIn("github-token-99", script99)
        self.assertNotIn("github-token-99", script13)

    def test_fetches_stt_params(self):
        script = _read_secrets_from_ssm_script(42)
        self.assertIn(f"/blitzlog/{BLITZLOG_ENV}/stt/api-url", script)
        self.assertIn(f"/blitzlog/{BLITZLOG_ENV}/stt/api-key", script)
        self.assertIn(f"/blitzlog/{BLITZLOG_ENV}/stt/model", script)
        self.assertIn(f"/blitzlog/{BLITZLOG_ENV}/stt/language", script)
        self.assertIn(f"/blitzlog/{BLITZLOG_ENV}/stt/models-bucket", script)
        self.assertIn("export STT_API_URL", script)
        self.assertIn("export STT_API_KEY", script)
        self.assertIn("export STT_MODEL", script)
        self.assertIn("export STT_LANGUAGE", script)
        self.assertIn("export STT_MODELS_BUCKET", script)

    def test_stt_api_key_uses_with_decryption(self):
        script = _read_secrets_from_ssm_script(42)
        stt_key_idx = script.find(f"/blitzlog/{BLITZLOG_ENV}/stt/api-key")
        self.assertNotEqual(stt_key_idx, -1)
        self.assertIn("--with-decryption", script[stt_key_idx : stt_key_idx + 200])

    def test_paths_use_dev_env_when_blitzlog_env_set(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"BLITZLOG_ENV": "dev"}):
            script = _read_secrets_from_ssm_script(42)
        self.assertIn("/blitzlog/dev/ephemeral/github-token-42", script)
        self.assertIn("/blitzlog/dev/opencode/api-key", script)
        self.assertIn("export BLITZLOG_ENV=dev", script)


class TestSTTInBotConfig(unittest.TestCase):
    """The STT env vars must be present in the opencode-telegram-bot .env
    so the bot can transcribe voice messages.
    """

    @staticmethod
    def _build_assisted():
        import os
        from unittest.mock import patch

        from scripts.assisted import build_assisted_user_data

        with patch.dict(
            os.environ,
            {
                "S3_LOGS_BUCKET": "test-bucket",
                "OPENCODE_MODEL": "minimax-coding-plan/MiniMax-M3",
            },
        ):
            return build_assisted_user_data(
                "owner/repo",
                42,
                sender_login="octocat",
                bot_name="escobar",
                bot_token="123:ABC",
                telegram_user_id="99999",
            )

    def test_bot_env_has_stt_api_url(self):
        user_data = self._build_assisted()
        self.assertIn("STT_API_URL=${STT_API_URL}", user_data)

    def test_bot_env_has_stt_api_key(self):
        user_data = self._build_assisted()
        self.assertIn("STT_API_KEY=${STT_API_KEY}", user_data)

    def test_bot_env_has_stt_model_and_language(self):
        user_data = self._build_assisted()
        self.assertIn("STT_MODEL=${STT_MODEL}", user_data)
        self.assertIn("STT_LANGUAGE=${STT_LANGUAGE}", user_data)
        self.assertIn("STT_REQUEST_FORMAT=multipart", user_data)


class TestWhisperSttScript(unittest.TestCase):
    def test_whisper_install_script_downloads_from_github_release(self):
        script = _install_whisper_stt_script()
        self.assertIn("github.com/ggml-org/whisper.cpp/releases/download", script)
        self.assertIn("whisper-bin-aarch64-linux-gnu", script)

    def test_whisper_install_script_falls_back_to_source_build(self):
        script = _install_whisper_stt_script()
        self.assertIn("building whisper.cpp from source", script)
        self.assertIn("cmake -S", script)

    def test_whisper_install_script_downloads_model_from_s3(self):
        script = _install_whisper_stt_script()
        self.assertIn("aws s3 cp", script)
        self.assertIn("s3://${STT_MODELS_BUCKET}/models/", script)
        self.assertIn("ggml-${STT_MODEL}.bin", script)

    def test_whisper_install_script_writes_shim_source(self):
        script = _install_whisper_stt_script()
        self.assertIn("/opt/whisper-stt/server.py", script)
        self.assertNotIn("/opt/whisper-stt/server.js", script)

    def test_whisper_install_script_installs_pywhispercpp(self):
        script = _install_whisper_stt_script()
        self.assertIn("pywhispercpp", script)
        self.assertIn("pip install", script)

    def test_whisper_install_script_installs_systemd_unit(self):
        script = _install_whisper_stt_script()
        self.assertIn("/etc/systemd/system/whisper-stt-shim.service", script)
        self.assertIn("systemctl enable whisper-stt-shim.service", script)
        self.assertIn("systemctl restart whisper-stt-shim.service", script)

    def test_whisper_install_script_health_checks_before_bot(self):
        script = _install_whisper_stt_script()
        self.assertIn("http://127.0.0.1:7878/healthz", script)
        self.assertIn("curl -sf", script)

    def test_whisper_install_script_embeds_loaded_shim_source(self):
        script = _install_whisper_stt_script()
        # The embedded source must contain recognizable Python shim
        # identifiers so we catch accidental overwrites / empty reads.
        self.assertIn("pywhispercpp", script)
        self.assertIn("whisper-stt-shim listening", script)
        self.assertIn("HTTPServer", script)

    def test_whisper_install_script_does_not_install_npm_deps(self):
        # Regression: the Node.js shim is gone; npm install / busboy /
        # ffmpeg-static must not reappear.
        script = _install_whisper_stt_script()
        self.assertNotIn("npm install", script)
        self.assertNotIn("busboy", script)
        self.assertNotIn("ffmpeg-static", script)

    def test_whisper_shim_pip_install_fails_loud(self):
        """Regression for the silent-pip-fail bug: pip install must NOT
        be wrapped in `... | tail -3` (which masks exit codes under
        `set -eu` and silently swallows failures). Use an explicit
        `if ! ... ; then exit 1; fi` guard instead."""
        script = _install_whisper_stt_script()
        self.assertRegex(
            script, r"if\s+!\s+python3\s+-m\s+pip\s+install\s+pywhispercpp"
        )
        # No `| tail -3` masking on pip install.
        self.assertNotRegex(script, r"pip install[^|]*\|\s*tail")

    def test_whisper_shim_verifies_pywhispercpp_imports(self):
        """Catches "installed but broken" — pywhispercpp is on disk but
        unimportable (e.g., ABI mismatch, missing libpython)."""
        script = _install_whisper_stt_script()
        self.assertIn(
            'python3 -c "import pywhispercpp; from pywhispercpp.model import Model"',
            script,
        )

    def test_whisper_shim_binds_mise_python_globally(self):
        """`mise install -y` installs Python 3.12.x but does NOT bind the
        global shim — until `mise use -g python` runs, `python3 --version`
        in any clean shell reports "No version is set for shim: python3"
        (and the systemd ExecStart fails to start)."""
        script = _install_whisper_stt_script()
        self.assertRegex(script, r"mise\s+use\s+-g\s+python\b")

    def test_whisper_shim_systemd_uses_mise_shim_path(self):
        """The systemd ExecStart must use the actual mise shim path
        (/root/.local/share/mise/shims/python3 — that `whereis` confirms
        exists), not /root/.local/bin/python3 (which doesn't exist on
        AL2023; systemd starts with a clean PATH that doesn't include
        the mise shim dir)."""
        script = _install_whisper_stt_script()
        unit_block = script.split("<<'__WHISPER_SHIM_UNIT__'\n", 1)[1].split(
            "__WHISPER_SHIM_UNIT__", 1
        )[0]
        self.assertIn(
            "ExecStart=/root/.local/share/mise/shims/python3",
            unit_block,
        )
        self.assertNotIn("ExecStart=/usr/bin/python3 ", unit_block)
        self.assertNotIn("ExecStart=/root/.local/bin/python3", unit_block)

    def test_whisper_shim_script_is_executable(self):
        """Hygiene: the systemd ExecStart runs `python3 <script>` (data
        not exec), but chmod +x the script for consistency."""
        script = _install_whisper_stt_script()
        self.assertIn("chmod +x /opt/whisper-stt/server.py", script)


class TestOpencodeProviderConfig(unittest.TestCase):
    def test_heredoc_uses_minimax_provider(self):
        script = _write_opencode_config_script()
        self.assertIn('"minimax-coding-plan":', script)

    def test_heredoc_provider_block_has_no_legacy_providers(self):
        script = _write_opencode_config_script()
        provider_block = script.split('"provider":', 1)[1].split("}", 1)[0]
        self.assertNotIn("zai", provider_block)
        self.assertNotIn("glm", provider_block)

    def test_heredoc_injects_api_key_from_env(self):
        script = _write_opencode_config_script()
        self.assertIn("{env:OPENCODE_API_KEY}", script)


class TestDecodeApiErrorsScript(unittest.TestCase):
    def test_decodes_insufficient_balance_1008(self):
        script = _decode_api_errors_script()
        self.assertIn("1008", script)

    def test_decodes_unauthorized_401(self):
        script = _decode_api_errors_script()
        self.assertIn("Unauthorized", script)
        self.assertIn("401", script)
        self.assertIn("opencode/api-key", script)

    def test_decodes_rate_limit_429(self):
        script = _decode_api_errors_script()
        self.assertIn("429", script)
        self.assertIn("rate", script.lower())

    def test_watchdog_invokes_decoder(self):
        import os
        from unittest.mock import patch

        with patch.dict(
            os.environ,
            {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"},
        ):
            from scripts.autonomous import build_autonomous_user_data

            user_data = build_autonomous_user_data("owner/repo", 42)
        self.assertIn("ACTIONABLE", user_data)
        self.assertIn("insufficient_balance", user_data)
        self.assertIn("platform.minimax.io", user_data)


class TestConfigureGitScript(unittest.TestCase):
    def test_uses_env_var_not_literal(self):
        script = _configure_git_script()
        self.assertIn("${_CC_GITHUB_TOKEN}", script)
        self.assertNotIn("x-access-token:ghp_", script)

    def test_no_identity_when_no_sender(self):
        script = _configure_git_script()
        self.assertNotIn("user.name", script)
        self.assertNotIn("user.email", script)

    def test_sets_identity_with_sender_info(self):
        script = _configure_git_script(
            "octocat", "12345+octocat@users.noreply.github.com"
        )
        self.assertIn('git config --global user.name "octocat"', script)
        self.assertIn(
            'git config --global user.email "12345+octocat@users.noreply.github.com"',
            script,
        )

    def test_no_identity_with_empty_login(self):
        script = _configure_git_script("", "12345")
        self.assertNotIn("user.name", script)
        self.assertNotIn("user.email", script)


class TestToolchainBootstrapScript(unittest.TestCase):
    def test_session_archive_uses_global_directory(self):
        from plugins import _write_session_archive_plugin_script

        script = _write_session_archive_plugin_script()
        self.assertIn("/root/.config/opencode/plugins/session-archive.js", script)
        self.assertNotIn("/workspace/repo/.opencode/plugins", script)

    def test_script_installs_mise(self):
        script = _install_toolchain_script()
        self.assertIn("mise.run", script)
        self.assertIn("mise install", script)

    def test_script_checks_config_files(self):
        script = _install_toolchain_script()
        self.assertIn("mise.toml", script)
        self.assertIn(".tool-versions", script)

    def test_script_trusts_config(self):
        script = _install_toolchain_script()
        self.assertIn("mise trust", script)

    def test_script_sets_up_shims_path(self):
        script = _install_toolchain_script()
        self.assertIn("mise/shims", script)
        self.assertIn("/etc/profile.d/mise.sh", script)

    def test_script_handles_missing_config(self):
        script = _install_toolchain_script()
        self.assertIn("No mise.toml or .tool-versions found", script)

    def test_toolchain_runs_bootstrap_if_present(self):
        script = _install_toolchain_script()
        self.assertIn("bootstrap", script)
        self.assertIn("mise tasks --name-only", script)

    def test_no_secrets_in_toolchain_script(self):
        script = _install_toolchain_script()
        self.assertNotIn("ghp_", script)
        self.assertNotIn("sk-", script)


class TestPreflightScript(unittest.TestCase):
    def test_preflight_script_defines_function(self):
        script = _preflight_local_llm_script("autonomous")
        self.assertIn("preflight_local_llm()", script)

    def test_preflight_script_probes_health(self):
        script = _preflight_local_llm_script("autonomous")
        self.assertIn("/health", script)
        self.assertIn("/v1/models", script)

    def test_preflight_script_loop_count(self):
        script = _preflight_local_llm_script("autonomous")
        self.assertIn("seq 1 10", script)

    def test_autonomous_preflight_exits_no_telegram(self):
        script = _preflight_local_llm_script("autonomous")
        self.assertIn("exit 1", script)
        self.assertIn("Autonomous mode aborting", script)
        self.assertNotIn("sendMessage", script)
        self.assertNotIn("getUpdates", script)
        self.assertNotIn("inline_keyboard", script)

    def test_assisted_preflight_sends_telegram(self):
        script = _preflight_local_llm_script("assisted")
        self.assertIn("sendMessage", script)
        self.assertIn("getUpdates", script)
        self.assertIn("inline_keyboard", script)

    def test_assisted_preflight_has_retry_button(self):
        script = _preflight_local_llm_script("assisted")
        self.assertIn('"callback_data":"retry"', script)

    def test_assisted_preflight_has_abort_button(self):
        script = _preflight_local_llm_script("assisted")
        self.assertIn('"callback_data":"abort"', script)

    def test_assisted_preflight_has_cloud_button_when_fallback(self):
        script_with_cloud = _preflight_local_llm_script("assisted")
        self.assertIn("LOCAL_LLM_FALLBACK", script_with_cloud)
        self.assertIn("HAS_CLOUD_KEY", script_with_cloud)

    def test_assisted_preflight_references_switch_function(self):
        script = _preflight_local_llm_script("assisted")
        self.assertIn("switch_to_cloud_fallback", script)


class TestReadSecretsWithLocalLlm(unittest.TestCase):
    def test_cloud_path_exports_api_key(self):
        script = _read_secrets_from_ssm_script(42, local_llm=False)
        self.assertIn("export OPENCODE_API_KEY", script)

    def test_local_path_omits_api_key_export(self):
        script = _read_secrets_from_ssm_script(42, local_llm=True)
        self.assertNotIn("export OPENCODE_API_KEY", script)
        self.assertIn("export _CC_GITHUB_TOKEN", script)

    def test_default_local_llm_false(self):
        script = _read_secrets_from_ssm_script(42)
        self.assertIn("export OPENCODE_API_KEY", script)


class TestLocalLlmOpencodeConfig(unittest.TestCase):
    def test_default_renders_cloud_provider(self):
        script = _write_opencode_config_script()
        self.assertIn('"minimax-coding-plan":', script)
        self.assertNotIn('"local":', script)

    def test_local_provider_omits_cloud_block(self):
        script = _write_opencode_config_script(
            local_provider={
                "endpoint": "http://100.64.0.5:11434",
                "model": "qwen2.5-coder:32b",
                "api_key": "",
            }
        )
        self.assertIn('"local":', script)
        self.assertNotIn('"minimax-coding-plan":', script)

    def test_local_provider_with_api_key_renders_key(self):
        script = _write_opencode_config_script(
            local_provider={
                "endpoint": "http://100.64.0.5:11434",
                "model": "qwen2.5-coder:32b",
                "api_key": "secret",
            }
        )
        self.assertIn('"apiKey": "{env:LOCAL_LLM_API_KEY}"', script)

    def test_local_provider_without_api_key_omits_field(self):
        script = _write_opencode_config_script(
            local_provider={
                "endpoint": "http://100.64.0.5:11434",
                "model": "qwen2.5-coder:32b",
                "api_key": "",
            }
        )
        self.assertNotIn('"apiKey":', script)

    def test_local_provider_renders_baseurl(self):
        script = _write_opencode_config_script(
            local_provider={
                "endpoint": "http://100.64.0.5:11434",
                "model": "qwen2.5-coder:32b",
                "api_key": "",
            }
        )
        self.assertIn('"baseURL": "{env:LOCAL_LLM_ENDPOINT}"', script)


if __name__ == "__main__":
    unittest.main()
