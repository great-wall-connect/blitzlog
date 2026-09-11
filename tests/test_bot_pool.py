"""Per-user bot pool, telegram user lookup, local LLM config."""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from bot_pool import (
    acquire_bot_token,
    get_telegram_user_id,
    get_local_llm_config,
    parse_telegram_decision,
)


class TestAcquireBotToken(unittest.TestCase):
    SENDER = "octocat"

    def _pool_pages(self, names_and_tokens):
        return [
            {
                "Parameters": [
                    {
                        "Name": f"/blitzlog/users/{self.SENDER}/telegram/pool/{name}",
                        "Value": token,
                    }
                    for name, token in names_and_tokens
                ]
            }
        ]

    @patch("bot_pool._s3")
    @patch("bot_pool._ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_all_bots_free_returns_first_sorted(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = self._pool_pages(
            [
                ("escobar", "token1"),
                ("chaparro", "token2"),
                ("frank", "token3"),
            ]
        )
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey"}}, "GetObject"
        )
        mock_s3.put_object.return_value = {}

        result = acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)
        self.assertIsNotNone(result)
        bot_name, bot_token = result
        self.assertEqual(bot_name, "chaparro")
        self.assertEqual(bot_token, "token2")

    @patch("bot_pool._s3")
    @patch("bot_pool._ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_some_bots_locked_skips_to_free(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = self._pool_pages(
            [
                ("escobar", "token1"),
                ("chaparro", "token2"),
                ("frank", "token3"),
            ]
        )

        now = datetime.now(timezone.utc).isoformat()
        lock_body = json.dumps(
            {
                "instance_id": "i-other",
                "issue_number": 1,
                "repo": "org/repo",
                "acquired_at": now,
            }
        ).encode()
        mock_s3.get_object.side_effect = [
            {"Body": MagicMock(read=MagicMock(return_value=lock_body))},
            ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject"),
            ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject"),
        ]
        mock_s3.put_object.return_value = {}

        result = acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)
        self.assertIsNotNone(result)
        bot_name, _ = result
        self.assertEqual(bot_name, "escobar")

    @patch("bot_pool._s3")
    @patch("bot_pool._ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_all_bots_locked_returns_none(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = self._pool_pages(
            [
                ("escobar", "token1"),
                ("frank", "token2"),
            ]
        )

        now = datetime.now(timezone.utc).isoformat()
        lock_body = json.dumps(
            {
                "instance_id": "i-other",
                "issue_number": 1,
                "repo": "org/repo",
                "acquired_at": now,
            }
        ).encode()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=lock_body))
        }

        result = acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)
        self.assertIsNone(result)

    @patch("bot_pool._s3")
    @patch("bot_pool._ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_stale_lock_overwritten(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = self._pool_pages(
            [
                ("escobar", "token1"),
            ]
        )

        stale_time = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        lock_body = json.dumps(
            {
                "instance_id": "i-crashed",
                "issue_number": 1,
                "repo": "org/repo",
                "acquired_at": stale_time,
            }
        ).encode()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=lock_body))
        }
        mock_s3.put_object.return_value = {}

        result = acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)
        self.assertIsNotNone(result)
        bot_name, _ = result
        self.assertEqual(bot_name, "escobar")

    @patch("bot_pool._s3")
    @patch("bot_pool._ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_recent_lock_not_overwritten(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = self._pool_pages(
            [
                ("escobar", "token1"),
            ]
        )

        recent_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        lock_body = json.dumps(
            {
                "instance_id": "i-active",
                "issue_number": 1,
                "repo": "org/repo",
                "acquired_at": recent_time,
            }
        ).encode()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=lock_body))
        }

        result = acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)
        self.assertIsNone(result)

    @patch("bot_pool._ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_empty_pool_returns_none(self, mock_ssm):
        mock_ssm.get_paginator.return_value.paginate.return_value = [{"Parameters": []}]

        result = acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)
        self.assertIsNone(result)

    @patch("bot_pool._s3")
    @patch("bot_pool._ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_lock_key_is_sender_scoped(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = self._pool_pages(
            [
                ("escobar", "token1"),
            ]
        )
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey"}}, "GetObject"
        )
        mock_s3.put_object.return_value = {}

        acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)

        put_call = mock_s3.put_object.call_args
        self.assertEqual(
            put_call[1]["Key"],
            f"bot-pool-locks/{self.SENDER}/escobar.json",
        )

    @patch("bot_pool._s3")
    @patch("bot_pool._ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_get_lock_reads_sender_scoped_key(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = self._pool_pages(
            [
                ("escobar", "token1"),
            ]
        )
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey"}}, "GetObject"
        )
        mock_s3.put_object.return_value = {}

        acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)

        get_call = mock_s3.get_object.call_args
        self.assertEqual(
            get_call[1]["Key"],
            f"bot-pool-locks/{self.SENDER}/escobar.json",
        )

    @patch("bot_pool._s3")
    @patch("bot_pool._ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_pool_pagination_scoped_to_user(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = [{"Parameters": []}]
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey"}}, "GetObject"
        )

        acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)

        paginator_call = mock_ssm.get_paginator.return_value.paginate.call_args
        self.assertEqual(
            paginator_call[1]["Path"],
            f"/blitzlog/users/{self.SENDER}/telegram/pool",
        )

    @patch("bot_pool._s3")
    @patch("bot_pool._ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_user_without_pool_returns_none(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = [{"Parameters": []}]

        result = acquire_bot_token("lonely-user", "i-test", "org/repo", 42)
        self.assertIsNone(result)


class TestGetTelegramUserId(unittest.TestCase):
    @patch("bot_pool._ssm")
    def test_returns_value_when_present(self, mock_ssm):
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "12345"}}
        self.assertEqual(get_telegram_user_id("octocat"), "12345")
        call = mock_ssm.get_parameter.call_args
        self.assertEqual(
            call[1]["Name"],
            "/blitzlog/users/octocat/telegram/allowed-user-id",
        )

    @patch("bot_pool._ssm")
    def test_returns_none_when_missing(self, mock_ssm):
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound"}}, "GetParameter"
        )
        self.assertIsNone(get_telegram_user_id("nobody"))


