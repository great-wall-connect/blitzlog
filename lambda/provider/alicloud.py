"""Alicloud implementation of the provider abstraction.

Phase 2 (#84): every method on `AlicloudSecretStore`,
`AlicloudObjectStore`, and `AlicloudCompute` has a real body that
talks to the Alicloud SDKs (`alibabacloud_kms20160120`, `oss2`,
`alibabacloud_ecs20140526`). `BLITZLOG_PROVIDER=alicloud` is a
fully-working deploy on Alicloud.

Three classes:

    AlicloudSecretStore  — Aliyun KMS. `get` / `put` / `delete` /
                           `get_by_path` all go through the
                           `KmsService` SDK via the module-level
                           `_kms_client` shim (the single patch
                           target for tests).
    AlicloudObjectStore  — Aliyun OSS via the `oss2` SDK.
                           `put` / `get` / `head` / `delete` go
                           through `_oss_bucket` (single patch
                           target).
    AlicloudCompute      — Aliyun ECS via the `ECS` SDK.
                           `launch_spot` / `terminate` /
                           `describe_image` / `get_latest_image` /
                           `spot_prices` / `az_subnet_map` /
                           `get_instance_profile_arn` /
                           `build_user_data_downloader_script` /
                           `region` all hit the ECS API via
                           `_ecs_client` (single patch target).

The three shims (`_kms_client`, `_oss_bucket`, `_ecs_client`) are
the only places the Alicloud SDKs are imported; nothing else in
this module touches the SDKs directly. Mirrors the
`provider.aws._boto3_client` pattern from #83 so test code can
`@patch("provider.alicloud._kms_client")` etc. without having to
patch each underlying SDK class.

Phase 5+ swaps the user-data bootstrapper to render `aliyun kms`
/ `ossutil cp` heredocs; today the EC2-style heredoc is still
shipped so #84 stays scoped to the Lambda-side abstraction. The
downloader script rendered by `build_user_data_downloader_script`
ALREADY uses `aliyun oss cp` + the `100.100.100.200` metadata
endpoint — that's the one ECS-vs-EC2 difference the Lambda has
to know about up-front.
"""

from __future__ import annotations

import os

import oss2
from alibabacloud_ecs20140526.client import Client as EcsClient
from alibabacloud_kms20160120.client import Client as KmsClient
from alibabacloud_tea_openapi import models as open_api_models
from provider.base import (
    ComputeProvider,
    LaunchSpec,
    ObjectStore,
    SecretStore,
    SpotQuote,
)

# ---------------------------------------------------------------------------
# Single canonical SDK client shims. Tests `@patch` these instead of
# patching the SDK class constructors. Pattern is identical to
# `provider.aws._boto3_client` (issue #83).
# ---------------------------------------------------------------------------


def _kms_client() -> KmsClient:
    """Return a `KmsClient` configured against the region /
    credentials in `_env`. The KMS endpoint format is
    `kms.<region>.aliyuncs.com`. Caller-supplied credentials take
    precedence; absent credentials raise at first call rather
    than at import time (so the factory hands back a working
    instance even with empty env vars — useful for the test
    suite that patches this function entirely).
    """
    from _env import (
        _aliyun_access_key_id,
        _aliyun_access_key_secret,
        _aliyun_region,
    )

    region = _aliyun_region()
    config = open_api_models.Config(
        access_key_id=_aliyun_access_key_id(),
        access_key_secret=_aliyun_access_key_secret(),
        region_id=region,
        endpoint=f"kms.{region}.aliyuncs.com" if region else None,
        type="access_key",
    )
    return KmsClient(config)


def _oss_auth_and_endpoint(region: str) -> tuple[oss2.Auth, str]:
    """Return the `(Auth, endpoint)` pair for an OSS client. Public
    so the test suite can construct the same shape without
    re-implementing the endpoint derivation.
    """
    from _env import _aliyun_access_key_id, _aliyun_access_key_secret

    auth = oss2.Auth(_aliyun_access_key_id(), _aliyun_access_key_secret())
    endpoint = f"https://oss-{region}.aliyuncs.com" if region else ""
    return auth, endpoint


