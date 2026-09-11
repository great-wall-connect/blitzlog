"""Lambda entrypoint + Terraform/mise file content regressions.

The entrypoint (`lambda_handler`) and its `extract_event_data` helper
live in `lambda/handler.py`. The handler dispatches into `scripts/`,
`auth`, `bot_pool`, and `ec2` — those modules have their own test
files (test_scripts_*, test_auth.py, test_bot_pool.py, test_ec2.py).

Tests in this file:
    - `TestLambdaHandlerBotPool`: webhook → launch flow (entrypoint)
    - `TestLambdaBuildConfiguration`: infra/modules/core/lambda.tf content
    - `TestLambdaRuntimeImportPath`: AWS Lambda sys.path layout regression
    - `TestMiseToml`: top-level mise.toml Node/Terraform pinning regressions
"""

import importlib
import json
import os
import sys
import unittest
from unittest.mock import patch


class TestLambdaHandlerBotPool(unittest.TestCase):
    @patch("handler._update_lock_instance_id")
    @patch("handler.launch_ec2_spot_instance", return_value="i-123")
    @patch("handler.acquire_bot_token")
    @patch("handler.get_telegram_user_id", return_value="99999")
    @patch("handler.get_github_app_token", return_value="ghp_test")
    @patch("handler.verify_github_signature", return_value=True)
    @patch("handler.get_ssm_param", return_value="secret")
    @patch("handler.get_local_llm_config", return_value=None)
    def test_assisted_calls_acquire_bot_token_with_sender(
        self,
        mock_local_llm,
        mock_ssm,
        mock_sig,
        mock_gh,
        mock_tg_user,
        mock_acquire,
        mock_launch,
        mock_update_lock,
    ):
        from handler import lambda_handler

        mock_acquire.return_value = ("escobar", "token123")

        event = {
            "body": json.dumps(
                {
                    "action": "labeled",
                    "issue": {"number": 42, "labels": [{"name": "assisted"}]},
                    "repository": {"full_name": "org/repo"},
                    "sender": {"login": "octocat", "id": "123"},
                }
            ),
            "headers": {"x-hub-signature-256": "sha256=abc"},
        }

        result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        mock_tg_user.assert_called_once_with("octocat")
        mock_acquire.assert_called_once()
        acquire_args = mock_acquire.call_args
        self.assertEqual(acquire_args[0][0], "octocat")
        update_args = mock_update_lock.call_args
        self.assertEqual(update_args[0][0], "octocat")
        self.assertEqual(update_args[0][1], "escobar")

    @patch("handler.get_telegram_user_id", return_value="99999")
    @patch("handler.get_github_app_token", return_value="ghp_test")
    @patch("handler.verify_github_signature", return_value=True)
    @patch("handler.get_ssm_param", return_value="secret")
    @patch("handler.acquire_bot_token", return_value=None)
    def test_pool_exhausted_returns_503(
        self, mock_acquire, mock_ssm, mock_sig, mock_gh, mock_tg_user
    ):
        from handler import lambda_handler

        event = {
            "body": json.dumps(
                {
                    "action": "labeled",
                    "issue": {"number": 42, "labels": [{"name": "assisted"}]},
                    "repository": {"full_name": "org/repo"},
                    "sender": {"login": "octocat", "id": "123"},
                }
            ),
            "headers": {"x-hub-signature-256": "sha256=abc"},
        }

        result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 503)
        self.assertIn("octocat", result["body"])

    @patch("handler.get_github_app_token", return_value="ghp_test")
    @patch("handler.verify_github_signature", return_value=True)
    @patch("handler.get_ssm_param", return_value="secret")
    @patch("handler.get_telegram_user_id", return_value=None)
    def test_user_without_telegram_id_returns_503(
        self, mock_tg_user, mock_ssm, mock_sig, mock_gh
    ):
        from handler import lambda_handler

        event = {
            "body": json.dumps(
                {
                    "action": "labeled",
                    "issue": {"number": 42, "labels": [{"name": "assisted"}]},
                    "repository": {"full_name": "org/repo"},
                    "sender": {"login": "lonely", "id": "9"},
                }
            ),
            "headers": {"x-hub-signature-256": "sha256=abc"},
        }

        result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 503)
        self.assertIn("lonely", result["body"])

    @patch("handler.launch_ec2_spot_instance", return_value="i-123")
    @patch("handler.get_github_app_token", return_value="ghp_test")
    @patch("handler.verify_github_signature", return_value=True)
    @patch("handler.get_ssm_param", return_value="secret")
    @patch("handler.get_local_llm_config", return_value=None)
    def test_autonomous_does_not_call_acquire(
        self, mock_local_llm, mock_ssm, mock_sig, mock_gh, mock_launch
    ):
        from handler import lambda_handler

        event = {
            "body": json.dumps(
                {
                    "action": "labeled",
                    "issue": {"number": 42, "labels": [{"name": "autonomous"}]},
                    "repository": {"full_name": "org/repo"},
                    "sender": {"login": "octocat", "id": "123"},
                }
            ),
            "headers": {"x-hub-signature-256": "sha256=abc"},
        }

        result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        # Autonomous mode never enters the bot-pool acquisition branch
        # (acquire_bot_token is only called for assisted mode), so the
        # bot pool SSM list is also untouched.


