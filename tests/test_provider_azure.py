"""Smoke tests for the Azure concrete implementation of the
provider abstraction (issue #84 Phase 2).

Phase 2 / #84 ships the Azure *factory* entry — every method
on `AzureCompute` / `AzureSecretStore` / `AzureObjectStore`
still raises `NotImplementedError`; Phase 3 fills the bodies
against Key Vault + Blob Storage + Azure VM.

The deep wire-format coverage of the Azure classes will land
in `tests/test_provider_azure_wire.py` (or similar) when
Phase 3 ships, mirroring the AWS / Alicloud wire-format
suites. Today this file covers only:

  * The factory wires the right classes for
    `BLITZLOG_PROVIDER=azure`.
  * Every method on every Azure class raises
    `NotImplementedError` so a misconfigured deploy fails
    loudly with a pointer at the follow-up issue rather than
    silently returning a half-implemented provider.
"""

import unittest

import provider
from provider.azure import (
    AzureCompute,
    AzureObjectStore,
    AzureSecretStore,
)
from provider.base import LaunchSpec


class TestAzureFactoryDispatch(unittest.TestCase):
    """`BLITZLOG_PROVIDER=azure` must dispatch to the Azure
    implementations via the factory in
    `lambda/provider/__init__.py`. Mirrors the
    `test_explicit_aws_provider` /
    `test_alicloud_provider_dispatch` cases in
    `test_provider_base.py` / `test_provider_alicloud.py`.
    """

    def setUp(self):
        self._saved = {}
        for key in ("BLITZLOG_PROVIDER",):
            self._saved[key] = __import__("os").environ.get(key)
        provider.reset_provider_singletons()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                __import__("os").environ.pop(key, None)
            else:
                __import__("os").environ[key] = value
        provider.reset_provider_singletons()

    def test_azure_provider_returns_azure_classes(self):
        __import__("os").environ["BLITZLOG_PROVIDER"] = "azure"
        provider.reset_provider_singletons()
        self.assertIsInstance(provider.get_compute(), AzureCompute)
        self.assertIsInstance(provider.get_secrets(), AzureSecretStore)
        self.assertIsInstance(provider.get_object_store(), AzureObjectStore)

    def test_azure_singletons_are_memoised(self):
        __import__("os").environ["BLITZLOG_PROVIDER"] = "azure"
        provider.reset_provider_singletons()
        self.assertIs(provider.get_compute(), provider.get_compute())
        self.assertIs(provider.get_secrets(), provider.get_secrets())
        self.assertIs(provider.get_object_store(), provider.get_object_store())


class TestAzureStubsRaise(unittest.TestCase):
    """Every method on every Azure class must raise
    `NotImplementedError` so a misconfigured deploy fails
    loudly with a pointer at the follow-up issue.

    Phase 3 ships the bodies; the tests are kept narrow so a
    Phase 3 implementation can replace this whole class with
    the proper wire-format coverage.
    """

    def test_secret_store_methods_raise(self):
        store = AzureSecretStore()
        for method, args in (
            ("get", ("/blitzlog/prod/foo",)),
            ("put", ("/blitzlog/prod/foo", "v", "Secure")),
            ("delete", ("/blitzlog/prod/foo",)),
            ("get_by_path", ("/blitzlog/prod/local-llm",)),
        ):
            with self.subTest(method=method), self.assertRaises(NotImplementedError):
                getattr(store, method)(*args)

    def test_object_store_methods_raise(self):
        store = AzureObjectStore()
        for method, args in (
            ("put", ("b", "k", b"body")),
            ("get", ("b", "k")),
            ("head", ("b", "k")),
            ("delete", ("b", "k")),
        ):
            with self.subTest(method=method), self.assertRaises(NotImplementedError):
                getattr(store, method)(*args)

    def test_compute_methods_raise(self):
        compute = AzureCompute()
        spec = LaunchSpec(
            image_id="m-bp1",
            instance_type="Standard_B2s",
            user_data_b64="Zm9v",
            subnet_id="subnet-1",
            security_group_id="sg-1",
            instance_profile_name="my-role",
            volume_size_gb=30,
        )
        for method, args, kwargs in (
            ("launch_spot", (spec,), {}),
            ("terminate", ("i-bp1abc",), {}),
            ("describe_image", ("m-bp1",), {}),
            ("get_latest_image", ("26.04",), {}),
            ("spot_prices", (["Standard_B2s"],), {}),
            ("az_subnet_map", ("vpc-1",), {}),
            ("get_instance_profile_arn", ("my-role",), {}),
            (
                "build_user_data_downloader_script",
                (),
                {"bucket": "b", "key": "k"},
            ),
            ("region", (), {}),
        ):
            with self.subTest(method=method), self.assertRaises(NotImplementedError):
                getattr(compute, method)(*args, **kwargs)


if __name__ == "__main__":
    unittest.main()
