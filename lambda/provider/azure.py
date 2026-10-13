"""Azure implementation of the provider abstraction.

Phase 2 / #84: stub only. Every method on `AzureCompute`,
`AzureSecretStore`, and `AzureObjectStore` raises
`NotImplementedError` pointing at the follow-up issue so a
reviewer can navigate from the error message to the
implementation plan.

The factory in `lambda/provider/__init__.py` knows the name
(`"azure"`) and imports this module unconditionally so a typo
in the provider name surfaces as `KeyError` at cold start,
not as a half-initialised factory at request time.

Out of scope here (Phase 3, per the #84 issue body):

    * `AzureSecretStore.get` / `put` / `delete` / `get_by_path`
      against Key Vault. The `get_by_path` signature is
      documented as returning a client-side filtered dict
      (Key Vault has no native path-listing), but the body is
      `NotImplementedError`.
    * `AzureObjectStore.put` / `get` / `head` / `delete` against
      Blob Storage. Wire shape mirrors the ABC.
    * `AzureCompute.launch_spot` / `terminate` /
      `describe_image` / `get_latest_image` / `spot_prices` /
      `az_subnet_map` / `get_instance_profile_arn` /
      `build_user_data_downloader_script` / `region` against
      Azure VM / Compute Gallery / Spot + the
      `169.254.169.254` IMDS.

No new SDK dependencies are pulled in for Phase 2 — the
`azure-identity` / `azure-keyvault-secrets` /
`azure-storage-blob` / `azure-mgmt-compute` packages land
when Phase 3 ships the implementations.
"""

from __future__ import annotations

from provider.base import (
    ComputeProvider,
    LaunchSpec,
    ObjectStore,
    SecretStore,
    SpotQuote,
)

# Placeholder so Phase 3 has a single site to thread the SDK
# import-time side effects through; today the value is never
# observed (every method raises `NotImplementedError`).
_NOT_IMPLEMENTED = "Phase 3: see <follow-up>"


class AzureSecretStore(SecretStore):
    """Azure Key Vault adapter. Phase 3 ships the bodies. The
    `get_by_path` signature mirrors `AwsSecretStore.get_by_path`
    (Key Vault has no native path-listing so the Phase 3
    implementation will paginate `list_secrets` and filter
    client-side, exactly like the Alicloud equivalent).
    """

    def get(self, name: str, decrypt: bool = True) -> str:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def put(self, name: str, value: str, type: str, overwrite: bool = True) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def delete(self, name: str) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def get_by_path(self, path: str, decrypt: bool = True) -> dict[str, str]:
        raise NotImplementedError(_NOT_IMPLEMENTED)


class AzureObjectStore(ObjectStore):
    """Azure Blob Storage adapter. Phase 3 ships the bodies."""

    def put(self, bucket: str, key: str, body: bytes) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def get(self, bucket: str, key: str) -> bytes:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def head(self, bucket: str, key: str) -> bool:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def delete(self, bucket: str, key: str) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)


class AzureCompute(ComputeProvider):
    """Azure VM + Managed Identity adapter. Phase 3 ships the
    bodies. Owns spot launch, image lookup, the user-data
    downloader (replacing `aws s3 cp` with `az storage blob
    download` and IMDSv2 with the Azure metadata endpoint at
    `169.254.169.254`).
    """

    def launch_spot(self, spec: LaunchSpec) -> str:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def terminate(self, instance_id: str) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def describe_image(self, image_id: str) -> dict:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def get_latest_image(self, ubuntu_version: str) -> str:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def spot_prices(self, instance_types: list[str]) -> list[SpotQuote]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def az_subnet_map(self, vpc_id: str) -> dict[str, str]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def get_instance_profile_arn(self, profile_name: str) -> str:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def build_user_data_downloader_script(self, bucket: str, key: str) -> str:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def region(self) -> str:
        raise NotImplementedError(_NOT_IMPLEMENTED)


__all__ = [
    "AzureCompute",
    "AzureObjectStore",
    "AzureSecretStore",
]
