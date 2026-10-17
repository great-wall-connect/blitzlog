"""EC2 spot instance launch + subnet/AMI/price helpers.

After #83, this module is a thin adapter over `provider.aws.AwsCompute`
and the other two provider singletons. The original behaviours
(spot pricing discovery, AMI lookup with custom-or-fallback chain,
user-data downloader that fits EC2's 16 KB cap, and the launch
loop with on-demand fallback) all live in the provider now; this
module preserves the function names that the test suite and the
handler import so the diff stays narrow.

The user-data upload to S3 (the bootstrap script EC2 fetches at
first boot) goes through `get_object_store()` rather than a
dedicated boto3 client. The ephemeral GitHub token write to SSM
goes through `get_secrets()`.

`grep -r boto3 lambda/ --exclude-dir=provider` returns no hits —
verified by the `#83` acceptance test.
"""

import base64
import os
import uuid

from _env import BLITZLOG_ENV, ephemeral_token_ssm_param, logger
from botocore.exceptions import ClientError
from provider import (
    LaunchSpec,
    get_compute,
    get_object_store,
    get_secrets,
)
from provider.aws import AwsCompute

# Fallback used when SPOT_INSTANCE_TYPES_JSON is unset or unparseable. Kept
# in sync with the default of `var.spot_instance_types` in infra/modules/core
# /variables.tf — Terraform is the source of truth at deploy time; this only
# protects unit tests and out-of-Lambda callers from a hard failure.
_DEFAULT_SPOT_INSTANCE_TYPES = AwsCompute._default_spot_instance_types()


def _load_spot_instance_types() -> list[str]:
    """Parse the SPOT_INSTANCE_TYPES_JSON env var set by Terraform.

    Returns the parsed list on success, or falls back to
    _DEFAULT_SPOT_INSTANCE_TYPES when the env var is missing, empty,
    unparseable, or not a non-empty list of strings. Logging at warning
    on parse failure so a misconfigured Terraform apply is visible in
    CloudWatch without aborting the request.

    Implementation lives in `AwsCompute._load_spot_instance_types`
    (post-#83). Kept as a top-level shim so `tests/test_ec2.py`
    can still import it via `from ec2 import _load_spot_instance_types`.
    """
    return AwsCompute()._load_spot_instance_types()


# `SPOT_INSTANCE_TYPES` is the module-level frozen copy other code
# might import. Reads the env var at import time so out-of-Lambda
# callers (and tests that don't reload) see the operator override.
SPOT_INSTANCE_TYPES: list[str] = _load_spot_instance_types()


def get_latest_ubuntu_ami() -> str:
    """Fallback source for the Ubuntu 26.04 LTS arm64 agent AMI when no
    Packer build has populated /blitzlog/<env>/agent-ami-id-docker-ubuntu.

    Delegates to `AwsCompute.get_latest_image("26.04")` which reads the
    Canonical-published image id from the AWS-managed parameter store.
    Kept as a public function so the test suite can import it.
    """
    return get_compute().get_latest_image("26.04")


def _read_custom_agent_ami(ssm_param_suffix: str, fallback_getter) -> str:
    """Read /blitzlog/<env>/<suffix>; validate; fall back via fallback_getter().

    The custom agent AMI is published by the Packer pipelines in
    infra/packer/ to this SSM parameter. We validate the id is still
    launchable via compute.describe_image so a retired/deregistered AMI
    doesn't silently turn every webhook into a 500.

    Fallback chain:
      1. SSM param missing (ParameterNotFound) → fallback_getter()
      2. AMI is retired (InvalidAMIID.NotFound) → clear the SSM param
         (best-effort) and fall back
      3. Any other ClientError → re-raise so the caller surfaces it
    """
    from _env import _ssm_root  # late import to read env at call time

    param_name = f"{_ssm_root()}/{ssm_param_suffix}"
    secrets = get_secrets()
    try:
        ami_id = secrets.get(param_name, decrypt=False)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ParameterNotFound", "404"):
            logger.info("No custom agent AMI at %s; falling back", param_name)
            return fallback_getter()
        raise

    try:
        get_compute().describe_image(ami_id)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("InvalidAMIID.NotFound", "InvalidAMIID.Unavailable"):
            logger.warning(
                "Custom agent AMI %s is unlaunchable (%s); clearing SSM param and falling back",
                ami_id,
                code,
            )
            try:
                secrets.delete(param_name)
            except ClientError as del_err:
                logger.warning(
                    "Failed to delete stale agent-ami-id param %s: %s",
                    param_name,
                    del_err,
                )
            return fallback_getter()
        raise

    logger.info("Using custom agent AMI: %s", ami_id)
    return ami_id


def get_agent_ami() -> str:
    """Read the Packer-built Ubuntu 26.04 arm64 agent AMI id from SSM.

    Packer publishes the AMI id to /blitzlog/<env>/agent-ami-id-docker-ubuntu
    via infra/packer/agent-docker-ubuntu.pkr.hcl. If the SSM param is
    missing or the AMI is retired, fall back to the Canonical-published
    Ubuntu 26.04 LTS arm64 minimal AMI (the same one Packer uses as
    source_ami_filter).
    """
    return _read_custom_agent_ami("agent-ami-id-docker-ubuntu", get_latest_ubuntu_ami)


