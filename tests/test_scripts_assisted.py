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
        # shell variable, not a literal value. The block now includes
        # Tailscale + local LLM vars between the cloud vars and STT_* vars,
        # so search the full heredoc instead of a fixed offset. Skip past
        # the opening delimiter `<<ENVEOF` (which contains the substring
        # "ENVEOF") and find the CLOSING delimiter on its own line.
        env_start = script.index("cat > /etc/blitzlog.env")
        env_end = script.index("\nENVEOF\n", env_start)
        env_section = script[env_start:env_end]
        self.assertIn('STT_API_KEY="${STT_API_KEY}"', env_section)
        # No literal STT_API_KEY=xxx should appear anywhere. Allow the
        # quoted shell-param form `STT_API_KEY="${STT_API_KEY}"` — the
        # quoting is from the heredoc-fix for /etc/blitzlog.env (see
        # TestAssistedEnvFileQuoting); it expands at source-time, not
        # at heredoc-write-time, so it doesn't embed a literal key.
        self.assertNotRegex(script, r'STT_API_KEY=(?!\"\$\{)[^$\n][^\n]*')


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
        self.assertIn('OPENCODE_MODEL="minimax-coding-plan/MiniMax-M3"', env_section)

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
        # Regression guard for #73: the host bootstrap used to grep
        # /root/.config/opencode/opencode.json to derive the provider,
        # but the file lives in the container, not the host — so the
        # grep always failed with "No such file or directory" and the
        # diagnostic reported `provider=` (empty). The provider now
        # comes from shell parameter expansion on $OPENCODE_MODEL.
        self.assertNotIn("/root/.config/opencode/opencode.json", user_data)
        self.assertNotIn("grep -oE", user_data)

    def test_default_opencode_model_is_minimax(self):
        with patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"}, clear=True):
            self.assertIn(
                "minimax-coding-plan/MiniMax-M3",
                build_assisted_user_data(
                    "owner/repo", 1, bot_name="b", bot_token="t", telegram_user_id="9"
                ),
            )


def _extract_blitzlog_env_heredoc(script: str) -> str:
    """Return the body of the `cat > /etc/blitzlog.env <<ENVEOF ... ENVEOF`
    heredoc in the rendered bootstrap. Used by the quoting regression
    tests below."""
    start = script.index("cat > /etc/blitzlog.env")
    end = script.index("\nENVEOF\n", start)
    return script[start:end]


class TestAssistedEnvFileQuoting(unittest.TestCase):
    """Regression: the heredoc that writes /etc/blitzlog.env is consumed
    by `bash source /etc/blitzlog.env` in the host's watchdog
    (`infra/packer/scripts-docker-ubuntu/watchdog.sh:26`), BEFORE the
    watchdog rewrites it with a properly-quoted copy. In assisted mode
    the prompt is empty (Telegram supplies it at runtime), but
    `OPENCODE_RESUMED_TITLE` carries the issue's title on a resumed
    session and a real title is essentially guaranteed to have spaces.
    Same quoting invariant as autonomous: every `${{VAR...}}` expansion
    must be wrapped in double quotes.
    """

    @staticmethod
    def _render_with_resume():
        """Render the bootstrap with OPENCODE_RESUMED_TITLE set to a
        multi-word title — the canonical trigger for the latent
        assisted-mode bug."""
        env = {
            "S3_LOGS_BUCKET": "test-bucket",
            "OPENCODE_MODEL": "test/model",
            "OPENCODE_RESUMED": "1",
            "OPENCODE_RESUMED_TITLE": "My task with spaces and - punctuation",
            "OPENCODE_API_KEY": "sk-secret",
            "BLITZLOG_ENV": "test",
            "STT_MODEL": "base.en",
            "STT_LANGUAGE": "en",
            "STT_API_URL": "http://stt.local",
            "STT_API_KEY": "stt-key",
            "STT_MODELS_BUCKET": "stt-models",
            "TELEGRAM_BOT_TOKEN": "tg-token",
            "TELEGRAM_USER_ID": "99999",
        }
        with patch.dict(os.environ, env):
            return _build_assisted()

    def test_blitzlog_env_lines_are_quoted(self):
        script = self._render_with_resume()
        heredoc = _extract_blitzlog_env_heredoc(script)
        for raw_line in heredoc.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            value = line.split("=", 1)[1]
            if "$" in value:
                self.assertTrue(
                    value.startswith('"') or value.startswith("'"),
                    f"unquoted shell expansion in /etc/blitzlog.env heredoc: {line!r}",
                )

    def test_blitzlog_env_passes_bash_n(self):
        import subprocess

        script = self._render_with_resume()
        heredoc = _extract_blitzlog_env_heredoc(script)
        body = heredoc.split("\n", 1)[1]
        result = subprocess.run(
            ["bash", "-n", "-c", body],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"bash -n failed: stderr={result.stderr!r} body={body!r}",
        )

    def test_blitzlog_env_sources_cleanly(self):
        import subprocess

        script = self._render_with_resume()
        heredoc = _extract_blitzlog_env_heredoc(script)
        body = heredoc.split("\n", 1)[1]
        result = subprocess.run(
            ["bash", "-c", f"set -e\n{body}"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"source failed: stderr={result.stderr!r} body={body!r}",
        )


if __name__ == "__main__":
    unittest.main()
