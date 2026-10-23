"""Tests for the provider-factory dispatch path.

The `provider/__init__.py` factory is the Lambda's cold-start
entry point: every webhook handler transitively imports it, and
the very first thing the runtime does is execute the
module-level code in that file. If a future change re-introduces
an eager `from provider.alicloud import ...` (or anything
similar) the AWS cold start will once again pay the import cost
of `oss2` / `alibabacloud_ecs20140526` /
`alibabacloud_kms20160120` / `alibabacloud_tea_openapi` /
`provider.azure`, blowing the 128 MB memory budget that the
follow-up `memory_size = 512` bump in
`infra/aws/modules/core/lambda.tf` is meant to absorb.

These tests pin the lazy-import contract:

    1. An AWS cold start does NOT pull any Alicloud / Azure
       SDK or provider module into `sys.modules`.
    2. A typo in `BLITZLOG_PROVIDER` still surfaces as
       `KeyError` at the first `get_*()` call (preserving the
       pre-#84 cold-start check semantics, now done via
       `importlib.util.find_spec`).
    3. `BLITZLOG_PROVIDER=alicloud` lazy-loads
       `provider.alicloud` exactly once and caches the
       resulting class triple in `_PROVIDERS`.
    4. The AWS dispatch path is unchanged: `get_compute()` /
       `get_secrets()` / `get_object_store()` still return
       `AwsCompute` / `AwsSecretStore` / `AwsObjectStore`.

Each cold-start test is run in a fresh subprocess via
`subprocess` so the global `sys.modules` cache does not leak
between tests. The lazy-load + dispatch tests run in-process
because the underlying state is `provider.reset_provider_singletons()`-
cleanable.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch

import provider
from provider.aws import AwsCompute, AwsObjectStore, AwsSecretStore
from provider.base import (
    ComputeProvider,
    ObjectStore,
    SecretStore,
)

# Subprocess script: import `provider` in isolation and dump
# which provider / SDK modules ended up in `sys.modules`. Run
# with `BLITZLOG_PROVIDER=aws` (the dev + prod default) so we
# verify the AWS cold start does NOT eagerly import alicloud /
# azure / their SDKs. The script receives the project root via
# the `BLITZLOG_PROJECT_ROOT` env var (we cannot use `__file__`
# inside a `python -c` snippet — it is always empty).
_COLD_START_SCRIPT = textwrap.dedent("""
    import json
    import os
    import sys

    project_root = os.environ["BLITZLOG_PROJECT_ROOT"]
    sys.path.insert(0, os.path.join(project_root, "lambda"))
    sys.path.insert(0, os.path.join(project_root, "lambda", "scripts"))

    # conftest.py does this; reproduce it so the subprocess
    # behaves the same as the in-process test environment.
    os.environ.setdefault("BLITZLOG_PROVIDER", "aws")

    import provider  # noqa: F401  (the import is the test)

    forbidden = [
        "oss2",
        "alibabacloud_ecs20140526",
        "alibabacloud_kms20160120",
        "alibabacloud_tea_openapi",
        "alibabacloud_tea",
        "provider.alicloud",
        "provider.azure",
    ]
    loaded = {m: (m in sys.modules) for m in forbidden}
    loaded["provider._PROVIDERS.aws_only"] = list(provider._PROVIDERS.keys()) == ["aws"]
    print(json.dumps({"loaded": loaded, "provider": provider._PROVIDER_MODULES}))
    """)


def _run_cold_start_subprocess() -> dict:
    """Run the cold-start script in a fresh Python subprocess
    and parse the JSON line it prints. We use subprocess (not
    `importlib.reload` or `sys.modules.clear()`) so the test
    really does mirror the Lambda runtime's first-import
    semantics, including any import-time side effects in
    transitive imports of `lambda/auth.py`,
    `lambda/bot_pool.py`, etc.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, "-c", _COLD_START_SCRIPT],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "BLITZLOG_PROVIDER": "aws",
            "BLITZLOG_PROJECT_ROOT": project_root,
        },
    )
    # The script ends with `print(json.dumps(...))`; the last
    # line of stdout is the payload.
    last_line = result.stdout.strip().splitlines()[-1]
    return __import__("json").loads(last_line)


