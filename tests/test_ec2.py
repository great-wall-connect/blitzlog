"""EC2 spot-instance launch + subnet/AMI/price helpers."""

import base64
import os
import unittest
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from ec2 import _build_s3_downloader_script, get_az_subnet_map, get_spot_prices


class TestGetSpotPrices(unittest.TestCase):
    @patch("ec2.ec2")
    def test_returns_sorted_by_price(self, mock_ec2):
        mock_ec2.describe_spot_price_history.return_value = {
            "SpotPriceHistory": [
                {
                    "AvailabilityZone": "ap-east-1b",
                    "InstanceType": "t4g.medium",
                    "SpotPrice": "0.018500",
                },
                {
                    "AvailabilityZone": "ap-east-1a",
                    "InstanceType": "t4g.medium",
                    "SpotPrice": "0.009300",
                },
                {
                    "AvailabilityZone": "ap-east-1c",
                    "InstanceType": "t4g.medium",
                    "SpotPrice": "0.010500",
                },
            ]
        }
        result = get_spot_prices(["t4g.medium"])
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], ("ap-east-1a", "t4g.medium", 0.0093))
        self.assertEqual(result[1], ("ap-east-1c", "t4g.medium", 0.0105))
        self.assertEqual(result[2], ("ap-east-1b", "t4g.medium", 0.0185))

    @patch("ec2.ec2")
    def test_multiple_instance_types(self, mock_ec2):
        mock_ec2.describe_spot_price_history.return_value = {
            "SpotPriceHistory": [
                {
                    "AvailabilityZone": "ap-east-1a",
                    "InstanceType": "t4g.large",
                    "SpotPrice": "0.0200",
                },
                {
                    "AvailabilityZone": "ap-east-1a",
                    "InstanceType": "t4g.medium",
                    "SpotPrice": "0.0093",
                },
                {
                    "AvailabilityZone": "ap-east-1c",
                    "InstanceType": "t4g.xlarge",
                    "SpotPrice": "0.0350",
                },
            ]
        }
        result = get_spot_prices(["t4g.medium", "t4g.large", "t4g.xlarge"])
        self.assertEqual(result[0], ("ap-east-1a", "t4g.medium", 0.0093))
        self.assertEqual(result[1], ("ap-east-1a", "t4g.large", 0.02))
        self.assertEqual(result[2], ("ap-east-1c", "t4g.xlarge", 0.035))

    @patch("ec2.ec2")
    def test_empty_response(self, mock_ec2):
        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        result = get_spot_prices(["t4g.medium"])
        self.assertEqual(result, [])

    @patch("ec2.ec2")
    def test_api_failure_returns_empty(self, mock_ec2):
        from botocore.exceptions import ClientError

        mock_ec2.describe_spot_price_history.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "boom"}},
            "DescribeSpotPriceHistory",
        )
        result = get_spot_prices(["t4g.medium"])
        self.assertEqual(result, [])


class TestGetAzSubnetMap(unittest.TestCase):
    @patch.dict(os.environ, {"VPC_ID": "vpc-12345"})
    @patch("ec2.ec2")
    def test_returns_az_to_subnet_mapping(self, mock_ec2):
        mock_ec2.describe_subnets.return_value = {
            "Subnets": [
                {"AvailabilityZone": "ap-east-1a", "SubnetId": "subnet-aaa"},
                {"AvailabilityZone": "ap-east-1b", "SubnetId": "subnet-bbb"},
                {"AvailabilityZone": "ap-east-1c", "SubnetId": "subnet-ccc"},
            ]
        }
        result = get_az_subnet_map()
        self.assertEqual(
            result,
            {
                "ap-east-1a": "subnet-aaa",
                "ap-east-1b": "subnet-bbb",
                "ap-east-1c": "subnet-ccc",
            },
        )

    @patch.dict(os.environ, {"VPC_ID": "vpc-12345"})
    @patch("ec2.ec2")
    def test_empty_subnets(self, mock_ec2):
        mock_ec2.describe_subnets.return_value = {"Subnets": []}
        result = get_az_subnet_map()
        self.assertEqual(result, {})

    @patch.dict(os.environ, {"VPC_ID": ""})
    def test_missing_vpc_id(self):
        result = get_az_subnet_map()
        self.assertEqual(result, {})

    @patch.dict(os.environ, {"VPC_ID": "vpc-12345"})
    @patch("ec2.ec2")
    def test_api_failure_returns_empty(self, mock_ec2):
        from botocore.exceptions import ClientError

        mock_ec2.describe_subnets.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "boom"}}, "DescribeSubnets"
        )
        result = get_az_subnet_map()
        self.assertEqual(result, {})


