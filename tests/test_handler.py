"""Lambda entrypoint + Terraform/mise file content regressions.

The entrypoint (`lambda_handler`) and its `extract_event_data` helper
live in `lambda/handler.py`. The handler dispatches into `scripts/`,
`auth`, `bot_pool`, and `ec2` — those modules have their own test
files (test_scripts_*, test_auth.py, test_bot_pool.py, test_ec2.py).

Tests in this file:
    - `TestLambdaHandlerBotPool`: webhook → launch flow (entrypoint)
    - `TestLambdaBuildConfiguration`: infra/aws/modules/core/lambda.tf content
    - `TestLambdaRuntimeImportPath`: AWS Lambda sys.path layout regression
    - `TestMiseToml`: top-level mise.toml Node/Terraform pinning regressions
    - `TestRefreshBranch` (issue #145): the EventBridge Scheduler
      direct-invocation `action == "refresh"` flow, the chain-end
      invariant at iteration 9, and the existing-mint-path guard.
    - `TestScheduleNextRefresh`: the EventBridge Scheduler
      `CreateSchedule` call shape (60-min out, role ARN, payload).
"""

import importlib
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError


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
    """Regression tests for infra/aws/modules/core/lambda.tf build-time configuration.

    A mistake here silently produces a broken Lambda zip at runtime
    (e.g., 1-byte server.py from a stale cp reference). These tests
    assert the file content directly so we catch the bug at PR review
    time, not on the EC2 instance.
    """

    @staticmethod
    def _read_lambda_tf():
        with open("infra/aws/modules/core/lambda.tf", "r", encoding="utf-8") as f:
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


