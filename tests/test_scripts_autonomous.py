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
        # the GITHUB_TOKEN env var. The host bootstrap now also wires
        # GIT_USER_NAME / GIT_USER_EMAIL for issue #119's audit-trail
        # fix; the GITHUB_TOKEN export is an unrelated plumbing line.
        self.assertIn("GITHUB_TOKEN=", script)

    def test_no_identity_without_sender(self):
        script = build_autonomous_user_data("org/repo", 42)
        self.assertNotIn("user.name", script)
        self.assertNotIn("user.email", script)

    def test_identity_block_present_with_sender(self):
        """Issue #119: when the Lambda forwards the issue's
        `sender_login` / `sender_id`, the bootstrap must (a)
        `git config --global user.{name,email}` on the host so any
        host-side git operation (`_session_restore_script` in
        assisted mode; future pre-clone operations in autonomous)
        is attributed to the human who triggered the run, and (b)
        set `GIT_USER_NAME` / `GIT_USER_EMAIL` so the
        `/etc/blitzlog.env` heredoc forwards the identity into the
        container. Without these, the agent's commits land as
        `root@<hostname>` — unauditable.
        """
        script = build_autonomous_user_data("org/repo", 42, "octocat", "12345")
        # Host-side git config (echo'd by _git_identity_block).
        self.assertIn('git config --global user.name "octocat"', script)
        self.assertIn(
            'git config --global user.email "12345+octocat@users.noreply.github.com"',
            script,
        )
        # Env-var exports so the heredoc can forward to the container.
        self.assertIn('GIT_USER_NAME="octocat"', script)
        self.assertIn('GIT_USER_EMAIL="12345+octocat@users.noreply.github.com"', script)
        # Heredoc lines (the watchdog reads these and `-e`'s them into
        # the container). Sanity check the wrapper shell-expansion
        # shape, identical to OPENCODE_PROMPT.
        heredoc = _extract_blitzlog_env_heredoc(script)
        self.assertIn('GIT_USER_NAME="${GIT_USER_NAME:-}"', heredoc)
        self.assertIn('GIT_USER_EMAIL="${GIT_USER_EMAIL:-}"', heredoc)


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
        self.assertIn("model=$OPENCODE_MODEL", user_data)

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


def _extract_blitzlog_env_heredoc(script: str) -> str:
    """Return the body of the `cat > /etc/blitzlog.env <<ENVEOF ... ENVEOF`
    heredoc in the rendered bootstrap. Used by the quoting regression
    tests below."""
    start = script.index("cat > /etc/blitzlog.env")
    end = script.index("\nENVEOF\n", start)
    return script[start:end]


class TestAutonomousEnvFileQuoting(unittest.TestCase):
    """Regression: the heredoc that writes /etc/blitzlog.env is consumed
    by `bash source /etc/blitzlog.env` in the host's watchdog
    (`infra/packer/scripts-docker-ubuntu/watchdog.sh:26`), BEFORE the
    watchdog rewrites it with a properly-quoted copy. If a value with
    spaces is written unquoted, bash parses the second token as a
    command name and the bootstrap dies with `command not found`.

    `OPENCODE_PROMPT` is the canonical trigger (autonomous mode always
    sets it to a multi-word prompt), but any value could carry spaces
    in a future field, so the invariant is: every `${{VAR...}}` expansion
    must be wrapped in double quotes.
    """

    @staticmethod
    def _render_with_prompt():
        """Render the bootstrap with OPENCODE_PROMPT, OPENCODE_API_KEY,
        etc. set to realistic values so the heredoc body is fully expanded."""
        env = {
            "S3_LOGS_BUCKET": "test-bucket",
            "OPENCODE_MODEL": "test/model",
            "OPENCODE_PROMPT": "Work on GitHub issue #42. Follow AGENTS.md.",
            "OPENCODE_API_KEY": "sk-secret-with-no-spaces",
            "BLITZLOG_ENV": "test",
            "STT_MODEL": "base.en",
            "STT_LANGUAGE": "en",
            "STT_API_URL": "http://stt.local",
            "STT_API_KEY": "stt-key",
            "STT_MODELS_BUCKET": "stt-models",
        }
        with patch.dict(os.environ, env):
            return build_autonomous_user_data(
                "owner/repo", 42, sender_login="octocat", sender_id="12345"
            )

    def test_blitzlog_env_lines_are_quoted(self):
        """Every `${{VAR...}}` expansion inside the heredoc must be
        wrapped in double quotes. Without this, an expansion whose
        value contains spaces turns into a bash syntax error at
        source-time (the second token is parsed as a command name)."""
        script = self._render_with_prompt()
        heredoc = _extract_blitzlog_env_heredoc(script)
        for raw_line in heredoc.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            value = line.split("=", 1)[1]
            # Allow bare literals with no shell parameter — those
            # can't trigger the bug. But if the value contains a
            # `$` (i.e., still a shell expansion) it MUST be quoted.
            if "$" in value:
                self.assertTrue(
                    value.startswith(('"', "'")),
                    f"unquoted shell expansion in /etc/blitzlog.env heredoc: {line!r}",
                )

    def test_blitzlog_env_passes_bash_n(self):
        """The rendered heredoc, with realistic values substituted, must
        be syntactically valid bash (`bash -n` exits 0). Catches a
        missing quote / unbalanced quote at the parse level."""
        import subprocess

        script = self._render_with_prompt()
        heredoc = _extract_blitzlog_env_heredoc(script)
        # Strip the leading `cat > /etc/blitzlog.env` so `bash -n` only
        # sees the assignment body.
        body = heredoc.split("\n", 1)[1]
        result = subprocess.run(
            ["bash", "-n", "-c", body],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"bash -n failed: stderr={result.stderr!r} body={body!r}",
        )

    def test_blitzlog_env_sources_cleanly(self):
        """`source /etc/blitzlog.env` must succeed with realistic values.
        This is the original bug: an unquoted `OPENCODE_PROMPT=${OPENCODE_PROMPT:-}`
        expanded to `OPENCODE_PROMPT=Work on GitHub issue #5...` and bash
        aborted with `on: command not found`. Run with `set -e` so any
        uncaught command-not-found exits non-zero."""
        import subprocess

        script = self._render_with_prompt()
        heredoc = _extract_blitzlog_env_heredoc(script)
        body = heredoc.split("\n", 1)[1]
        result = subprocess.run(
            ["bash", "-c", f"set -e\n{body}"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"source failed: stderr={result.stderr!r} body={body!r}",
        )


if __name__ == "__main__":
    unittest.main()