class TestS3Downloader(unittest.TestCase):
    def test_downloader_size_within_limit(self):
        downloader = _build_s3_downloader_script("my-bucket", "user-data/test.sh")
        b64 = base64.b64encode(downloader.encode()).decode()
        self.assertLess(len(b64), 16384, "Downloader exceeds 16KB UserData limit")

    def test_downloader_references_correct_s3_path(self):
        downloader = _build_s3_downloader_script(
            "my-bucket", "user-data/assisted-issue-42-abc.sh"
        )
        self.assertIn("s3://my-bucket/user-data/assisted-issue-42-abc.sh", downloader)
        self.assertIn("aws s3 cp", downloader)
        self.assertIn("bash /tmp/bootstrap.sh", downloader)


class TestLaunchEc2SpotInstance(unittest.TestCase):
    @patch(
        "ec2.get_instance_profile_arn",
        return_value="arn:aws:iam::123:instance-profile/test",
    )
    @patch("ec2.get_agent_ami", return_value="ami-12345")
    @patch("ec2.s3")
    @patch("ec2.boto3")
    @patch("ec2.ec2")
    @patch.dict(
        os.environ,
        {
            "EC2_SECURITY_GROUP_ID": "sg-123",
            "EC2_SUBNET_ID": "subnet-123",
            "VPC_ID": "vpc-123",
            "S3_LOGS_BUCKET": "test-bucket",
        },
    )
    def test_stores_token_in_ssm(
        self, mock_ec2, mock_boto3, mock_s3, mock_ami, mock_profile
    ):
        mock_ssm = mock_boto3.client.return_value
        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}

        from ec2 import launch_ec2_spot_instance
        from scripts.autonomous import build_autonomous_user_data

        launch_ec2_spot_instance(
            "org/repo",
            42,
            "ghp_testtoken",
            "autonomous",
            build_autonomous_user_data,
            sender_login="octocat",
            sender_id="12345",
        )

        mock_ssm.put_parameter.assert_called_once()
        call_args = mock_ssm.put_parameter.call_args
        self.assertEqual(
            call_args[1]["Name"], "/blitzlog/prod/ephemeral/github-token-42"
        )
        self.assertEqual(call_args[1]["Value"], "ghp_testtoken")
        self.assertEqual(call_args[1]["Type"], "SecureString")

    @patch(
        "ec2.get_instance_profile_arn",
        return_value="arn:aws:iam::123:instance-profile/test",
    )
    @patch("ec2.get_agent_ami", return_value="ami-12345")
    @patch("ec2.s3")
    @patch("ec2.boto3")
    @patch("ec2.ec2")
    @patch.dict(
        os.environ,
        {
            "EC2_SECURITY_GROUP_ID": "sg-123",
            "EC2_SUBNET_ID": "subnet-123",
            "VPC_ID": "vpc-123",
            "S3_LOGS_BUCKET": "test-bucket",
        },
    )
    def test_instance_name_has_env_prefix(
        self, mock_ec2, mock_boto3, mock_s3, mock_ami, mock_profile
    ):
        """EC2 instance Name tag must include the env prefix so prod and dev
        instances are visually distinguishable in the AWS console and don't
        collide on a shared Name (which AWS treats as a soft-uniqueness hint).
        """
        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}

        from ec2 import launch_ec2_spot_instance
        from scripts.autonomous import build_autonomous_user_data

        launch_ec2_spot_instance(
            "org/repo",
            42,
            "ghp_testtoken",
            "autonomous",
            build_autonomous_user_data,
            sender_login="octocat",
            sender_id="12345",
        )

        tags = mock_ec2.run_instances.call_args[1]["TagSpecifications"][0]["Tags"]
        name_tag = next(t for t in tags if t["Key"] == "Name")
        self.assertEqual(
            name_tag["Value"],
            "blitzlog-prod-opencode-agent-autonomous-issue-42",
            "EC2 instance Name tag must be `blitzlog-<env>-opencode-agent-<mode>-issue-<N>`",
        )

    @patch(
        "ec2.get_instance_profile_arn",
        return_value="arn:aws:iam::123:instance-profile/test",
    )
    @patch("ec2.get_agent_ami", return_value="ami-12345")
    @patch("ec2.s3")
    @patch("ec2.boto3")
    @patch("ec2.ec2")
    @patch.dict(
        os.environ,
        {
            "EC2_SECURITY_GROUP_ID": "sg-123",
            "EC2_SUBNET_ID": "subnet-123",
            "VPC_ID": "vpc-123",
            "S3_LOGS_BUCKET": "test-bucket",
        },
    )
    def test_uploads_userdata_to_s3(
        self, mock_ec2, mock_boto3, mock_s3, mock_ami, mock_profile
    ):
        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}

        from ec2 import launch_ec2_spot_instance
        from scripts.assisted import build_assisted_user_data

        launch_ec2_spot_instance(
            "org/repo",
            42,
            "ghp_testtoken",
            "assisted",
            build_assisted_user_data,
            sender_login="octocat",
            sender_id="12345",
        )

        mock_s3.put_object.assert_called_once()
        call_args = mock_s3.put_object.call_args
        self.assertEqual(call_args[1]["Bucket"], "test-bucket")
        self.assertTrue(call_args[1]["Key"].startswith("user-data/assisted-issue-42-"))
        body = call_args[1]["Body"].decode()
        self.assertNotIn("ghp_testtoken", body)
        # Git identity is configured inside the agent container via
        # entrypoint.sh (consuming the GITHUB_TOKEN env var). The host
        # bootstrap only writes the env-var plumbing.
        self.assertIn("GITHUB_TOKEN=", body)

    @patch(
        "ec2.get_instance_profile_arn",
        return_value="arn:aws:iam::123:instance-profile/test",
    )
    @patch("ec2.get_agent_ami", return_value="ami-12345")
    @patch("ec2.s3")
    @patch("ec2.boto3")
    @patch("ec2.ec2")
    @patch.dict(
        os.environ,
        {
            "EC2_SECURITY_GROUP_ID": "sg-123",
            "EC2_SUBNET_ID": "subnet-123",
            "VPC_ID": "vpc-123",
            "S3_LOGS_BUCKET": "test-bucket",
        },
    )
    def test_ec2_receives_downloader_not_full_script(
        self, mock_ec2, mock_boto3, mock_s3, mock_ami, mock_profile
    ):
        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}

        from ec2 import launch_ec2_spot_instance
        from scripts.autonomous import build_autonomous_user_data

        launch_ec2_spot_instance(
            "org/repo",
            42,
            "ghp_testtoken",
            "autonomous",
            build_autonomous_user_data,
            sender_login="octocat",
            sender_id="12345",
        )

        run_args = mock_ec2.run_instances.call_args
        user_data_b64 = run_args[1]["UserData"]
        user_data = base64.b64decode(user_data_b64).decode()
        self.assertIn("aws s3 cp", user_data)
        self.assertIn("bash /tmp/bootstrap.sh", user_data)
        self.assertNotIn("opencode", user_data)

    @patch(
        "ec2.get_instance_profile_arn",
        return_value="arn:aws:iam::123:instance-profile/test",
    )
    @patch("ec2.get_agent_ami", return_value="ami-12345")
    @patch("ec2.s3")
    @patch("ec2.boto3")
    @patch("ec2.ec2")
    @patch.dict(
        os.environ,
        {
            "EC2_SECURITY_GROUP_ID": "sg-123",
            "EC2_SUBNET_ID": "subnet-123",
            "VPC_ID": "vpc-123",
            "S3_LOGS_BUCKET": "test-bucket",
        },
    )
    def test_ec2_volume_size_is_20gb(
        self, mock_ec2, mock_boto3, mock_s3, mock_ami, mock_profile
    ):
        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}

        from ec2 import launch_ec2_spot_instance
        from scripts.autonomous import build_autonomous_user_data

        launch_ec2_spot_instance(
            "org/repo", 42, "ghp_testtoken", "autonomous", build_autonomous_user_data
        )

        run_args = mock_ec2.run_instances.call_args
        block_device = run_args[1]["BlockDeviceMappings"]
        self.assertEqual(len(block_device), 1)
        self.assertEqual(block_device[0]["DeviceName"], "/dev/xvda")
        self.assertEqual(block_device[0]["Ebs"]["VolumeSize"], 20)
        self.assertEqual(block_device[0]["Ebs"]["VolumeType"], "gp3")
        self.assertTrue(block_device[0]["Ebs"]["DeleteOnTermination"])

    @patch(
        "ec2.get_instance_profile_arn",
        return_value="arn:aws:iam::123:instance-profile/test",
    )
    @patch("ec2.get_agent_ami", return_value="ami-12345")
    @patch("ec2.s3")
    @patch("ec2.boto3")
    @patch("ec2.ec2")
    @patch.dict(
        os.environ,
        {
            "EC2_SECURITY_GROUP_ID": "sg-123",
            "EC2_SUBNET_ID": "subnet-123",
            "VPC_ID": "vpc-123",
            "S3_LOGS_BUCKET": "test-bucket",
            "BLITZLOG_ENV": "prod",
        },
    )
    def test_instance_has_environment_tag(
        self, mock_ec2, mock_boto3, mock_s3, mock_ami, mock_profile
    ):
        """EC2 instances must carry an `Environment` tag matching the
        Lambda's BLITZLOG_ENV. The ec2_agent_role's `ec2:TerminateInstances`
        IAM condition (infra/modules/core/iam.tf:178-190) keys on
        `ec2:ResourceTag/Environment = var.environment`; without this tag
        the watchdog's `aws ec2 terminate-instances` fails with
        `UnauthorizedOperation` and the instance waits the full 2-hour EC2
        lifecycle timeout.
        """
        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}

        from ec2 import launch_ec2_spot_instance
        from scripts.autonomous import build_autonomous_user_data

        launch_ec2_spot_instance(
            "org/repo", 42, "ghp_testtoken", "autonomous", build_autonomous_user_data
        )

        tags = mock_ec2.run_instances.call_args[1]["TagSpecifications"][0]["Tags"]
        env_tag = next((t for t in tags if t["Key"] == "Environment"), None)
        self.assertIsNotNone(
            env_tag,
            "EC2 instance must be tagged with `Environment` so the EC2 role's "
            "ec2:TerminateInstances IAM condition can match. Without it the "
            "watchdog's terminate call is denied and the instance waits 2h.",
        )
        self.assertEqual(
            env_tag["Value"],
            "prod",
            "Environment tag value must equal BLITZLOG_ENV (set in the Lambda "
            "env by infra/modules/core/lambda.tf:95) so prod and dev resources "
            "are isolated in IAM conditions.",
        )


