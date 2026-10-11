"""Shared bootstrap-script helpers used by both autonomous and assisted
builders: SSM-secret reads, git identity, env-export blocks, and
decode_api_errors_script. opencode config writing, tailscale install,
preflight probe, and cloud fallback all live in the container's
entrypoint.sh now (since the docker refactor)."""

import unittest

from _common import (
    _decode_api_errors_script,
    _git_identity_block,
    _git_identity_env_block,
    _read_secrets_from_ssm_script,
    script_header,
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
        # In the new architecture, the host's watchdog (Packer-baked to
        # /usr/local/bin/watchdog.sh) runs the API-error decoder after the
        # container exits — the container has no AWS creds and can't
        # interpret log lines itself.
        with open("infra/packer/scripts-docker-ubuntu/watchdog.sh") as f:
            watchdog = f.read()
        self.assertIn("ACTIONABLE", watchdog)
        self.assertIn("insufficient_balance", watchdog)
        self.assertIn("1008", watchdog)


class TestSessionArchivePluginInstallLocation(unittest.TestCase):
    """The session-archive plugin must be installed in the global
    opencode config dir (~/.config/opencode/plugins/) — not under
    /workspace/repo/.opencode/plugins — so it runs across projects.

    After the plugins/tools refactor, the file lives at
    packages/images/agent/opencode/plugins/session_archive.js and is
    baked into the image by the Dockerfile. This test asserts the source
    file exists at the expected repo path so the Dockerfile COPY
    succeeds."""

    def test_session_archive_uses_global_directory(self):
        # session_archive plugin lives at
        # packages/images/agent/opencode/plugins/session_archive.js and is
        # baked into the image at /root/.config/opencode/plugins/ by the
        # Dockerfile. Verify the file is in the expected location.
        import os

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "packages",
            "images",
            "agent",
            "opencode",
            "plugins",
            "session_archive.js",
        )
        self.assertTrue(
            os.path.exists(path),
            f"session_archive.js should live at {path} for the Dockerfile COPY",
        )


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


class TestGitIdentityBlock(unittest.TestCase):
    """The host-side git identity snippet (issue #119). `_git_identity_block`
    was previously dead code; this test class pins the helper's two
    branches so a future refactor can't silently leave agent commits
    unattributed.
    """

    def test_emits_user_name_and_email_when_sender_login_set(self):
        block = _git_identity_block("octocat", "12345")
        self.assertIn('git config --global user.name "octocat"', block)
        self.assertIn(
            'git config --global user.email "12345+octocat@users.noreply.github.com"',
            block,
        )

    def test_emits_empty_string_when_sender_login_empty(self):
        # The bootstrap silently skips the identity step when no issue
        # sender is present (autonomous re-launch paths, manual test
        # entries). Empty string keeps the rest of the script layout
        # unchanged.
        self.assertEqual(_git_identity_block("", "12345"), "")
        self.assertEqual(_git_identity_block("octocat", ""), "")
        self.assertEqual(_git_identity_block("", ""), "")


class TestGitIdentityEnvBlock(unittest.TestCase):
    """The container-side env-var block (issue #119). The watchdog
    forwards GIT_USER_NAME / GIT_USER_EMAIL via `-e` flags to the
    container, and the container's entrypoint.sh does `git config
    --global user.{name,email}` from them. Without these exports the
    container falls back to `root@<hostname>` for any commit."""

    def test_emits_user_name_and_email_when_sender_login_set(self):
        block = _git_identity_env_block("octocat", "12345")
        self.assertIn('GIT_USER_NAME="octocat"', block)
        self.assertIn('GIT_USER_EMAIL="12345+octocat@users.noreply.github.com"', block)
        # The export line covers both vars; the watchdog and the
        # per-mode heredoc rely on them being in the env when
        # /etc/blitzlog.env is written.
        self.assertIn("export GIT_USER_NAME GIT_USER_EMAIL", block)

    def test_emits_empty_string_when_sender_login_empty(self):
        self.assertEqual(_git_identity_env_block("", "12345"), "")
        self.assertEqual(_git_identity_env_block("octocat", ""), "")
        self.assertEqual(_git_identity_env_block("", ""), "")