class TestRefreshBranch(unittest.TestCase):
    """Issue #145: the `action == "refresh"` branch in `lambda_handler`
    handles a direct EventBridge Scheduler invocation. The branch
    mints a fresh GitHub App token via the existing
    `get_github_app_token()` helper (re-uses the battle-tested mint
    path; no risk of drift) and overwrites the SecureString at
    `/blitzlog/<env>/ephemeral/gh/<owner>/<repo>/<issue>`. The chain self-
    perpetuates up to `TOKEN_REFRESH_MAX_ITERATIONS` (10) hops.

    These tests mock the boto3 client singletons inside `handler.py`
    so a unit test exercises the dispatch logic without standing up
    a real EventBridge. The wire-format assertions catch drift in:
      * the `CreateSchedule` arguments (schedule name, group,
        ScheduleExpression, RoleArn, Input payload, FlexibleTimeWindow)
      * the `PutParameter` arguments (type="SecureString", overwrite)
      * the chain-end invariant (no `CreateSchedule` at iteration 9)
      * the existing-mint-path guard (must call get_github_app_token,
        not a fresh mint path)
    """

    def setUp(self):
        # Snapshot env so each test can mutate without leaking.
        self._env_snapshot = dict(os.environ)
        os.environ["AWS_LAMBDA_FUNCTION_NAME"] = "blitzlog-prod-handler"
        os.environ["SCHEDULER_ROLE_ARN"] = (
            "arn:aws:iam::000000000000:role/blitzlog-prod-scheduler-role"
        )
        os.environ["AWS_REGION"] = "ap-east-1"
        # Reset the module-level boto3 client cache so each test gets
        # a fresh mock. Without this, the cache leaks across tests and
        # the patches below are no-ops. Force the import first so
        # the cache reset works for tests that don't use @patch
        # decorators (the decorator triggers the import; raw
        # `with patch(...)` does not). We assign to a dummy to keep
        # the import in scope; the actual `handler` reference below
        # resolves it via `sys.modules` so the linter doesn't see an
        # "unused import" on the bare form.
        import handler as _h_unused  # noqa: F401

        handler = sys.modules["handler"]
        handler._scheduler_client = None
        handler._sts_client = None
        handler._ec2_client = None
        handler._account_id_cache = None

        # The instance-liveness guard calls EC2. Stub it out by
        # default so the refresh tests never reach AWS; the guard's
        # own behaviour is pinned by the dedicated tests below.
        liveness = patch.object(handler, "_instance_is_alive", return_value=True)
        liveness.start()
        self.addCleanup(liveness.stop)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_snapshot)

        handler = sys.modules["handler"]
        handler._scheduler_client = None
        handler._sts_client = None
        handler._ec2_client = None
        handler._account_id_cache = None

    def _make_event(self, **overrides):
        base = {
            "action": "refresh",
            "issue": 145,
            "instance": "i-0b076c604585b01be",
            "iteration": 0,
            "repo": "octocat/Hello-World",
        }
        base.update(overrides)
        return base

    @patch("handler.schedule_next_refresh")
    @patch("handler.get_github_app_token", return_value="ghp_NEW")
    @patch("handler.get_secrets")
    def test_refresh_branch_mints_and_writes_secure_string(
        self, mock_secrets, mock_mint, mock_schedule
    ):
        """The refresh branch must call `get_github_app_token(repo)` and
        write the new value to the per-issue SecureString with
        `type=SecureString` and `overwrite=True` — the same call the
        webhook uses. Pin the exact parameter name so a future
        refactor doesn't accidentally split the write path."""
        from handler import lambda_handler

        mock_secrets.return_value.put = MagicMock()

        event = self._make_event()
        result = lambda_handler(event, None)

        self.assertEqual(result["statusCode"], 200)
        # Mints via the existing helper (drift guard).
        mock_mint.assert_called_once_with("octocat/Hello-World")
        # Writes to the per-issue SecureString with the same shape the
        # webhook uses. The provider's `put()` signature is
        # `put(name, value, *, type, overwrite)` — name + value
        # positional, type/overwrite keyword-only.
        mock_secrets.return_value.put.assert_called_once()
        args, kwargs = mock_secrets.return_value.put.call_args
        self.assertEqual(
            len(args), 2, f"put() must take exactly 2 positional args: {args!r}"
        )
        name, value = args
        self.assertIn(
            "145", name, f"parameter name must carry the issue number: {name!r}"
        )
        self.assertEqual(value, "ghp_NEW")
        # `type` is the SSM parameter type; must be SecureString so the
        # token is KMS-encrypted at rest.
        self.assertEqual(kwargs.get("type"), "SecureString")
        # `overwrite=True` so the second refresh in the chain doesn't
        # fail with `ParameterAlreadyExists`.
        self.assertTrue(kwargs.get("overwrite"))

    @patch("handler.schedule_next_refresh")
    @patch("handler.get_github_app_token", return_value="ghp_NEW")
    @patch("handler.get_secrets")
    def test_refresh_branch_schedules_next_iteration_when_below_max(
        self, mock_secrets, mock_mint, mock_schedule
    ):
        """At iteration < 9, the branch must call `schedule_next_refresh`
        with `iteration + 1` and the same `repo` / `instance`."""
        from handler import lambda_handler

        mock_secrets.return_value.put = MagicMock()

        event = self._make_event(iteration=3)
        result = lambda_handler(event, None)

        self.assertEqual(result["statusCode"], 200)
        mock_schedule.assert_called_once_with(
            issue=145,
            instance="i-0b076c604585b01be",
            iteration=4,
            repo="octocat/Hello-World",
        )

    @patch("handler.schedule_next_refresh")
    @patch("handler.get_github_app_token", return_value="ghp_NEW")
    @patch("handler.get_secrets")
    def test_refresh_branch_ends_chain_at_iteration_9(
        self, mock_secrets, mock_mint, mock_schedule
    ):
        """At iteration 9 (the 10th and final hop), the branch must NOT
        call `schedule_next_refresh` — the chain has reached
        `TOKEN_REFRESH_MAX_ITERATIONS` and ends. The token write still
        happens (so the agent has a valid token for the next 1 h)."""
        from handler import (
            TOKEN_REFRESH_MAX_ITERATIONS,
            lambda_handler,
        )

        mock_secrets.return_value.put = MagicMock()

        event = self._make_event(iteration=TOKEN_REFRESH_MAX_ITERATIONS - 1)
        result = lambda_handler(event, None)

        self.assertEqual(result["statusCode"], 200)
        mock_mint.assert_called_once()
        mock_secrets.return_value.put.assert_called_once()  # token still written
        mock_schedule.assert_not_called()  # chain ends

    def test_refresh_schedule_group_name_is_per_issue(self):
        """The schedule group name must be per-(env, repo, issue) so the
        watchdog's per-instance cleanup (delete-schedule-group) only
        affects this instance's chain. Two different issues must
        produce two different group names even within the same env.
        """
        import os

        from handler import _refresh_schedule_group_name

        os.environ["BLITZLOG_ENV"] = "dev"
        a = _refresh_schedule_group_name("octocat/Hello-World", 145)
        b = _refresh_schedule_group_name("octocat/Hello-World", 146)
        c = _refresh_schedule_group_name("octocat/Other-Repo", 145)

        # Format: blitzlog-<env>-token-refresh-<16 hex>

        for name in (a, b, c):
            self.assertRegex(
                name,
                r"^blitzlog-dev-token-refresh-[0-9a-f]{16}$",
                f"group name must match the per-issue pattern: {name!r}",
            )
        # Different issues / repos / envs must produce different hashes.
        self.assertNotEqual(a, b, "different issue numbers must hash differently")
        self.assertNotEqual(a, c, "different repos must hash differently")
        self.assertNotEqual(b, c)

    def test_schedule_next_refresh_creates_group_on_first_call(self):
        """On the first CreateSchedule for a given (env, repo, issue),
        the per-issue group doesn't exist yet. schedule_next_refresh
        must catch the ResourceNotFoundException, call
        CreateScheduleGroup, then retry CreateSchedule.

        We assert the call sequence: CreateSchedule → (ResourceNotFound)
        → CreateScheduleGroup → CreateSchedule.
        """
        from handler import schedule_next_refresh

        client = MagicMock()
        # First CreateSchedule: ResourceNotFoundException (group
        # missing). Second CreateSchedule: success.
        not_found = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "no group"}},
            "CreateSchedule",
        )
        client.create_schedule.side_effect = [not_found, None]

        with (
            patch("handler._get_scheduler_client", return_value=client),
            patch("handler._get_account_id", return_value="000000000000"),
        ):
            schedule_next_refresh(
                issue=145,
                instance="i-test",
                iteration=0,
                repo="octocat/Hello-World",
            )

        # CreateSchedule called twice (initial + retry after group create)
        self.assertEqual(client.create_schedule.call_count, 2)
        # CreateScheduleGroup called once
        client.create_schedule_group.assert_called_once()
        # Both CreateSchedule calls used the same GroupName (the
        # per-issue hashed name)
        first_call_group = client.create_schedule.call_args_list[0].kwargs["GroupName"]
        second_call_group = client.create_schedule.call_args_list[1].kwargs["GroupName"]
        create_group_call_name = client.create_schedule_group.call_args.kwargs["Name"]
        self.assertEqual(first_call_group, second_call_group)
        self.assertEqual(first_call_group, create_group_call_name)

    def test_schedule_next_refresh_propagates_non_notfound_errors(self):
        """Only ResourceNotFoundException should trigger the
        group-create retry. Other ClientErrors (AccessDenied, etc.)
        must propagate so the chain's failure logging in
        _handle_refresh fires."""
        from handler import schedule_next_refresh

        client = MagicMock()
        client.create_schedule.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
            "CreateSchedule",
        )

        with (
            patch("handler._get_scheduler_client", return_value=client),
            patch("handler._get_account_id", return_value="000000000000"),
            self.assertRaises(ClientError) as cm,
        ):
            schedule_next_refresh(
                issue=145,
                instance="i-test",
                iteration=0,
                repo="octocat/Hello-World",
            )
        self.assertEqual(
            cm.exception.response["Error"]["Code"],
            "AccessDeniedException",
        )
        # Critical: the group-create retry must NOT have run.
        client.create_schedule_group.assert_not_called()

    def test_refresh_branch_rejects_missing_repo(self):
        """A refresh payload without a `repo` must 400 — the mint
        function needs the repo name and the Lambda has no fallback
        (no per-issue -> repo cache yet, by design)."""
        from handler import lambda_handler

        event = self._make_event(repo="")
        result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 400)
        self.assertIn("repo", result["body"])

    def test_refresh_branch_rejects_missing_issue(self):
        """A refresh payload without an integer `issue` must 400 —
        the SecureString parameter name is derived from the issue
        number."""
        from handler import lambda_handler

        # Build the event dict directly so the `issue` key is absent
        # (int(None) would coerce to 0 and pass the check).
        event = {
            "action": "refresh",
            "instance": "i-abc",
            "iteration": 0,
            "repo": "octocat/Hello-World",
        }
        result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 400)
        self.assertIn("issue", result["body"])

    def test_refresh_branch_rejects_missing_instance(self):
        """A refresh payload without `instance` must 400 — we log it
        for observability and a missing instance is a sign the chain
        is being launched by a non-EventBridge source."""
        from handler import lambda_handler

        # Build the event dict directly so the `instance` key is
        # absent (str(None) would coerce to "None" and pass the check).
        event = {
            "action": "refresh",
            "issue": 145,
            "iteration": 0,
            "repo": "octocat/Hello-World",
        }
        result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 400)
        self.assertIn("instance", result["body"])

    @patch("handler.schedule_next_refresh")
    def test_refresh_branch_does_not_require_signature(self, mock_schedule):
        """The refresh branch must NOT call `verify_github_signature` —
        a direct EventBridge Scheduler invocation does not carry an
        `X-Hub-Signature-256` header. The IAM grant on the scheduler
        role (`lambda:InvokeFunction` on this function only) is the
        perimeter; the `action == "refresh"` discriminator is the
        inner check. Pin this so a future refactor doesn't accidentally
        route refresh payloads through the signature path."""
        from handler import lambda_handler

        with (
            patch("handler.verify_github_signature") as mock_verify,
            patch("handler.get_github_app_token", return_value="t"),
            patch("handler.get_secrets") as mock_secrets,
        ):
            event = self._make_event()
            mock_secrets.return_value.put = MagicMock()
            lambda_handler(event, None)
        mock_verify.assert_not_called()

    def test_refresh_branch_stops_the_chain_when_the_instance_is_gone(self):
        """The chain must not outlive its run.

        The watchdog's teardown deletes the schedule group, so the
        chain normally dies with the agent. When that cleanup
        failed (or the instance was killed hard), a chain left
        running would keep re-minting tokens into SSM for hours
        after the agent is gone — a zombie token nobody uses.

        So: if the instance is no longer alive, write NO token and
        schedule NO next hop.
        """
        from handler import lambda_handler

        with (
            patch("handler._instance_is_alive", return_value=False),
            patch("handler.get_github_app_token") as mock_mint,
            patch("handler.get_secrets") as mock_secrets,
            patch("handler.schedule_next_refresh") as mock_schedule,
        ):
            mock_secrets.return_value.put = MagicMock()
            result = lambda_handler(self._make_event(), None)

        self.assertEqual(result["statusCode"], 200)
        self.assertIn("instance-gone", result["body"])
        mock_mint.assert_not_called()
        mock_secrets.return_value.put.assert_not_called()
        mock_schedule.assert_not_called()

    def test_refresh_branch_continues_when_the_instance_lookup_fails(self):
        """A failed instance lookup must NOT kill the chain.

        The liveness guard is an optimisation, not a safety
        interlock: if EC2 cannot answer (throttling, a transient
        endpoint error), the refresh must carry on. A dead chain
        costs the agent its GitHub access, so the guard fails open.
        """
        from handler import lambda_handler

        with (
            patch("handler._instance_is_alive", return_value=None),
            patch("handler.get_github_app_token", return_value="ghp_NEW"),
            patch("handler.get_secrets") as mock_secrets,
            patch("handler.schedule_next_refresh") as mock_schedule,
        ):
            mock_secrets.return_value.put = MagicMock()
            result = lambda_handler(self._make_event(), None)

        self.assertEqual(result["statusCode"], 200)
        mock_secrets.return_value.put.assert_called_once()
        mock_schedule.assert_called_once()

    def test_refresh_branch_does_not_affect_webhook_path(self):
        """A non-refresh event must NOT enter the refresh branch. Pin
        this so a payload with a `action` of anything other than
        `refresh` (e.g. an `issues.labeled` webhook with a top-level
        `action` field) is not accidentally misrouted."""
        from handler import lambda_handler

        with patch("handler._handle_refresh") as mock_refresh:
            # A webhook with a top-level `action=labeled` and a body
            # field. The current handler treats this as a malformed
            # event (no signature), but the point is the refresh
            # branch must not be entered.
            event = {
                "body": json.dumps({"action": "labeled", "issue": {"number": 1}}),
                "headers": {"x-hub-signature-256": "sha256=invalid"},
            }
            with patch("handler.get_ssm_param", return_value="secret"):
                result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 401)
        mock_refresh.assert_not_called()


