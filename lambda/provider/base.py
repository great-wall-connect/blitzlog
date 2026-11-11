"""Abstract base classes for blitzlog's cloud-provider abstraction.

Three independent interfaces, one per external service surface the
Lambda touches:

    ComputeProvider  — launch/terminate spot compute, discover images
                       and pricing, render the user-data downloader.
    SecretStore      — read/write per-env parameters, get-by-path.
    ObjectStore      — put/get/head/delete on a (bucket, key) tuple.

Each provider implementation (currently `lambda/provider/aws.py`,
stubs in `lambda/provider/alicloud.py`) subclasses one ABC. The
factory in `lambda/provider/__init__.py` returns the right
concrete class based on the `BLITZLOG_PROVIDER` env var.

Shared dataclasses (`LaunchSpec`, `SpotQuote`, `Tag`) live here so
both the factory and the test fakes can import them without
circular dependencies.

This module is the canonical reference for the contract that
`tests/test_provider_base.py` enforces against a fake provider.
Every abstract method must be overridden; the test fakes in
`tests/_fakes.py` are the minimum-viable reference implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from _env import logger


def _error_code(exc: Exception) -> str:
    """Return the cloud-neutral error code for `exc`, or "".

    Each cloud SDK reports "this thing does not exist" differently:

      * AWS (botocore) → `exc.response["Error"]["Code"]`
      * Alicloud (Tea) → `exc.code`, e.g. `"Forbidden.ResourceNotExists"`
      * a cloud-neutral wrapper → `exc.code`

    The consumer (`ComputeProvider.get_agent_image_id`) compares the
    result against a per-provider tuple of acceptable codes, so the
    shape of the exception does not matter — only the code does.

    Returns "" for an exception with no recognisable code, which
    matches no provider's not-found tuple and therefore re-raises.
    """
    # botocore's ClientError: response["Error"]["Code"]
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            if isinstance(code, str):
                return code

    # Alicloud's TeaException and the cloud-neutral `_ProviderError`
    # both expose `.code`.
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        return code

    return ""


@dataclass
class LaunchSpec:
    """All inputs the compute provider needs to launch one spot
    instance. The Lambda builds this from the webhook payload, the
    bot pool result, and the env-file Terraform injected, then hands
    it to `ComputeProvider.launch_spot`.

    `user_data_b64` is the EC2-style base64-encoded UserData (or the
    Alicloud equivalent after Phase 3 lands). The provider is
    responsible for whatever per-cloud encoding the API wants.

    `tags` is a flat key→value mapping. The provider is responsible
    for any per-cloud keying (AWS allows up to 50 tags per instance;
    Aliyun uses `Tag.N.Key` form).
    """

    image_id: str
    instance_type: str
    user_data_b64: str
    subnet_id: str
    security_group_id: str
    instance_profile_name: str
    volume_size_gb: int
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class SpotQuote:
    """One row of spot-price discovery. `price_usd_hour` is normalised
    to USD per hour so the consumer in `lambda/ec2.py` can sort /
    compare across providers — Aliyun's API returns the same shape
    so the boundary adapter is trivial.
    """

    availability_zone: str
    instance_type: str
    price_usd_hour: float


@dataclass
class Tag:
    """Provider-neutral (key, value) tag. Compute providers that
    require list-of-dict shapes (e.g. AWS) can convert from this
    form, and providers that require flat k=v strings (e.g.
    Aliyun's CLI) can serialise it the other way.
    """

    key: str
    value: str


class ComputeProvider(ABC):
    """The interface the Lambda's `launch_ec2_spot_instance` uses to
    provision a single agent instance.

    The AWS implementation lives in `lambda/provider/aws.py::AwsCompute`;
    the Alicloud stub in `lambda/provider/alicloud.py::AlicloudCompute`
    (Phase 3 fills the bodies).
    """

    @abstractmethod
    def launch_spot(self, spec: LaunchSpec) -> str:
        """Launch a single spot instance. Returns the cloud-native
        instance id. May raise `botocore.exceptions.ClientError` (or
        the equivalent for non-AWS providers) when the request is
        rejected — the Lambda surfaces that as a 500.
        """

    @abstractmethod
    def terminate(self, instance_id: str) -> None:
        """Force-terminate the instance. Best-effort; the watchdog
        calls this after the agent container exits and the session
        archive is uploaded.
        """

    @abstractmethod
    def describe_image(self, image_id: str) -> dict:
        """Return the image descriptor. Raises a `ClientError`-shaped
        exception with the provider's "not found" code when the
        image id is unknown / retired. The consumer in
        `lambda/ec2.py::get_agent_ami` uses this to fall back from
        a Packer-baked image to the upstream Ubuntu image.
        """

    @abstractmethod
    def get_latest_image(self, ubuntu_version: str) -> str:
        """Return the cloud-published upstream image id for the
        given Ubuntu version (e.g. `"26.04"`). Used as the
        fallback path when the Packer-baked image is missing or
        retired.
        """

    # --- agent image id resolution (issue #125) ------------------------
    #
    # The Packer pipeline publishes the id of the baked agent image
    # to the provider's `SecretStore` under a provider-specific
    # suffix:
    #
    #   AWS      -> SSM Parameter Store
    #              /blitzlog/<env>/agent-ami-id-docker-ubuntu
    #   Alicloud -> KMS Secrets Manager
    #              /blitzlog/<env>/agent-ami-id-ecs-ubuntu
    #
    # The two paths differ in the suffix AND in the error taxonomy
    # ("this parameter does not exist" is `ParameterNotFound` on SSM
    # and `Forbidden.ResourceNotExists` on KMS; "this image is
    # retired" is `InvalidAMIID.NotFound` on EC2 and
    # `InvalidImageId.NotFound` on ECS). The shared algorithm lives
    # here so the per-provider differences are two class attributes
    # rather than two copies of the fallback chain.

    #: Suffix of the `SecretStore` parameter that holds the id of the
    #: Packer-baked agent image. Overridden per provider.
    agent_image_param_suffix: str = "agent-ami-id-docker-ubuntu"

    #: Error codes that mean "the parameter does not exist" — the
    #: signal to fall back to the cloud-published upstream image.
    #: Overridden per provider.
    secret_not_found_codes: tuple[str, ...] = ("ParameterNotFound", "404")

    #: Error codes that mean "the image id is not launchable" — the
    #: signal to clear the stale parameter and fall back.
    #: Overridden per provider.
    image_not_found_codes: tuple[str, ...] = (
        "InvalidAMIID.NotFound",
        "InvalidAMIID.Unavailable",
    )

    def get_agent_image_id(self, secret_store: SecretStore) -> str:
        """Return the id of the Packer-baked agent image, falling
        back to the cloud-published upstream Ubuntu image.

        The fallback chain:

          1. The parameter is missing → use the upstream image.
          2. The image is retired / deregistered → clear the stale
             parameter (best-effort) and use the upstream image.
          3. Any other error → re-raise so the caller surfaces it.

        Step 2 matters: a retired image id left in the store would
        turn every subsequent webhook into a 500, so the store is
        cleaned up rather than left holding a value that cannot be
        launched.
        """
        from _env import _ssm_root  # late import to read env at call time

        param_name = f"{_ssm_root()}/{self.agent_image_param_suffix}"
        try:
            image_id = secret_store.get(param_name, decrypt=False)
        except Exception as e:
            if _error_code(e) in self.secret_not_found_codes:
                logger.info("No custom agent image at %s; falling back", param_name)
                return self.get_latest_image("26.04")
            raise

        try:
            self.describe_image(image_id)
        except Exception as e:
            code = _error_code(e)
            if code in self.image_not_found_codes:
                logger.warning(
                    "Custom agent image %s is unlaunchable (%s); clearing the "
                    "stored id and falling back",
                    image_id,
                    code,
                )
                try:
                    secret_store.delete(param_name)
                except Exception as del_err:  # noqa: BLE001
                    logger.warning(
                        "Failed to delete stale agent-image id param %s: %s",
                        param_name,
                        del_err,
                    )
                return self.get_latest_image("26.04")
            raise

        logger.info("Using custom agent image: %s", image_id)
        return image_id

    @abstractmethod
    def spot_prices(self, instance_types: list[str]) -> list[SpotQuote]:
        """Return current spot prices for the given instance types
        in the current region, sorted cheapest-first. Empty list
        on API failure (the consumer falls back to a fixed order).
        """

    @abstractmethod
    def az_subnet_map(self, vpc_id: str) -> dict[str, str]:
        """Return a map of `availability_zone → subnet_id` for the
        given VPC. Empty dict when `vpc_id` is empty (the consumer
        falls back to `EC2_SUBNET_ID`).
        """

    @abstractmethod
    def get_instance_profile_arn(self, profile_name: str) -> str:
        """Return the ARN (or Aliyun RAM equivalent) of the instance
        profile the EC2/ECS instance will assume. The Lambda passes
        this into `LaunchSpec.instance_profile_name` indirectly.
        """

    @abstractmethod
    def build_user_data_downloader_script(self, bucket: str, key: str) -> str:
        """Render the tiny bootstrapper that runs as the instance's
        user-data. Must be small enough to fit the cloud's user-data
        cap (16 KB on EC2, similar on Aliyun ECS). The script must
        fetch region + instance id from the cloud metadata service
        and download the full bootstrap from the object store.
        """

    @abstractmethod
    def region(self) -> str:
        """Return the cloud region this provider is configured
        against. The Lambda logs this at cold start so an operator
        can verify the env-var wiring is correct.
        """


class SecretStore(ABC):
    """Per-env parameter store. Backed by AWS SSM Parameter Store
    today; Phase 2 / Phase 3 swap in the Alicloud equivalent.
    """

    @abstractmethod
    def get(self, name: str, decrypt: bool = True) -> str:
        """Return the parameter value. `name` is the full parameter
        name (including the `/blitzlog/<env>/` prefix); the
        provider does not add a namespace of its own.
        """

    @abstractmethod
    def put(self, name: str, value: str, type: str, overwrite: bool = True) -> None:
        """Write a parameter. `type` is the provider's parameter
        type (e.g. `"SecureString"` on AWS, `"Secure"` on Aliyun).
        """

    @abstractmethod
    def delete(self, name: str) -> None:
        """Delete a parameter. Best-effort — the consumer swallows
        `ParameterNotFound` so a missing parameter is not an error.
        """

    @abstractmethod
    def get_by_path(self, path: str, decrypt: bool = True) -> dict[str, str]:
        """Return every parameter under `path` as a flat
        `param_name → value` mapping. The last `/`-separated
        segment of each parameter name is used as the key.
        """


class ObjectStore(ABC):
    """Object storage. Backed by S3 today; Phase 2 swaps in OSS.
    All methods take raw bytes — encoding/decoding is the caller's
    responsibility.
    """

    @abstractmethod
    def put(self, bucket: str, key: str, body: bytes) -> None:
        """Write `body` to `(bucket, key)`. Overwrites silently if
        the key already exists.
        """

    @abstractmethod
    def get(self, bucket: str, key: str) -> bytes:
        """Return the bytes at `(bucket, key)`. Raises a
        `ClientError`-shaped exception with the provider's "no
        such key" code when the key is missing.
        """

    @abstractmethod
    def head(self, bucket: str, key: str) -> bool:
        """Return True iff the key exists. Does not download the
        body. Used by the resume-aborted-session flow to check
        whether a metadata file is present before paying for the
        download.
        """

    @abstractmethod
    def delete(self, bucket: str, key: str) -> None:
        """Delete the key. Best-effort — the watchdog calls this
        to release bot-pool locks and never wants to abort the
        terminate flow on a missing key.
        """
