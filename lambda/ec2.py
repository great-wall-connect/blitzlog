"""EC2 spot instance launch + subnet/AMI/price helpers.

The launch function uploads the user-data (autonomous or assisted) to S3
and writes a tiny S3 downloader as the EC2 `UserData` field (which has a
16 KB cap; the full bootstrap is multi-kilobyte and well over that).
Also responsible for picking the cheapest available AZ/instance-type
combination for the configured instance types.

The downloader script itself is intentionally tiny (well under the 16 KB
EC2 UserData limit) and downloads the full bootstrap from S3 at first
boot via IMDSv2-fetched credentials.
"""

import base64
import os
import uuid
from datetime import datetime, timezone

import boto3
from _env import BLITZLOG_ENV, SSM_PATH, logger
from botocore.config import Config
from botocore.exceptions import ClientError

ec2 = boto3.client("ec2", config=Config(retries={"max_attempts": 1}))
s3 = boto3.client("s3")

SPOT_INSTANCE_TYPES = ["t4g.medium", "t4g.large", "t4g.xlarge"]


def get_latest_ubuntu_ami() -> str:
    """Fallback source for the Ubuntu 26.04 LTS arm64 agent AMI when no
    Packer build has populated /blitzlog/<env>/agent-ami-id-docker-ubuntu.

    Reads the Canonical-published Ubuntu 26.04 LTS arm64 minimal AMI id
    from the AWS-managed parameter store namespace; matches the
    source_ami_filter used by infra/packer/agent-docker-ubuntu.pkr.hcl.
    """
    resp = boto3.client("ssm").get_parameter(
        Name="/aws/service/canonical/ubuntu/server/26.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
    )
    return resp["Parameter"]["Value"]


def _read_custom_agent_ami(ssm_param_suffix: str, fallback_getter) -> str:
    """Read /blitzlog/<env>/<suffix>; validate; fall back via fallback_getter().

    The custom agent AMI is published by the Packer pipelines in
    infra/packer/ to this SSM parameter. We validate the id is still
    launchable via ec2.describe_images so a retired/deregistered AMI
    doesn't silently turn every webhook into a 500.

    Fallback chain:
      1. SSM param missing (ParameterNotFound) → fallback_getter()
      2. AMI is retired (InvalidAMIID.NotFound) → clear the SSM param
         (best-effort) and fall back
      3. Any other ClientError → re-raise so the caller surfaces it
    """
    from _env import _ssm_root  # late import to read env at call time

    param_name = f"{_ssm_root()}/{ssm_param_suffix}"
    ssm = boto3.client("ssm")
    try:
        resp = ssm.get_parameter(Name=param_name)
        ami_id = resp["Parameter"]["Value"]
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ParameterNotFound", "404"):
            logger.info("No custom agent AMI at %s; falling back", param_name)
            return fallback_getter()
        raise

    try:
        ec2.describe_images(ImageIds=[ami_id])
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("InvalidAMIID.NotFound", "InvalidAMIID.Unavailable"):
            logger.warning(
                "Custom agent AMI %s is unlaunchable (%s); clearing SSM param and falling back",
                ami_id,
                code,
            )
            try:
                ssm.delete_parameter(Name=param_name)
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
    iam = boto3.client("iam")
    resp = iam.get_instance_profile(
        InstanceProfileName=os.environ["EC2_INSTANCE_PROFILE_NAME"]
    )
    return resp["InstanceProfile"]["Arn"]


def get_spot_prices(instance_types: list[str]) -> list[tuple[str, str, float]]:
    try:
        resp = ec2.describe_spot_price_history(
            InstanceTypes=instance_types,
            ProductDescriptions=["Linux/UNIX"],
            StartTime=datetime.now(timezone.utc),
            EndTime=datetime.now(timezone.utc),
        )
        results: list[tuple[str, str, float]] = []
        for entry in resp.get("SpotPriceHistory", []):
            price = float(entry["SpotPrice"])
            results.append((entry["AvailabilityZone"], entry["InstanceType"], price))
        results.sort(key=lambda x: x[2])
        return results
    except ClientError as e:
        logger.warning("DescribeSpotPriceHistory failed: %s", e)
        return []


def get_az_subnet_map() -> dict[str, str]:
    vpc_id = os.environ.get("VPC_ID", "")
    if not vpc_id:
        return {}
    try:
        resp = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
        return {
            subnet["AvailabilityZone"]: subnet["SubnetId"]
            for subnet in resp.get("Subnets", [])
        }
    except ClientError as e:
        logger.warning("DescribeSubnets failed: %s", e)
        return {}


