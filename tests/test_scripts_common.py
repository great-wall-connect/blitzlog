"""Shared bootstrap-script helpers used by both autonomous and assisted
builders: SSM-secret reads, git identity, env-export blocks, and
decode_api_errors_script. opencode config writing, tailscale install,
preflight probe, and cloud fallback all live in the container's
entrypoint.sh now (since the docker refactor)."""

import unittest

from _common import (
    _decode_api_errors_script,
    _read_secrets_from_ssm_script,
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
if __name__ == "__main__":
    unittest.main()