def get_instance_profile_arn() -> str:
    """Thin shim over `AwsCompute.get_instance_profile_arn` that
    reads the profile name from the env var Terraform injected.
    """
    return get_compute().get_instance_profile_arn(
        os.environ["EC2_INSTANCE_PROFILE_NAME"],
    )


def get_spot_prices(instance_types: list[str]) -> list[tuple[str, str, float]]:
    """Return `(az, instance_type, price_usd_per_hour)` tuples sorted
    cheapest-first. Kept as a tuple-shaped shim over the new
    `AwsCompute.spot_prices` (which returns `list[SpotQuote]`) so
    the consumer in `launch_ec2_spot_instance` is unchanged.
    """
    quotes = get_compute().spot_prices(instance_types)
    return [(q.availability_zone, q.instance_type, q.price_usd_hour) for q in quotes]


def get_az_subnet_map() -> dict[str, str]:
    """Return `{az: subnet_id}` for the configured VPC. Empty dict
    when `VPC_ID` is unset (the consumer falls back to
    `EC2_SUBNET_ID`).
    """
    return get_compute().az_subnet_map(os.environ.get("VPC_ID", ""))


def _build_s3_downloader_script(bucket: str, key: str) -> str:
    """Render the tiny bootstrapper that EC2's UserData field runs at
    first boot. Delegates to `AwsCompute.build_user_data_downloader_script`
    — kept as a public shim so `tests/test_ec2.py::TestS3Downloader`
    can still import the symbol and assert the wire format.
    """
    return get_compute().build_user_data_downloader_script(bucket, key)


def _build_launch_spec(
    *,
    image_id: str,
    user_data_b64: str,
    security_group_id: str,
    instance_profile_name: str,
    issue_number: int,
    mode: str,
) -> LaunchSpec:
    """Build a `LaunchSpec` from the parameters `launch_ec2_spot_instance`
    computed inline. Tags include the `Environment` tag the watchdog's
    IAM condition keys on (`ec2:ResourceTag/Environment = var.environment`).
    """
    return LaunchSpec(
        image_id=image_id,
        instance_type="",  # filled per-iteration inside `launch_spot`
        user_data_b64=user_data_b64,
        subnet_id="",  # filled per-iteration inside `launch_spot`
        security_group_id=security_group_id,
        instance_profile_name=instance_profile_name,
        volume_size_gb=20,
        tags={
            "Purpose": "autonomous-agent",
            "Mode": mode,
            "Issue": str(issue_number),
            "Environment": BLITZLOG_ENV,
            "Name": (
                f"blitzlog-{BLITZLOG_ENV}-opencode-agent-{mode}-issue-{issue_number}"
            ),
        },
    )


def launch_ec2_spot_instance(
    repo: str,
    issue_number: int,
    github_token: str,
    mode: str,
    user_data_builder,
    sender_login: str = "",
    sender_id: str = "",
) -> str:
    """Launch a single spot instance for `issue_number`. Mirrors the
    pre-#83 function's contract: returns the instance id, raises
    `ClientError` on unrecoverable failure.

    The function is now a thin orchestration layer that:
      1. Writes the GitHub token to the ephemeral SSM namespace.
      2. Builds the user-data and uploads it to S3 (object store).
      3. Renders the user-data downloader (compute provider).
      4. Resolves the agent AMI (compute provider).
      5. Builds a `LaunchSpec` and asks the compute provider to
         launch it (with spot + on-demand fallback).
    """
    ephemeral_param = ephemeral_token_ssm_param(repo, issue_number)
    get_secrets().put(
        ephemeral_param,
        github_token,
        type="SecureString",
        overwrite=True,
    )

    user_data = user_data_builder(repo, issue_number, sender_login, sender_id)

    s3_bucket = os.environ.get("S3_LOGS_BUCKET", "<your-agent-logs-bucket>")
    s3_key = f"user-data/{mode}-issue-{issue_number}-{uuid.uuid4().hex[:8]}.sh"
    get_object_store().put(s3_bucket, s3_key, user_data.encode())
    logger.info(
        "Uploaded user-data to s3://%s/%s (%d bytes)",
        s3_bucket,
        s3_key,
        len(user_data.encode()),
    )

    downloader = _build_s3_downloader_script(s3_bucket, s3_key)
    user_data_b64 = base64.b64encode(downloader.encode()).decode()

    image_id = get_agent_ami()
    instance_profile_name = os.environ["EC2_INSTANCE_PROFILE_NAME"]
    security_group_id = os.environ["EC2_SECURITY_GROUP_ID"]

    spec = _build_launch_spec(
        image_id=image_id,
        user_data_b64=user_data_b64,
        security_group_id=security_group_id,
        instance_profile_name=instance_profile_name,
        issue_number=issue_number,
        mode=mode,
    )

    return get_compute().launch_spot(spec)


__all__ = [
    "_DEFAULT_SPOT_INSTANCE_TYPES",
    "_build_s3_downloader_script",
    "_load_spot_instance_types",
    "get_agent_ami",
    "get_az_subnet_map",
    "get_instance_profile_arn",
    "get_latest_ubuntu_ami",
    "get_spot_prices",
    "launch_ec2_spot_instance",
]
