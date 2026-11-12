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

from _env import BLITZLOG_ENV, _logs_bucket, ephemeral_token_ssm_param, logger
from provider import (
    LaunchSpec,
    get_compute,
    get_object_store,
    get_secrets,
)
from provider.aws import AwsCompute

# Fallback used when SPOT_INSTANCE_TYPES_JSON is unset or unparseable. Kept
# in sync with the default of `var.spot_instance_types` in infra/aws/modules/core
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


def get_agent_ami() -> str:
    """Read the Packer-built agent image id from the provider's secret store.

    AWS: the AMI id lives in SSM Parameter Store at
    `/blitzlog/<env>/agent-ami-id-docker-ubuntu`, published by
    infra/aws/packer/agent-docker-ubuntu.pkr.hcl.

    Alicloud (issue #125): the image id lives in KMS Secrets Manager at
    `/blitzlog/<env>/agent-ami-id-ecs-ubuntu`, published by
    infra/alicloud/packer/agent-ecs-ubuntu.pkr.hcl.

    If the parameter is missing or the image is retired, fall back to
    the cloud-published Ubuntu 26.04 LTS arm64 minimal image (the same
    one Packer uses as its source image).

    The resolution logic lives on the provider
    (`ComputeProvider.get_agent_image_id`) because the parameter
    suffix and the error taxonomy are provider-specific; this function
    is the provider-agnostic entry point the rest of the Lambda uses.
    """
    return get_compute().get_agent_image_id(get_secrets())


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

    s3_bucket = _logs_bucket()
    s3_key = f"user-data/{mode}-issue-{issue_number}-{uuid.uuid4().hex[:8]}.sh"
    get_object_store().put(s3_bucket, s3_key, user_data.encode())
    logger.info(
        "Uploaded user-data to %s/%s (%d bytes)",
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