def _build_s3_downloader_script(bucket: str, key: str) -> str:
    """Render the tiny bootstrapper that EC2's UserData field runs at
    first boot. It uses IMDSv2 to find the region, then `aws s3 cp`s the
    full bootstrap from S3 and `bash`-es it. Kept under 16 KB so the
    base64-encoded EC2 UserData payload stays under the cap.
    """
    return f"""#!/bin/bash
TOKEN=$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/dynamic/instance-identity/document | python3 -c "import sys,json; print(json.load(sys.stdin)['region'])")
aws s3 cp s3://{bucket}/{key} /tmp/bootstrap.sh --region "$REGION"
bash /tmp/bootstrap.sh
"""


def launch_ec2_spot_instance(
    repo: str,
    issue_number: int,
    github_token: str,
    mode: str,
    user_data_builder,
    sender_login: str = "",
    sender_id: str = "",
) -> str:
    ephemeral_param = f"{SSM_PATH}/ephemeral/github-token-{issue_number}"
    boto3.client("ssm").put_parameter(
        Name=ephemeral_param,
        Value=github_token,
        Type="SecureString",
        Overwrite=True,
    )

    user_data = user_data_builder(repo, issue_number, sender_login, sender_id)

    s3_bucket = os.environ.get("S3_LOGS_BUCKET", "<your-agent-logs-bucket>")
    s3_key = f"user-data/{mode}-issue-{issue_number}-{uuid.uuid4().hex[:8]}.sh"
    s3.put_object(Bucket=s3_bucket, Key=s3_key, Body=user_data.encode())
    logger.info(
        "Uploaded user-data to s3://%s/%s (%d bytes)",
        s3_bucket,
        s3_key,
        len(user_data.encode()),
    )

    downloader = _build_s3_downloader_script(s3_bucket, s3_key)
    user_data_b64 = base64.b64encode(downloader.encode()).decode()

    image_id = get_agent_ami()
    instance_profile_arn = get_instance_profile_arn()

    common_params = {
        "MinCount": 1,
        "MaxCount": 1,
        "ImageId": image_id,
        "SecurityGroupIds": [os.environ["EC2_SECURITY_GROUP_ID"]],
        "UserData": user_data_b64,
        "InstanceInitiatedShutdownBehavior": "terminate",
        "IamInstanceProfile": {"Arn": instance_profile_arn},
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {
                    "VolumeSize": 20,
                    "VolumeType": "gp3",
                    "DeleteOnTermination": True,
                },
            },
        ],
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Purpose", "Value": "autonomous-agent"},
                    {"Key": "Mode", "Value": mode},
                    {"Key": "Issue", "Value": str(issue_number)},
                    {
                        "Key": "Environment",
                        "Value": BLITZLOG_ENV,
                    },
                    {
                        "Key": "Name",
                        "Value": f"blitzlog-{BLITZLOG_ENV}-opencode-agent-{mode}-issue-{issue_number}",
                    },
                ],
            },
        ],
    }

    az_subnet_map = get_az_subnet_map()
    spot_prices = get_spot_prices(SPOT_INSTANCE_TYPES)

    if spot_prices and az_subnet_map:
        for az, instance_type, price in spot_prices:
            subnet_id = az_subnet_map.get(az)
            if not subnet_id:
                logger.info("No subnet in %s, skipping", az)
                continue
            try:
                logger.info("Trying spot %s in %s ($%.6f)...", instance_type, az, price)
                resp = ec2.run_instances(
                    **common_params,
                    SubnetId=subnet_id,
                    InstanceType=instance_type,
                    InstanceMarketOptions={
                        "MarketType": "spot",
                        "SpotOptions": {
                            "SpotInstanceType": "one-time",
                            "InstanceInterruptionBehavior": "terminate",
                        },
                    },
                )
                return resp["Instances"][0]["InstanceId"]
            except ClientError as e:
                logger.warning("Spot %s in %s failed: %s", instance_type, az, e)
                continue
    else:
        fallback_subnet = os.environ.get("EC2_SUBNET_ID", "")
        for instance_type in SPOT_INSTANCE_TYPES:
            try:
                logger.info("Trying spot %s (fallback order)...", instance_type)
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
                return resp["Instances"][0]["InstanceId"]
            except ClientError as e:
                logger.warning("Spot %s failed: %s", instance_type, e)
                continue

    logger.warning("All spot types failed, falling back to on-demand t4g.medium")
    resp = ec2.run_instances(
        **common_params,
        SubnetId=os.environ.get("EC2_SUBNET_ID", ""),
        InstanceType="t4g.medium",
    )
    return resp["Instances"][0]["InstanceId"]