class TestParseTelegramDecision(unittest.TestCase):
    def test_empty_result_returns_none(self):
        self.assertIsNone(
            parse_telegram_decision({"result": []}, {"retry", "cloud", "abort"})
        )

    def test_missing_result_returns_none(self):
        self.assertIsNone(parse_telegram_decision({}, {"retry", "cloud", "abort"}))

    def test_callback_retry(self):
        payload = {"result": [{"callback_query": {"data": "retry"}}]}
        self.assertEqual(
            parse_telegram_decision(payload, {"retry", "cloud", "abort"}),
            "retry",
        )

    def test_callback_cloud(self):
        payload = {"result": [{"callback_query": {"data": "cloud"}}]}
        self.assertEqual(
            parse_telegram_decision(payload, {"retry", "cloud", "abort"}),
            "cloud",
        )

    def test_callback_abort(self):
        payload = {"result": [{"callback_query": {"data": "abort"}}]}
        self.assertEqual(
            parse_telegram_decision(payload, {"retry", "cloud", "abort"}),
            "abort",
        )

    def test_callback_filtered_out(self):
        payload = {"result": [{"callback_query": {"data": "cloud"}}]}
        self.assertIsNone(parse_telegram_decision(payload, {"retry", "abort"}))

    def test_free_text_message_ignored(self):
        payload = {"result": [{"message": {"text": "hi"}}]}
        self.assertIsNone(parse_telegram_decision(payload, {"retry", "cloud", "abort"}))

    def test_multiple_updates_first_matching_wins(self):
        payload = {
            "result": [
                {"message": {"text": "unrelated"}},
                {"callback_query": {"data": "retry"}},
                {"callback_query": {"data": "cloud"}},
            ]
        }
        self.assertEqual(
            parse_telegram_decision(payload, {"retry", "cloud", "abort"}),
            "retry",
        )

    def test_empty_data_ignored(self):
        payload = {"result": [{"callback_query": {"data": ""}}]}
        self.assertIsNone(parse_telegram_decision(payload, {"retry", "abort"}))


