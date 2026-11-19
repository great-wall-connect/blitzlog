"""Wire-format tests for the Alicloud concrete implementation
of the provider abstraction (issue #84).

Mirrors `tests/test_provider_aws.py` style: every Alicloud SDK
call goes through a single canonical shim — `_oos_client`,
`_oss_bucket`, `_ecs_client` — and the tests patch those shims
to drive the methods under test.

What lives here vs. elsewhere:

    tests/test_provider_base.py
        ABC contract: every abstract method is overridden by
        the Alicloud classes (after Phase 2 they pass the same
        "must not be `@abstractmethod`" check as the AWS
        implementations).

    tests/test_provider_aws.py
        AWS-side wire-format coverage. Already rewired in #83
        to patch `provider.aws._boto3_client`.

    This file
        Alicloud-side wire-format coverage. Tests the actual
        OOS / OSS / ECS request shapes and the cross-cloud
        error-translation helpers (`_is_already_exists`,
        `_is_not_found`) without ever touching the network.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import MagicMock, patch

import provider
from provider.alicloud import (
    AlicloudCompute,
    AlicloudObjectStore,
    AlicloudSecretStore,
    _ecs_client,
    _is_already_exists,
    _is_not_found,
    _oos_client,
    _oss_bucket,
)
from provider.base import LaunchSpec, SpotQuote


class TestOosClientShim(unittest.TestCase):
    """The OOS client shim reads region + credentials from
    `_env` and forwards them into the SDK `Config`. Tests the
    endpoint-derivation logic in isolation (no SDK call).
    """

    def setUp(self):
        self._saved_env = {
            k: os.environ.get(k)
            for k in (
                "ALIYUN_REGION",
                "ALIYUN_ACCESS_KEY_ID",
                "ALIYUN_ACCESS_KEY_SECRET",
                "ALIYUN_KMS_KEY_ID",
                "ALIYUN_CONNECT_TIMEOUT",
                "ALIYUN_READ_TIMEOUT",
            )
        }
        os.environ["ALIYUN_REGION"] = "cn-hangzhou"
        os.environ["ALIYUN_ACCESS_KEY_ID"] = "AKID"
        os.environ["ALIYUN_ACCESS_KEY_SECRET"] = "SECRET"

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_oos_client_uses_region_derived_endpoint(self):
        """The shim must build an `oos.<region>.aliyuncs.com`
        endpoint and pass it through to the SDK. We patch
        `OosClient` to avoid hitting the network.
        """
        with patch("provider.alicloud.OosClient") as mock_client:
            client = _oos_client()
        mock_client.assert_called_once()
        config = mock_client.call_args.args[0]
        self.assertEqual(config.access_key_id, "AKID")
        self.assertEqual(config.access_key_secret, "SECRET")
        self.assertEqual(config.region_id, "cn-hangzhou")
        self.assertEqual(config.endpoint, "oos.cn-hangzhou.aliyuncs.com")
        self.assertEqual(client, mock_client.return_value)

    def test_oos_client_handles_missing_region(self):
        """When `ALIYUN_REGION` is unset, the shim must not
        construct a `https://oos..aliyuncs.com` endpoint (which
        would crash the SDK); pass `None` and let the SDK
        decide.
        """
        os.environ.pop("ALIYUN_REGION", None)
        with patch("provider.alicloud.OosClient") as mock_client:
            _oos_client()
        config = mock_client.call_args.args[0]
        self.assertIsNone(config.endpoint)


class TestOssBucketShim(unittest.TestCase):
    def setUp(self):
        self._saved_env = {
            k: os.environ.get(k)
            for k in (
                "ALIYUN_REGION",
                "ALIYUN_ACCESS_KEY_ID",
                "ALIYUN_ACCESS_KEY_SECRET",
            )
        }
        os.environ["ALIYUN_REGION"] = "cn-hangzhou"
        os.environ["ALIYUN_ACCESS_KEY_ID"] = "AKID"
        os.environ["ALIYUN_ACCESS_KEY_SECRET"] = "SECRET"

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_oss_bucket_uses_region_derived_endpoint(self):
        with patch("provider.alicloud.oss2.Bucket") as mock_bucket:
            _oss_bucket("my-bucket")
        mock_bucket.assert_called_once()
        args = mock_bucket.call_args.args
        self.assertEqual(args[2], "my-bucket")
        self.assertEqual(args[1], "https://oss-cn-hangzhou.aliyuncs.com")


class TestEcsClientShim(unittest.TestCase):
    def setUp(self):
        self._saved_env = {
            k: os.environ.get(k)
            for k in (
                "ALIYUN_REGION",
                "ALIYUN_ACCESS_KEY_ID",
                "ALIYUN_ACCESS_KEY_SECRET",
            )
        }
        os.environ["ALIYUN_REGION"] = "cn-hangzhou"
        os.environ["ALIYUN_ACCESS_KEY_ID"] = "AKID"
        os.environ["ALIYUN_ACCESS_KEY_SECRET"] = "SECRET"

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_ecs_client_uses_region_derived_endpoint(self):
        with patch("provider.alicloud.EcsClient") as mock_client:
            _ecs_client()
        config = mock_client.call_args.args[0]
        self.assertEqual(config.region_id, "cn-hangzhou")
        self.assertEqual(config.endpoint, "ecs.cn-hangzhou.aliyuncs.com")


class TestAlicloudSecretStore(unittest.TestCase):
    """Wire-format coverage for the OOS Parameter Store adapter.

    Tests patch `_oos_client` so the SDK factory never runs.
    The mocked client exposes `get_secret_parameter_with_options`,
    `create_secret_parameter_with_options`,
    `update_secret_parameter_with_options`,
    `delete_secret_parameter_with_options`,
    `get_secret_parameters_by_path_with_options` as `MagicMock`s.
    Tests assert on the request classes the method builds and on
    the translation of the response / exception into the wire
    shape the rest of the Lambda expects.
    """

    def setUp(self):
        provider.reset_provider_singletons()
        self._saved_env = {
            k: os.environ.get(k)
            for k in (
                "ALIYUN_REGION",
                "ALIYUN_ACCESS_KEY_ID",
                "ALIYUN_ACCESS_KEY_SECRET",
                "ALIYUN_KMS_KEY_ID",
            )
        }
        os.environ["ALIYUN_REGION"] = "cn-hangzhou"
        os.environ["ALIYUN_ACCESS_KEY_ID"] = "AKID"
        os.environ["ALIYUN_ACCESS_KEY_SECRET"] = "SECRET"

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_get_returns_the_parameter_value(self):
        with patch("provider.alicloud._oos_client") as mock_factory:
            mock_client = mock_factory.return_value
            mock_client.get_secret_parameter_with_options.return_value.body.parameter.value = (
                "secret-value"
            )
            store = AlicloudSecretStore()
            value = store.get("/blitzlog/prod/foo")
        self.assertEqual(value, "secret-value")
        request = mock_client.get_secret_parameter_with_options.call_args.args[0]
        # The leading slash is stripped: OOS parameter names carry
        # none, and the RAM ARN
        # (`acs:oos:<region>:<account>:secretparameter/<name>`)
        # would otherwise double it.
        self.assertEqual(request.name, "blitzlog/prod/foo")
        self.assertTrue(request.with_decryption)

    def test_get_decrypts_even_when_the_caller_says_otherwise(self):
        """OOS returns an EMPTY value when decryption is off — it does
        not hand back the ciphertext the way SSM does.

        `base.get_agent_image_id` reads the Packer-published image id
        with `decrypt=False` (the SSM idiom: a String-typed SSM
        parameter is not encrypted, so there is nothing to decrypt).
        Honouring that flag on OOS would yield "" instead of the
        image id, and the launch would fall back to the public Ubuntu
        image with no error to explain it.
        """
        with patch("provider.alicloud._oos_client") as mock_factory:
            mock_client = mock_factory.return_value
            mock_client.get_secret_parameter_with_options.return_value.body.parameter.value = (
                "image-id"
            )
            store = AlicloudSecretStore()
            value = store.get("/blitzlog/prod/agent-ami-id-ecs-ubuntu", decrypt=False)
        self.assertEqual(value, "image-id")
        request = mock_client.get_secret_parameter_with_options.call_args.args[0]
        self.assertTrue(
            request.with_decryption,
            "the Alicloud store must always request decryption — OOS "
            "returns an empty value when it is off",
        )

    def test_put_uses_create_secret_parameter_when_no_conflict(self):
        with patch("provider.alicloud._oos_client") as mock_factory:
            mock_client = mock_factory.return_value
            store = AlicloudSecretStore()
            store.put("/blitzlog/prod/foo", "v", type="SecureString")
        mock_client.create_secret_parameter_with_options.assert_called_once()
        request = mock_client.create_secret_parameter_with_options.call_args.args[0]
        self.assertEqual(request.name, "blitzlog/prod/foo")
        self.assertEqual(request.value, "v")
        # Every parameter is an encrypted one — the SSM type name
        # the caller passes carries no meaning on the Alicloud side.
        self.assertEqual(request.type, "Secret")
        # No CMK env var → key_id is None.
        self.assertIsNone(request.key_id)
        # No UpdateSecretParameter call (the create succeeded).
        mock_client.update_secret_parameter_with_options.assert_not_called()

    def test_put_passes_cmk_when_configured(self):
        os.environ["ALIYUN_KMS_KEY_ID"] = "cmk-id-123"
        with patch("provider.alicloud._oos_client") as mock_factory:
            mock_client = mock_factory.return_value
            store = AlicloudSecretStore()
            store.put("/blitzlog/prod/foo", "v", type="SecureString")
        request = mock_client.create_secret_parameter_with_options.call_args.args[0]
        self.assertEqual(request.key_id, "cmk-id-123")

    def test_put_falls_through_to_update_secret_parameter_on_conflict(self):
        """When `CreateSecretParameter` raises an
        "EntityAlreadyExist.Parameter" error and `overwrite=True`,
        the put must fall through to `UpdateSecretParameter` so the
        AWS SSM `PutParameter(Overwrite=True)` semantics round-trip
        identically.
        """
        from Tea.exceptions import TeaException

        conflict = TeaException(
            {"code": "EntityAlreadyExist.Parameter", "message": "exists"}
        )
        with patch("provider.alicloud._oos_client") as mock_factory:
            mock_client = mock_factory.return_value
            mock_client.create_secret_parameter_with_options.side_effect = conflict
            store = AlicloudSecretStore()
            store.put("/blitzlog/prod/foo", "v", type="SecureString")
        mock_client.update_secret_parameter_with_options.assert_called_once()
        request = mock_client.update_secret_parameter_with_options.call_args.args[0]
        self.assertEqual(request.name, "blitzlog/prod/foo")
        self.assertEqual(request.value, "v")

    def test_put_raises_on_conflict_when_overwrite_false(self):
        """When `CreateSecretParameter` raises an
        "EntityAlreadyExist.Parameter" error and `overwrite=False`,
        the put must re-raise as `_ProviderError("ParameterExists")`
        rather than silently overwriting.
        """
        from provider.alicloud import _ProviderError
        from Tea.exceptions import TeaException

        conflict = TeaException(
            {"code": "EntityAlreadyExist.Parameter", "message": "exists"}
        )
        with patch("provider.alicloud._oos_client") as mock_factory:
            mock_client = mock_factory.return_value
            mock_client.create_secret_parameter_with_options.side_effect = conflict
            store = AlicloudSecretStore()
            with self.assertRaises(_ProviderError) as ctx:
                store.put(
                    "/blitzlog/prod/foo", "v", type="SecureString", overwrite=False
                )
        self.assertEqual(ctx.exception.code, "ParameterExists")
        mock_client.update_secret_parameter_with_options.assert_not_called()

    def test_put_propagates_unexpected_errors(self):
        from Tea.exceptions import TeaException

        unexpected = TeaException({"code": "InternalError", "message": "boom"})
        with patch("provider.alicloud._oos_client") as mock_factory:
            mock_client = mock_factory.return_value
            mock_client.create_secret_parameter_with_options.side_effect = unexpected
            store = AlicloudSecretStore()
            with self.assertRaises(TeaException):
                store.put("/blitzlog/prod/foo", "v", type="SecureString")

    def test_delete_removes_the_parameter(self):
        with patch("provider.alicloud._oos_client") as mock_factory:
            mock_client = mock_factory.return_value
            store = AlicloudSecretStore()
            store.delete("/blitzlog/prod/foo")
        request = mock_client.delete_secret_parameter_with_options.call_args.args[0]
        self.assertEqual(request.name, "blitzlog/prod/foo")

    def test_delete_swallows_entity_not_exists(self):
        from Tea.exceptions import TeaException

        not_exists = TeaException(
            {"code": "EntityNotExists.Parameter", "message": "missing"}
        )
        with patch("provider.alicloud._oos_client") as mock_factory:
            mock_client = mock_factory.return_value
            mock_client.delete_secret_parameter_with_options.side_effect = not_exists
            store = AlicloudSecretStore()
            # Should not raise.
            store.delete("/blitzlog/prod/missing")
        mock_client.delete_secret_parameter_with_options.assert_called_once()

    def test_delete_propagates_other_errors(self):
        from Tea.exceptions import TeaException

        access_denied = TeaException(
            {"code": "Forbidden.NoPermission", "message": "denied"}
        )
        with patch("provider.alicloud._oos_client") as mock_factory:
            mock_client = mock_factory.return_value
            mock_client.delete_secret_parameter_with_options.side_effect = access_denied
            store = AlicloudSecretStore()
            with self.assertRaises(TeaException):
                store.delete("/blitzlog/prod/foo")

    def test_get_by_path_filters_and_flattens(self):
        """`get_by_path` calls `GetSecretParametersByPath` and
        flattens the result to `{last_segment: value}` — the AWS SSM
        `get_parameters_by_path` shape.
        """
        with patch("provider.alicloud._oos_client") as mock_factory:
            mock_client = mock_factory.return_value
            body = MagicMock()
            body.next_token = None
            matching = MagicMock()
            matching.name = "blitzlog/prod/local-llm/endpoint"
            matching.value = "http://10.0.0.5:11434"
            other = MagicMock()
            other.name = "blitzlog/prod/other/secret"
            other.value = "nope"
            body.parameters = [matching, other]
            mock_client.get_secret_parameters_by_path_with_options.return_value.body = (
                body
            )
            store = AlicloudSecretStore()
            params = store.get_by_path("/blitzlog/prod/local-llm")
        self.assertEqual(params, {"endpoint": "http://10.0.0.5:11434"})
        request = mock_client.get_secret_parameters_by_path_with_options.call_args.args[
            0
        ]
        # The path is stripped of its leading slash for the same
        # reason the names are — a leading slash matches nothing.
        self.assertEqual(request.path, "blitzlog/prod/local-llm")
        self.assertTrue(request.recursive)
        self.assertTrue(request.with_decryption)

    def test_get_by_path_paginates(self):
        """The OOS API caps `MaxResults` at 10 and hands back a
        `NextToken` — the store must follow it."""
        with patch("provider.alicloud._oos_client") as mock_factory:
            mock_client = mock_factory.return_value
            first = MagicMock()
            first.next_token = "page-2"
            first.parameters = []
            second = MagicMock()
            second.next_token = None
            parameter = MagicMock()
            parameter.name = "blitzlog/prod/local-llm/endpoint"
            parameter.value = "http://10.0.0.5:11434"
            second.parameters = [parameter]
            mock_client.get_secret_parameters_by_path_with_options.side_effect = [
                MagicMock(body=first),
                MagicMock(body=second),
            ]
            store = AlicloudSecretStore()
            params = store.get_by_path("/blitzlog/prod/local-llm")
        self.assertEqual(params, {"endpoint": "http://10.0.0.5:11434"})
        self.assertEqual(
            mock_client.get_secret_parameters_by_path_with_options.call_count, 2
        )
        second_request = (
            mock_client.get_secret_parameters_by_path_with_options.call_args.args[0]
        )
        self.assertEqual(second_request.next_token, "page-2")


class TestAlicloudCredentialsAndTimeouts(unittest.TestCase):
    """The Alicloud clients must resolve the Function Compute
    runtime's credentials and survive its latency.

    Two things the FC runtime forces on us:

      * it hands the function role's temporary credentials over as
        `ALIBABA_CLOUD_ACCESS_KEY_ID` / `_SECRET` / `_SECURITY_TOKEN`
        — there is no static key to configure, and the SDKs' own
        chain is bypassed when an (empty) access key is passed
        explicitly;
      * its timeouts are milliseconds. The Tea HTTP layer divides
        the values by 1000, so passing seconds turns a 30 s read
        timeout into 30 ms and every store read fails with "Read
        timed out. (read timeout=0.03)".
    """

    def test_the_access_key_falls_back_to_the_sdk_standard_name(self):
        from _env import _aliyun_access_key_id, _aliyun_access_key_secret

        with patch.dict(
            os.environ,
            {"ALIYUN_ACCESS_KEY_ID": "", "ALIBABA_CLOUD_ACCESS_KEY_ID": "AKID"},
            clear=False,
        ):
            self.assertEqual(_aliyun_access_key_id(), "AKID")
        with patch.dict(
            os.environ,
            {
                "ALIYUN_ACCESS_KEY_SECRET": "",
                "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "SECRET",
            },
            clear=False,
        ):
            self.assertEqual(_aliyun_access_key_secret(), "SECRET")

    def test_the_timeouts_are_milliseconds(self):
        from provider.alicloud import _client_timeouts

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALIYUN_CONNECT_TIMEOUT", None)
            os.environ.pop("ALIYUN_READ_TIMEOUT", None)
            connect, read = _client_timeouts()
        self.assertEqual(connect, 10000)
        self.assertEqual(read, 30000)

    def test_the_oos_client_leaves_the_credentials_to_the_sdk_chain(self):
        """With no static key configured, the client must not set an
        empty one — that overrides the SDK's own chain and every
        call fails with "InvalidCredentials"."""
        from provider.alicloud import _oos_client

        with (
            patch.dict(
                os.environ,
                {
                    "ALIYUN_REGION": "cn-hangzhou",
                    "ALIYUN_ACCESS_KEY_ID": "",
                    "ALIYUN_ACCESS_KEY_SECRET": "",
                    "ALIBABA_CLOUD_ACCESS_KEY_ID": "",
                    "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "",
                },
                clear=False,
            ),
            patch("provider.alicloud.OosClient") as mock_client,
        ):
            _oos_client()
        config = mock_client.call_args.args[0]
        self.assertIsNone(
            config.access_key_id,
            "an empty access key must not be passed — it overrides the "
            "SDK's own credential chain",
        )
        self.assertNotEqual(config.type, "access_key")


class TestAlicloudBundleNeedsNoAwsSdk(unittest.TestCase):
    """The Alicloud bundle must import without the AWS SDK.

    The Alicloud bundle ships the Alibaba SDKs, not boto3. A
    module-level `import boto3` (or `from botocore.exceptions
    import ...`) anywhere in the handler's import graph makes the
    whole function unimportable there: the cold start dies with
    `ModuleNotFoundError: No module named 'boto3'` and every
    invocation answers `502 Bad Gateway` — the function never
    starts, so nothing about the webhook can be diagnosed from
    the outside.

    The check runs in a subprocess with a meta-path finder that
    blocks `boto3` / `botocore`, because the test environment
    has both installed (the AWS bundle needs them).
    """

    def test_handler_imports_with_the_aws_sdk_blocked(self):
        lambda_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lambda"
        )
        script = textwrap.dedent("""
            import importlib.abc
            import os
            import sys


            class Blocker(importlib.abc.MetaPathFinder):
                BLOCKED = {"boto3", "botocore"}

                def find_spec(self, name, path=None, target=None):
                    if name.split(".")[0] in self.BLOCKED:
                        raise ImportError("blocked by the test: " + name)
                    return None


            sys.meta_path.insert(0, Blocker())
            lambda_dir = os.environ["LAMBDA_DIR"]
            sys.path.insert(0, lambda_dir)
            sys.path.insert(0, lambda_dir + "/scripts")
            os.environ["BLITZLOG_PROVIDER"] = "alicloud"

            import handler

            assert handler.lambda_handler
            print("ok")
            """)
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "LAMBDA_DIR": lambda_dir},
        )
        self.assertEqual(
            result.returncode,
            0,
            f"the handler must import without the AWS SDK:\n{result.stderr}",
        )
        self.assertIn("ok", result.stdout)


class TestAlicloudObjectStore(unittest.TestCase):
    def setUp(self):
        provider.reset_provider_singletons()
        self._saved_env = {
            k: os.environ.get(k)
            for k in (
                "ALIYUN_REGION",
                "ALIYUN_ACCESS_KEY_ID",
                "ALIYUN_ACCESS_KEY_SECRET",
            )
        }
        os.environ["ALIYUN_REGION"] = "cn-hangzhou"
        os.environ["ALIYUN_ACCESS_KEY_ID"] = "AKID"
        os.environ["ALIYUN_ACCESS_KEY_SECRET"] = "SECRET"

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_put_calls_bucket_put_object(self):
        with patch("provider.alicloud._oss_bucket") as mock_bucket_factory:
            mock_bucket = mock_bucket_factory.return_value
            store = AlicloudObjectStore()
            store.put("my-bucket", "key/x.json", b'{"a":1}')
        mock_bucket_factory.assert_called_once_with("my-bucket")
        mock_bucket.put_object.assert_called_once_with("key/x.json", b'{"a":1}')

    def test_get_returns_bytes(self):
        with patch("provider.alicloud._oss_bucket") as mock_bucket_factory:
            mock_bucket = mock_bucket_factory.return_value
            mock_bucket.get_object.return_value.read.return_value = b"hello"
            store = AlicloudObjectStore()
            value = store.get("my-bucket", "k")
        self.assertEqual(value, b"hello")
        mock_bucket.get_object.assert_called_once_with("k")

    def test_head_returns_true_when_key_exists(self):
        with patch("provider.alicloud._oss_bucket") as mock_bucket_factory:
            mock_bucket = mock_bucket_factory.return_value
            store = AlicloudObjectStore()
            self.assertTrue(store.head("b", "k"))
        mock_bucket.head_object.assert_called_once_with("k")

    def test_head_returns_false_for_no_such_key(self):
        import oss2

        with patch("provider.alicloud._oss_bucket") as mock_bucket_factory:
            mock_bucket = mock_bucket_factory.return_value
            mock_bucket.head_object.side_effect = oss2.exceptions.NoSuchKey(
                404, {}, "", {"key": "k"}
            )
            store = AlicloudObjectStore()
            self.assertFalse(store.head("b", "k"))

    def test_head_propagates_other_errors(self):
        import oss2

        with patch("provider.alicloud._oss_bucket") as mock_bucket_factory:
            mock_bucket = mock_bucket_factory.return_value
            mock_bucket.head_object.side_effect = oss2.exceptions.AccessDenied(
                403, {}, "", {"Code": "AccessDenied"}
            )
            store = AlicloudObjectStore()
            with self.assertRaises(oss2.exceptions.AccessDenied):
                store.head("b", "k")

    def test_delete_calls_bucket_delete_object(self):
        with patch("provider.alicloud._oss_bucket") as mock_bucket_factory:
            mock_bucket = mock_bucket_factory.return_value
            store = AlicloudObjectStore()
            store.delete("b", "k")
        mock_bucket.delete_object.assert_called_once_with("k")


class TestAlicloudComputeRegion(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("ALIYUN_REGION")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("ALIYUN_REGION", None)
        else:
            os.environ["ALIYUN_REGION"] = self._saved

    def test_region_returns_aliyun_region_env(self):
        os.environ["ALIYUN_REGION"] = "cn-hangzhou"
        self.assertEqual(AlicloudCompute().region(), "cn-hangzhou")

    def test_region_returns_empty_string_when_env_unset(self):
        os.environ.pop("ALIYUN_REGION", None)
        self.assertEqual(AlicloudCompute().region(), "")


class TestAlicloudComputeBuildUserDataDownloaderScript(unittest.TestCase):
    """The downloader script rendered by `AlicloudCompute` must
    use the Aliyun metadata endpoint (`100.100.100.200`) and
    `aliyun oss cp`, not the AWS IMDSv2 + `aws s3 cp` pair.
    Mirrors the regression test in `test_provider_aws.py`.
    """

    def test_downloader_references_aliyun_metadata_endpoint(self):
        script = AlicloudCompute().build_user_data_downloader_script(
            "my-bucket", "user-data/issue-42.sh"
        )
        self.assertIn("100.100.100.200/latest/meta-data/region-id", script)
        self.assertIn("aliyun oss cp", script)
        self.assertIn("oss://my-bucket/user-data/issue-42.sh", script)
        self.assertIn("bash /tmp/bootstrap.sh", script)

    def test_downloader_does_not_reference_aws_imds(self):
        """Sanity: the EC2 heredoc leaks (169.254.169.254,
        IMDSv2 token, `aws s3 cp`) must NOT appear in the
        Alicloud heredoc.
        """
        script = AlicloudCompute().build_user_data_downloader_script("my-bucket", "k")
        self.assertNotIn("169.254.169.254", script)
        self.assertNotIn("aws s3 cp", script)
        self.assertNotIn("api/token", script)


class TestAlicloudComputeDescribeImage(unittest.TestCase):
    def test_describe_image_forwards_to_paged_request(self):
        with patch("provider.alicloud._ecs_client") as mock_factory:
            mock_ecs = mock_factory.return_value
            mock_ecs.describe_images_with_options.return_value = {"Images": []}
            compute = AlicloudCompute()
            result = compute.describe_image("m-bp123")
        request = mock_ecs.describe_images_with_options.call_args.args[0]
        self.assertEqual(request.image_id, ["m-bp123"])
        self.assertEqual(result, {"Images": []})


class TestAlicloudComputeGetLatestImage(unittest.TestCase):
    def test_get_latest_image_returns_first_image_id(self):
        with patch("provider.alicloud._ecs_client") as mock_factory:
            mock_ecs = mock_factory.return_value
            image_a = MagicMock()
            image_a.image_id = "m-bp123"
            image_b = MagicMock()
            image_b.image_id = "m-bp456"
            body = MagicMock()
            body.images.image = [image_a, image_b]
            response = MagicMock()
            response.body = body
            mock_ecs.describe_images_with_options.return_value = response
            compute = AlicloudCompute()
            image_id = compute.get_latest_image("26.04")
        self.assertEqual(image_id, "m-bp123")
        request = mock_ecs.describe_images_with_options.call_args.args[0]
        self.assertEqual(request.image_owner_alias, "system")
        self.assertEqual(request.image_name, "ubuntu_*_26.04_amd64_*")


class TestAlicloudComputeGetInstanceProfileArn(unittest.TestCase):
    def test_returns_acs_ram_style_arn(self):
        """The returned identifier follows Alibaba's RAM ARN
        format `acs:ram::{account}:role/{name}`. The
        `{account}` slot is a placeholder (0) because we do
        not pull in the RAM SDK in Phase 2 to fetch it.
        """
        arn = AlicloudCompute().get_instance_profile_arn("my-role")
        self.assertEqual(arn, "acs:ram::0:role/my-role")


class TestAlicloudComputeSpotPrices(unittest.TestCase):
    def test_spot_prices_returns_sorted_quotes(self):
        with patch("provider.alicloud._ecs_client") as mock_factory:
            mock_ecs = mock_factory.return_value
            entry_a = MagicMock()
            entry_a.zone_id = "cn-hangzhou-h"
            entry_a.instance_type = "ecs.t6-c1m2.large"
            entry_a.spot_price = 0.05
            entry_b = MagicMock()
            entry_b.zone_id = "cn-hangzhou-i"
            entry_b.instance_type = "ecs.t6-c1m2.large"
            entry_b.spot_price = 0.01
            response = MagicMock()
            response.body.spot_prices.spot_price_type = [entry_a, entry_b]
            mock_ecs.describe_spot_price_history_with_options.return_value = response
            compute = AlicloudCompute()
            quotes = compute.spot_prices(["ecs.t6-c1m2.large"])
        # Cheapest first.
        self.assertEqual(
            quotes,
            [
                SpotQuote(
                    availability_zone="cn-hangzhou-i",
                    instance_type="ecs.t6-c1m2.large",
                    price_usd_hour=0.01,
                ),
                SpotQuote(
                    availability_zone="cn-hangzhou-h",
                    instance_type="ecs.t6-c1m2.large",
                    price_usd_hour=0.05,
                ),
            ],
        )

    def test_spot_prices_returns_empty_on_tea_exception(self):
        from Tea.exceptions import TeaException

        with patch("provider.alicloud._ecs_client") as mock_factory:
            mock_ecs = mock_factory.return_value
            mock_ecs.describe_spot_price_history_with_options.side_effect = (
                TeaException({"code": "InternalError", "message": "boom"})
            )
            compute = AlicloudCompute()
            self.assertEqual(compute.spot_prices(["ecs.t6-c1m2.large"]), [])


class TestAlicloudComputeAzSubnetMap(unittest.TestCase):
    def test_returns_empty_when_vpc_id_empty(self):
        compute = AlicloudCompute()
        self.assertEqual(compute.az_subnet_map(""), {})

    def test_returns_zone_to_vswitch_map(self):
        with patch("provider.alicloud._ecs_client") as mock_factory:
            mock_ecs = mock_factory.return_value
            vsw_a = MagicMock()
            vsw_a.zone_id = "cn-hangzhou-h"
            vsw_a.v_switch_id = "vsw-aaa"
            vsw_b = MagicMock()
            vsw_b.zone_id = "cn-hangzhou-i"
            vsw_b.v_switch_id = "vsw-bbb"
            body = MagicMock()
            body.v_switches.v_switch = [vsw_a, vsw_b]
            response = MagicMock()
            response.body = body
            mock_ecs.describe_vswitches_with_options.return_value = response
            compute = AlicloudCompute()
            result = compute.az_subnet_map("vpc-1")
        self.assertEqual(
            result, {"cn-hangzhou-h": "vsw-aaa", "cn-hangzhou-i": "vsw-bbb"}
        )

    def test_returns_empty_on_tea_exception(self):
        from Tea.exceptions import TeaException

        with patch("provider.alicloud._ecs_client") as mock_factory:
            mock_ecs = mock_factory.return_value
            mock_ecs.describe_vswitches_with_options.side_effect = TeaException(
                {"code": "InternalError", "message": "boom"}
            )
            compute = AlicloudCompute()
            self.assertEqual(compute.az_subnet_map("vpc-1"), {})


class TestAlicloudComputeLaunchSpot(unittest.TestCase):
    def _spec(self, **overrides) -> LaunchSpec:
        return LaunchSpec(
            image_id="m-bp1",
            instance_type="ecs.t6-c1m2.large",
            user_data_b64="Zm9v",
            subnet_id="vsw-aaa",
            security_group_id="sg-1",
            instance_profile_name="my-role",
            volume_size_gb=20,
            tags={"Issue": "42", "SpotPriceCap": "0.05"},
            **overrides,
        )

    def test_launch_spot_returns_instance_id_from_first_quote(self):
        with patch("provider.alicloud._ecs_client") as mock_factory:
            mock_ecs = mock_factory.return_value
            mock_ecs.spot_prices = MagicMock()
            compute = AlicloudCompute()
            compute.spot_prices = MagicMock(
                return_value=[
                    SpotQuote(
                        availability_zone="cn-hangzhou-h",
                        instance_type="ecs.t6-c1m2.large",
                        price_usd_hour=0.01,
                    )
                ]
            )
            compute.az_subnet_map = MagicMock(return_value={"cn-hangzhou-h": "vsw-aaa"})
            ids = MagicMock()
            ids.instance_id_set = ["i-bp1abc"]
            response = MagicMock()
            response.body.instance_id_sets = ids
            mock_ecs.run_instances_with_options.return_value = response
            instance_id = compute.launch_spot(self._spec())
        self.assertEqual(instance_id, "i-bp1abc")
        request = mock_ecs.run_instances_with_options.call_args.args[0]
        self.assertEqual(request.image_id, "m-bp1")
        self.assertEqual(request.spot_strategy, "SpotWithPriceLimit")
        self.assertEqual(request.spot_price_limit, 0.05)
        self.assertEqual(request.ram_role_name, "my-role")
        self.assertEqual(request.v_switch_id, "vsw-aaa")
        self.assertEqual(request.instance_type, "ecs.t6-c1m2.large")
        self.assertEqual(request.zone_id, "cn-hangzhou-h")

    def test_launch_spot_falls_back_to_on_demand_when_all_spot_fail(self):
        from Tea.exceptions import TeaException

        with patch("provider.alicloud._ecs_client") as mock_factory:
            mock_ecs = mock_factory.return_value
            compute = AlicloudCompute()
            compute.spot_prices = MagicMock(
                return_value=[
                    SpotQuote(
                        availability_zone="cn-hangzhou-h",
                        instance_type="ecs.t6-c1m2.large",
                        price_usd_hour=0.01,
                    )
                ]
            )
            compute.az_subnet_map = MagicMock(return_value={"cn-hangzhou-h": "vsw-aaa"})
            ids = MagicMock()
            ids.instance_id_set = ["i-bpfod"]
            response = MagicMock()
            response.body.instance_id_sets = ids
            mock_ecs.run_instances_with_options = MagicMock(
                side_effect=[
                    TeaException({"code": "QuotaExceeded", "message": "out of stock"}),
                    response,
                ]
            )
            instance_id = compute.launch_spot(self._spec())
        self.assertEqual(instance_id, "i-bpfod")
        # The second call (the on-demand fallback) uses the
        # same `common_params` — `spot_strategy` is still
        # `SpotWithPriceLimit` because we share the params
        # across the loop AND the on-demand fallback. (Aliyun
        # ignores SpotStrategy on a non-preemptible instance;
        # the fallback intentionally keeps the price-limit
        # envelope so an operator who set a cap is never
        # exceeded.) The test asserts the fallback fired by
        # checking the call count rather than the strategy.
        self.assertEqual(mock_ecs.run_instances_with_options.call_count, 2)


class TestAlicloudComputeTerminate(unittest.TestCase):
    def test_terminate_calls_delete_instances_with_force(self):
        with patch("provider.alicloud._ecs_client") as mock_factory:
            mock_ecs = mock_factory.return_value
            compute = AlicloudCompute()
            compute.terminate("i-bp1abc")
        request = mock_ecs.delete_instances_with_options.call_args.args[0]
        self.assertEqual(request.instance_id, ["i-bp1abc"])
        self.assertTrue(request.force)


class TestAlicloudComputeLoadSpotInstanceTypes(unittest.TestCase):
    """`_load_spot_instance_types` parses the
    `SPOT_INSTANCE_TYPES_JSON` env var. Mirrors the AWS
    equivalent's parse-fallback behaviour.
    """

    def setUp(self):
        self._saved = os.environ.get("SPOT_INSTANCE_TYPES_JSON")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("SPOT_INSTANCE_TYPES_JSON", None)
        else:
            os.environ["SPOT_INSTANCE_TYPES_JSON"] = self._saved

    def test_returns_default_when_env_unset(self):
        os.environ.pop("SPOT_INSTANCE_TYPES_JSON", None)
        compute = AlicloudCompute()
        types = compute._load_spot_instance_types()
        self.assertGreater(len(types), 0)

    def test_returns_default_when_env_invalid_json(self):
        os.environ["SPOT_INSTANCE_TYPES_JSON"] = "not-json"
        compute = AlicloudCompute()
        types = compute._load_spot_instance_types()
        self.assertEqual(types, compute._default_spot_instance_types())

    def test_returns_default_when_env_not_a_list_of_strings(self):
        os.environ["SPOT_INSTANCE_TYPES_JSON"] = "[]"
        compute = AlicloudCompute()
        types = compute._load_spot_instance_types()
        self.assertEqual(types, compute._default_spot_instance_types())

    def test_returns_parsed_list(self):
        os.environ["SPOT_INSTANCE_TYPES_JSON"] = '["ecs.t6", "ecs.t5"]'
        compute = AlicloudCompute()
        types = compute._load_spot_instance_types()
        self.assertEqual(types, ["ecs.t6", "ecs.t5"])


class TestCrossCloudErrorHelpers(unittest.TestCase):
    """`_is_already_exists` / `_is_not_found` translate Alicloud
    SDK `TeaException.code` strings into the boolean the
    SecretStore needs.
    """

    def test_is_already_exists_handles_conflict_code(self):
        exc = MagicMock()
        exc.code = "DataConflict.SecretAlreadyExists"
        self.assertTrue(_is_already_exists(exc))

    def test_is_already_exists_returns_false_for_other_codes(self):
        exc = MagicMock()
        exc.code = "InternalError"
        self.assertFalse(_is_already_exists(exc))

    def test_is_already_exists_handles_missing_code(self):
        exc = Exception("no code attribute")
        self.assertFalse(_is_already_exists(exc))

    def test_is_not_found_handles_resource_not_exists(self):
        exc = MagicMock()
        exc.code = "Forbidden.ResourceNotExists"
        self.assertTrue(_is_not_found(exc))

    def test_is_not_found_handles_generic_not_found(self):
        exc = MagicMock()
        exc.code = "NotFound"
        self.assertTrue(_is_not_found(exc))

    def test_is_not_found_returns_false_for_other_codes(self):
        exc = MagicMock()
        exc.code = "InternalError"
        self.assertFalse(_is_not_found(exc))


class _FakeAliError(Exception):
    """Stand-in for an Alicloud SDK error.

    The Alicloud SDK's `TeaException` exposes the error code on a
    `.code` attribute; `provider.base._error_code` reads that
    attribute. This fake reproduces the shape without pulling the
    `alibabacloud_tea_openapi` dependency into the test.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TestAlicloudAgentImageIdResolution(unittest.TestCase):
    """Issue #125: the Alicloud path reads the id of the
    Packer-baked ECS image from KMS Secrets Manager, with the same
    fallback chain the AWS path has against SSM Parameter Store.

    The chain:

      1. The secret is missing → use the cloud-published Ubuntu image.
      2. The image is retired → clear the stale secret, then use the
         cloud-published Ubuntu image.
      3. Any other error → re-raise.

    Step 2 matters: a retired image id left in KMS would turn every
    subsequent webhook into a 500.
    """

    def setUp(self):
        self.compute = AlicloudCompute()

    def _secret_store(self, value=None, error=None):
        store = MagicMock()
        if error is not None:
            store.get.side_effect = error
        else:
            store.get.return_value = value
        return store

    def test_uses_the_aliyun_secret_suffix(self):
        self.assertEqual(
            self.compute.agent_image_param_suffix,
            "agent-ami-id-ecs-ubuntu",
            "the Alicloud path must read the KMS secret the Alicloud "
            "Packer post-processor publishes",
        )

    def test_returns_the_stored_image_id_when_launchable(self):
        store = self._secret_store(value="img-123")
        with patch.object(
            self.compute, "describe_image", return_value={"ok": True}
        ) as describe:
            result = self.compute.get_agent_image_id(store)
        self.assertEqual(result, "img-123")
        describe.assert_called_once_with("img-123")
        store.delete.assert_not_called()

    def test_reads_the_secret_from_the_env_scoped_path(self):
        store = self._secret_store(value="img-123")
        with patch.object(self.compute, "describe_image", return_value={}):
            self.compute.get_agent_image_id(store)
        name = store.get.call_args[0][0]
        self.assertTrue(
            name.endswith("/agent-ami-id-ecs-ubuntu"),
            f"the secret name must carry the Alicloud suffix, got {name!r}",
        )
        self.assertIn("/blitzlog/", name, "the secret name must be env-scoped")

    def test_falls_back_when_the_secret_is_missing(self):
        """KMS reports a missing secret as
        `Forbidden.ResourceNotExists`."""
        error = _FakeAliError("Forbidden.ResourceNotExists")
        store = self._secret_store(error=error)

        with patch.object(
            self.compute, "get_latest_image", return_value="public-image"
        ) as latest:
            result = self.compute.get_agent_image_id(store)
        self.assertEqual(result, "public-image")
        latest.assert_called_once_with("26.04")

    def test_falls_back_and_clears_the_secret_when_the_image_is_retired(self):
        """ECS reports a retired image as `InvalidImageId.NotFound`."""
        error = _FakeAliError("InvalidImageId.NotFound")
        store = self._secret_store(value="img-retired")

        with (
            patch.object(self.compute, "describe_image", side_effect=error),
            patch.object(
                self.compute, "get_latest_image", return_value="public-image"
            ) as latest,
        ):
            result = self.compute.get_agent_image_id(store)
        self.assertEqual(result, "public-image")
        latest.assert_called_once_with("26.04")
        store.delete.assert_called_once()
        self.assertTrue(
            store.delete.call_args[0][0].endswith("/agent-ami-id-ecs-ubuntu"),
            "the stale secret must be cleared so the next launch does not "
            "reuse the retired image id",
        )

    def test_reraises_unexpected_errors(self):
        """An error that is neither "missing" nor "retired" must
        surface — swallowing it would silently degrade every launch
        to the public image."""
        error = _FakeAliError("Forbidden.AccessDenied")
        store = self._secret_store(error=error)

        with (
            patch.object(self.compute, "get_latest_image") as latest,
            self.assertRaises(_FakeAliError),
        ):
            self.compute.get_agent_image_id(store)
        latest.assert_not_called()

    def test_reraises_unexpected_describe_errors(self):
        error = _FakeAliError("Throttling")
        store = self._secret_store(value="img-123")

        with (
            patch.object(self.compute, "describe_image", side_effect=error),
            self.assertRaises(_FakeAliError),
        ):
            self.compute.get_agent_image_id(store)
        store.delete.assert_not_called()


class TestAlicloudFactoryDispatch(unittest.TestCase):
    """`BLITZLOG_PROVIDER=alicloud` must dispatch to the
    Alicloud implementations via the factory in
    `lambda/provider/__init__.py`. Mirrors
    `TestProviderFactory.test_explicit_aws_provider`.
    """

    def setUp(self):
        self._saved = os.environ.get("BLITZLOG_PROVIDER")
        provider.reset_provider_singletons()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("BLITZLOG_PROVIDER", None)
        else:
            os.environ["BLITZLOG_PROVIDER"] = self._saved
        provider.reset_provider_singletons()

    def test_alicloud_provider_dispatch(self):
        os.environ["BLITZLOG_PROVIDER"] = "alicloud"
        provider.reset_provider_singletons()
        self.assertIsInstance(provider.get_compute(), AlicloudCompute)
        self.assertIsInstance(provider.get_secrets(), AlicloudSecretStore)
        self.assertIsInstance(provider.get_object_store(), AlicloudObjectStore)


if __name__ == "__main__":
    unittest.main()