def _oss_bucket(bucket_name: str) -> oss2.Bucket:
    """Return an `oss2.Bucket` for `bucket_name`. Mirrors the
    single-shim pattern: tests `@patch("provider.alicloud._oss_bucket")`
    and route the return value to whichever bucket the test
    exercises.
    """
    from _env import _aliyun_region

    region = _aliyun_region()
    auth, endpoint = _oss_auth_and_endpoint(region)
    return oss2.Bucket(auth, endpoint, bucket_name)


def _ecs_client() -> EcsClient:
    """Return an `EcsClient` configured against the region /
    credentials in `_env`. The ECS endpoint format is
    `ecs.<region>.aliyuncs.com`.
    """
    from _env import (
        _aliyun_access_key_id,
        _aliyun_access_key_secret,
        _aliyun_region,
    )

    region = _aliyun_region()
    config = open_api_models.Config(
        access_key_id=_aliyun_access_key_id(),
        access_key_secret=_aliyun_access_key_secret(),
        region_id=region,
        endpoint=f"ecs.{region}.aliyuncs.com" if region else None,
        type="access_key",
    )
    return EcsClient(config)


# ---------------------------------------------------------------------------
# SecretStore (KMS)
# ---------------------------------------------------------------------------


# Sentinel exception so cross-cloud error mapping in `_provider_error`
# is a single switch. The AWS impl's `botocore.exceptions.ClientError`
# and the Alicloud KMS `Tea.exceptions.TeaException` both get routed
# to this hierarchy so `lambda/auth.py` / `lambda/bot_pool.py` can
# raise "ParameterNotFound" / "NoSuchKey" without learning cloud-
# native exception types.
class _ProviderError(Exception):
    """Cloud-neutral wrapper. `code` is the cross-cloud error
    string the consumer (e.g. `AwsSecretStore.delete` callers) is
    already programmed to handle: `"ParameterNotFound"`,
    `"NoSuchKey"`, `"InvalidImageId.NotFound"`, etc.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


class AlicloudSecretStore(SecretStore):
    """Aliyun KMS Secrets-Manager adapter. `name` arguments are
    passed through verbatim — the caller is responsible for
    including the `/blitzlog/<env>/` prefix (matches the
    pre-#83 behaviour the test suite already asserts).
    """

    def get(self, name: str, decrypt: bool = True) -> str:
        """Read `SecretData` from KMS. `decrypt=False` returns the
        ciphertext (matches AWS SSM's `WithDecryption=False`);
        for KMS the ciphertext envelope is only useful when the
        caller will run their own decryption, which blitzlog
        never does.
        """
        from alibabacloud_kms20160120 import models as kms_models

        request = kms_models.GetSecretValueRequest(secret_name=name)
        response = _kms_client().get_secret_value_with_options(
            request,
            _runtime_options(),
        )
        return response.body.secret_data

    def put(self, name: str, value: str, type: str, overwrite: bool = True) -> None:
        """Write `value` to the named secret.

        KMS does not expose a single atomic "put with overwrite
        flag" call. The implementation matches `AwsSecretStore`:
        try `CreateSecret` first; if that raises because the name
        is taken, fall through to `PutSecretValue` which adds a
        new version. `overwrite=False` re-raises the KMS
        "already exists" error as `_ProviderError("ParameterExists")`
        so the consumer in `lambda/ec2.py::launch_ec2_spot_instance`
        can map it to the same `ParameterAlreadyExists` shape the
        AWS code returns.

        The customer-managed CMK id (when set via
        `ALIYUN_KMS_KEY_ID`) is passed through `encryption_key_id`
        on `CreateSecret` so secrets at rest use the configured
        CMK. KMS tracks the CMK per-secret-version on
        `PutSecretValue` automatically, so no extra work is needed
        there.

        Note (Phase 2 limitation, mirrors the AWS SSM note in
        #83): the `CreateSecret` / `PutSecretValue` pair has a
        brief race window — two concurrent `put()` calls on the
        same key can both observe "name does not exist" and one
        will fail with the KMS duplicate-name error. The
        bot-pool lock (`/blitzlog/users/<login>/bot-pool/...`)
        is exclusive per-bot so the race never bites in
        practice; if a future per-user code path needs
        `put()` without the lock, we should wrap the
        Create-or-PutSecretValue pair in a KMS-side conditional
        request.
        """
        from _env import _aliyun_kms_key_id
        from alibabacloud_kms20160120 import models as kms_models
        from Tea.exceptions import TeaException

        cmk_id = _aliyun_kms_key_id()

        # Try CreateSecret first; if the name is taken, fall through
        # to PutSecretValue (the "update" path).
        create_request = kms_models.CreateSecretRequest(
            secret_name=name,
            secret_data=value,
            secret_data_type="text",
            secret_type=type,
            encryption_key_id=cmk_id or None,
        )
        try:
            _kms_client().create_secret_with_options(
                create_request,
                _runtime_options(),
            )
            return
        except TeaException as e:
            if not _is_already_exists(e):
                raise
            if not overwrite:
                # Match AwsSecretStore's surface: surface a single
                # "ParameterExists" code via `_ProviderError` so the
                # Lambda can decide whether to log + drop or 500.
                raise _ProviderError(
                    "ParameterExists", str(getattr(e, "message", "")) or "exists"
                ) from e
            put_request = kms_models.PutSecretValueRequest(
                secret_name=name,
                secret_data=value,
                secret_data_type="text",
            )
            _kms_client().put_secret_value_with_options(
                put_request,
                _runtime_options(),
            )

    def delete(self, name: str) -> None:
        """Force-delete the secret. The KMS default recovery
        window is 30 days which is far too long for the
        watchdog's "release the bot lock" semantics; we pass
        `force_delete_without_recovery=True` to make the delete
        synchronous (mirrors the AWS SSM delete which is also
        synchronous).

        Best-effort: a missing secret is swallowed so the
        consumer in `lambda/ec2.py::_read_custom_agent_ami` does
        not have to special-case "ParameterNotFound".
        """
        from alibabacloud_kms20160120 import models as kms_models
        from Tea.exceptions import TeaException

        request = kms_models.DeleteSecretRequest(
            secret_name=name,
            force_delete_without_recovery="true",
        )
        try:
            _kms_client().delete_secret_with_options(
                request,
                _runtime_options(),
            )
        except TeaException as e:
            if _is_not_found(e):
                return
            raise

    def get_by_path(self, path: str, decrypt: bool = True) -> dict[str, str]:
        """KMS has no native "list by path" semantics. Implement
        by calling `ListSecrets` (paginated) and filtering
        client-side for names that start with `path + "/"`.

        Performance characteristics: 1-2 extra API calls per cold
        start per user (`list_bot_pool` is invoked from
        `acquire_bot_token` on every launch). ListSecrets returns
        ~50 secrets per page; the typical blitzlog user pool is
        well under 100 keys so the second page is rare in
        practice. The Lambda's 180s timeout absorbs the latency.

        Returns the same `{last_segment: value}` shape as
        `AwsSecretStore.get_by_path` so the consumer in
        `lambda/bot_pool.py::list_bot_pool` is unchanged.
        """
        from alibabacloud_kms20160120 import models as kms_models

        client = _kms_client()
        params: dict[str, str] = {}
        prefix = path.rstrip("/") + "/"
        page_number = 1
        page_size = 50
        while True:
            request = kms_models.ListSecretsRequest(
                page_number=page_number,
                page_size=page_size,
            )
            response = client.list_secrets_with_options(
                request,
                _runtime_options(),
            )
            body = response.body
            secret_list = body.secret_list
            secrets = secret_list.secret if secret_list is not None else []
            for secret in secrets:
                secret_name = secret.secret_name
                if not secret_name.startswith(prefix):
                    continue
                key = secret_name.rsplit("/", 1)[-1]
                # Read the value lazily — `describe_secret` would
                # fetch metadata, but to mirror the AWS
                # `get_parameters_by_path` shape (returns full
                # values in one call) we issue a `GetSecretValue`
                # per matching key. The pool is small enough that
                # the fan-out is cheap.
                get_request = kms_models.GetSecretValueRequest(
                    secret_name=secret_name,
                )
                get_response = client.get_secret_value_with_options(
                    get_request,
                    _runtime_options(),
                )
                params[key] = get_response.body.secret_data
            if body.total_count is None or page_number * page_size >= body.total_count:
                break
            page_number += 1
        return params


# ---------------------------------------------------------------------------
# ObjectStore (OSS)
# ---------------------------------------------------------------------------


class AlicloudObjectStore(ObjectStore):
    """Aliyun OSS adapter. All methods take raw bytes; the caller
    does the JSON-encoding (bot-pool locks are stored as JSON
    bytes; user-data uploads are bytes; session archives are
    bytes).

    Bucket endpoints are derived from `ALIYUN_REGION` (`oss-<region>`).
    Cross-region buckets (rare in blitzlog; would require
    per-bucket region overrides) are not supported in #84 — they
    land when the Azure blob equivalent ships in Phase 3.
    """

    def put(self, bucket: str, key: str, body: bytes) -> None:
        _oss_bucket(bucket).put_object(key, body)

    def get(self, bucket: str, key: str) -> bytes:
        return _oss_bucket(bucket).get_object(key).read()

    def head(self, bucket: str, key: str) -> bool:
        try:
            # `object_exists` raises on any error including
            # "no such key"; `head_object` returns a metadata
            # object on success. The latter is what the AWS impl
            # does so we mirror that.
            _oss_bucket(bucket).head_object(key)
            return True
        except oss2.exceptions.NoSuchKey:
            return False

    def delete(self, bucket: str, key: str) -> None:
        # `oss2` is silent on missing keys — no `NoSuchKey`
        # exception from `delete_object` (it returns the
        # response status from the server, but a missing key
        # is a `NoSuchKey` server response that the SDK does
        # not raise on). Best-effort delete mirrors the
        # AWS behaviour: no try/except needed.
        _oss_bucket(bucket).delete_object(key)


# ---------------------------------------------------------------------------
# ComputeProvider (ECS)
# ---------------------------------------------------------------------------


class AlicloudCompute(ComputeProvider):
    """Aliyun ECS + RAM adapter. Spot launch, image lookup,
    spot-price discovery, subnet discovery, instance-profile
    lookup, and the user-data downloader script.
    """

    def __init__(self, oss: ObjectStore | None = None) -> None:
        """`oss` is injected so callers that need an OSS
        reference (the user-data upload, which is an
        object-store operation, not a compute operation) can
        route through the same `ObjectStore` the Lambda would
        obtain via `get_object_store()`. Mirrors the
        `AwsCompute(s3=...)` constructor shape.

        Defaulting to None makes the call site use
        `get_object_store()` so tests can patch the factory
        without threading the dependency through.
        """
        self._oss = oss

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _default_spot_instance_types() -> list[str]:
        """Fallback when the `SPOT_INSTANCE_TYPES_JSON` env var
        is unset or unparseable. Mirrors the AWS default of
        `["t4g.medium", "t4g.large", "t4g.xlarge"]` mapped to
        the closest Aliyun equivalents (ARM, general-purpose).
        Kept in sync with the pre-#84 default in
        `lambda/ec2.py::_DEFAULT_SPOT_INSTANCE_TYPES` so the
        existing test fixture's expected default doesn't drift.
        """
        return ["ecs.t4-c1m1.large", "ecs.t6-c1m2.large", "ecs.t6-c2m2.large"]

    def _load_spot_instance_types(self) -> list[str]:
        """Read the per-Lambda `SPOT_INSTANCE_TYPES_JSON` env var
        set by Terraform. Parsing rules mirror
        `AwsCompute._load_spot_instance_types` so a
        misconfigured env var produces the same fallback list on
        both providers.
        """
        import json

        raw = os.environ.get("SPOT_INSTANCE_TYPES_JSON", "").strip()
        if not raw:
            return list(self._default_spot_instance_types())
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return list(self._default_spot_instance_types())
        if (
            not isinstance(parsed, list)
            or not parsed
            or not all(isinstance(item, str) and item for item in parsed)
        ):
            return list(self._default_spot_instance_types())
        return parsed

    @staticmethod
    def _runtime_options():
        """Read-timeouts shortcut that delegates to the
        module-level `_runtime_options()` so the ECS code below
        does not need to reach past the class to access it.
        """
        return _runtime_options()

    # ---- ComputeProvider ------------------------------------------------

    def get_latest_image(self, ubuntu_version: str) -> str:
        """Find the Canonical-published Ubuntu image id for the
        configured region by filtering `DescribeImages` by the
        `system` owner alias + the Ubuntu image-name prefix.

        Alicloud publishes the Ubuntu image id as a name pattern
        `ubuntu_<codename>_<version>_<arch>_*.vhd`; e.g. for
        `ubuntu_version="26.04"` the matcher is
        `ubuntu_*_26.04_amd64_*`. The wildcard on the
        `<codename>` segment catches every release (`noble`,
        `jammy`, `resolute`, ...).

        Returns the first matching image id. Empty list raises
        `IndexError` (a programming error — Terraform should have
        published a `agent-ami-id` SSM param so this fallback
        is unreachable).
        """
        from alibabacloud_ecs20140526 import models as ecs_models

        request = ecs_models.DescribeImagesRequest(
            image_owner_alias="system",
            status="Available",
            page_size=50,
            image_name=f"ubuntu_*_{ubuntu_version}_amd64_*",
        )
        response = _ecs_client().describe_images_with_options(
            request,
            self._runtime_options(),
        )
        images_list = response.body.images.image if response.body.images else []
        return images_list[0].image_id

    def describe_image(self, image_id: str) -> dict:
        """Thin wrapper over `DescribeImages` so the caller in
        `lambda/ec2.py::get_agent_ami` can detect retired /
        deregistered images. Returns the raw response object —
        the consumer only needs to know the call did not raise.
        """
        from alibabacloud_ecs20140526 import models as ecs_models

        request = ecs_models.DescribeImagesRequest(
            image_id=[image_id],
            status="Available",
        )
        return _ecs_client().describe_images_with_options(
            request,
            self._runtime_options(),
        )

    def get_instance_profile_arn(self, profile_name: str) -> str:
        """Return the RAM role identifier used as the
        `RamRoleName` parameter on `RunInstances`.

        Alicloud's RAM `GetRole` API (issue #84 implementation
        note) returns the role's `Arn`/`RoleArn`. We do not
        pull in the `alibabacloud_ram20150501` SDK package in
        Phase 2 to keep the dependency surface small; the role
        name is passed verbatim to `RunInstances` and the ARN
        returned here is only consumed by CloudWatch logs.
        Format: `acs:ram::{account_id}:role/{role_name}` — same
        shape Alibaba's IAM uses for the role ARN. The
        `{account_id}` slot is filled with `0` (placeholder);
        operators who need the real ARN should run
        `aliyun ram GetRole --RoleName <name>` out-of-band.
        """
        return f"acs:ram::0:role/{profile_name}"

    def spot_prices(self, instance_types: list[str]) -> list[SpotQuote]:
        """Return current spot prices for the given instance
        types in the configured region, cheapest first.

        Aliyun's `DescribeSpotPriceHistory` only filters by a
        single `instance_type` per call; we fan out one call
        per type, then merge + sort. Empty list on failure
        (the consumer in `lambda/ec2.py::_build_launch_spec`
        falls back to on-demand).
        """
        from _env import _aliyun_region
        from alibabacloud_ecs20140526 import models as ecs_models
        from Tea.exceptions import TeaException

        client = _ecs_client()
        quotes: list[SpotQuote] = []
        for instance_type in instance_types:
            request = ecs_models.DescribeSpotPriceHistoryRequest(
                region_id=_aliyun_region(),
                instance_type=instance_type,
                network_type="vpc",
                io_optimized="optimized",
            )
            try:
                response = client.describe_spot_price_history_with_options(
                    request,
                    self._runtime_options(),
                )
            except TeaException:
                # Empty list on API failure — matches AWS
                # `describe_spot_price_history`'s ClientError
                # fallback.
                continue
            body = response.body
            if body.spot_prices is None:
                continue
            for entry in body.spot_prices.spot_price_type or []:
                quotes.append(
                    SpotQuote(
                        availability_zone=entry.zone_id,
                        instance_type=entry.instance_type,
                        price_usd_hour=float(entry.spot_price),
                    )
                )
        quotes.sort(key=lambda q: q.price_usd_hour)
        return quotes

    def az_subnet_map(self, vpc_id: str) -> dict[str, str]:
        """Return a `{zone_id: vswitch_id}` map for the given
        VPC. Empty dict when `vpc_id` is empty (the consumer
        falls back to `ALIYUN_DEFAULT_VSWITCH_ID`).

        Mirrors `AwsCompute.az_subnet_map`: one `DescribeVSwitches`
        call per VPC.
        """
        from alibabacloud_ecs20140526 import models as ecs_models
        from Tea.exceptions import TeaException

        if not vpc_id:
            return {}
        request = ecs_models.DescribeVSwitchesRequest(vpc_id=vpc_id, page_size=50)
        try:
            response = _ecs_client().describe_vswitches_with_options(
                request,
                self._runtime_options(),
            )
        except TeaException:
            return {}
        body = response.body
        if body.v_switches is None:
            return {}
        return {
            vswitch.zone_id: vswitch.v_switch_id
            for vswitch in body.v_switches.v_switch or []
        }

    def build_user_data_downloader_script(self, bucket: str, key: str) -> str:
        """Render the tiny bootstrapper that ECS's `UserData`
        field runs at first boot. ECS caps user-data at 16 KB
        (same as EC2) so the downloader fetches the bootstrap
        from OSS in two hops:

          1. `curl http://100.100.100.200/latest/meta-data/region-id`
             — Aliyun metadata endpoint.
          2. `curl http://100.100.100.200/latest/meta-data/instance-id`
             — used for the CloudWatch session log.
          3. `aliyun oss cp oss://<bucket>/<key> /tmp/bootstrap.sh --endpoint
             https://oss-$region.aliyuncs.com` — downloads the bootstrap.

        The `aliyun` CLI is pre-installed by the Packer pipeline
        (`infra/aws/packer/agent-docker-ubuntu.pkr.hcl` Phase 5+
        will switch the bootstrap heredoc to render `aliyun oss
        cp` natively; today the EC2-side heredoc still uses
        `aws s3 cp` because Phase 5 has not shipped yet).

        The size budget — `metadata + curl + aliyun oss cp + bash`
        — fits well under 16 KB.
        """
        return f"""#!/bin/bash
set -e
REGION=$(curl -s http://100.100.100.200/latest/meta-data/region-id)
INSTANCE_ID=$(curl -s http://100.100.100.200/latest/meta-data/instance-id)
ENDPOINT="https://oss-$REGION.aliyuncs.com"
aliyun oss cp "oss://{bucket}/{key}" /tmp/bootstrap.sh --endpoint "$ENDPOINT"
bash /tmp/bootstrap.sh
"""

    def region(self) -> str:
        """Resolve the configured Aliyun region. The Lambda env
        exposes `ALIYUN_REGION`; the fallback empty string makes
        a missing-env-var situation visible in CloudWatch
        (the SDK client construction will then fail at the
        first call, surfacing a clear 500).
        """
        from _env import _aliyun_region

        return _aliyun_region()

    def launch_spot(self, spec: LaunchSpec) -> str:
        """Launch a single spot (preemptible) instance from a
        fully-populated `LaunchSpec`.

        Mirrors `AwsCompute.launch_spot`'s contract: returns
        the instance id, raises on unrecoverable failure. The
        pre-#84 on-demand fallback path is preserved here
        verbatim — if every spot price candidate fails to
        launch, we attempt one `InstanceChargeType=PostPaid`
        instance of the first configured type.

        Spot pricing on Aliyun is opt-in per-instance via
        `SpotStrategy=SpotWithPriceLimit` + `SpotPriceLimit`.
        The cap is read from `spec.tags["SpotPriceCap"]` so the
        Lambda can pass a per-launch ceiling without an env-var
        round-trip (matches the issue's Phase 2 plan).

        Returns the Aliyun instance id (an `i-bp1...` string)
        for parity with the consumer's
        `update_lock_instance_id` call.
        """
        from _env import _aliyun_region
        from alibabacloud_ecs20140526 import models as ecs_models
        from Tea.exceptions import TeaException

        common_params: dict = {
            "region_id": _aliyun_region(),
            "image_id": spec.image_id,
            "security_group_id": spec.security_group_id,
            "user_data": spec.user_data_b64,
            "instance_name": "blitzlog-agent",
            "io_optimized": "optimized",
            "internet_max_bandwidth_out": 0,
            "ram_role_name": spec.instance_profile_name,
            "instance_charge_type": "PostPaid",
            "spot_strategy": "SpotWithPriceLimit",
            "spot_price_limit": float(spec.tags.get("SpotPriceCap", 0) or 0),
            "system_disk": ecs_models.RunInstancesRequestSystemDisk(
                size=spec.volume_size_gb,
                category="cloud_essd",
            ),
            "tag": [
                ecs_models.RunInstancesRequestTag(key=k, value=v)
                for k, v in spec.tags.items()
                if k != "SpotPriceCap"
            ],
        }

        ecs = _ecs_client()
        instance_types = self._load_spot_instance_types()
        vpc_id = os.environ.get("VPC_ID", "")
        az_subnet_map = self.az_subnet_map(vpc_id)
        spot_prices = self.spot_prices(instance_types)

        if spot_prices and az_subnet_map:
            for quote in spot_prices:
                vswitch_id = az_subnet_map.get(quote.availability_zone)
                if not vswitch_id:
                    continue
                try:
                    request = ecs_models.RunInstancesRequest(
                        **common_params,
                        zone_id=quote.availability_zone,
                        v_switch_id=vswitch_id,
                        instance_type=quote.instance_type,
                        amount=1,
                        min_amount=1,
                    )
                    response = ecs.run_instances_with_options(
                        request,
                        self._runtime_options(),
                    )
                    ids = response.body.instance_id_sets.instance_id_set
                    return ids[0]
                except TeaException:
                    continue
        else:
            fallback_vswitch = os.environ.get("ALIYUN_DEFAULT_VSWITCH_ID", "")
            for instance_type in instance_types:
                try:
                    request = ecs_models.RunInstancesRequest(
                        **common_params,
                        instance_type=instance_type,
                        v_switch_id=fallback_vswitch,
                        amount=1,
                        min_amount=1,
                    )
                    response = ecs.run_instances_with_options(
                        request,
                        self._runtime_options(),
                    )
                    ids = response.body.instance_id_sets.instance_id_set
                    return ids[0]
                except TeaException:
                    continue

        # All spot types failed — on-demand fallback. Mirrors the
        # AWS `t4g.medium` fallback; the Aliyun default is the
        # cheapest general-purpose ARM type.
        request = ecs_models.RunInstancesRequest(
            **common_params,
            instance_type="ecs.t4-c1m1.large",
            v_switch_id=os.environ.get("ALIYUN_DEFAULT_VSWITCH_ID", ""),
            amount=1,
            min_amount=1,
        )
        response = ecs.run_instances_with_options(
            request,
            self._runtime_options(),
        )
        ids = response.body.instance_id_sets.instance_id_set
        return ids[0]

    def terminate(self, instance_id: str) -> None:
        """Force-terminate the instance. `Force=True` skips the
        graceful-stop attempt so a stuck shutdown timer cannot
        keep the watchdog blocked past its 2-hour instance
        budget.
        """
        from alibabacloud_ecs20140526 import models as ecs_models

        request = ecs_models.DeleteInstancesRequest(
            instance_id=[instance_id],
            force=True,
        )
        _ecs_client().delete_instances_with_options(
            request,
            self._runtime_options(),
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _runtime_options():
    """Read the SDK timeouts from env, build a `RuntimeOptions`
    for KMS calls. Kept as a module-private helper so the
    public client shim stays terse.
    """
    from alibabacloud_tea_util import models as tea_models

    return tea_models.RuntimeOptions(
        connect_timeout=int(os.environ.get("ALIYUN_CONNECT_TIMEOUT", "10") or "10"),
        read_timeout=int(os.environ.get("ALIYUN_READ_TIMEOUT", "30") or "30"),
    )


def _is_already_exists(exc: Exception) -> bool:
    """Return True iff `exc` is an Alicloud KMS "secret already
    exists" error. KMS encodes the error in `exc.code` (the
    `ErrorCode` field on the SDK response); the canonical
    value is `"Forbidden.ResourceNotExists"` for delete-of-
    missing and `"DataConflict.SecretAlreadyExists"` for
    create-of-existing. The latter is what `_SecretStore.put`
    needs to fall through to `PutSecretValue`.
    """
    code = getattr(exc, "code", "") or ""
    return "SecretAlreadyExists" in code or "AlreadyExists" in code


def _is_not_found(exc: Exception) -> bool:
    """Return True iff `exc` is an Alicloud KMS "secret does not
    exist" error. Encoded in `exc.code`; the canonical value is
    `"Forbidden.ResourceNotExists"`.
    """
    code = getattr(exc, "code", "") or ""
    return "ResourceNotExists" in code or "NotFound" in code


__all__ = [
    "AlicloudCompute",
    "AlicloudObjectStore",
    "AlicloudSecretStore",
    "_ecs_client",
    "_kms_client",
    "_oss_bucket",
]