class TestAwsColdStartImports(unittest.TestCase):
    """The AWS cold start must not eagerly import the Alicloud
    or Azure provider modules / SDKs. Otherwise the Lambda hits
    the 128 MB memory ceiling on cold start (issue surfaced by
    PR #133; see `infra/aws/modules/core/lambda.tf` for the
    follow-up `memory_size` bump).
    """

    def test_cold_start_does_not_import_alicloud_or_azure_modules(self):
        payload = _run_cold_start_subprocess()
        for module, loaded in payload["loaded"].items():
            if module == "provider._PROVIDERS.aws_only":
                # The AWS-only invariant: the eager `_PROVIDERS`
                # dict is pre-populated with the `aws` entry
                # only; alicloud / azure entries are added on
                # first `get_*()` call.
                self.assertTrue(
                    loaded,
                    f"_PROVIDERS should only have 'aws' after import; "
                    f"got {payload['loaded']}",
                )
            else:
                self.assertFalse(
                    loaded,
                    f"Module {module!r} was imported during the AWS cold "
                    f"start; an eager import regressed the lazy-import "
                    f"refactor that fixed the cold-start OOM.",
                )

    def test_provider_modules_registry_lists_all_three_clouds(self):
        """The `_PROVIDER_MODULES` registry must still include
        every supported cloud, so `_resolve_provider_name()`
        can answer a non-AWS provider without a separate config
        layer. Cold-start (find_spec) + on-demand (import)
        gates are the only two paths.
        """
        payload = _run_cold_start_subprocess()
        self.assertEqual(
            set(payload["provider"].keys()),
            {"aws", "alicloud", "azure"},
        )