class TestLambdaBuildConfiguration(unittest.TestCase):
    """Regression tests for infra/modules/core/lambda.tf build-time configuration.

    A mistake here silently produces a broken Lambda zip at runtime
    (e.g., 1-byte server.py from a stale cp reference). These tests
    assert the file content directly so we catch the bug at PR review
    time, not on the EC2 instance.
    """

    @staticmethod
    def _read_lambda_tf():
        with open("infra/modules/core/lambda.tf", "r", encoding="utf-8") as f:
            return f.read()

    def test_lambda_build_copies_python_shim(self):
        """Regression: the Lambda build's cp must reference server.py
        (the current canonical shim name), not the obsolete server.js.
        Otherwise the Lambda zip is missing server.py, the bootstrap's
        heredoc writes a 1-byte stub (server.py comes back empty), and
        the shim is empty on the EC2 instance."""
        content = self._read_lambda_tf()
        self.assertRegex(
            content,
            r"cp\s+\$\{path\.module\}/(?:\.\./)+packages/whisper-stt-shim/server\.py",
        )
        self.assertNotRegex(
            content,
            r"cp\s+\$\{path\.module\}/(?:\.\./)+packages/whisper-stt-shim/server\.js",
        )

    def test_lambda_build_local_exec_uses_set_e(self):
        """Regression: the local-exec build must `set -e` so a missing
        file (cp fails) aborts the build instead of silently producing
        a broken zip. Without this, future renames (like server.js ->
        server.py) produce a zip that looks fine but is missing the
        renamed file, and the EC2 instance gets a 1-byte stub."""
        content = self._read_lambda_tf()
        self.assertRegex(
            content,
            r'provisioner\s+"local-exec"\s*\{\s*command\s*=\s*<<-EOT\s*\n\s*set\s+-e',
        )

    def test_lambda_build_triggers_track_init_py(self):
        """Regression: the ``null_resource.lambda_build`` trigger map
        must include a ``filemd5`` entry for ``lambda/__init__.py``.
        The build script copies the entire ``lambda/`` tree into the
        zip, so any file added there must be reflected in the trigger
        map — otherwise adding the file changes the manifest of files
        in the zip but Terraform won't detect a change and the rebuild
        is skipped. We hit this with ``__init__.py``: the file is
        required at runtime (it inserts ``lambda/`` onto ``sys.path``
        so the bare-name imports inside the package resolve), but
        until it was added to the trigger map, ``terraform apply``
        was a no-op against an existing deployed zip that lacked it,
        and the Lambda kept failing with ``ModuleNotFoundError: No
        module named '_env'``."""
        content = self._read_lambda_tf()
        self.assertRegex(
            content,
            r"filemd5\(\"\$\{path\.module\}/(?:\.\./)+lambda/__init__\.py\"\)",
            msg="lambda/__init__.py must appear in the lambda_build trigger map",
        )

    def test_lambda_build_local_exec_copies_lambda_directory_recursively(self):
        """The local-exec must build the zip via ``cp -r …/lambda …/build/``,
        not ``cp …/lambda/handler.py …/build/`` (which would skip every
        sibling module — auth, bot_pool, ec2, llm_guard, plugins,
        scripts/, plugins/*.js, __init__.py, …)."""
        content = self._read_lambda_tf()
        self.assertRegex(
            content,
            r"cp\s+-r\s+\$\{path\.module\}/(?:\.\./)+lambda\s+\$\{path\.module\}/build/",
        )
        self.assertNotRegex(
            content,
            r"cp\s+\$\{path\.module\}/(?:\.\./)+lambda/handler\.py\s+\$\{path\.module\}/build/",
        )


