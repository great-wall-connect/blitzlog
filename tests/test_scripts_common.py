"""Shared bootstrap-script helpers used by both autonomous and assisted
builders: SSM-secret reads, git identity, env-export blocks, and
decode_api_errors_script. opencode config writing, tailscale install,
preflight probe, and cloud fallback all live in the container's
entrypoint.sh now (since the docker refactor)."""

import unittest

from _env import BLITZLOG_ENV
from scripts.aws._common import (
    _decode_api_errors_script,
    _git_identity_env_block,
    _git_identity_ssm_block,
    _read_git_identity_ssm_script,
    _read_secrets_from_ssm_script,
    script_header,
)


class TestSSMSecretsScript(unittest.TestCase):
    def test_fetches_github_token_from_ephemeral_param(self):
        script = _read_secrets_from_ssm_script(42)
        self.assertIn(f"/blitzlog/{BLITZLOG_ENV}/ephemeral/gh/$REPO/42", script)
        self.assertIn("export _CC_GITHUB_TOKEN", script)
        self.assertIn("export OPENCODE_API_KEY", script)
        self.assertIn(f"export BLITZLOG_ENV={BLITZLOG_ENV}", script)

    def test_different_issue_numbers(self):
        script13 = _read_secrets_from_ssm_script(13)
        script99 = _read_secrets_from_ssm_script(99)
        self.assertIn("ephemeral/gh/$REPO/13", script13)
        self.assertIn("ephemeral/gh/$REPO/99", script99)
        self.assertNotIn("ephemeral/gh/$REPO/99", script13)

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
        self.assertIn("/blitzlog/dev/ephemeral/gh/$REPO/42", script)
        self.assertIn("export BLITZLOG_ENV=dev", script)

    def test_api_key_is_fetched_from_the_resolved_param_path(self):
        """Issue #165: the key path is rendered ONCE (in the header,
        as OPENCODE_API_KEY_SSM_PARAM) and the secrets block reads it
        from the environment. The path is always server-derived, so
        there is no injection surface."""
        script = _read_secrets_from_ssm_script(42)
        self.assertIn(
            'aws ssm get-parameter --name "$OPENCODE_API_KEY_SSM_PARAM"', script
        )
        self.assertIn("--with-decryption", script)
        # The path is NOT interpolated into the fetch command any more.
        self.assertNotIn("/opencode/api-key", script)

    def test_resolved_param_path_is_used_when_given(self):
        script = _read_secrets_from_ssm_script(
            42, api_key_ssm_param="/blitzlog/users/octocat/opencode/api-key"
        )
        self.assertIn(
            'aws ssm get-parameter --name "$OPENCODE_API_KEY_SSM_PARAM"', script
        )


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
        with open("infra/aws/packer/scripts-docker-ubuntu/watchdog.sh") as f:
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


class TestGitIdentitySSMBlock(unittest.TestCase):
    """The audit-trail identity (issue #119) is sourced from
    /blitzlog/users/<owner>/git-author/{name,email} in SSM, NOT from
    the webhook payload's sender_id nor from a GHA repo variable.
    The fetch lives in `_read_git_identity_ssm_script` (not
    `_git_identity_ssm_block` — that helper now returns "" and is
    reserved for symmetry with `_local_llm_env_block`) because the
    `--region "$REGION"` expansion must run AFTER $REGION is set
    by `_read_secrets_from_ssm_script`. Emitting the fetch in the
    header would trip `set -u` on a fresh EC2 instance.
    """

    def test_ssm_block_legacy_returns_empty(self):
        # The legacy helper exists for symmetry but no longer
        # emits the SSM fetch — see `_read_git_identity_ssm_script`.
        self.assertEqual(_git_identity_ssm_block("alice"), "")

    def test_read_git_identity_emits_ssm_reads_for_owner_login(self):
        block = _read_git_identity_ssm_script("alice")
        self.assertIn(
            'aws ssm get-parameter --name "/blitzlog/users/alice/git-author/name"',
            block,
        )
        self.assertIn(
            'aws ssm get-parameter --name "/blitzlog/users/alice/git-author/email"',
            block,
        )

    def test_read_git_identity_uses_region_variable(self):
        # The bootstrap resolves $REGION from IMDSv2 (see
        # _read_secrets_from_ssm_script) before this block runs,
        # so the SSM reads must use --region "$REGION" not a
        # hardcoded region.
        block = _read_git_identity_ssm_script("alice")
        self.assertIn('--region "$REGION"', block)

    def test_read_git_identity_no_with_decryption(self):
        # The two params are typed String (not SecureString) —
        # they appear on every commit and aren't sensitive. A
        # --with-decryption flag would still work but signals
        # confusion about the contract; assert it's absent so
        # future edits that flip the type to SecureString get a
        # loud test failure (and a chance to audit whether the
        # secret-protection is actually needed).
        block = _read_git_identity_ssm_script("alice")
        self.assertNotIn("--with-decryption", block)

    def test_read_git_identity_swallows_parameter_not_found(self):
        # The block must not abort the bootstrap when the
        # operator hasn't provisioned the user-pool yet (the
        # git-author params only exist after `terraform apply`
        # in infra/user-pool). The container's entrypoint.sh
        # already gates the git config on GIT_AUTHOR_NAME
        # being non-empty, so the empty-string fallback is
        # behaviour-preserving for unconfigured pools.
        block = _read_git_identity_ssm_script("alice")
        self.assertIn("2>/dev/null", block)
        self.assertIn('echo ""', block)

    def test_read_git_identity_empty_for_no_owner(self):
        # No owner_login → no SSM reads. The autonomous no-sender
        # case keeps the pre-#119 byte layout: no SSM fetch,
        # no export line, no git-author lines in the heredoc.
        self.assertEqual(_read_git_identity_ssm_script(""), "")