def _local_llm_pages(params, login="octocat"):
    return [
        {
            "Parameters": [
                {
                    "Name": f"/blitzlog/users/{login}/local-llm/{key}",
                    "Value": value,
                }
                for key, value in params.items()
            ]
        }
    ]


class TestGetLocalLlmConfig(unittest.TestCase):
    @patch("bot_pool._ssm")
    def test_returns_none_when_no_params(self, mock_ssm):
        mock_ssm.get_paginator.return_value.paginate.return_value = [{"Parameters": []}]
        self.assertIsNone(get_local_llm_config("octocat"))

    @patch("bot_pool._ssm")
    def test_returns_none_when_ssm_path_missing(self, mock_ssm):
        mock_ssm.get_paginator.return_value.paginate.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound"}}, "GetParametersByPath"
        )
        self.assertIsNone(get_local_llm_config("octocat"))

    @patch("bot_pool._ssm")
    def test_returns_none_when_endpoint_missing(self, mock_ssm):
        mock_ssm.get_paginator.return_value.paginate.return_value = _local_llm_pages(
            {"model": "qwen2.5-coder:32b"}
        )
        self.assertIsNone(get_local_llm_config("octocat"))

    @patch("bot_pool._ssm")
    def test_returns_none_when_model_missing(self, mock_ssm):
        mock_ssm.get_paginator.return_value.paginate.return_value = _local_llm_pages(
            {"endpoint": "http://100.64.0.5:11434"}
        )
        self.assertIsNone(get_local_llm_config("octocat"))

    @patch("bot_pool._resolve_endpoint_ips")
    @patch("bot_pool._ssm")
    def test_accepts_tailscale_cgnat_when_opt_in(self, mock_ssm, mock_resolve):
        mock_ssm.get_paginator.return_value.paginate.return_value = _local_llm_pages(
            {
                "endpoint": "http://100.64.0.5:11434",
                "model": "qwen2.5-coder:32b",
                "allow-private-cidrs": "true",
                "fallback": "cloud",
            }
        )
        mock_resolve.return_value = ["100.64.0.5"]
        cfg = get_local_llm_config("octocat")
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["endpoint"], "http://100.64.0.5:11434")
        self.assertEqual(cfg["model"], "qwen2.5-coder:32b")
        self.assertTrue(cfg["allow_private"])
        self.assertEqual(cfg["fallback"], "cloud")

    @patch("bot_pool._resolve_endpoint_ips")
    @patch("bot_pool._ssm")
    def test_rejects_tailscale_cgnat_without_opt_in(self, mock_ssm, mock_resolve):
        mock_ssm.get_paginator.return_value.paginate.return_value = _local_llm_pages(
            {
                "endpoint": "http://100.64.0.5:11434",
                "model": "qwen2.5-coder:32b",
            }
        )
        mock_resolve.return_value = ["100.64.0.5"]
        self.assertIsNone(get_local_llm_config("octocat"))

    @patch("bot_pool._resolve_endpoint_ips")
    @patch("bot_pool._ssm")
    def test_rejects_imds_ip(self, mock_ssm, mock_resolve):
        mock_ssm.get_paginator.return_value.paginate.return_value = _local_llm_pages(
            {
                "endpoint": "http://169.254.169.254/latest",
                "model": "x",
                "allow-private-cidrs": "true",
            }
        )
        mock_resolve.return_value = ["169.254.169.254"]
        self.assertIsNone(get_local_llm_config("octocat"))

    @patch("bot_pool._resolve_endpoint_ips")
    @patch("bot_pool._ssm")
    def test_rejects_loopback(self, mock_ssm, mock_resolve):
        mock_ssm.get_paginator.return_value.paginate.return_value = _local_llm_pages(
            {
                "endpoint": "http://127.0.0.1:11434",
                "model": "x",
                "allow-private-cidrs": "true",
            }
        )
        mock_resolve.return_value = ["127.0.0.1"]
        self.assertIsNone(get_local_llm_config("octocat"))

    @patch("bot_pool._resolve_endpoint_ips")
    @patch("bot_pool._ssm")
    def test_rejects_rfc1918_without_opt_in(self, mock_ssm, mock_resolve):
        mock_ssm.get_paginator.return_value.paginate.return_value = _local_llm_pages(
            {
                "endpoint": "http://10.0.0.5:11434",
                "model": "x",
            }
        )
        mock_resolve.return_value = ["10.0.0.5"]
        self.assertIsNone(get_local_llm_config("octocat"))

    @patch("bot_pool._resolve_endpoint_ips")
    @patch("bot_pool._ssm")
    def test_accepts_rfc1918_with_opt_in(self, mock_ssm, mock_resolve):
        mock_ssm.get_paginator.return_value.paginate.return_value = _local_llm_pages(
            {
                "endpoint": "http://10.0.0.5:11434",
                "model": "x",
                "allow-private-cidrs": "true",
            }
        )
        mock_resolve.return_value = ["10.0.0.5"]
        cfg = get_local_llm_config("octocat")
        self.assertIsNotNone(cfg)

    @patch("bot_pool._resolve_endpoint_ips")
    @patch("bot_pool._ssm")
    def test_hard_rejects_public_ip(self, mock_ssm, mock_resolve):
        mock_ssm.get_paginator.return_value.paginate.return_value = _local_llm_pages(
            {
                "endpoint": "https://llm.example.com",
                "model": "x",
                "allow-private-cidrs": "true",
            }
        )
        mock_resolve.return_value = ["203.0.113.5"]
        self.assertIsNone(get_local_llm_config("octocat"))

    @patch("bot_pool._resolve_endpoint_ips")
    @patch("bot_pool._ssm")
    def test_hard_rejects_public_ip_even_with_opt_in(self, mock_ssm, mock_resolve):
        mock_ssm.get_paginator.return_value.paginate.return_value = _local_llm_pages(
            {
                "endpoint": "https://api.openai.com",
                "model": "x",
                "allow-private-cidrs": "true",
            }
        )
        mock_resolve.return_value = ["104.18.32.47"]
        self.assertIsNone(get_local_llm_config("octocat"))

    @patch("bot_pool._resolve_endpoint_ips")
    @patch("bot_pool._ssm")
    def test_rejects_non_http_scheme(self, mock_ssm, mock_resolve):
        mock_ssm.get_paginator.return_value.paginate.return_value = _local_llm_pages(
            {
                "endpoint": "ftp://10.0.0.5",
                "model": "x",
            }
        )
        mock_resolve.return_value = ["10.0.0.5"]
        self.assertIsNone(get_local_llm_config("octocat"))

    @patch("bot_pool._resolve_endpoint_ips")
    @patch("bot_pool._ssm")
    def test_fallback_defaults_to_closed_when_invalid(self, mock_ssm, mock_resolve):
        mock_ssm.get_paginator.return_value.paginate.return_value = _local_llm_pages(
            {
                "endpoint": "http://10.0.0.5:11434",
                "model": "x",
                "allow-private-cidrs": "true",
                "fallback": "bogus",
            }
        )
        mock_resolve.return_value = ["10.0.0.5"]
        cfg = get_local_llm_config("octocat")
        self.assertEqual(cfg["fallback"], "closed")

    @patch("bot_pool._resolve_endpoint_ips")
    @patch("bot_pool._ssm")
    def test_api_key_returned(self, mock_ssm, mock_resolve):
        mock_ssm.get_paginator.return_value.paginate.return_value = _local_llm_pages(
            {
                "endpoint": "http://10.0.0.5:11434",
                "model": "x",
                "api-key": "secret-key",
                "allow-private-cidrs": "true",
            }
        )
        mock_resolve.return_value = ["10.0.0.5"]
        cfg = get_local_llm_config("octocat")
        self.assertEqual(cfg["api_key"], "secret-key")

    @patch("bot_pool._resolve_endpoint_ips")
    @patch("bot_pool._ssm")
    def test_empty_api_key_handled(self, mock_ssm, mock_resolve):
        mock_ssm.get_paginator.return_value.paginate.return_value = _local_llm_pages(
            {
                "endpoint": "http://10.0.0.5:11434",
                "model": "x",
                "api-key": "",
                "allow-private-cidrs": "true",
            }
        )
        mock_resolve.return_value = ["10.0.0.5"]
        cfg = get_local_llm_config("octocat")
        self.assertEqual(cfg["api_key"], "")

    @patch("bot_pool._ssm")
    def test_returns_none_when_sender_login_empty(self, mock_ssm):
        self.assertIsNone(get_local_llm_config(""))

    @patch("bot_pool._resolve_endpoint_ips")
    @patch("bot_pool._ssm")
    def test_includes_tailscale_auth_key_when_set(self, mock_ssm, mock_resolve):
        mock_ssm.get_paginator.return_value.paginate.return_value = _local_llm_pages(
            {
                "endpoint": "http://100.64.0.5:11434",
                "model": "qwen2.5-coder:32b",
                "allow-private-cidrs": "true",
                "tailscale-auth-key": "tskey-auth-foobar",
            }
        )
        mock_resolve.return_value = ["100.64.0.5"]
        cfg = get_local_llm_config("octocat")
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["tailscale_auth_key"], "tskey-auth-foobar")

    @patch("bot_pool._resolve_endpoint_ips")
    @patch("bot_pool._ssm")
    def test_tailscale_auth_key_empty_when_unset(self, mock_ssm, mock_resolve):
        mock_ssm.get_paginator.return_value.paginate.return_value = _local_llm_pages(
            {
                "endpoint": "http://100.64.0.5:11434",
                "model": "qwen2.5-coder:32b",
                "allow-private-cidrs": "true",
            }
        )
        mock_resolve.return_value = ["100.64.0.5"]
        cfg = get_local_llm_config("octocat")
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["tailscale_auth_key"], "")

    @patch("bot_pool._ssm")
    def test_uses_env_independent_ssm_path(self, mock_ssm):
        """Regression for handler.py:196 — local-llm config is per-user, not per-env.

        The lookup path must be /blitzlog/users/<login>/local-llm (the env-independent
        namespace the user-pool Terraform writes to), NOT /blitzlog/<env>/users/<login>/local-llm.
        Using the env-prefixed path caused get_local_llm_config to silently return
        None on ParameterNotFound, with no warning logged.
        """
        mock_ssm.get_paginator.return_value.paginate.return_value = _local_llm_pages(
            {
                "endpoint": "http://100.64.0.5:11434",
                "model": "qwen2.5-coder:32b",
            }
        )
        get_local_llm_config("daniel-sarosi-gwc")
        called_path = mock_ssm.get_paginator.return_value.paginate.call_args.kwargs[
            "Path"
        ]
        self.assertEqual(called_path, "/blitzlog/users/daniel-sarosi-gwc/local-llm")
        self.assertNotIn("/blitzlog/dev/", called_path)
        self.assertNotIn("/blitzlog/prod/", called_path)


if __name__ == "__main__":
    unittest.main()