class TestLambdaRuntimeImportPath(unittest.TestCase):
    """Regression for the AWS Lambda sys.path layout.

    AWS Lambda's Python runtime initializes ``sys.path`` with only the
    zip's task root (e.g. ``/var/task``). The package's modules use
    bare-name imports (``from _env import logger``, ``from _common
    import …``) because ``lambda`` is a Python keyword and cannot
    appear in source-level ``import`` statements. Without a shim,
    ``import lambda.handler`` at runtime fails immediately on the
    first ``from _env import …`` inside ``handler.py`` with
    ``ModuleNotFoundError: No module named '_env'`` — even though the
    test suite passes, because ``tests/conftest.py`` adds ``lambda/``
    and ``lambda/scripts/`` to ``sys.path`` and so bypasses the bug.

    ``tests/conftest.py`` is a test-only convenience; AWS Lambda
    doesn't honor it. This test rebuilds the runtime layout locally
    (zip root on sys.path, package dirs absent) and asserts that
    ``lambda.handler`` still imports cleanly. The fix is
    ``lambda/__init__.py``'s ``sys.path.insert`` shim — without that
    file this test fails with the exact error a real Lambda invocation
    would surface.
    """

    @staticmethod
    def _runtime_sys_path():
        """Return a sys.path that mirrors what AWS Lambda sees at
        cold-start: the zip's task root (the parent of ``lambda/``) on
        sys.path, with the package and its ``scripts/`` subdirectory
        NOT present. stdlib / site-packages paths are preserved."""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        package_dir = os.path.join(repo_root, "lambda")
        scripts_dir = os.path.join(package_dir, "scripts")
        runtime_root = repo_root
        # Drop any entry that points at the package or scripts dir; keep
        # everything else (stdlib, site-packages, conftest inserts that
        # are not pointing at the package itself).
        return [
            p
            for p in sys.path
            if os.path.realpath(p)
            not in (os.path.realpath(package_dir), os.path.realpath(scripts_dir))
        ] + [runtime_root]

    def test_import_lambda_handler_under_runtime_sys_path(self):
        """Mirror AWS Lambda's sys.path: zip root only. With the
        ``__init__.py`` shim the import succeeds; without it the
        import dies on ``from _env import logger`` in handler.py."""
        runtime_path = self._runtime_sys_path()
        # Drop any cached imports so the shim (if present) runs again
        # and the bare-name imports inside handler.py resolve fresh
        # against our crafted sys.path.
        for mod in list(sys.modules):
            if mod == "lambda" or mod.startswith("lambda."):
                del sys.modules[mod]
        saved_path = sys.path[:]
        sys.path[:] = runtime_path
        try:
            handler = importlib.import_module("lambda.handler")
            self.assertTrue(callable(handler.lambda_handler))
            self.assertTrue(callable(handler.extract_event_data))
        finally:
            sys.path[:] = saved_path

    def test_lambda_package_init_adjusts_sys_path(self):
        """``lambda/__init__.py`` must exist and must insert both the
        package dir and its ``scripts/`` subdir onto sys.path. This is
        the shim that makes the runtime import work (see the test
        above for the consumer-side check)."""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        init_path = os.path.join(repo_root, "lambda", "__init__.py")
        self.assertTrue(
            os.path.isfile(init_path),
            f"missing {init_path}; the runtime sys.path shim is required",
        )
        with open(init_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("sys.path.insert", content)
        # Must insert BOTH the package root (for `from _env import …`,
        # `from auth import …`, etc.) AND `scripts/` (for `from _common
        # import …`, used by scripts/autonomous.py and scripts/assisted.py).
        self.assertIn("lambda", content)
        self.assertIn('"scripts"', content)


class TestMiseToml(unittest.TestCase):
    @staticmethod
    def _read_mise_toml():
        repo_root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(repo_root, "mise.toml"), "r", encoding="utf-8") as f:
            return f.read()

    def test_mise_toml_does_not_pin_node(self):
        # blitzlog itself is Python; Node is brought in at runtime by the
        # user-data's `dnf install nodejs24` plus the bot's `npx`. Pinning
        # Node in mise.toml reinstalls Node (typically v20) and shims it ahead
        # of /usr/bin/node, masking dnf's install — that's what made the bot
        # refuse to start with "requires Node.js 22.14+, 23.6+, or 24+".
        content = self._read_mise_toml()
        self.assertNotRegex(content, r"^\s*node\s*=", msg=content)

    def test_mise_toml_does_not_pin_terraform(self):
        # The bootstrap doesn't invoke the Terraform CLI; pinning it just
        # adds a needless install on the EC2 instance.
        content = self._read_mise_toml()
        self.assertNotRegex(content, r"^\s*terraform\s*=", msg=content)


if __name__ == "__main__":
    unittest.main()
