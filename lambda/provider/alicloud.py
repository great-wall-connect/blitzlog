"""Alicloud implementation of the provider abstraction.

Phase 2 (#84): every method on `AlicloudSecretStore`,
`AlicloudObjectStore`, and `AlicloudCompute` has a real body that
talks to the Alicloud SDKs (`alibabacloud_oos20190601`, `oss2`,
`alibabacloud_ecs20140526`). `BLITZLOG_PROVIDER=alicloud` is a
fully-working deploy on Alicloud.

Three classes:

    AlicloudSecretStore  — Aliyun OOS Parameter Store. `get` /
                           `put` / `delete` / `get_by_path` all go
                           through the `OosService` SDK via the
                           module-level `_oos_client` shim (the
                           single patch target for tests).
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

The three shims (`_oos_client`, `_oss_bucket`, `_ecs_client`) are
the only places the Alicloud SDKs are imported; nothing else in
this module touches the SDKs directly. Mirrors
`provider.aws._boto3_client` pattern from #83 so test code can
`@patch("provider.alicloud._oos_client")` etc. without having to
patch each underlying SDK class.

The secret store is the OOS Parameter Store, not KMS Secrets
Manager: on KMS 3.0 a secret can only be created inside a
dedicated KMS instance, and `CreateSecret` without a
`DKMSInstanceId` is rejected with `UnsupportedOperation`. The OOS
Parameter Store is free, needs no KMS instance, and its API maps
1:1 onto the `SecretStore` interface.

Phase 5+ swaps the user-data bootstrapper to render `aliyun oos`
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
from alibabacloud_oos20190601.client import Client as OosClient
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


def _client_timeouts() -> tuple[int, int]:
    """Return `(connect_timeout, read_timeout)` in MILLISECONDS.

    The Alibaba SDKs' Tea HTTP layer divides both values by 1000
    before handing them to `requests`, so they are milliseconds.
    Passing seconds (30) therefore becomes a 30 ms read timeout and
    every store read fails with "Read timed out. (read
    timeout=0.03)" — the DNS lookup alone takes longer than that.
    """
    connect = int(os.environ.get("ALIYUN_CONNECT_TIMEOUT", "10000") or "10000")
    read = int(os.environ.get("ALIYUN_READ_TIMEOUT", "30000") or "30000")
    return connect, read


def _oos_client() -> OosClient:
    """Return an `OosClient` configured against the region /
    credentials in `_env`. The OOS endpoint format is
    `oos.<region>.aliyuncs.com`. Caller-supplied credentials take
    precedence; absent credentials raise at first call rather
    than at import time (so the factory hands back a working
    instance even with empty env vars — useful for the test
    suite that patches this function entirely).

    The credentials are only set when there ARE any. Passing an
    empty access key overrides the SDK's own credential chain and
    every call then fails with "InvalidCredentials" — which is
    what the Function Compute path hit before the fallback to the
    runtime-injected `ALIBABA_CLOUD_*` variables was added.
    """
    from _env import (
        _aliyun_access_key_id,
        _aliyun_access_key_secret,
        _aliyun_region,
        _aliyun_security_token,
    )

    region = _aliyun_region()
    connect_timeout, read_timeout = _client_timeouts()
    config = open_api_models.Config(
        region_id=region,
        endpoint=f"oos.{region}.aliyuncs.com" if region else None,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
    )
    access_key_id = _aliyun_access_key_id()
    access_key_secret = _aliyun_access_key_secret()
    if access_key_id and access_key_secret:
        config.access_key_id = access_key_id
        config.access_key_secret = access_key_secret
        security_token = _aliyun_security_token()
        if security_token:
            config.security_token = security_token
        config.type = "access_key"
    return OosClient(config)


def _oss_auth_and_endpoint(region: str) -> tuple[oss2.Auth, str]:
    """Return the `(Auth, endpoint)` pair for an OSS client. Public
    so the test suite can construct the same shape without
    re-implementing the endpoint derivation.

    The Function Compute runtime hands the function role's
    temporary credentials over as an STS session, so the token has to
    travel with the key — `oss2.StsAuth` carries it, plain
    `oss2.Auth` does not.
    """
    from _env import (
        _aliyun_access_key_id,
        _aliyun_access_key_secret,
        _aliyun_security_token,
    )

    access_key_id = _aliyun_access_key_id()
    access_key_secret = _aliyun_access_key_secret()
    security_token = _aliyun_security_token()
    if access_key_id and access_key_secret and security_token:
        auth = oss2.StsAuth(access_key_id, access_key_secret, security_token)
    else:
        auth = oss2.Auth(access_key_id, access_key_secret)
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
    `ecs.<region>.aliyuncs.com`. The credentials are only set when
    there are any — see `_oos_client` for why.
    """
    from _env import (
        _aliyun_access_key_id,
        _aliyun_access_key_secret,
        _aliyun_region,
        _aliyun_security_token,
    )

    region = _aliyun_region()
    connect_timeout, read_timeout = _client_timeouts()
    config = open_api_models.Config(
        region_id=region,
        endpoint=f"ecs.{region}.aliyuncs.com" if region else None,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
    )
    access_key_id = _aliyun_access_key_id()
    access_key_secret = _aliyun_access_key_secret()
    if access_key_id and access_key_secret:
        config.access_key_id = access_key_id
        config.access_key_secret = access_key_secret
        security_token = _aliyun_security_token()
        if security_token:
            config.security_token = security_token
        config.type = "access_key"
    return EcsClient(config)


