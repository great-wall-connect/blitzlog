"""Fake provider implementations for unit tests.

`tests/test_provider_base.py` uses these fakes to exercise the
ABC contract — that every abstract method is implemented and
that the interface actually round-trips the right shapes.
`tests/test_provider_aws.py` uses them as the "what an AWS
provider must be able to do" reference (the AWS tests use
`MagicMock` patches of `provider.aws._boto3_client` for the real
wire-format checks; the fakes are for the contract layer).

The fakes are minimal — every method either returns a sensible
default or records the call. They are not a complete in-memory
implementation of S3/SSM/EC2; that's `moto`'s job and is out of
scope for the #83 contract tests.
"""

from __future__ import annotations

from provider.base import (
    ComputeProvider,
    LaunchSpec,
    ObjectStore,
    SecretStore,
    SpotQuote,
)


class FakeSecretStore(SecretStore):
    """In-memory parameter store. Keys are stored verbatim; values
    are stored as strings. `delete` is a no-op for missing keys
    so the `AwsSecretStore.delete` shape is testable.
    """

    def __init__(self) -> None:
        self.params: dict[str, str] = {}
        self.calls: list[tuple[str, tuple, dict]] = []

    def get(self, name: str, decrypt: bool = True) -> str:
        self.calls.append(("get", (name,), {"decrypt": decrypt}))
        return self.params[name]

    def put(self, name: str, value: str, type: str, overwrite: bool = True) -> None:
        self.calls.append(("put", (name, value, type), {"overwrite": overwrite}))
        self.params[name] = value

    def delete(self, name: str) -> None:
        self.calls.append(("delete", (name,), {}))
        self.params.pop(name, None)

    def get_by_path(self, path: str, decrypt: bool = True) -> dict[str, str]:
        self.calls.append(("get_by_path", (path,), {"decrypt": decrypt}))
        return {
            k.rsplit("/", 1)[-1]: v
            for k, v in self.params.items()
            if k.startswith(path + "/") or k == path
        }


class FakeObjectStore(ObjectStore):
    """In-memory object store. Bytes-only; encoding is the
    caller's responsibility. `head` checks key existence without
    downloading the body.
    """

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[str, tuple, dict]] = []

    def put(self, bucket: str, key: str, body: bytes) -> None:
        self.calls.append(("put", (bucket, key, body), {}))
        self.objects[(bucket, key)] = body

    def get(self, bucket: str, key: str) -> bytes:
        self.calls.append(("get", (bucket, key), {}))
        return self.objects[(bucket, key)]

    def head(self, bucket: str, key: str) -> bool:
        self.calls.append(("head", (bucket, key), {}))
        return (bucket, key) in self.objects

    def delete(self, bucket: str, key: str) -> None:
        self.calls.append(("delete", (bucket, key), {}))
        self.objects.pop((bucket, key), None)


class FakeCompute(ComputeProvider):
    """In-memory compute provider. Records every call so tests
    can assert on the wire-format without standing up boto3.
    `launch_spot` returns a deterministic instance id derived
    from the spec; `describe_image` returns a fixed shape; the
    user-data downloader script is a tiny echo so the byte-
    size invariant is testable.
    """

    def __init__(self) -> None:
        self.launched: list[LaunchSpec] = []
        self.terminated: list[str] = []
        self.calls: list[tuple[str, tuple, dict]] = []
        self.images: dict[str, dict] = {}
        self.quotes: list[SpotQuote] = []
        self.az_subnets: dict[str, str] = {}

    def launch_spot(self, spec: LaunchSpec) -> str:
        self.calls.append(("launch_spot", (spec,), {}))
        self.launched.append(spec)
        return "i-fake0000000000000"

    def terminate(self, instance_id: str) -> None:
        self.calls.append(("terminate", (instance_id,), {}))
        self.terminated.append(instance_id)

    def describe_image(self, image_id: str) -> dict:
        self.calls.append(("describe_image", (image_id,), {}))
        return self.images.get(image_id, {"Images": [{"ImageId": image_id}]})

    def get_latest_image(self, ubuntu_version: str) -> str:
        self.calls.append(("get_latest_image", (ubuntu_version,), {}))
        return "ami-fake0000000000000"

    def spot_prices(self, instance_types: list[str]) -> list[SpotQuote]:
        self.calls.append(("spot_prices", (instance_types,), {}))
        return list(self.quotes)

    def az_subnet_map(self, vpc_id: str) -> dict[str, str]:
        self.calls.append(("az_subnet_map", (vpc_id,), {}))
        return dict(self.az_subnets)

    def get_instance_profile_arn(self, profile_name: str) -> str:
        self.calls.append(("get_instance_profile_arn", (profile_name,), {}))
        return f"arn:aws:iam::000000000000:instance-profile/{profile_name}"

    def build_user_data_downloader_script(self, bucket: str, key: str) -> str:
        self.calls.append(("build_user_data_downloader_script", (bucket, key), {}))
        return f"#!/bin/bash\necho fake-downloader bucket={bucket} key={key}\n"

    def region(self) -> str:
        self.calls.append(("region", (), {}))
        return "fake-region-1"


__all__ = ["FakeCompute", "FakeObjectStore", "FakeSecretStore"]
