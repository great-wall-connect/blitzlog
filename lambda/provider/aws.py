"""AWS implementation of the provider abstraction.

Every boto3 call goes through the module-level `_boto3_client` shim
so tests have a single canonical patch target
(`@patch("provider.aws._boto3_client")`) instead of a different
patch per service. The shim is the only place `boto3` is imported;
nothing else in this module touches the boto3 SDK directly.

Three classes:

    AwsCompute    — wraps ec2, ec2-image lookup, iam, and the
                    IMDSv2 user-data downloader script.
    AwsSecretStore — wraps ssm. The `get_ssm_param` shim in
                     `lambda/auth.py` is the public entry point the
                     rest of the Lambda uses.
    AwsObjectStore — wraps s3. Bot-pool locks, user-data uploads,
                     and session-archive plugin reads/writes all
                     route through this class.

Behaviour is byte-identical to the pre-#83 functions in
`lambda/auth.py`, `lambda/bot_pool.py`, and `lambda/ec2.py`. The
acceptance criterion in #83 is that the existing wire-format
tests (`test_ec2.py::TestS3Downloader`, etc.) still pass.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from provider.base import (
    ComputeProvider,
    LaunchSpec,
    ObjectStore,
    SecretStore,
    SpotQuote,
)

# `max_attempts=1` matches the pre-#83 behaviour in
# `lambda/auth.py::_ssm` and `lambda/ec2.py::ec2`. The Lambda is
# fronted by a DLQ; a single retry on transient boto3 errors was
# double-counted and made a misbehaving EC2 launch harder to debug.
_DEFAULT_BOTO_CONFIG = Config(retries={"max_attempts": 1})


def _boto3_client(service_name: str, config: Config | None = None):
    """The single entry point every AWS provider class uses to
    obtain a boto3 client. Tests `@patch("provider.aws._boto3_client")`
    and routes the return value to whichever service they need.

    Accepts an optional `config` so callers that need a non-default
    `botocore.config.Config` (e.g. longer timeouts for spot
    pricing) can override without losing the single patch target.
    """
    return boto3.client(service_name, config=config or _DEFAULT_BOTO_CONFIG)


# ---------------------------------------------------------------------------
# SecretStore
# ---------------------------------------------------------------------------


class AwsSecretStore(SecretStore):
    """SSM Parameter Store adapter. `name` arguments are passed
    through verbatim — the caller is responsible for including
    the `/blitzlog/<env>/` prefix.
    """

    def get(self, name: str, decrypt: bool = True) -> str:
        resp = _boto3_client("ssm").get_parameter(
            Name=name,
            WithDecryption=decrypt,
        )
        return resp["Parameter"]["Value"]

    def put(self, name: str, value: str, type: str, overwrite: bool = True) -> None:
        _boto3_client("ssm").put_parameter(
            Name=name,
            Value=value,
            Type=type,
            Overwrite=overwrite,
        )

    def delete(self, name: str) -> None:
        try:
            _boto3_client("ssm").delete_parameter(Name=name)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("ParameterNotFound", "404"):
                return
            raise

    def get_by_path(self, path: str, decrypt: bool = True) -> dict[str, str]:
        paginator = _boto3_client("ssm").get_paginator("get_parameters_by_path")
        params: dict[str, str] = {}
        for page in paginator.paginate(Path=path, WithDecryption=decrypt):
            for param in page.get("Parameters", []):
                key = param["Name"].rsplit("/", 1)[-1]
                params[key] = param["Value"]
        return params


# ---------------------------------------------------------------------------
# ObjectStore
# ---------------------------------------------------------------------------


class AwsObjectStore(ObjectStore):
    """S3 adapter. All methods take raw bytes; the caller does the
    JSON-encoding (bot-pool locks are stored as JSON bytes; user-
    data uploads are bytes; session archives are bytes).
    """

    def put(self, bucket: str, key: str, body: bytes) -> None:
        _boto3_client("s3").put_object(Bucket=bucket, Key=key, Body=body)

    def get(self, bucket: str, key: str) -> bytes:
        resp = _boto3_client("s3").get_object(Bucket=bucket, Key=key)
        return resp["Body"].read()

    def head(self, bucket: str, key: str) -> bool:
        try:
            _boto3_client("s3").head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
                return False
            raise

    def delete(self, bucket: str, key: str) -> None:
        try:
            _boto3_client("s3").delete_object(Bucket=bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
                return
            raise


# ---------------------------------------------------------------------------
# ComputeProvider
# ---------------------------------------------------------------------------


class AwsCompute(ComputeProvider):
    """EC2 + IAM adapter. Spot launch, image lookup, spot pricing,
    subnet discovery, and the user-data downloader script.
    """

    def __init__(self, s3: ObjectStore | None = None) -> None:
        # `s3` is injected so the user-data upload (which is an
        # object-store operation, not a compute operation) routes
        # through the same `ObjectStore` the Lambda would obtain
        # via `get_object_store()`. Defaulting to None makes the
        # call site use `get_object_store()` so tests can patch
        # the factory without threading the dependency through.
        self._s3 = s3

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _default_spot_instance_types() -> list[str]:
        """Fallback when the `SPOT_INSTANCE_TYPES_JSON` env var is
        unset or unparseable. Mirrors the pre-#83 default in
        `lambda/ec2.py::_DEFAULT_SPOT_INSTANCE_TYPES`.
        """
        return ["t4g.medium", "t4g.large", "t4g.xlarge"]

    def _load_spot_instance_types(self) -> list[str]:
        """Read the per-Lambda `SPOT_INSTANCE_TYPES_JSON` env var.
        Keeps the parsing rules from the pre-#83
        `lambda/ec2.py::_load_spot_instance_types` so the existing
        `test_ec2.py::TestLoadSpotInstanceTypes` cases still pass.
        """
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

    # ---- ComputeProvider ------------------------------------------------

    def get_latest_image(self, ubuntu_version: str) -> str:
        """Read the Canonical-published Ubuntu arm64 minimal AMI
        id from the AWS-managed parameter store. Mirrors the
        pre-#83 `lambda/ec2.py::get_latest_ubuntu_ami`.
        """
        resp = _boto3_client("ssm").get_parameter(
            Name=(
                f"/aws/service/canonical/ubuntu/server/{ubuntu_version}"
                "/stable/current/arm64/hvm/ebs-gp3/ami-id"
            )
        )
        return resp["Parameter"]["Value"]

    def describe_image(self, image_id: str) -> dict:
        """Thin wrapper over `ec2.describe_images` so the caller
        in `get_agent_ami` can detect retired AMIs by checking
        for `InvalidAMIID.NotFound` / `InvalidAMIID.Unavailable`.
        """
        return _boto3_client("ec2").describe_images(ImageIds=[image_id])

    def get_instance_profile_arn(self, profile_name: str) -> str:
        resp = _boto3_client("iam").get_instance_profile(
            InstanceProfileName=profile_name,
        )
        return resp["InstanceProfile"]["Arn"]

    def spot_prices(self, instance_types: list[str]) -> list[SpotQuote]:
        ec2 = _boto3_client("ec2")
        now = datetime.now(timezone.utc)
        try:
            resp = ec2.describe_spot_price_history(
                InstanceTypes=instance_types,
                ProductDescriptions=["Linux/UNIX"],
                StartTime=now,
                EndTime=now,
            )
        except ClientError:
            return []
        quotes: list[SpotQuote] = []
        for entry in resp.get("SpotPriceHistory", []):
            quotes.append(
                SpotQuote(
                    availability_zone=entry["AvailabilityZone"],
                    instance_type=entry["InstanceType"],
                    price_usd_hour=float(entry["SpotPrice"]),
                )
            )
        quotes.sort(key=lambda q: q.price_usd_hour)
        return quotes

    def az_subnet_map(self, vpc_id: str) -> dict[str, str]:
        if not vpc_id:
            return {}
        try:
            resp = _boto3_client("ec2").describe_subnets(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}],
            )
        except ClientError:
            return {}
        return {
            subnet["AvailabilityZone"]: subnet["SubnetId"]
            for subnet in resp.get("Subnets", [])
        }

    def build_user_data_downloader_script(self, bucket: str, key: str) -> str:
        """Render the tiny bootstrapper. EC2's `UserData` is capped
        at 16 KB; the full bootstrap is well over that so the
        downloader fetches region via IMDSv2 and then `aws s3 cp`s
        the bootstrap. Mirrors the pre-#83
        `lambda/ec2.py::_build_s3_downloader_script`.
        """
        return f"""#!/bin/bash
