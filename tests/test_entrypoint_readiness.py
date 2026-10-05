"""Regression guards for the agent container's opencode-serve readiness loop.

The container's entrypoint (packages/images/agent/entrypoint.sh) polls
http://127.0.0.1:4096/ with curl until opencode serve is ready, then
auto-selects the project and starts the Telegram bot. If a curl in that
loop hangs forever, the shell blocks in waitpid and the rest of the
entrypoint never runs — the agent sits idle and Telegram never gets a
"ready" notification. Reproduced on prod for issue #30.

These tests are static (string-level checks on entrypoint.sh). They mirror
tests/test_mise_bootstrap.py's pattern: read a config file, assert the
shapes that prevent the bug from regressing. No shell, no docker, no AWS.
"""

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "packages" / "images" / "agent" / "entrypoint.sh"


class TestEntrypointReadiness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = ENTRYPOINT.read_text()

    def test_wait_loop_curl_has_max_time(self):
        """The wait-loop curl MUST cap each request with --max-time.

        Without it, an opencode serve that is up (port 4096 listening)
        but slow on / traps curl forever, the shell blocks in waitpid,
        and the bot never starts. See #73.
        """
        i = self.text.index("opencode serve ready (after")
        wait_loop = self.text[: i + 200]  # the wait loop is just above
        self.assertIn(
            "--max-time 3",
            wait_loop,
            "wait-loop curl must set --max-time 3 (regression guard for #73)",
        )

    def test_wait_loop_curl_has_connect_timeout(self):
        """The wait-loop curl MUST also cap the TCP handshake."""
        i = self.text.index("opencode serve ready (after")
        wait_loop = self.text[: i + 200]
        self.assertIn(
            "--connect-timeout 2",
            wait_loop,
            "wait-loop curl must set --connect-timeout 2 (regression guard for #73)",
        )

    def test_opencode_json_written_log_present(self):
        """After writing opencode.jsonc/.json, the entrypoint must log a
        confirmation so the cloud-success path has the same operator
        diagnostic the local-LLM path has had since the refactor.

        The cloud-fallback path at line ~411 also has a `cp
        opencode.jsonc opencode.json` (when the user accepts the cloud
        fallback after local LLM fails), so we can't simply rindex for
        `cp` — we need to find the cp that's in the *initial* config
        writing block. The diagnostic log we want to guard is the
        unconditional one (after the if/elif/elif/fi chain), not the
        fallback restart. Find the log line directly and verify it
        follows some cp + fi in the initial block.
        """
        i = self.text.index('log "Wrote /root/.config/opencode/opencode.json')
        # Sanity: the log must come after at least one `cp` of the
        # .jsonc to .json rename in the initial config-writing block.
        # We require the cp to be at most 600 chars before the log —
        # the initial block is ~100 lines but the cp is at the very end.
        preceding = self.text[max(0, i - 600) : i]
        self.assertIn(
            "cp /root/.config/opencode/opencode.jsonc /root/.config/opencode/opencode.json",
            preceding,
            "the 'Wrote opencode.json' log must follow a cp of .jsonc to .json",
        )
        # And the log itself must include the diagnostic fields.
        line = self.text[i : i + 200]
        self.assertIn("provider=$opencode_provider", line)
        self.assertIn("api_key_prefix=", line)

    def test_opencode_config_branches_still_present(self):
        """Sanity: the four opencode.json writing branches (lambda
        pre-render, local LLM, cloud+key, model-only) must still exist.
        Guards against a refactor that drops the cloud or model-only
        fallback while adding the new log line.
        """
        self.assertIn("OPENCODE_CONFIG_JSON:-}", self.text)
        self.assertIn("local_llm_endpoint", self.text)
        self.assertIn('opencode_provider" ] && [ -n "$opencode_api_key" ]', self.text)
        self.assertIn('elif [ -n "$opencode_model" ]; then', self.text)


if __name__ == "__main__":
    unittest.main()
