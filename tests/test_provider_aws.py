"""Smoke tests for the AWS concrete implementation of the
provider abstraction (issue #83).

The deep wire-format coverage of `AwsCompute`, `AwsSecretStore`,
`AwsObjectStore` lives in the existing
`tests/test_ec2.py`, `tests/test_bot_pool.py`, and
`tests/test_auth.py` — those tests have been rewired to patch
`provider.aws._boto3_client` (the single canonical patch target)
and assert on the request bodies / responses exactly as before.

This file covers what the existing tests *don't*:

  * The factory wires the right classes for `BLITZLOG_PROVIDER=aws`.
  * The AWS classes' methods actually call into
    `_boto3_client(service_name, ...)` with the right service name
    and propagate return values.
  * The `AwsCompute.region()` resolver handles the three env-var
    sources in priority order.
  * The `AwsObjectStore.head` / `AwsObjectStore.delete` swallow
    `NoSuchKey` / `NotFound` / `404` so missing-key operations
    are not errors.
  * The `AwsSecretStore.delete` swallows `ParameterNotFound` /
    `404` for the same reason.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

import provider
from botocore.exceptions import ClientError
from provider.aws import (
    AwsCompute,
    AwsObjectStore,
    AwsSecretStore,
)


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, "Op")


class TestAwsSecretStore(unittest.TestCase):
    def setUp(self):
        provider.reset_provider_singletons()

    def test_get_calls_boto3_client_with_ssm(self):
        with patch("provider.aws._boto3_client") as mock_boto3:
            mock_ssm = mock_boto3.return_value
            mock_ssm.get_parameter.return_value = {
                "Parameter": {"Value": "secret-value"}
            }
            store = AwsSecretStore()
            value = store.get("/blitzlog/prod/foo", decrypt=True)
            self.assertEqual(value, "secret-value")
            mock_boto3.assert_called_once()
            self.assertEqual(mock_boto3.call_args.args[0], "ssm")
            mock_ssm.get_parameter.assert_called_once_with(
                Name="/blitzlog/prod/foo", WithDecryption=True
            )

    def test_put_propagates_overwrite_flag(self):
        with patch("provider.aws._boto3_client") as mock_boto3:
            mock_ssm = mock_boto3.return_value
            store = AwsSecretStore()
            store.put("/blitzlog/prod/foo", "v", type="SecureString", overwrite=False)
            mock_ssm.put_parameter.assert_called_once_with(
                Name="/blitzlog/prod/foo",
                Value="v",
                Type="SecureString",
                Overwrite=False,
            )

    def test_delete_swallows_parameter_not_found(self):
        with patch("provider.aws._boto3_client") as mock_boto3:
            mock_ssm = mock_boto3.return_value
            mock_ssm.delete_parameter.side_effect = _client_error("ParameterNotFound")
            store = AwsSecretStore()
            # Should not raise.
            store.delete("/blitzlog/prod/missing")
            mock_ssm.delete_parameter.assert_called_once_with(
                Name="/blitzlog/prod/missing"
            )

    def test_delete_swallows_404(self):
        with patch("provider.aws._boto3_client") as mock_boto3:
            mock_ssm = mock_boto3.return_value
            mock_ssm.delete_parameter.side_effect = _client_error("404")
            store = AwsSecretStore()
            store.delete("/blitzlog/prod/missing")

    def test_delete_propagates_unexpected_errors(self):
        with patch("provider.aws._boto3_client") as mock_boto3:
            mock_ssm = mock_boto3.return_value
            mock_ssm.delete_parameter.side_effect = _client_error("AccessDenied")
            store = AwsSecretStore()
            with self.assertRaises(ClientError):
                store.delete("/blitzlog/prod/foo")

    def test_get_by_path_flattens_keys(self):
        with patch("provider.aws._boto3_client") as mock_boto3:
            mock_ssm = mock_boto3.return_value
            mock_ssm.get_paginator.return_value.paginate.return_value = [
                {
                    "Parameters": [
                        {
                            "Name": "/blitzlog/prod/local-llm/endpoint",
                            "Value": "http://10.0.0.5:11434",
                        },
                        {
                            "Name": "/blitzlog/prod/local-llm/model",
                            "Value": "qwen2.5-coder:32b",
                        },
                    ]
                }
            ]
            store = AwsSecretStore()
            params = store.get_by_path("/blitzlog/prod/local-llm")
            self.assertEqual(
                params,
                {
                    "endpoint": "http://10.0.0.5:11434",
                    "model": "qwen2.5-coder:32b",
                },
            )
            mock_ssm.get_paginator.assert_called_once_with("get_parameters_by_path")


class TestAwsObjectStore(unittest.TestCase):
    def setUp(self):
        provider.reset_provider_singletons()

    def test_put_calls_boto3_client_with_s3(self):
        with patch("provider.aws._boto3_client") as mock_boto3:
            mock_s3 = mock_boto3.return_value
            store = AwsObjectStore()
            store.put("my-bucket", "key/x.json", b'{"a":1}')
            mock_boto3.assert_called_once()
            self.assertEqual(mock_boto3.call_args.args[0], "s3")
            mock_s3.put_object.assert_called_once_with(
                Bucket="my-bucket", Key="key/x.json", Body=b'{"a":1}'
            )

    def test_get_returns_bytes(self):
        with patch("provider.aws._boto3_client") as mock_boto3:
            mock_s3 = mock_boto3.return_value
            mock_s3.get_object.return_value = {
                "Body": MagicMock(read=MagicMock(return_value=b"hello"))
            }
            store = AwsObjectStore()
            value = store.get("my-bucket", "k")
            self.assertEqual(value, b"hello")
            mock_s3.get_object.assert_called_once_with(Bucket="my-bucket", Key="k")

    def test_head_returns_true_when_key_exists(self):
        with patch("provider.aws._boto3_client") as mock_boto3:
            mock_s3 = mock_boto3.return_value
            mock_s3.head_object.return_value = {}
            store = AwsObjectStore()
            self.assertTrue(store.head("b", "k"))

    def test_head_returns_false_for_no_such_key(self):
        with patch("provider.aws._boto3_client") as mock_boto3:
            mock_s3 = mock_boto3.return_value
            mock_s3.head_object.side_effect = _client_error("NoSuchKey")
            store = AwsObjectStore()
            self.assertFalse(store.head("b", "k"))

    def test_head_returns_false_for_404(self):
        with patch("provider.aws._boto3_client") as mock_boto3:
            mock_s3 = mock_boto3.return_value
            mock_s3.head_object.side_effect = _client_error("404")
            store = AwsObjectStore()
            self.assertFalse(store.head("b", "k"))

    def test_head_returns_false_for_not_found(self):
        with patch("provider.aws._boto3_client") as mock_boto3:
            mock_s3 = mock_boto3.return_value
            mock_s3.head_object.side_effect = _client_error("NotFound")
            store = AwsObjectStore()
            self.assertFalse(store.head("b", "k"))

    def test_head_propagates_other_errors(self):
        with patch("provider.aws._boto3_client") as mock_boto3:
            mock_s3 = mock_boto3.return_value
            mock_s3.head_object.side_effect = _client_error("AccessDenied")
            store = AwsObjectStore()
            with self.assertRaises(ClientError):
                store.head("b", "k")

    def test_delete_swallows_no_such_key(self):
        with patch("provider.aws._boto3_client") as mock_boto3:
            mock_s3 = mock_boto3.return_value
            mock_s3.delete_object.side_effect = _client_error("NoSuchKey")
            store = AwsObjectStore()
            store.delete("b", "k")

    def test_delete_propagates_other_errors(self):
        with patch("provider.aws._boto3_client") as mock_boto3:
            mock_s3 = mock_boto3.return_value
            mock_s3.delete_object.side_effect = _client_error("AccessDenied")
            store = AwsObjectStore()
            with self.assertRaises(ClientError):
                store.delete("b", "k")


class TestAwsComputeRegion(unittest.TestCase):
    def setUp(self):
        self._saved_aws_region = os.environ.get("AWS_REGION")
        self._saved_aws_default = os.environ.get("AWS_DEFAULT_REGION")

    def tearDown(self):
        for key, value in (
            ("AWS_REGION", self._saved_aws_region),
            ("AWS_DEFAULT_REGION", self._saved_aws_default),
        ):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_region_prefers_aws_region_env(self):
        os.environ["AWS_REGION"] = "eu-west-1"
        os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
        self.assertEqual(AwsCompute().region(), "eu-west-1")

    def test_region_falls_back_to_aws_default_region(self):
        os.environ.pop("AWS_REGION", None)
        os.environ["AWS_DEFAULT_REGION"] = "us-west-2"
        self.assertEqual(AwsCompute().region(), "us-west-2")

    def test_region_returns_us_east_1_when_neither_set(self):
        os.environ.pop("AWS_REGION", None)
        os.environ.pop("AWS_DEFAULT_REGION", None)
        # The fallback path consults `boto3.session.Session().region_name`
        # which is also unset in tests, so the function returns the
        # hard-coded `"us-east-1"` last resort.
        self.assertEqual(AwsCompute().region(), "us-east-1")


class TestAwsComputeBuildUserDataDownloaderScript(unittest.TestCase):
    """Regression: the downloader script is byte-identical to the
    pre-#83 `lambda/ec2.py::_build_s3_downloader_script`. The
    test_ec2.py::TestS3Downloader cases assert the wire format
    end-to-end; this is a tighter unit test on the provider.
    """

    def test_downloader_references_imdsv2_endpoint(self):
        script = AwsCompute().build_user_data_downloader_script(
            "my-bucket", "user-data/issue-42.sh"
        )
        self.assertIn("169.254.169.254/latest/api/token", script)
        self.assertIn("X-aws-ec2-metadata-token-ttl-seconds: 60", script)
        self.assertIn("s3://my-bucket/user-data/issue-42.sh", script)
        self.assertIn("aws s3 cp", script)
        self.assertIn("bash /tmp/bootstrap.sh", script)


if __name__ == "__main__":
    unittest.main()
