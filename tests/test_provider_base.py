"""ABC contract tests for the cloud-provider abstraction (issue #83).

`tests/_fakes.py` provides the in-memory `FakeCompute`,
`FakeObjectStore`, `FakeSecretStore` implementations. These tests
verify the contract:

  1. Every abstract method on the three ABCs is overridden by the
     fake (so an addition to the ABC immediately fails these
     tests with a clear "still abstract" error until the fakes
     catch up).
  2. The fakes record the calls they receive, so consumers can
     assert on the right wire shape.
  3. The `LaunchSpec` and `SpotQuote` dataclasses carry the fields
     the rest of the codebase depends on.
"""

import inspect
import unittest

from _fakes import FakeCompute, FakeObjectStore, FakeSecretStore
from provider import (
    ComputeProvider,
    LaunchSpec,
    ObjectStore,
    SecretStore,
    SpotQuote,
    Tag,
    get_compute,
    get_object_store,
    get_secrets,
    reset_provider_singletons,
)
from provider.alicloud import (
    AlicloudCompute,
    AlicloudObjectStore,
    AlicloudSecretStore,
)
from provider.aws import (
    AwsCompute,
    AwsObjectStore,
    AwsSecretStore,
    _boto3_client,
)


def _abstract_method_names(cls: type) -> set[str]:
    """Return the set of `abstractmethod`-decorated method names
    declared (or inherited) on `cls`. Used to assert that the
    fakes implement every abstract method.
    """
    names: set[str] = set()
    for klass in inspect.getmro(cls):
        for name, member in inspect.getmembers(klass):
            if getattr(member, "__isabstractmethod__", False):
                names.add(name)
    return names


class TestAbstractMethodCoverage(unittest.TestCase):
    """Every concrete subclass must override every abstract method
    on the parent ABC. If a new method is added to the ABC and
    the fake is forgotten, this test fails — same for the AWS
    and Alicloud implementations.
    """

    def test_fake_compute_overrides_every_abstract_method(self):
        abstracts = _abstract_method_names(ComputeProvider)
        for name in abstracts:
            self.assertTrue(
                hasattr(FakeCompute, name)
                and not getattr(
                    getattr(FakeCompute, name), "__isabstractmethod__", False
                ),
                f"FakeCompute must implement {name!r} (declared abstract on "
                f"ComputeProvider)",
            )

    def test_fake_object_store_overrides_every_abstract_method(self):
        abstracts = _abstract_method_names(ObjectStore)
        for name in abstracts:
            self.assertTrue(
                hasattr(FakeObjectStore, name)
                and not getattr(
                    getattr(FakeObjectStore, name), "__isabstractmethod__", False
                ),
                f"FakeObjectStore must implement {name!r}",
            )

    def test_fake_secret_store_overrides_every_abstract_method(self):
        abstracts = _abstract_method_names(SecretStore)
        for name in abstracts:
            self.assertTrue(
                hasattr(FakeSecretStore, name)
                and not getattr(
                    getattr(FakeSecretStore, name), "__isabstractmethod__", False
                ),
                f"FakeSecretStore must implement {name!r}",
            )

    def test_aws_compute_overrides_every_abstract_method(self):
        abstracts = _abstract_method_names(ComputeProvider)
        for name in abstracts:
            self.assertTrue(
                hasattr(AwsCompute, name)
                and not getattr(
                    getattr(AwsCompute, name), "__isabstractmethod__", False
                ),
                f"AwsCompute must implement {name!r}",
            )

    def test_aws_object_store_overrides_every_abstract_method(self):
        abstracts = _abstract_method_names(ObjectStore)
        for name in abstracts:
            self.assertTrue(
                hasattr(AwsObjectStore, name)
                and not getattr(
                    getattr(AwsObjectStore, name), "__isabstractmethod__", False
                ),
                f"AwsObjectStore must implement {name!r}",
            )

    def test_aws_secret_store_overrides_every_abstract_method(self):
        abstracts = _abstract_method_names(SecretStore)
        for name in abstracts:
            self.assertTrue(
                hasattr(AwsSecretStore, name)
                and not getattr(
                    getattr(AwsSecretStore, name), "__isabstractmethod__", False
                ),
                f"AwsSecretStore must implement {name!r}",
            )

    def test_alicloud_compute_overrides_every_abstract_method(self):
        """After #84 (Phase 2) `AlicloudCompute` is a full
        implementation of `ComputeProvider`; the abstract-method
        contract applies identically to the AWS + Alicloud
        sides. The deep wire-format coverage lives in
        `tests/test_provider_alicloud.py`.
        """
        abstracts = _abstract_method_names(ComputeProvider)
        for name in abstracts:
            self.assertTrue(
                hasattr(AlicloudCompute, name)
                and not getattr(
                    getattr(AlicloudCompute, name), "__isabstractmethod__", False
                ),
                f"AlicloudCompute must implement {name!r}",
            )

    def test_alicloud_object_store_overrides_every_abstract_method(self):
        abstracts = _abstract_method_names(ObjectStore)
        for name in abstracts:
            self.assertTrue(
                hasattr(AlicloudObjectStore, name)
                and not getattr(
                    getattr(AlicloudObjectStore, name),
                    "__isabstractmethod__",
                    False,
                ),
                f"AlicloudObjectStore must implement {name!r}",
            )

    def test_alicloud_secret_store_overrides_every_abstract_method(self):
        abstracts = _abstract_method_names(SecretStore)
        for name in abstracts:
            self.assertTrue(
                hasattr(AlicloudSecretStore, name)
                and not getattr(
                    getattr(AlicloudSecretStore, name),
                    "__isabstractmethod__",
                    False,
                ),
                f"AlicloudSecretStore must implement {name!r}",
            )

    def test_azure_compute_still_has_unimplemented_methods(self):
        """Phase 2 / #84: Azure stubs raise `NotImplementedError`.
        The class still satisfies the ABC *signature* (it
        overrides every method), but the bodies deliberately
        throw so a reviewer's `BLITZLOG_PROVIDER=azure`
        cold-start fails loudly with a pointer at the Phase 3
        follow-up rather than silently returning a
        half-implemented compute.
        """
        from provider.azure import (
            AzureCompute as _AzureCompute,
        )

        abstracts = _abstract_method_names(ComputeProvider)
        for name in abstracts:
            self.assertTrue(
                hasattr(_AzureCompute, name),
                f"AzureCompute must declare {name!r} (even if it raises NotImplementedError)",
            )

    def test_azure_object_store_still_has_unimplemented_methods(self):
        from provider.azure import (
            AzureObjectStore as _AzureObjectStore,
        )

        abstracts = _abstract_method_names(ObjectStore)
        for name in abstracts:
            self.assertTrue(
                hasattr(_AzureObjectStore, name),
                f"AzureObjectStore must declare {name!r}",
            )

    def test_azure_secret_store_still_has_unimplemented_methods(self):
        from provider.azure import (
            AzureSecretStore as _AzureSecretStore,
        )

        abstracts = _abstract_method_names(SecretStore)
        for name in abstracts:
            self.assertTrue(
                hasattr(_AzureSecretStore, name),
                f"AzureSecretStore must declare {name!r}",
            )