class TestScheduleNextRefresh(unittest.TestCase):
    """Issue #145: `handler.schedule_next_refresh` is the only
    producer of refresh schedules. Pins the wire format of the
    `CreateSchedule` call so the on-the-wire shape is locked in
    even if the function body is refactored.
    """

    def setUp(self):
        self._env_snapshot = dict(os.environ)
        os.environ["AWS_LAMBDA_FUNCTION_NAME"] = "blitzlog-prod-handler"
        os.environ["SCHEDULER_ROLE_ARN"] = (
            "arn:aws:iam::000000000000:role/blitzlog-prod-scheduler-role"
        )
        os.environ["AWS_REGION"] = "ap-east-1"
        # Reset module-level boto3 client cache so each test gets a
        # fresh mock.

        handler = sys.modules["handler"]
        handler._scheduler_client = None
        handler._sts_client = None
        handler._account_id_cache = None

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_snapshot)

        handler = sys.modules["handler"]
        handler._scheduler_client = None
        handler._sts_client = None
        handler._account_id_cache = None

    @patch("handler.boto3.client")
    def test_create_schedule_uses_at_expression_50_minutes_out(self, mock_boto):
        """The schedule must fire ~50 min from now via
        `at(<ISO 8601>)`, with `FlexibleTimeWindow.mode = "OFF"` so
        the event fires exactly once (no Slack on either side)."""
        import datetime as _dt

        from handler import schedule_next_refresh

        # Stub STS for the account-id lookup.
        sts_client = MagicMock()
        sts_client.get_caller_identity.return_value = {"Account": "000000000000"}
        scheduler_client = MagicMock()
        mock_boto.side_effect = lambda service: (
            sts_client if service == "sts" else scheduler_client
        )

        before = _dt.datetime.now(_dt.UTC)
        schedule_next_refresh(
            issue=145,
            instance="i-abc",
            iteration=0,
            repo="octocat/Hello-World",
            delay_seconds=3000,
        )
        after = _dt.datetime.now(_dt.UTC)

        scheduler_client.create_schedule.assert_called_once()
        kwargs = scheduler_client.create_schedule.call_args.kwargs

        # Schedule name encodes (issue, iteration) so two events in
        # the same chain never collide.
        self.assertEqual(kwargs["Name"], "refresh-issue-145-iter-0")
        # Group name carries the env so prod and dev never collide.
        self.assertIn("blitzlog-", kwargs["GroupName"])
        # `at(...)` expression, 50 min in the future.
        self.assertTrue(
            kwargs["ScheduleExpression"].startswith("at("),
            f"ScheduleExpression must use `at(...)`: {kwargs['ScheduleExpression']!r}",
        )
        # Pull the timestamp out and assert it's ~50 min from now.
        ts_str = kwargs["ScheduleExpression"].removeprefix("at(").removesuffix(")")
        fire_at = _dt.datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=_dt.UTC
        )
        expected_min = before + _dt.timedelta(seconds=3000) - _dt.timedelta(seconds=2)
        expected_max = after + _dt.timedelta(seconds=3000) + _dt.timedelta(seconds=2)
        self.assertGreaterEqual(fire_at, expected_min)
        self.assertLessEqual(fire_at, expected_max)
        # Flexible time window OFF = fire exactly once, no slack.
        self.assertEqual(kwargs["FlexibleTimeWindow"], {"Mode": "OFF"})
        # Target ARN, role ARN, payload.
        target = kwargs["Target"]
        self.assertEqual(
            target["Arn"],
            "arn:aws:lambda:ap-east-1:000000000000:function:blitzlog-prod-handler",
        )
        self.assertEqual(target["RoleArn"], os.environ["SCHEDULER_ROLE_ARN"])
        payload = json.loads(target["Input"])
        self.assertEqual(payload["action"], "refresh")
        self.assertEqual(payload["issue"], 145)
        self.assertEqual(payload["instance"], "i-abc")
        self.assertEqual(payload["iteration"], 0)
        self.assertEqual(payload["repo"], "octocat/Hello-World")
        self.assertEqual(kwargs["State"], "ENABLED")

    def test_schedule_next_refresh_raises_without_function_name(self):
        """When `AWS_LAMBDA_FUNCTION_NAME` is unset, the function must
        raise `RuntimeError` rather than build a malformed ARN. This
        surfaces a misconfigured Lambda env in a clear way (vs. a
        500 from the scheduler SDK at CreateSchedule time)."""
        from handler import schedule_next_refresh

        os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
        with self.assertRaises(RuntimeError):
            schedule_next_refresh(issue=145, instance="i-abc", iteration=0, repo="r")

    def test_schedule_next_refresh_raises_without_scheduler_role_arn(self):
        """When `SCHEDULER_ROLE_ARN` is unset, the function must raise
        `RuntimeError` rather than submit a schedule EventBridge will
        reject. Same rationale as the function-name check."""
        from handler import schedule_next_refresh

        os.environ.pop("SCHEDULER_ROLE_ARN", None)
        with patch("handler.boto3.client"), self.assertRaises(RuntimeError):
            schedule_next_refresh(issue=145, instance="i-abc", iteration=0, repo="r")

    def test_first_hop_sweeps_the_leftover_schedule_group(self):
        """FALLBACK cleanup: the first hop of a chain must delete a
        leftover per-issue schedule group before creating its own
        schedule.

        Why this matters: EventBridge Scheduler does NOT delete a
        one-time `at()` schedule after it fires, so the previous
        run's `refresh-issue-<N>-iter-0` lingers in the group. The
        next run's first `CreateSchedule` then fails with
        `ConflictException`, the chain never starts, and the agent's
        token dies 1 h after the launch-time mint — the exact
        symptom seen on the dev instance.

        The watchdog's teardown is the PRIMARY cleanup path; this
        sweep only covers the case where that teardown never ran.
        """
        from handler import schedule_next_refresh

        client = MagicMock()
        with (
            patch("handler._get_scheduler_client", return_value=client),
            patch("handler._get_account_id", return_value="000000000000"),
        ):
            schedule_next_refresh(
                issue=145,
                instance="i-test",
                iteration=0,
                repo="octocat/Hello-World",
            )

        client.delete_schedule_group.assert_called_once()
        self.assertEqual(
            client.delete_schedule_group.call_args.kwargs["Name"],
            "blitzlog-prod-token-refresh-d54f61277109bf4c",
            "the sweep must target the per-(env, repo, issue) group.",
        )

    def test_later_hops_never_sweep_the_schedule_group(self):
        """Iterations 1..9 ARE the chain — they live in the group and
        must never delete it. Only the first hop sweeps."""
        from handler import schedule_next_refresh

        client = MagicMock()
        with (
            patch("handler._get_scheduler_client", return_value=client),
            patch("handler._get_account_id", return_value="000000000000"),
        ):
            schedule_next_refresh(
                issue=145,
                instance="i-test",
                iteration=3,
                repo="octocat/Hello-World",
            )

        client.delete_schedule_group.assert_not_called()

    def test_sweep_failure_does_not_stop_the_chain(self):
        """A failed sweep is best-effort: the chain must still start.

        The sweep exists to cover the case where the watchdog's
        teardown never ran; if the sweep itself fails (e.g. the
        Lambda role is missing `scheduler:DeleteScheduleGroup`) the
        chain must still come up rather than leave the agent with a
        1 h token and no refresh.
        """
        from handler import schedule_next_refresh

        client = MagicMock()
        client.delete_schedule_group.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
            "DeleteScheduleGroup",
        )
        with (
            patch("handler._get_scheduler_client", return_value=client),
            patch("handler._get_account_id", return_value="000000000000"),
        ):
            schedule_next_refresh(
                issue=145,
                instance="i-test",
                iteration=0,
                repo="octocat/Hello-World",
            )

        client.create_schedule.assert_called_once()

    def test_conflict_exception_deletes_the_leftover_and_retries(self):
        """A `ConflictException` means a schedule with this name
        already exists — a leftover from a previous run. The handler
        must delete it and recreate, so the chain can start.

        This is the belt-and-braces path for the case where the
        sweep could not remove the group (e.g. the group is shared
        with a schedule we must not touch).
        """
        from handler import schedule_next_refresh

        client = MagicMock()
        client.create_schedule.side_effect = [
            ClientError(
                {"Error": {"Code": "ConflictException", "Message": "exists"}},
                "CreateSchedule",
            ),
            None,
        ]
        with (
            patch("handler._get_scheduler_client", return_value=client),
            patch("handler._get_account_id", return_value="000000000000"),
        ):
            schedule_next_refresh(
                issue=145,
                instance="i-test",
                iteration=0,
                repo="octocat/Hello-World",
            )

        client.delete_schedule.assert_called_once()
        self.assertEqual(
            client.delete_schedule.call_args.kwargs["Name"],
            "refresh-issue-145-iter-0",
        )
        self.assertEqual(client.create_schedule.call_count, 2)

    def test_group_being_deleted_is_retried_not_raised(self):
        """EventBridge Scheduler is eventually consistent on group
        deletion: a `CreateSchedule` right after a `DeleteScheduleGroup`
        fails with `ValidationException: ... is being deleted`.

        The handler must wait it out and retry rather than lose the
        chain (this is the race the live unblock hit).
        """
        from handler import schedule_next_refresh

        client = MagicMock()
        client.create_schedule.side_effect = [
            ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": (
                            "You cannot perform this operation because "
                            "schedule group blitzlog-prod-token-refresh-x "
                            "is being deleted."
                        ),
                    }
                },
                "CreateSchedule",
            ),
            None,
        ]
        with (
            patch("handler._get_scheduler_client", return_value=client),
            patch("handler._get_account_id", return_value="000000000000"),
            patch("handler.time.sleep") as mock_sleep,
        ):
            schedule_next_refresh(
                issue=145,
                instance="i-test",
                iteration=0,
                repo="octocat/Hello-World",
            )

        mock_sleep.assert_called_once()
        self.assertEqual(client.create_schedule.call_count, 2)