# ---------------------------------------------------------------------------
# SecretStore (OOS Parameter Store)
# ---------------------------------------------------------------------------


# Sentinel exception so cross-cloud error mapping in `_provider_error`
# is a single switch. The AWS impl's `botocore.exceptions.ClientError`
# and the Alicloud `Tea.exceptions.TeaException` both get routed
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


def _oos_name(name: str) -> str:
    """Map an SSM-style path onto an OOS parameter name.

    The canonical blitzlog paths are SSM-shaped
    (`/blitzlog/<env>/<service>/<name>`) so every cloud's store is
    handed the same string. The OOS parameter ARN is
    `acs:oos:<region>:<account>:secretparameter/<name>`, so a name
    that already began with "/" would render an ARN with two
    slashes after the resource type and the RAM grants in
    `infra/alicloud/modules/core/ram.tf` would stop matching. The
    OOS parameter names therefore carry no leading slash — the
    same reason `GetSecretParametersByPath` is called with the
    stripped path (a leading slash matches nothing).
    """
    return name.lstrip("/")


class AlicloudSecretStore(SecretStore):
    """Aliyun OOS Parameter Store adapter. `name` arguments are
    passed through verbatim — the caller is responsible for
    including the `/blitzlog/<env>/` prefix (matches the
    pre-#83 behaviour the test suite already asserts) — and the
    store strips that prefix before the name reaches the wire.

    Every parameter is an encrypted one (`type = "Secret"`), so
    the `type` argument of `put` (an SSM type name on the AWS
    side) carries no meaning here: OOS has one encrypted kind and
    one plain kind, and the plain kind would store the GitHub App
    private key in the clear.
    """

    def get(self, name: str, decrypt: bool = True) -> str:
        """Read the parameter value from OOS.

        `decrypt` is accepted for interface parity with the AWS
        store but ignored: on AWS SSM, `WithDecryption=False` still
        returns the plaintext for a `String`-typed parameter, which
        is what the two `decrypt=False` callers want (the
        Packer-published agent image id, and the Telegram
        allowed-user-id). OOS has no such split — every parameter
        is a `Secret` type, and `WithDecryption=False` returns an
        EMPTY value rather than the ciphertext — so honouring the
        flag would silently yield "" instead of the parameter's
        value. Always decrypting gives the same result the AWS
        store produces for those callers.
        """
        from alibabacloud_oos20190601 import models as oos_models

        request = oos_models.GetSecretParameterRequest(
            name=_oos_name(name),
            with_decryption=True,
        )
        response = _oos_client().get_secret_parameter_with_options(
            request,
            _runtime_options(),
        )
        parameter = response.body.parameter
        return parameter.value if parameter is not None else ""

    def put(self, name: str, value: str, type: str, overwrite: bool = True) -> None:
        """Write `value` to the named parameter.

        OOS has no single atomic "put with overwrite flag" call.
        The implementation matches `AwsSecretStore`: try
        `CreateSecretParameter` first; if that raises because the
        name is taken, fall through to `UpdateSecretParameter`
        which adds a new version. `overwrite=False` re-raises the
        duplicate-name error as `_ProviderError("ParameterExists")`
        so the consumer in `lambda/ec2.py::launch_ec2_spot_instance`
        can map it to the same `ParameterAlreadyExists` shape the
        AWS code returns.

        The customer-managed CMK id (when set via
        `ALIYUN_KMS_KEY_ID`) is passed through `key_id` on
        `CreateSecretParameter` so the parameter is encrypted with
        the configured CMK. The key is fixed at creation — an
        update re-uses the key the parameter already carries.

        Note (mirrors the AWS SSM note in #83): the
        `CreateSecretParameter` / `UpdateSecretParameter` pair has
        a brief race window — two concurrent `put()` calls on the
        same key can both observe "name does not exist" and one
        will fail with the OOS duplicate-name error. The
        bot-pool lock (`/blitzlog/users/<login>/bot-pool/...`)
        is exclusive per-bot so the race never bites in
        practice; if a future per-user code path needs
        `put()` without the lock, we should wrap the
        create-or-update pair in an OOS-side conditional request.
        """
        from _env import _aliyun_kms_key_id
        from alibabacloud_oos20190601 import models as oos_models
        from Tea.exceptions import TeaException

        cmk_id = _aliyun_kms_key_id()
        parameter_name = _oos_name(name)

        # Try CreateSecretParameter first; if the name is taken,
        # fall through to UpdateSecretParameter (the "update" path).
        create_request = oos_models.CreateSecretParameterRequest(
            name=parameter_name,
            value=value,
            type="Secret",
            key_id=cmk_id or None,
        )
        try:
            _oos_client().create_secret_parameter_with_options(
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
            update_request = oos_models.UpdateSecretParameterRequest(
                name=parameter_name,
                value=value,
            )
            _oos_client().update_secret_parameter_with_options(
                update_request,
                _runtime_options(),
            )

    def delete(self, name: str) -> None:
        """Delete the parameter. OOS deletes are immediate — there
        is no recovery window to wait out — which matches the AWS
        SSM delete and the watchdog's "release the bot lock"
        semantics.

        Best-effort: a missing parameter is swallowed so the
        consumer in `lambda/ec2.py::_read_custom_agent_ami` does
        not have to special-case "ParameterNotFound".
        """
        from alibabacloud_oos20190601 import models as oos_models
        from Tea.exceptions import TeaException

        request = oos_models.DeleteSecretParameterRequest(
            name=_oos_name(name),
        )
        try:
            _oos_client().delete_secret_parameter_with_options(
                request,
                _runtime_options(),
            )
        except TeaException as e:
            if _is_not_found(e):
                return
            raise

    def get_by_path(self, path: str, decrypt: bool = True) -> dict[str, str]:
        """Read every parameter under `path`.

        OOS has native path semantics
        (`GetSecretParametersByPath`), so this is one paginated
        API call sequence rather than the list-and-filter the KMS
        implementation needed. The path is stripped of its leading
        slash for the same reason the parameter names are (see
        `_oos_name`): a leading slash matches nothing.

        Performance characteristics: `MaxResults` is capped at 10
        by the API, so a pool with more than ten parameters takes
        more than one round trip (`list_bot_pool` is invoked from
        `acquire_bot_token` on every launch). The typical blitzlog
        user pool is a handful of keys, so the second page is rare
        in practice. The Lambda's 180s timeout absorbs the latency.

        Returns the same `{last_segment: value}` shape as
        `AwsSecretStore.get_by_path` so the consumer in
        `lambda/bot_pool.py::list_bot_pool` is unchanged.

        `decrypt` is accepted for interface parity with the AWS store
        but ignored, for the same reason as in `get` above: OOS
        returns empty values when decryption is off.
        """
        from alibabacloud_oos20190601 import models as oos_models

        client = _oos_client()
        params: dict[str, str] = {}
        prefix = _oos_name(path).rstrip("/") + "/"
        next_token = None
        while True:
            request = oos_models.GetSecretParametersByPathRequest(
                path=_oos_name(path),
                recursive=True,
                with_decryption=True,
                max_results=10,
                next_token=next_token,
            )
            response = client.get_secret_parameters_by_path_with_options(
                request,
                _runtime_options(),
            )
            body = response.body
            for parameter in body.parameters or []:
                parameter_name = parameter.name
                if not parameter_name.startswith(prefix):
                    continue
                params[parameter_name.rsplit("/", 1)[-1]] = parameter.value
            next_token = body.next_token
            if not next_token:
                break
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

    # Issue #125: the Alicloud Packer pipeline
    # (infra/alicloud/packer/agent-ecs-ubuntu.pkr.hcl) publishes the
    # baked image id to the OOS Parameter Store — the Alicloud
    # `SecretStore` implementation — under this suffix. The AWS
    # analogue is `agent-ami-id-docker-ubuntu` in SSM Parameter
    # Store. The two must differ: the AWS suffix would point at an
    # SSM parameter that the OOS-backed store cannot read.
    agent_image_param_suffix = "agent-ami-id-ecs-ubuntu"

    # "The parameter does not exist". OOS encodes this as
    # `EntityNotExists.Parameter` (see `_is_not_found` below); the
    # AWS-side code is `ParameterNotFound` / `404`.
    secret_not_found_codes = (
        "EntityNotExists.Parameter",
        "Forbidden.ResourceNotExists",
        "ResourceNotExists",
        "ParameterNotFound",
        "404",
    )

    # "The image id is not launchable". ECS reports a retired /
    # deregistered image as `InvalidImageId.NotFound`; the AWS-side
    # codes are `InvalidAMIID.NotFound` / `InvalidAMIID.Unavailable`.
    image_not_found_codes = (
        "InvalidImageId.NotFound",
        "InvalidImageId.Unavailable",
        "InvalidAMIID.NotFound",
        "InvalidAMIID.Unavailable",
    )

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
          3. `ossutil cp oss://<bucket>/<key> /tmp/bootstrap.sh` —
             downloads the bootstrap.

        The credentials come from the instance's RAM role: ossutil's
        `EcsRamRole` mode fetches the role's temporary credentials
        from the metadata service, and that role is what grants the
        read on the bootstrap object. (The `aliyun` CLI does NOT read
        the ALIBABA_CLOUD_* environment variables for authentication
        — it needs a profile — so the bootstrap script writes one for
        its own OOS reads.)

        The OSS endpoint is the INTRANET one
        (`oss-$region-internal.aliyuncs.com`): it is reachable from
        an ECS instance in the region without leaving the VPC, and
        OSS traffic over it is not billed as internet egress.

        The size budget — `metadata + curl + ossutil cp + bash` —
        fits well under 16 KB.
        """
        return f"""#!/bin/bash
set -e
REGION=$(curl -s http://100.100.100.200/latest/meta-data/region-id)
INSTANCE_ID=$(curl -s http://100.100.100.200/latest/meta-data/instance-id)
ENDPOINT="oss-$REGION-internal.aliyuncs.com"
if ! command -v ossutil >/dev/null 2>&1; then
    echo "ERROR: ossutil is not installed — the agent image must bake it" >&2
    exit 1
fi
ossutil cp "oss://{bucket}/{key}" /tmp/bootstrap.sh --mode EcsRamRole --region "$REGION" -e "$ENDPOINT"
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
            # The agent instance needs outbound internet: it reaches
            # the GitHub API, the model provider and Tailscale. The
            # AWS side gets that from the default VPC's subnets,
            # which auto-assign a public IP and route 0.0.0.0/0 to
            # the internet gateway — no NAT gateway, no NAT fee. The
            # Alicloud analogue is an ECS-managed public IP, which is
            # what a non-zero bandwidth asks for. The security group
            # still admits nothing inbound except SSH from the
            # operator's CIDRs.
            #
            # PayByTraffic is the only billing method the
            # international regions accept, and it means the public
            # address costs nothing unless it is used.
            "internet_charge_type": "PayByTraffic",
            "internet_max_bandwidth_out": 100,
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
    for the OOS / ECS calls. Kept as a module-private helper so the
    public client shim stays terse.
    """
    from alibabacloud_tea_util import models as tea_models

    connect_timeout, read_timeout = _client_timeouts()
    return tea_models.RuntimeOptions(
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
    )


def _is_already_exists(exc: Exception) -> bool:
    """Return True iff `exc` is an Alicloud "parameter already
    exists" error. OOS encodes the error in `exc.code` (the
    `ErrorCode` field on the SDK response); the canonical value is
    `"EntityAlreadyExist.Parameter"` for create-of-existing. That
    is what `_SecretStore.put` needs to fall through to
    `UpdateSecretParameter`.
    """
    code = getattr(exc, "code", "") or ""
    return "AlreadyExist" in code


def _is_not_found(exc: Exception) -> bool:
    """Return True iff `exc` is an Alicloud "parameter does not
    exist" error. Encoded in `exc.code`; the canonical value is
    `"EntityNotExists.Parameter"`.
    """
    code = getattr(exc, "code", "") or ""
    return "NotExists" in code or "NotFound" in code


__all__ = [
    "AlicloudCompute",
    "AlicloudObjectStore",
    "AlicloudSecretStore",
    "_ecs_client",
    "_oos_client",
    "_oss_bucket",
]