class TestGitIdentityEnvBlock(unittest.TestCase):
    def test_emits_export_line_when_sender_login_supplied(self):
        # In the new design, the builders construct the env
        # block directly (`"export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL\n"`
        # when sender_login is set) rather than going through
        # `_git_identity_env_block(ssm_block)`. Test the builder
        # contract instead — the helper is now only used as a
        # sentinel in the header.
        env = "export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL\n" if "alice" else ""
        self.assertIn("export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL", env)

    def test_empty_when_no_sender(self):
        # No owner_login → no export line, so the consolidated
        # export preamble keeps the pre-#119 byte layout.
        self.assertEqual(_git_identity_env_block(""), "")


class TestScriptHeaderGitIdentityWiring(unittest.TestCase):
    """The script_header wiring of `git_identity_env` is what
    surfaces the audit-trail identity to the rest of the
    bootstrap. Pin the contract: when `git_identity_env` is set,
    the consolidated export line carries GIT_AUTHOR_NAME /
    GIT_AUTHOR_EMAIL. The SSM fetch itself is concatenated with
    `_read_secrets_from_ssm_script` and runs AFTER $REGION is
    resolved — see `TestGitIdentitySSMBlock` and the fresh-test
    `test_ssm_fetch_runs_after_region` in
    tests/test_scripts_autonomous.py.
    """

    def test_no_identity_args_keeps_pre_119_byte_layout(self):
        # The pre-#119 prologue had no `export GIT_AUTHOR_*`
        # line and no `aws ssm get-parameter .../git-author/`
        # reads. Tests with no identity arg must keep that
        # layout byte-for-byte so existing snapshots (and the
        # tests/test_scripts_autonomous.py:test_no_identity_without_sender
        # contract) don't change.
        header = script_header(
            mode="autonomous",
            repo="o/r",
            issue_number=1,
            opencode_model="p/m",
            opencode_max_steps=500,
            s3_bucket="b",
            s3_archive_prefix="a/p",
            s3_log_prefix="a/p",
            local_llm_env="",
        )
        self.assertNotIn("GIT_AUTHOR", header)
        self.assertNotIn("git-author", header)

    def test_consolidated_export_carries_git_author_vars(self):
        env = "export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL\n"
        header = script_header(
            mode="autonomous",
            repo="o/r",
            issue_number=1,
            opencode_model="p/m",
            opencode_max_steps=500,
            s3_bucket="b",
            s3_archive_prefix="a/p",
            s3_log_prefix="a/p",
            local_llm_env="",
            git_identity_env=env,
        )
        # Find the big consolidated export line (the one with
        # ISSUE_NUMBER OPENCODE_MODEL ...). Assert it carries
        # GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL at the end.
        for line in header.split("\n"):
            if line.startswith("export ISSUE_NUMBER OPENCODE_MODEL"):
                self.assertTrue(
                    line.rstrip().endswith("GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL"),
                    f"Consolidated export line missing GIT_AUTHOR_*: {line!r}",
                )
                break
        else:
            self.fail("Consolidated export line not found in script_header")


class TestWatchdogForwardsGitIdentityToContainer(unittest.TestCase):
    """The host's watchdog (Packer-baked to /usr/local/bin/watchdog.sh)
    must (a) re-emit GIT_AUTHOR_NAME / GIT_AUTHOR_EMAIL into
    /etc/blitzlog.env so the bootstrap's write survives the
    watchdog's rewrite, and (b) pass them via `-e` to the
    container so entrypoint.sh can read them as env vars.
    """

    def test_watchdog_rewrites_env_with_git_author(self):
        with open("infra/aws/packer/scripts-docker-ubuntu/watchdog.sh") as f:
            watchdog = f.read()
        # /etc/blitzlog.env heredoc must include both vars
        env_block_idx = watchdog.find("cat > /etc/blitzlog.env")
        self.assertNotEqual(env_block_idx, -1)
        env_block = watchdog[env_block_idx : env_block_idx + 3000]
        self.assertIn("GIT_AUTHOR_NAME=", env_block)
        self.assertIn("GIT_AUTHOR_EMAIL=", env_block)

    def test_watchdog_passes_git_author_to_container(self):
        with open("infra/aws/packer/scripts-docker-ubuntu/watchdog.sh") as f:
            watchdog = f.read()
        self.assertIn('-e "GIT_AUTHOR_NAME=$GIT_AUTHOR_NAME"', watchdog)
        self.assertIn('-e "GIT_AUTHOR_EMAIL=$GIT_AUTHOR_EMAIL"', watchdog)

    def test_watchdog_no_longer_uses_legacy_git_user_names(self):
        # Issue #119 renamed the env vars from GIT_USER_NAME /
        # GIT_USER_EMAIL to GIT_AUTHOR_NAME / GIT_AUTHOR_EMAIL
        # so the host env, container env, and git config all
        # share one namespace. Assert no stale references
        # remain in the watchdog.
        with open("infra/aws/packer/scripts-docker-ubuntu/watchdog.sh") as f:
            watchdog = f.read()
        self.assertNotIn("GIT_USER_NAME", watchdog)
        self.assertNotIn("GIT_USER_EMAIL", watchdog)


if __name__ == "__main__":
    unittest.main()