class TestRefreshWireIntegration(unittest.TestCase):
    """Issue #145: the webhook path (labelled GitHub event) must
    schedule the FIRST refresh event in the chain. The chain itself
    is driven by `schedule_next_refresh`; this test pins that
    `lambda_handler` calls it after a successful launch.
    """

    @patch("handler.schedule_next_refresh")
    @patch("handler._update_lock_instance_id")
    @patch("handler.launch_ec2_spot_instance", return_value="i-abc")
    @patch("handler.get_github_app_token", return_value="ghp_test")
    @patch("handler.verify_github_signature", return_value=True)
    @patch("handler.get_ssm_param", return_value="secret")
    @patch("handler.get_local_llm_config", return_value=None)
    def test_webhook_schedules_first_refresh_after_launch(
        self,
        mock_local_llm,
        mock_ssm,
        mock_sig,
        mock_mint,
        mock_launch,
        mock_lock,
        mock_schedule,
    ):
        from handler import lambda_handler

        event = {
            "body": json.dumps(
                {
                    "action": "labeled",
                    "issue": {"number": 145, "labels": [{"name": "autonomous"}]},
                    "repository": {"full_name": "octocat/Hello-World"},
                    "sender": {"login": "octocat", "id": "1"},
                }
            ),
            "headers": {"x-hub-signature-256": "sha256=abc"},
        }
        os.environ["AWS_LAMBDA_FUNCTION_NAME"] = "blitzlog-prod-handler"
        os.environ["SCHEDULER_ROLE_ARN"] = (
            "arn:aws:iam::000000000000:role/blitzlog-prod-scheduler-role"
        )
        result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        mock_launch.assert_called_once()
        # The webhook schedules the first refresh (iteration 0) for
        # the just-launched instance, with the issue's repo.
        mock_schedule.assert_called_once_with(
            issue=145,
            instance="i-abc",
            iteration=0,
            repo="octocat/Hello-World",
        )

    @patch(
        "handler.schedule_next_refresh",
        side_effect=RuntimeError("no SCHEDULER_ROLE_ARN"),
    )
    @patch("handler.launch_ec2_spot_instance", return_value="i-abc")
    @patch("handler.get_github_app_token", return_value="ghp_test")
    @patch("handler.verify_github_signature", return_value=True)
    @patch("handler.get_ssm_param", return_value="secret")
    @patch("handler.get_local_llm_config", return_value=None)
    def test_webhook_still_succeeds_when_initial_schedule_fails(
        self, mock_local_llm, mock_ssm, mock_sig, mock_mint, mock_launch, mock_schedule
    ):
        """If `schedule_next_refresh` raises (e.g. SCHEDULER_ROLE_ARN
        env var missing on a fresh deploy), the webhook must still
        return 200. The static token in SSM is still good for ~1 h —
        a single missed refresh is far less bad than a webhook that
        500s and a missed EC2 launch."""
        from handler import lambda_handler

        event = {
            "body": json.dumps(
                {
                    "action": "labeled",
                    "issue": {"number": 145, "labels": [{"name": "autonomous"}]},
                    "repository": {"full_name": "octocat/Hello-World"},
                    "sender": {"login": "octocat", "id": "1"},
                }
            ),
            "headers": {"x-hub-signature-256": "sha256=abc"},
        }
        result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        mock_schedule.assert_called_once()


if __name__ == "__main__":
    unittest.main()