TOKEN=$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/dynamic/instance-identity/document | python3 -c "import sys,json; print(json.load(sys.stdin)['region'])")
aws s3 cp s3://{bucket}/{key} /tmp/bootstrap.sh --region "$REGION"
bash /tmp/bootstrap.sh
"""

    def region(self) -> str:
        """Resolve the configured AWS region. The Lambda env
        exposes `AWS_REGION` (Terraform sets it); the fallback
        path through `boto3.session.Session` covers local-test
        invocations.
        """
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if region:
            return region
        return boto3.session.Session().region_name or "us-east-1"

    def launch_spot(self, spec: LaunchSpec) -> str:
        """Launch one spot instance from a fully-populated
        `LaunchSpec`. Mirrors the body of the pre-#83
        `lambda/ec2.py::launch_ec2_spot_instance` after the
        user-data upload + AMI lookup.

        `instance_id` is returned as a string for parity with the
        consumer's `update_lock_instance_id` call.

        If the per-AZ spot-iteration loop finds no candidate, the
        pre-#83 on-demand fallback (`t4g.medium`) is invoked. That
        is the documented behaviour, not a bug.
        """
        ec2 = _boto3_client("ec2")
        common_params = {
            "MinCount": 1,
            "MaxCount": 1,
            "ImageId": spec.image_id,
            "SecurityGroupIds": [spec.security_group_id],
            "UserData": spec.user_data_b64,
            "InstanceInitiatedShutdownBehavior": "terminate",
            "IamInstanceProfile": {
                "Arn": self.get_instance_profile_arn(spec.instance_profile_name)
            },
            "BlockDeviceMappings": [
                {
                    "DeviceName": "/dev/xvda",
                    "Ebs": {
                        "VolumeSize": spec.volume_size_gb,
                        "VolumeType": "gp3",
                        "DeleteOnTermination": True,
                    },
                },
            ],
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [{"Key": k, "Value": v} for k, v in spec.tags.items()],
                },
            ],
        }

        instance_types = self._load_spot_instance_types()
        # `az_subnet_map` requires a VPC id; we don't have one in
        # `LaunchSpec` (it's an env-var on the Lambda today, not
        # part of the per-launch payload). Look it up here from
        # the same env var the pre-#83 code read.
        vpc_id = os.environ.get("VPC_ID", "")
        az_subnet_map = self.az_subnet_map(vpc_id)
        spot_prices = self.spot_prices(instance_types)

        if spot_prices and az_subnet_map:
            for quote in spot_prices:
                subnet_id = az_subnet_map.get(quote.availability_zone)
                if not subnet_id:
                    continue
                try:
                    resp = ec2.run_instances(
                        **common_params,
                        SubnetId=subnet_id,
                        InstanceType=quote.instance_type,
                        InstanceMarketOptions={
                            "MarketType": "spot",
                            "SpotOptions": {
                                "SpotInstanceType": "one-time",
                                "InstanceInterruptionBehavior": "terminate",
                            },
                        },
                    )
                    return str(resp["Instances"][0]["InstanceId"])
                except ClientError:
                    continue
        else:
            fallback_subnet = os.environ.get("EC2_SUBNET_ID", "")
            for instance_type in instance_types:
                try:
                    resp = ec2.run_instances(
                        **common_params,
                        SubnetId=fallback_subnet,
                        InstanceType=instance_type,
                        InstanceMarketOptions={
                            "MarketType": "spot",
                            "SpotOptions": {
                                "SpotInstanceType": "one-time",
                                "InstanceInterruptionBehavior": "terminate",
                            },
                        },
                    )
                    return str(resp["Instances"][0]["InstanceId"])
                except ClientError:
                    continue

        # All spot types failed: on-demand t4g.medium fallback.
        resp = ec2.run_instances(
            **common_params,
            SubnetId=os.environ.get("EC2_SUBNET_ID", ""),
            InstanceType="t4g.medium",
        )
        return str(resp["Instances"][0]["InstanceId"])

    def terminate(self, instance_id: str) -> None:
        _boto3_client("ec2").terminate_instances(InstanceIds=[instance_id])


# Module-level public surface. `_boto3_client` is exported so
# tests can `@patch("provider.aws._boto3_client")` — the issue
# body's acceptance criterion depends on this single patch target.
__all__ = [
    "AwsCompute",
    "AwsObjectStore",
    "AwsSecretStore",
    "_boto3_client",
]
