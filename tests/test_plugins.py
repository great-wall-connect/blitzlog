"""JS plugin loader + heredoc-emit helpers."""

import unittest

from plugins import (
    IDLE_WATCHDOG_PLUGIN_JS,
    PERIODIC_AUTOSAVE_PLUGIN_JS,
    SHUTDOWN_TOOL_JS,
    SPOT_WATCHDOG_PLUGIN_JS,
    _write_idle_watchdog_plugin_script,
    _write_periodic_autosave_plugin_script,
    _write_session_archive_plugin_script,
    _write_shutdown_tool_script,
    _write_spot_watchdog_plugin_script,
)


class TestShutdownTool(unittest.TestCase):
    def test_tool_uses_plain_object_export(self):
        self.assertIn("export default", SHUTDOWN_TOOL_JS)

    def test_tool_has_no_external_imports(self):
        self.assertNotIn("import", SHUTDOWN_TOOL_JS)

    def test_tool_calls_shutdown_script(self):
        self.assertIn("/usr/local/bin/assisted-shutdown.sh", SHUTDOWN_TOOL_JS)

    def test_tool_has_description(self):
        self.assertIn("Shut down this assisted agent instance", SHUTDOWN_TOOL_JS)

    def test_tool_has_args(self):
        self.assertIn("args: {}", SHUTDOWN_TOOL_JS)

    def test_tool_has_execute(self):
        self.assertIn("async execute()", SHUTDOWN_TOOL_JS)


class TestShutdownToolPassesReason(unittest.TestCase):
    def test_tool_passes_agent_requested_reason(self):
        self.assertIn("agent_requested", SHUTDOWN_TOOL_JS)

    def test_tool_passes_reason_via_env(self):
        self.assertIn("_SHUTDOWN_REASON", SHUTDOWN_TOOL_JS)