class TestGetAgentAmi(unittest.TestCase):
    """Tests for get_agent_ami() (Ubuntu 26.04 only — issue #55)."""

    @patch("ec2.ec2")
    @patch("ec2.boto3")
    @patch.dict(os.environ, {"BLITZLOG_ENV": "dev"})
    def test_reads_docker_ubuntu_param(self, mock_boto3, mock_ec2):
        from ec2 import get_agent_ami

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "ami-ubuntu"}}
        mock_boto3.client.return_value = mock_ssm
        mock_ec2.describe_images.return_value = {"Images": [{"ImageId": "ami-ubuntu"}]}

        result = get_agent_ami()
        self.assertEqual(result, "ami-ubuntu")
        mock_ssm.get_parameter.assert_called_once_with(
            Name="/blitzlog/dev/agent-ami-id-docker-ubuntu"
        )

    @patch("ec2.ec2")
    @patch("ec2.boto3")
    @patch.dict(os.environ, {"BLITZLOG_ENV": "prod"})
    def test_falls_back_to_upstream_ubuntu_when_param_missing(
        self, mock_boto3, mock_ec2
    ):
        from ec2 import get_agent_ami

        mock_ssm = MagicMock()
        # First call (primary): SSM param missing. Second call
        # (fallback via get_latest_ubuntu_ami): returns upstream AMI.
        mock_ssm.get_parameter.side_effect = [
            ClientError({"Error": {"Code": "ParameterNotFound"}}, "GetParameter"),
            {"Parameter": {"Value": "ami-upstream-ubuntu"}},
        ]
        mock_boto3.client.return_value = mock_ssm

        result = get_agent_ami()
        self.assertEqual(result, "ami-upstream-ubuntu")
        self.assertEqual(mock_ssm.get_parameter.call_count, 2)

    @patch("ec2.ec2")
    @patch("ec2.boto3")
    @patch.dict(os.environ, {"BLITZLOG_ENV": "prod"})
    def test_clears_param_and_falls_back_when_ami_retired(self, mock_boto3, mock_ec2):
        from ec2 import get_agent_ami

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "ami-stale"}},
            {"Parameter": {"Value": "ami-upstream-ubuntu"}},
        ]
        mock_boto3.client.return_value = mock_ssm
        mock_ec2.describe_images.side_effect = ClientError(
            {"Error": {"Code": "InvalidAMIID.NotFound"}}, "DescribeImages"
        )

        result = get_agent_ami()
        self.assertEqual(result, "ami-upstream-ubuntu")
        mock_ssm.delete_parameter.assert_called_once_with(
            Name="/blitzlog/prod/agent-ami-id-docker-ubuntu"
        )

    @patch("ec2.ec2")
    @patch("ec2.boto3")
    @patch.dict(os.environ, {"BLITZLOG_ENV": "prod"})
    def test_propagates_unexpected_client_error(self, mock_boto3, mock_ec2):
        from ec2 import get_agent_ami

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "GetParameter"
        )
        mock_boto3.client.return_value = mock_ssm
        with self.assertRaises(ClientError):
            get_agent_ami()


if __name__ == "__main__":
    unittest.main()