class TestScriptHeaderGitIdentityWiring(unittest.TestCase):
    """`script_header` was the second half of the dead-code path in
    issue #119: it never called `_git_identity_block`, so even when
    the helper ran, the bootstrap script didn't carry the identity.
    These tests pin the wiring between the two helpers and the header
    preamble that `script_header` emits."""

    @staticmethod
    def _render(sender_login: str = "", sender_id: str = "") -> str:
        """Render the header without `local_llm_env` /
        `opencode_prompt` so the test focuses on the git-identity
        path. The other inputs are the minimum needed to produce a
        full preamble.
        """
        return script_header(
            mode="autonomous",
            repo="org/repo",
            issue_number=42,
            opencode_model="test/model",
            opencode_max_steps=500,
            s3_bucket="b",
            s3_archive_prefix="p",
            s3_log_prefix="l",
            local_llm_env="",
            local_llm_log_line="",
            opencode_prompt="do work",
            sender_login=sender_login,
            sender_id=sender_id,
        )

    def test_no_sender_emits_no_identity_lines(self):
        """When sender_login is empty, neither the host-side
        `git config` snippet nor the GIT_USER_NAME / GIT_USER_EMAIL
        env vars should appear in the header.
        """
        header = self._render()
        self.assertNotIn("user.name", header)
        self.assertNotIn("user.email", header)
        self.assertNotIn("GIT_USER_NAME", header)
        self.assertNotIn("GIT_USER_EMAIL", header)

    def test_with_sender_emits_host_git_config_and_env_exports(self):
        header = self._render("octocat", "12345")
        # Host-side git config
        self.assertIn('git config --global user.name "octocat"', header)
        self.assertIn(
            'git config --global user.email "12345+octocat@users.noreply.github.com"',
            header,
        )
        # Env exports (consumed by the per-mode heredoc that writes
        # /etc/blitzlog.env, which the watchdog then forwards to the
        # container via `-e`).
        self.assertIn('GIT_USER_NAME="octocat"', header)
        self.assertIn('GIT_USER_EMAIL="12345+octocat@users.noreply.github.com"', header)
        self.assertIn("export GIT_USER_NAME GIT_USER_EMAIL", header)
        # Diagnostic log line so the bootstrap has a visible
        # "git identity set for ..." entry in
        # /var/log/backend-bootstrap.log.
        self.assertIn('log "Git identity set for octocat"', header)

    def test_with_sender_includes_env_export_in_main_export_line(self):
        """The header's consolidated `export ISSUE_NUMBER ...
        GIT_USER_NAME GIT_USER_EMAIL` line must carry both vars so
        any downstream script that sources the bootstrap sees the
        identity in env. Regression guard for a refactor that drops
        GIT_USER_NAME / GIT_USER_EMAIL from the export list.
        """
        header = self._render("octocat", "12345")
        # Find the consolidated export line. It's the only `export`
        # that carries multiple vars in the preamble. We accept either
        # ordering of the two git-identity vars.
        export_lines = [
            ln for ln in header.splitlines() if ln.lstrip().startswith("export ")
        ]
        # There's at least one multi-var export line (the issue-level
        # one) AND a single-var one (`export GIT_USER_NAME GIT_USER_EMAIL`
        # from _git_identity_env_block, when a sender is set).
        multi = [
            ln
            for ln in export_lines
            if "GIT_USER_NAME" in ln and "GIT_USER_EMAIL" in ln
        ]
        self.assertTrue(
            multi,
            "expected at least one export line carrying both GIT_USER_NAME and GIT_USER_EMAIL",
        )


class TestWatchdogForwardsGitIdentityToContainer(unittest.TestCase):
    """Regression guard for issue #119: the host's watchdog.sh must
    forward GIT_USER_NAME and GIT_USER_EMAIL through `/etc/blitzlog.env`
    and via `-e` to the docker run, otherwise the container never
    receives the audit-friendly identity.
    """

    @classmethod
    def setUpClass(cls):
        with open("infra/packer/scripts-docker-ubuntu/watchdog.sh") as f:
            cls.watchdog = f.read()

    def test_watchdog_env_block_contains_git_user_name(self):
        # The watchdog rewrites /etc/blitzlog.env from the host's
        # bootstrap values; the rewritten file must carry
        # GIT_USER_NAME / GIT_USER_EMAIL through (or subsequent
        # scripts that source it lose the identity).
        self.assertIn('GIT_USER_NAME="${GIT_USER_NAME:-}"', self.watchdog)
        self.assertIn('GIT_USER_EMAIL="${GIT_USER_EMAIL:-}"', self.watchdog)

    def test_watchdog_passes_git_user_name_to_docker_run(self):
        # The container picks up GIT_USER_NAME / GIT_USER_EMAIL from
        # `-e` flags (NOT from /etc/blitzlog.env), so the COMMON_ARGS
        # list must include them.
        self.assertIn('-e "GIT_USER_NAME=$GIT_USER_NAME"', self.watchdog)
        self.assertIn('-e "GIT_USER_EMAIL=$GIT_USER_EMAIL"', self.watchdog)


if __name__ == "__main__":
    unittest.main()