class TestIdleWatchdogPlugin(unittest.TestCase):
    def test_plugin_exports_named_function(self):
        self.assertIn("export const IdleWatchdog", IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_handles_session_idle(self):
        self.assertIn("session.idle", IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_handles_session_updated(self):
        self.assertIn("message.part.updated", IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_handles_session_deleted(self):
        self.assertIn("session.deleted", IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_sets_autosave_timer(self):
        self.assertIn("5 * 60 * 1000", IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_sets_ping_timer(self):
        self.assertIn("35 * 60 * 1000", IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_sets_shutdown_timer(self):
        self.assertIn("3 * 60 * 60 * 1000", IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_autosave_branch(self):
        self.assertIn("autosave/issue-", IDLE_WATCHDOG_PLUGIN_JS)
        self.assertIn("ISSUE_NUMBER", IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_force_pushes(self):
        self.assertIn("push --force", IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_passes_idle_timeout_reason(self):
        self.assertIn("idle_timeout", IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_uses_telegram_env_vars(self):
        self.assertIn("TELEGRAM_BOT_TOKEN", IDLE_WATCHDOG_PLUGIN_JS)
        self.assertIn("TELEGRAM_USER_ID", IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_clears_timers(self):
        self.assertIn("clearTimers", IDLE_WATCHDOG_PLUGIN_JS)
        self.assertIn("clearTimeout", IDLE_WATCHDOG_PLUGIN_JS)

    def test_no_secrets_in_plugin(self):
        self.assertNotIn("ghp_", IDLE_WATCHDOG_PLUGIN_JS)
        self.assertNotIn("sk-", IDLE_WATCHDOG_PLUGIN_JS)


class TestSpotWatchdogPlugin(unittest.TestCase):
    def test_plugin_exports_named_function(self):
        self.assertIn("export const SpotWatchdog", SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_handles_session_created(self):
        self.assertIn("session.created", SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_handles_session_deleted(self):
        self.assertIn("session.deleted", SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_polls_imds_spot_action(self):
        self.assertIn("spot/instance-action", SPOT_WATCHDOG_PLUGIN_JS)
        self.assertIn("169.254.169.254", SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_uses_set_interval(self):
        self.assertIn("setInterval", SPOT_WATCHDOG_PLUGIN_JS)
        self.assertIn("5000", SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_clears_interval_on_deleted(self):
        self.assertIn("clearInterval", SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_triggers_emergency_save(self):
        self.assertIn("emergencySave", SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_uses_interruption_branch_name(self):
        self.assertIn("autosave/issue-", SPOT_WATCHDOG_PLUGIN_JS)
        self.assertIn("interruption-", SPOT_WATCHDOG_PLUGIN_JS)
        self.assertIn("ISSUE_NUMBER", SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_force_pushes(self):
        self.assertIn("push --force", SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_archives_session_to_s3(self):
        self.assertIn("SESSION_ARCHIVE_BUCKET", SPOT_WATCHDOG_PLUGIN_JS)
        self.assertIn("aws s3 cp", SPOT_WATCHDOG_PLUGIN_JS)
        self.assertIn("opencode export", SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_prevents_double_trigger(self):
        self.assertIn("emergencySaveTriggered", SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_logs_via_client(self):
        self.assertIn("client.app.log", SPOT_WATCHDOG_PLUGIN_JS)
        self.assertIn("spot-watchdog", SPOT_WATCHDOG_PLUGIN_JS)

    def test_no_secrets_in_plugin(self):
        self.assertNotIn("ghp_", SPOT_WATCHDOG_PLUGIN_JS)
        self.assertNotIn("sk-", SPOT_WATCHDOG_PLUGIN_JS)


class TestPeriodicAutosavePlugin(unittest.TestCase):
    def test_plugin_exports_named_function(self):
        self.assertIn(
            "export const PeriodicAutosave", PERIODIC_AUTOSAVE_PLUGIN_JS
        )

    def test_plugin_handles_session_created(self):
        self.assertIn("session.created", PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_plugin_handles_session_deleted(self):
        self.assertIn("session.deleted", PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_plugin_uses_set_interval(self):
        self.assertIn("setInterval", PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_plugin_uses_5_minute_interval(self):
        self.assertIn("5 * 60 * 1000", PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_plugin_clears_interval_on_deleted(self):
        self.assertIn("clearInterval", PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_plugin_uses_stable_branch_name(self):
        self.assertIn("autosave/issue-", PERIODIC_AUTOSAVE_PLUGIN_JS)
        self.assertIn("-latest", PERIODIC_AUTOSAVE_PLUGIN_JS)
        self.assertIn("ISSUE_NUMBER", PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_plugin_force_pushes(self):
        self.assertIn("push --force", PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_plugin_uses_allow_empty_commit(self):
        self.assertIn("--allow-empty", PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_plugin_logs_via_client(self):
        self.assertIn("client.app.log", PERIODIC_AUTOSAVE_PLUGIN_JS)
        self.assertIn("periodic-autosave", PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_no_secrets_in_plugin(self):
        self.assertNotIn("ghp_", PERIODIC_AUTOSAVE_PLUGIN_JS)
        self.assertNotIn("sk-", PERIODIC_AUTOSAVE_PLUGIN_JS)


class TestPluginWriters(unittest.TestCase):
    """The writers emit heredoc'd installation of the JS sources. We
    assert on the resulting paths + marker names (matching the
    pre-refactor single-file handler) so the bootstrap continues to
    drop plugins into the same files.
    """

    def test_session_archive_uses_global_directory(self):
        script = _write_session_archive_plugin_script()
        self.assertIn("/root/.config/opencode/plugins/session-archive.js", script)
        self.assertNotIn("/workspace/repo/.opencode/plugins", script)

    def test_spot_watchdog_uses_global_directory(self):
        script = _write_spot_watchdog_plugin_script()
        self.assertIn("/root/.config/opencode/plugins/spot-watchdog.js", script)
        self.assertNotIn("/workspace/repo/.opencode/plugins", script)

    def test_periodic_autosave_uses_global_directory(self):
        script = _write_periodic_autosave_plugin_script()
        self.assertIn("/root/.config/opencode/plugins/periodic-autosave.js", script)
        self.assertNotIn("/workspace/repo/.opencode/plugins", script)

    def test_idle_watchdog_uses_idle_marker(self):
        """Idle watchdog uses the IDLE_WATCHDOG_PLUGIN_JS heredoc marker
        (the pre-refactor marker name), without the trailing `mkdir -p`
        the other writers include — the surrounding bootstrap step
        creates the directory.
        """
        script = _write_idle_watchdog_plugin_script()
        self.assertIn("/root/.config/opencode/plugins/idle-watchdog.js", script)
        self.assertIn("<<'IDLE_WATCHDOG_PLUGIN_JS'", script)
        self.assertNotIn("<<'PLUGIN_EOF'", script)
        self.assertNotIn("mkdir -p", script)

    def test_shutdown_tool_uses_shutdown_tool_marker(self):
        """Shutdown tool uses the SHUTDOWN_TOOL_JS heredoc marker (the
        pre-refactor marker name) and creates its own tools/ directory.
        """
        script = _write_shutdown_tool_script()
        self.assertIn("/root/.config/opencode/tools/shutdown.js", script)
        self.assertIn("<<'SHUTDOWN_TOOL_JS'", script)
        self.assertIn("mkdir -p /root/.config/opencode/tools", script)


if __name__ == "__main__":
    unittest.main()
