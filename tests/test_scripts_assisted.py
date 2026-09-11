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
        self.assertIn("${_CC_GITHUB_TOKEN}", script)
        self.assertIn('user.name "octocat"', script)
        self.assertIn('user.email "12345+octocat@users.noreply.github.com"', script)
        self.assertIn("escobar", script)

    @patch.dict(
        os.environ,
        {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"},
    )
    def test_no_embedded_stt_api_key(self):
        # STT_API_KEY is a SecureString; the user-data must reference the
        # runtime-fetched shell variable rather than bake a literal value.
        script = _build_assisted()
        self.assertIn("STT_API_KEY=${STT_API_KEY}", script)
        self.assertIn("export STT_API_KEY", script)
        # The bot .env heredoc assigns from the runtime shell variable only;
        # no literal value should be embedded.
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
        user_data = _build_assisted()
        self.assertIn("minimax-coding-plan", user_data)
        self.assertIn("OPENCODE_MODEL_PROVIDER=minimax-coding-plan", user_data)

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


class TestAssistedToolchainInUserData(unittest.TestCase):
    def test_includes_toolchain(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn("mise install", user_data)
        self.assertIn("mise.toml", user_data)


class TestAssistedShutdownInUserData(unittest.TestCase):
    def test_contains_shutdown_tool(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn("shutdown.js", user_data)
        self.assertIn("SHUTDOWN_TOOL_JS", user_data)

    def test_contains_shutdown_script(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn("assisted-shutdown.sh", user_data)
        self.assertIn("shutdown -h now", user_data)

    def test_plugins_use_global_directory(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn("/root/.config/opencode/tools/shutdown.js", user_data)
        self.assertIn("/root/.config/opencode/plugins/idle-watchdog.js", user_data)
        self.assertNotIn("/workspace/repo/.opencode/plugins/", user_data)


class TestShutdownReasonDetection(unittest.TestCase):
    def test_shutdown_script_detects_reason(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn("SHUTDOWN_REASON", user_data)
        self.assertIn("spot_interruption", user_data)
        self.assertIn("system_shutdown", user_data)
        self.assertIn("unknown", user_data)

    def test_shutdown_script_checks_spot_instance_action(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn("spot/instance-action", user_data)

    def test_shutdown_script_checks_shutdown_reason_env(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn("_SHUTDOWN_REASON", user_data)

    def test_telegram_message_includes_reason(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn("reason:", user_data)
        self.assertIn("SHUTDOWN_REASON", user_data)


class TestIdleWatchdogInUserData(unittest.TestCase):
    def test_contains_idle_watchdog_plugin(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn("idle-watchdog.js", user_data)
        self.assertIn("IdleWatchdog", user_data)
        self.assertIn("IDLE_WATCHDOG_PLUGIN_JS", user_data)

    def test_telegram_vars_exported_before_opencode(self):
        user_data = _with_env(lambda: _build_assisted())
        export_pos = user_data.index("export TELEGRAM_BOT_TOKEN TELEGRAM_USER_ID")
        serve_pos = user_data.index("opencode serve")
        self.assertLess(export_pos, serve_pos)

    def test_blitzlog_env_includes_telegram_vars(self):
        user_data = _with_env(lambda: _build_assisted())
        env_start = user_data.index("cat > /etc/blitzlog.env")
        env_section = user_data[env_start : env_start + 500]
        self.assertIn("TELEGRAM_BOT_TOKEN", env_section)
        self.assertIn("TELEGRAM_USER_ID", env_section)


class TestBotPoolInUserData(unittest.TestCase):
    def test_contains_bot_name(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn("[Bot: escobar]", user_data)

    def test_injects_token_directly(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn('TELEGRAM_BOT_TOKEN="123:ABC"', user_data)

    def test_injects_user_id_directly(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn('TELEGRAM_USER_ID="99999"', user_data)

    def test_no_ssm_telegram_reads(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertNotIn("/blitzlog/telegram/bot-token", user_data)
        self.assertNotIn("/blitzlog/telegram/allowed-user-id", user_data)
        self.assertNotIn("/blitzlog/users/octocat/telegram/allowed-user-id", user_data)

    def test_shutdown_releases_sender_scoped_lock(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn("bot-pool-locks/octocat/escobar.json", user_data)
        self.assertIn("Released bot pool lock for octocat/escobar", user_data)

    def test_shutdown_notification_includes_bot_name(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn("[Bot: escobar]", user_data)


class TestAssistedPluginOrdering(unittest.TestCase):
    def test_contains_spot_watchdog_plugin(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn("spot-watchdog.js", user_data)
        self.assertIn("SpotWatchdog", user_data)

    def test_contains_periodic_autosave_plugin(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn("periodic-autosave.js", user_data)
        self.assertIn("PeriodicAutosave", user_data)

    def test_plugins_after_session_archive(self):
        user_data = _with_env(lambda: _build_assisted())
        archive_pos = user_data.index("session-archive.js")
        spot_pos = user_data.index("spot-watchdog.js")
        periodic_pos = user_data.index("periodic-autosave.js")
        self.assertGreater(spot_pos, archive_pos)
        self.assertGreater(periodic_pos, archive_pos)


class TestNodeVersionGuard(unittest.TestCase):
    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_installs_node_24_via_dnf(self):
        # dnf install of nodejs24 is the supported path on AL2023. The
        # mise.toml `[tools] node = ...` entry would otherwise reinstall
        # Node v20 via mise shims and mask this install — that's why
        # `test_mise_toml_does_not_pin_node` exists.
        user_data = _build_assisted()
        self.assertIn("dnf install -y nodejs24 nodejs24-npm", user_data)
        self.assertIn("alternatives --set node /usr/bin/node-24", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_invokes_bot_via_npx(self):
        user_data = _build_assisted()
        self.assertIn("npx -y @grinev/opencode-telegram-bot@latest start", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_no_bot_install_line(self):
        user_data = _build_assisted()
        self.assertNotIn("npm install -g @grinev/opencode-telegram-bot", user_data)
        self.assertNotIn("npm-22 install -g @grinev/opencode-telegram-bot", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_no_build_tools_install(self):
        user_data = _build_assisted()
        self.assertNotIn("dnf install -y gcc-c++ make python3", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_no_hardcoded_cli_path(self):
        user_data = _build_assisted()
        self.assertNotIn(
            "/usr/local/lib/node_modules/@grinev/opencode-telegram-bot/dist/cli.js",
            user_data,
        )

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_no_shebang_patch(self):
        user_data = _build_assisted()
        self.assertNotIn("sed -i '1c", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_pre_warms_npx_cache(self):
        user_data = _build_assisted()
        self.assertIn("npx -y @grinev/opencode-telegram-bot@latest status", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_pre_warm_before_notification(self):
        user_data = _build_assisted()
        pre_warm_pos = user_data.index("Pre-warming opencode-telegram-bot")
        notification_pos = user_data.index("Sending Telegram notification")
        self.assertLess(pre_warm_pos, notification_pos)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_pre_warm_runs_once(self):
        user_data = _build_assisted()
        self.assertEqual(
            user_data.count("npx -y @grinev/opencode-telegram-bot@latest status"),
            1,
        )

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_pre_warm_uses_status_subcommand(self):
        user_data = _build_assisted()
        self.assertIn("npx -y @grinev/opencode-telegram-bot@latest status", user_data)
        self.assertNotIn(
            "npx -y @grinev/opencode-telegram-bot@latest --help", user_data
        )

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_pre_warm_captures_exit_code(self):
        user_data = _build_assisted()
        self.assertIn("PRE_WARM_EXIT=$?", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_pre_warm_failure_sends_telegram(self):
        user_data = _build_assisted()
        guard_pos = user_data.index('"$PRE_WARM_EXIT" -ne 0')
        failure_block = user_data[guard_pos:]
        self.assertIn("Assisted agent cannot be started", failure_block)
        self.assertIn("sendMessage", failure_block)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_pre_warm_uses_real_chat_id(self):
        user_data = _build_assisted()
        guard_pos = user_data.index('"$PRE_WARM_EXIT" -ne 0')
        failure_block = user_data[guard_pos:]
        self.assertIn('chat_id="${TELEGRAM_USER_ID}"', failure_block)
        self.assertIn("bot${TELEGRAM_BOT_TOKEN}", failure_block)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_pre_warm_continues_on_failure(self):
        user_data = _build_assisted()
        failure_end = user_data.index("Sending Telegram notification")
        bot_install = user_data.index(
            "npx -y @grinev/opencode-telegram-bot@latest start", failure_end
        )
        self.assertGreater(bot_install, failure_end)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_pre_warm_log_written_to_var_log(self):
        user_data = _build_assisted()
        self.assertIn("> /var/log/pre-warm.log", user_data)
        self.assertNotIn("/tmp/pre-warm.log", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_pre_warm_failure_includes_repo_context(self):
        user_data = _build_assisted()
        guard_pos = user_data.index('"$PRE_WARM_EXIT" -ne 0')
        failure_block = user_data[guard_pos : guard_pos + 1500]
        self.assertIn("Repo: ${REPO}", failure_block)
        self.assertIn(
            "[Issue #${ISSUE_NUMBER}: ${ISSUE_TITLE}]",
            failure_block,
        )
        self.assertIn("Mode: Assisted (interactive via Telegram)", failure_block)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_pre_warm_failure_includes_resume_status(self):
        user_data = _build_assisted()
        guard_pos = user_data.index('"$PRE_WARM_EXIT" -ne 0')
        failure_block = user_data[guard_pos : guard_pos + 1500]
        self.assertIn("$RESUME_STATUS", failure_block)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_gh_issue_view_uses_explicit_repo(self):
        """`gh issue view` must include --repo so the title fetch doesn't depend
        on CWD-based repo detection (which fails when the CWD's git remote is
        broken, detached, or unreachable). Without this, the bootstrap prints
        "Issue #N: unknown" in the Telegram message instead of the real title.
        """
        user_data = _build_assisted()
        self.assertIn(
            'gh issue view "$ISSUE_NUMBER" --repo "${REPO}"',
            user_data,
            "gh issue view must use --repo to avoid CWD-detection edge cases",
        )

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_issue_title_fetched_before_pre_warm_message(self):
        """The gh issue view call must run before both Telegram notifications so
        the pre-warm failure path also gets the real issue title. Pre-fix, the
        $ISSUE_TITLE shell variable was unset when the pre-warm failure block
        ran, so the failure message had an empty title (bash expanded unset
        to the empty string).
        """
        user_data = _build_assisted()
        gh_pos = user_data.index("gh issue view")
        pre_warm_msg_pos = user_data.index("Assisted agent cannot be started")
        success_msg_pos = user_data.index("Assisted agent ready")
        self.assertLess(
            gh_pos,
            pre_warm_msg_pos,
            "gh issue view must run before the pre-warm failure notification, "
            "otherwise that path sends a Telegram message with an empty title.",
        )
        self.assertLess(
            gh_pos,
            success_msg_pos,
            "gh issue view must run before the success notification too.",
        )

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_pre_warm_failure_uses_markdown(self):
        user_data = _build_assisted()
        guard_pos = user_data.index('"$PRE_WARM_EXIT" -ne 0')
        failure_block = user_data[guard_pos : guard_pos + 1500]
        self.assertIn('parse_mode="Markdown"', failure_block)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_pre_warm_failure_omits_log_path_hint(self):
        user_data = _build_assisted()
        guard_pos = user_data.index('"$PRE_WARM_EXIT" -ne 0')
        failure_block = user_data[guard_pos : guard_pos + 1500]
        self.assertNotIn("/var/log", failure_block)


class TestAssistedLocalLlm(unittest.TestCase):
    LOCAL_LLM = {  # noqa: RUF012 - intentional class-level test fixture
        "endpoint": "http://100.64.0.5:11434",
        "model": "qwen2.5-coder:32b",
        "api_key": "secret",
        "allow_private": True,
        "fallback": "cloud",
    }

    def test_no_local_llm_keeps_cloud_block(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn("minimax-coding-plan", user_data)
        self.assertIn("OPENCODE_MODEL_PROVIDER=minimax-coding-plan", user_data)
        self.assertIn("export OPENCODE_API_KEY", user_data)

    def test_local_llm_switches_model(self):
        user_data = _with_env(lambda: _build_assisted(local_llm=self.LOCAL_LLM))
        self.assertIn('OPENCODE_MODEL="qwen2.5-coder:32b"', user_data)
        self.assertIn("OPENCODE_MODEL_PROVIDER=local", user_data)
        self.assertIn("OPENCODE_MODEL_ID=qwen2.5-coder:32b", user_data)
        self.assertIn('LOCAL_LLM_API_KEY="secret"', user_data)
        self.assertIn('LOCAL_LLM_FALLBACK="cloud"', user_data)
        # The agent's startup env must not export OPENCODE_API_KEY. The
        # switch_to_cloud_fallback function (only called if the user clicks
        # Use cloud) reads it from SSM just-in-time, so the substring can
        # legitimately appear inside that function body — but it must NOT
        # be exported at startup.
        read_block_start = user_data.index("Reading secrets from SSM")
        preflight_block_start = user_data.index("preflight_local_llm", read_block_start)
        startup_block = user_data[read_block_start:preflight_block_start]
        self.assertNotIn("export OPENCODE_API_KEY", startup_block)
        # The startup opencode.json must use the local provider only.
        cfg_start = user_data.index("Writing opencode config")
        cfg_end = user_data.index("session-archive.js", cfg_start)
        startup_cfg = user_data[cfg_start:cfg_end]
        self.assertIn('"local":', startup_cfg)
        self.assertNotIn('"minimax-coding-plan":', startup_cfg)

    def test_local_llm_includes_preflight(self):
        user_data = _with_env(lambda: _build_assisted(local_llm=self.LOCAL_LLM))
        self.assertIn("preflight_local_llm()", user_data)
        self.assertIn("MODE=assisted", user_data)

    def test_no_local_llm_no_preflight(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertNotIn("preflight_local_llm", user_data)

    def test_defensive_unset_always_present(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn("unset OPENCODE_API_KEY HTTPS_PROXY HTTP_PROXY", user_data)

    def test_opencode_serve_binds_localhost(self):
        user_data = _with_env(lambda: _build_assisted())
        self.assertIn("opencode serve --hostname 127.0.0.1 --port 4096", user_data)

    def test_local_llm_includes_tailscale_up_when_key_set(self):
        llm = dict(self.LOCAL_LLM, tailscale_auth_key="tskey-auth-foobar")
        user_data = _with_env(lambda: _build_assisted(local_llm=llm))
        self.assertIn("TAILSCALE_AUTH_KEY=", user_data)
        self.assertIn("tailscale up", user_data)
        self.assertIn("--accept-routes=false", user_data)
        self.assertNotIn("--ephemeral", user_data)
        self.assertNotIn(" -ephemeral", user_data)

    def test_local_llm_omits_tailscale_when_key_empty(self):
        user_data = _with_env(
            lambda: _build_assisted(
                local_llm={**self.LOCAL_LLM, "tailscale_auth_key": ""}
            )
        )
        self.assertNotIn("TAILSCALE_AUTH_KEY=", user_data)
        self.assertNotIn("tailscale up", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_preflight_call_line_bounds_opencode_api_key(self):
        """Regression for handler.py:1947. When local_llm is configured, the
        bootstrap does NOT export OPENCODE_API_KEY at startup (intentional —
        see _read_secrets_from_ssm_script local_llm=True path; the cloud
        key is read on demand from SSM only inside
        _switch_to_cloud_fallback_script, via IMDSv2 from the Lambda).
        The preflight invocation line must therefore use
        ${OPENCODE_API_KEY:-} (not bare $OPENCODE_API_KEY) to avoid
        crashing the script under `set -u`.
        """
        user_data = build_assisted_user_data(
            "owner/repo",
            42,
            bot_name="b",
            bot_token="t",
            telegram_user_id="999",
            local_llm={
                "endpoint": "http://100.64.0.5:11434",
                "model": "x",
                "api_key": "",
                "allow_private": True,
                "fallback": "closed",
            },
        )
        invocation_line = None
        for line in user_data.splitlines():
            if line.startswith("MODE=assisted HAS_CLOUD_KEY="):
                invocation_line = line
                break
        self.assertIsNotNone(
            invocation_line,
            "could not find MODE=assisted HAS_CLOUD_KEY= invocation line",
        )
        self.assertIn("${OPENCODE_API_KEY:-}", invocation_line)
        # Bare $OPENCODE_API_KEY would crash under set -u. Substitute the
        # safe form out, then check no bare reference remains.
        remainder = invocation_line.replace("${OPENCODE_API_KEY:-}", "")
        self.assertNotIn("$OPENCODE_API_KEY", remainder)

    def test_user_data_does_not_emit_nonexistent_tailscale_flags(self):
        """Ephemeral-ness is a property of the auth key (set when the key
        is generated at https://login.tailscale.com/admin/settings/keys),
        not a runtime flag on `tailscale up`. The flag doesn't exist and
        is rejected with 'flag provided but not defined: -ephemeral',
        which silently leaves the node unauthenticated and the preflight
        probe fails. Covers both autonomous and assisted bootstrap."""
        from scripts.autonomous import build_autonomous_user_data

        for builder, kwargs in [
            (build_autonomous_user_data, {}),
            (
                build_assisted_user_data,
                {"bot_name": "b", "bot_token": "t", "telegram_user_id": "999"},
            ),
        ]:
            with self.subTest(builder=builder.__name__):
                user_data = _with_env(
                    lambda b=builder, k=kwargs: b(
                        "owner/repo",
                        42,
                        **k,
                        local_llm={
                            "endpoint": "http://100.64.0.5:11434",
                            "model": "x",
                            "api_key": "",
                            "allow_private": True,
                            "fallback": "closed",
                            "tailscale_auth_key": "tskey-auth-foo",
                        },
                    )
                )
                self.assertNotIn("--ephemeral", user_data)
                self.assertNotIn(" -ephemeral", user_data)

    def test_local_llm_preserves_slash_in_model_id(self):
        """When the configured model id contains a slash (as returned by
        many OpenAI-compatible servers, e.g. LiteLLM model ids like
        'qwen/qwen3.8-27b'), the bootstrap must pass it through verbatim
        — never prefix it with 'local/', which would produce the malformed
        'local/qwen/qwen3.8-27b' and cause opencode to look up model='qwen'
        under provider='local', failing with 'Model not found'.

        See https://opencode.ai/docs/models — the config-file `model`
        field uses the format `provider_id/model_id` with a single slash
        as the separator, so an embedded slash in the model id has to be
        the model id portion. With local_llm configured the `local`
        provider is the only one present, so opencode infers it from
        being the sole provider."""
        from scripts.autonomous import build_autonomous_user_data

        for builder, kwargs in [
            (build_autonomous_user_data, {}),
            (
                build_assisted_user_data,
                {"bot_name": "b", "bot_token": "t", "telegram_user_id": "999"},
            ),
        ]:
            with self.subTest(builder=builder.__name__):
                user_data = _with_env(
                    lambda b=builder, k=kwargs: b(
                        "owner/repo",
                        42,
                        **k,
                        local_llm={
                            "endpoint": "http://100.64.0.5:11434",
                            "model": "qwen/qwen3.8-27b",  # note the slash
                            "api_key": "",
                            "allow_private": True,
                            "fallback": "closed",
                        },
                    )
                )
                self.assertIn('OPENCODE_MODEL="qwen/qwen3.8-27b"', user_data)
                self.assertNotIn('OPENCODE_MODEL="local/qwen', user_data)
                self.assertNotIn("local/qwen/qwen", user_data)
                # Regression for the ProviderModelNotFoundError: the rendered
                # opencode.json must register the model id under
                # provider.local.models so opencode can resolve the bot's
                # 'local/<model>' invocation when the id contains a slash.
                self.assertIn('"qwen/qwen3.8-27b": {}', user_data)


if __name__ == "__main__":
    unittest.main()