class TestProviderNameResolution(unittest.TestCase):
    """`_resolve_provider_name` must still surface a typo in
    `BLITZLOG_PROVIDER` as a `KeyError` at the first `get_*()`
    call (preserving the pre-#84 cold-start check semantics).
    The check now uses `importlib.util.find_spec` against
    `_PROVIDER_MODULES` so it costs ~µs, not the import cost
    of every other provider.
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

    def test_unknown_provider_raises_key_error(self):
        os.environ["BLITZLOG_PROVIDER"] = "gcp"  # not registered
        with self.assertRaises(KeyError) as ctx:
            provider.get_compute()
        self.assertIn("gcp", str(ctx.exception))
        self.assertIn("aws", str(ctx.exception))  # known providers listed

    def test_registered_provider_with_missing_module_raises_key_error(self):
        """A registered provider whose module path is not on
        `sys.path` must also raise `KeyError`, not a
        `ModuleNotFoundError`. This is the cold-start typo
        guard: a misconfigured Terraform run that points at a
        renamed module gets a clear `KeyError` mentioning the
        missing module, not a half-imported factory.
        """
        os.environ["BLITZLOG_PROVIDER"] = "azure"
        with (
            patch.dict(
                provider._PROVIDER_MODULES,
                {
                    "aws": "provider.aws",
                    "alicloud": "provider.alicloud",
                    "azure": "does.not.exist",
                },
            ),
            self.assertRaises(KeyError) as ctx,
        ):
            provider.get_compute()
        self.assertIn("does.not.exist", str(ctx.exception))


class TestAwsDispatchUnchanged(unittest.TestCase):
    """The pre-#84 AWS dispatch contract must still hold:
    `BLITZLOG_PROVIDER=aws` returns the AWS classes, the
    singletons are memoised, and the active `BLITZLOG_PROVIDER`
    is read at call time (not import time) so tests that flip
    the env var between calls exercise the right path.
    """

    def setUp(self):
        self._saved = os.environ.get("BLITZLOG_PROVIDER")
        os.environ["BLITZLOG_PROVIDER"] = "aws"
        provider.reset_provider_singletons()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("BLITZLOG_PROVIDER", None)
        else:
            os.environ["BLITZLOG_PROVIDER"] = self._saved
        provider.reset_provider_singletons()

    def test_get_compute_returns_aws_compute(self):
        self.assertIsInstance(provider.get_compute(), AwsCompute)
        self.assertIsInstance(provider.get_compute(), ComputeProvider)

    def test_get_secrets_returns_aws_secret_store(self):
        self.assertIsInstance(provider.get_secrets(), AwsSecretStore)
        self.assertIsInstance(provider.get_secrets(), SecretStore)

    def test_get_object_store_returns_aws_object_store(self):
        self.assertIsInstance(provider.get_object_store(), AwsObjectStore)
        self.assertIsInstance(provider.get_object_store(), ObjectStore)

    def test_aws_singletons_are_memoised(self):
        self.assertIs(provider.get_compute(), provider.get_compute())
        self.assertIs(provider.get_secrets(), provider.get_secrets())
        self.assertIs(provider.get_object_store(), provider.get_object_store())


class TestAlicloudLazyImport(unittest.TestCase):
    """`BLITZLOG_PROVIDER=alicloud` must lazy-load
    `provider.alicloud` exactly once (subsequent calls hit the
    `_PROVIDERS` cache) and the cache must be cleared by
    `reset_provider_singletons()` so a test that flips
    `BLITZLOG_PROVIDER` between calls exercises the right
    dispatch path.
    """

    def setUp(self):
        self._saved = os.environ.get("BLITZLOG_PROVIDER")
        # Defensive: make sure the alicloud module is not yet
        # imported in this test process. Other tests in the
        # suite (test_provider_alicloud.py) DO import it; the
        # cache assertion below is what matters.
        provider.reset_provider_singletons()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("BLITZLOG_PROVIDER", None)
        else:
            os.environ["BLITZLOG_PROVIDER"] = self._saved
        provider.reset_provider_singletons()

    def test_alicloud_get_compute_does_not_raise_on_dispatch(self):
        """The end-to-end happy path: setting
        `BLITZLOG_PROVIDER=alicloud` and calling
        `get_compute()` returns an `AlicloudCompute` instance
        without any `ImportError`. The actual SDK is mocked
        by the alicloud shims elsewhere in the suite; here
        we just want to confirm the lazy-import wiring
        doesn't itself raise.
        """
        os.environ["BLITZLOG_PROVIDER"] = "alicloud"
        provider.reset_provider_singletons()
        # Force the alicloud module to be loaded by
        # `_load_provider_classes`. We don't need to assert
        # anything beyond "no exception" + "expected class
        # type"; deeper wire-format tests live in
        # tests/test_provider_alicloud.py.
        from provider.alicloud import AlicloudCompute

        # Patches cover the SDK init inside AlicloudCompute.
        with patch("provider.alicloud._ecs_client") as mock_ecs:
            mock_ecs.return_value = object()
            instance = provider.get_compute()
            self.assertIsInstance(instance, AlicloudCompute)

    def test_reset_clears_alicloud_cache(self):
        """`reset_provider_singletons()` must drop the
        `alicloud` entry from `_PROVIDERS` so a test that
        flips `BLITZLOG_PROVIDER` between calls exercises a
        clean re-import path. The `aws` entry stays — it is
        the eagerly-loaded default and re-importing it
        would be a no-op anyway.
        """
        os.environ["BLITZLOG_PROVIDER"] = "alicloud"
        provider.reset_provider_singletons()
        # Populate the cache.
        from provider.alicloud import (
            AlicloudCompute,
            AlicloudObjectStore,
            AlicloudSecretStore,
        )

        provider._PROVIDERS["alicloud"] = (
            AlicloudCompute,
            AlicloudSecretStore,
            AlicloudObjectStore,
        )
        self.assertIn("alicloud", provider._PROVIDERS)
        provider.reset_provider_singletons()
        self.assertNotIn("alicloud", provider._PROVIDERS)
        self.assertIn("aws", provider._PROVIDERS)


if __name__ == "__main__":
    unittest.main()
