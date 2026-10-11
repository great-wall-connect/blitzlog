"""Alicloud implementation of the provider abstraction.

Phase 1 of #83: stubs only. Every method raises
`NotImplementedError` pointing at the follow-up issue so a
reviewer can navigate from the error message to the
implementation plan. The factory in `lambda/provider/__init__.py`
already knows the name (`"alicloud"`) so Phase 3 (#124) only
fills in the bodies — the dispatch site does not change.

Two of the three classes land bodies in #84 (Phase 2):
    AlicloudSecretStore  — Aliyun KMS + Parameter Store
    AlicloudObjectStore  — Aliyun OSS

The third class, `AlicloudCompute`, lands in #124 (Phase 3).
Because Phase 3 is the heaviest of the three (it owns the spot
launch + image lookup + downloader script), the factory still
loads this module even when the class bodies are stubs so the
import-time singleton path works. A cold-start invocation with
`BLITZLOG_PROVIDER=alicloud` will fail loudly with a clear
"NotImplementedError" pointing at #124, which is exactly the
behaviour #83's acceptance criteria demand.
"""

from __future__ import annotations

from provider.base import (
    ComputeProvider,
    LaunchSpec,
    ObjectStore,
    SecretStore,
    SpotQuote,
)


class AlicloudSecretStore(SecretStore):
    """Aliyun KMS + Parameter Store adapter. Phase 2 (#84) fills
    in the bodies. The factory imports this class at cold start
    so a typo in the provider name surfaces as `KeyError`
    rather than a half-initialised factory.
    """

    def get(self, name: str, decrypt: bool = True) -> str:
        raise NotImplementedError("Alicloud SecretStore.get lands in #84 (Phase 2).")

    def put(self, name: str, value: str, type: str, overwrite: bool = True) -> None:
        raise NotImplementedError("Alicloud SecretStore.put lands in #84 (Phase 2).")

    def delete(self, name: str) -> None:
        raise NotImplementedError("Alicloud SecretStore.delete lands in #84 (Phase 2).")

    def get_by_path(self, path: str, decrypt: bool = True) -> dict[str, str]:
        raise NotImplementedError(
            "Alicloud SecretStore.get_by_path lands in #84 (Phase 2)."
        )


class AlicloudObjectStore(ObjectStore):
    """Aliyun OSS adapter. Phase 2 (#84) fills in the bodies."""

    def put(self, bucket: str, key: str, body: bytes) -> None:
        raise NotImplementedError("Alicloud ObjectStore.put lands in #84 (Phase 2).")

    def get(self, bucket: str, key: str) -> bytes:
        raise NotImplementedError("Alicloud ObjectStore.get lands in #84 (Phase 2).")

    def head(self, bucket: str, key: str) -> bool:
        raise NotImplementedError("Alicloud ObjectStore.head lands in #84 (Phase 2).")

    def delete(self, bucket: str, key: str) -> None:
        raise NotImplementedError("Alicloud ObjectStore.delete lands in #84 (Phase 2).")


class AlicloudCompute(ComputeProvider):
    """Aliyun ECS + RAM adapter. Phase 3 (#124) fills in the
    bodies. Owns spot launch, image lookup, the user-data
    downloader (replacing `aws s3 cp` with `ossutil cp` and
    IMDSv2 with the Aliyun metadata endpoint at `100.100.100.200`).
    """

    def launch_spot(self, spec: LaunchSpec) -> str:
        raise NotImplementedError(
            "Alicloud ComputeProvider.launch_spot lands in #124 (Phase 3)."
        )

    def terminate(self, instance_id: str) -> None:
        raise NotImplementedError(
            "Alicloud ComputeProvider.terminate lands in #124 (Phase 3)."
        )

    def describe_image(self, image_id: str) -> dict:
        raise NotImplementedError(
            "Alicloud ComputeProvider.describe_image lands in #124 (Phase 3)."
        )

    def get_latest_image(self, ubuntu_version: str) -> str:
        raise NotImplementedError(
            "Alicloud ComputeProvider.get_latest_image lands in #124 (Phase 3)."
        )

    def spot_prices(self, instance_types: list[str]) -> list[SpotQuote]:
        raise NotImplementedError(
            "Alicloud ComputeProvider.spot_prices lands in #124 (Phase 3)."
        )

    def az_subnet_map(self, vpc_id: str) -> dict[str, str]:
        raise NotImplementedError(
            "Alicloud ComputeProvider.az_subnet_map lands in #124 (Phase 3)."
        )

    def get_instance_profile_arn(self, profile_name: str) -> str:
        raise NotImplementedError(
            "Alicloud ComputeProvider.get_instance_profile_arn lands in #124 (Phase 3)."
        )

    def build_user_data_downloader_script(self, bucket: str, key: str) -> str:
        raise NotImplementedError(
            "Alicloud ComputeProvider.build_user_data_downloader_script "
            "lands in #124 (Phase 3)."
        )

    def region(self) -> str:
        raise NotImplementedError(
            "Alicloud ComputeProvider.region lands in #124 (Phase 3)."
        )


__all__ = [
    "AlicloudCompute",
    "AlicloudObjectStore",
    "AlicloudSecretStore",
]