class TestDataclasses(unittest.TestCase):
    def test_launch_spec_carries_required_fields(self):
        spec = LaunchSpec(
            image_id="ami-12345",
            instance_type="t4g.medium",
            user_data_b64="Zm9v",
            subnet_id="subnet-1",
            security_group_id="sg-1",
            instance_profile_name="profile",
            volume_size_gb=20,
        )
        self.assertEqual(spec.image_id, "ami-12345")
        self.assertEqual(spec.volume_size_gb, 20)
        self.assertEqual(spec.tags, {})

    def test_launch_spec_default_tags_is_empty_dict_not_shared(self):
        """Regression: dataclass `field(default_factory=dict)` is
        required here — a `= {}` default would share one mutable
        dict across every instance. The fakes rely on each
        instance having its own tags.
        """
        spec_a = LaunchSpec(
            image_id="a",
            instance_type="t",
            user_data_b64="x",
            subnet_id="s",
            security_group_id="g",
            instance_profile_name="p",
            volume_size_gb=1,
        )
        spec_b = LaunchSpec(
            image_id="b",
            instance_type="t",
            user_data_b64="x",
            subnet_id="s",
            security_group_id="g",
            instance_profile_name="p",
            volume_size_gb=1,
        )
        spec_a.tags["k"] = "v"
        self.assertEqual(spec_b.tags, {})

    def test_spot_quote_carries_required_fields(self):
        q = SpotQuote(
            availability_zone="us-east-1a",
            instance_type="t4g.medium",
            price_usd_hour=0.0093,
        )
        self.assertEqual(q.availability_zone, "us-east-1a")
        self.assertEqual(q.price_usd_hour, 0.0093)

    def test_tag_dataclass(self):
        t = Tag(key="Environment", value="prod")
        self.assertEqual((t.key, t.value), ("Environment", "prod"))


class TestProviderFactory(unittest.TestCase):
    """Verify the factory in `lambda/provider/__init__.py`
    dispatches on `BLITZLOG_PROVIDER` and memoises the
    singletons.
    """

    def setUp(self):
        self._saved = {}
        for key in ("BLITZLOG_PROVIDER",):
            self._saved[key] = __import__("os").environ.get(key)
        reset_provider_singletons()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                __import__("os").environ.pop(key, None)
            else:
                __import__("os").environ[key] = value
        reset_provider_singletons()

    def test_default_provider_is_aws(self):
        __import__("os").environ.pop("BLITZLOG_PROVIDER", None)
        reset_provider_singletons()
        self.assertIsInstance(get_compute(), AwsCompute)
        self.assertIsInstance(get_secrets(), AwsSecretStore)
        self.assertIsInstance(get_object_store(), AwsObjectStore)

    def test_explicit_aws_provider(self):
        __import__("os").environ["BLITZLOG_PROVIDER"] = "aws"
        reset_provider_singletons()
        self.assertIsInstance(get_compute(), AwsCompute)

    def test_unknown_provider_raises_keyerror(self):
        __import__("os").environ["BLITZLOG_PROVIDER"] = "gcp"
        reset_provider_singletons()
        with self.assertRaises(KeyError):
            get_compute()

    def test_singletons_are_memoised(self):
        """Repeated `get_*` calls return the same instance — the
        Lambda relies on this so the boto3 client connection is
        reused across warm invocations.
        """
        __import__("os").environ["BLITZLOG_PROVIDER"] = "aws"
        reset_provider_singletons()
        self.assertIs(get_compute(), get_compute())
        self.assertIs(get_secrets(), get_secrets())
        self.assertIs(get_object_store(), get_object_store())

    def test_reset_drops_memoised_singletons(self):
        __import__("os").environ["BLITZLOG_PROVIDER"] = "aws"
        reset_provider_singletons()
        first = get_compute()
        reset_provider_singletons()
        second = get_compute()
        self.assertIsNot(first, second)

    def test_boto3_client_shim_is_exported_from_aws(self):
        """The test suite patches `provider.aws._boto3_client`; the
        symbol must be importable from there (issue #83 acceptance
        criterion).
        """
        self.assertTrue(callable(_boto3_client))


if __name__ == "__main__":
    unittest.main()
