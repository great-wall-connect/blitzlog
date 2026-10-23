"""EC2 spot-instance launch + subnet/AMI/price helpers."""

import base64
import importlib
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import ec2 as ec2_module
import provider
from botocore.exceptions import ClientError
from ec2 import (
    _DEFAULT_SPOT_INSTANCE_TYPES,
    _build_s3_downloader_script,
    _load_spot_instance_types,
    get_az_subnet_map,
    get_spot_prices,
)


def _boto3_dispatch(ssm=None, ec2=None, iam=None, s3=None):
    """Build a `side_effect` function for `provider.aws._boto3_client`
    that returns the right per-service mock. Used by tests that
    touch multiple boto3 services through the same shim.
    """
    table = {}
    if ssm is not None:
        table["ssm"] = ssm
    if ec2 is not None:
        table["ec2"] = ec2
    if iam is not None:
        table["iam"] = iam
    if s3 is not None:
        table["s3"] = s3

    def _dispatch(service, config=None):
        if service not in table:
            raise AssertionError(
                f"unexpected boto3 client requested: {service!r}; "
                f"known: {sorted(table)}"
            )
        return table[service]

    return _dispatch


class TestGetSpotPrices(unittest.TestCase):
    @patch("provider.aws._boto3_client")
    def test_returns_sorted_by_price(self, mock_boto3_client):
        mock_ec2 = mock_boto3_client.return_value
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

    @patch("provider.aws._boto3_client")
    def test_multiple_instance_types(self, mock_boto3_client):
        mock_ec2 = mock_boto3_client.return_value
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

    @patch("provider.aws._boto3_client")
    def test_empty_response(self, mock_boto3_client):
        mock_ec2 = mock_boto3_client.return_value
        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        result = get_spot_prices(["t4g.medium"])
        self.assertEqual(result, [])

    @patch("provider.aws._boto3_client")
    def test_api_failure_returns_empty(self, mock_boto3_client):
        from botocore.exceptions import ClientError

        mock_ec2 = mock_boto3_client.return_value
        mock_ec2.describe_spot_price_history.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "boom"}},
            "DescribeSpotPriceHistory",
        )
        result = get_spot_prices(["t4g.medium"])
        self.assertEqual(result, [])


class TestGetAzSubnetMap(unittest.TestCase):
    @patch.dict(os.environ, {"VPC_ID": "vpc-12345"})
    @patch("provider.aws._boto3_client")
    def test_returns_az_to_subnet_mapping(self, mock_boto3_client):
        mock_ec2 = mock_boto3_client.return_value
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
    @patch("provider.aws._boto3_client")
    def test_empty_subnets(self, mock_boto3_client):
        mock_ec2 = mock_boto3_client.return_value
        mock_ec2.describe_subnets.return_value = {"Subnets": []}
        result = get_az_subnet_map()
        self.assertEqual(result, {})

    @patch.dict(os.environ, {"VPC_ID": ""})
    def test_missing_vpc_id(self):
        result = get_az_subnet_map()
        self.assertEqual(result, {})

    @patch.dict(os.environ, {"VPC_ID": "vpc-12345"})
    @patch("provider.aws._boto3_client")
    def test_api_failure_returns_empty(self, mock_boto3_client):
        from botocore.exceptions import ClientError

        mock_ec2 = mock_boto3_client.return_value
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
    """The launch path is heavy — it writes the GitHub token to
    SSM, uploads user-data to S3, resolves the AMI (SSM +
    `ec2.describe_images`), looks up the instance profile ARN
    (IAM), and runs the spot-iteration loop. The `#83` rewrite
    routes all of these through the `provider.aws._boto3_client`
    shim so we have to wire multiple per-service mocks in each
    test.
    """

    def setUp(self):
        # Reset the provider singletons so the patched `_boto3_client`
        # shim is the one the cached `get_compute()` / `get_secrets()`
        # / `get_object_store()` call into.
        provider.reset_provider_singletons()

    def _wire_clients(self, mock_boto3_client):
        """Build per-service mocks and wire them into the
        `_boto3_client` shim's `side_effect`. Returns the four
        mocks so each test can assert on the right one.
        """
        mock_ssm = MagicMock()
        mock_s3 = MagicMock()
        mock_ec2 = MagicMock()
        mock_iam = MagicMock()
        mock_boto3_client.side_effect = _boto3_dispatch(
            ssm=mock_ssm, s3=mock_s3, ec2=mock_ec2, iam=mock_iam
        )
        return mock_ssm, mock_s3, mock_ec2, mock_iam

    @patch.dict(
        os.environ,
        {
            "EC2_SECURITY_GROUP_ID": "sg-123",
            "EC2_SUBNET_ID": "subnet-123",
            "VPC_ID": "vpc-123",
            "S3_LOGS_BUCKET": "test-bucket",
            "EC2_INSTANCE_PROFILE_NAME": "blitzlog-agent-profile",
        },
    )
    @patch("provider.aws._boto3_client")
    def test_stores_token_in_ssm(self, mock_boto3_client):
        mock_ssm, mock_s3, mock_ec2, mock_iam = self._wire_clients(mock_boto3_client)
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "ami-12345"}}
        mock_ssm.delete_parameter.return_value = {}
        mock_ec2.describe_images.return_value = {"Images": [{"ImageId": "ami-12345"}]}
        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}
        mock_iam.get_instance_profile.return_value = {
            "InstanceProfile": {
                "Arn": "arn:aws:iam::123:instance-profile/blitzlog-agent-profile"
            }
        }
        mock_s3.put_object.return_value = {}

        from ec2 import launch_ec2_spot_instance
        from scripts.aws.autonomous import build_autonomous_user_data

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
            call_args[1]["Name"],
            "/blitzlog/prod/ephemeral/gh/org/repo/42",
        )
        self.assertEqual(call_args[1]["Value"], "ghp_testtoken")
        self.assertEqual(call_args[1]["Type"], "SecureString")

    @patch.dict(
        os.environ,
        {
            "EC2_SECURITY_GROUP_ID": "sg-123",
            "EC2_SUBNET_ID": "subnet-123",
            "VPC_ID": "vpc-123",
            "S3_LOGS_BUCKET": "test-bucket",
            "EC2_INSTANCE_PROFILE_NAME": "blitzlog-agent-profile",
        },
    )
    @patch("provider.aws._boto3_client")
    def test_instance_name_has_env_prefix(self, mock_boto3_client):
        mock_ssm, mock_s3, mock_ec2, mock_iam = self._wire_clients(mock_boto3_client)
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "ami-12345"}}
        mock_ssm.delete_parameter.return_value = {}
        mock_ec2.describe_images.return_value = {"Images": [{"ImageId": "ami-12345"}]}
        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}
        mock_iam.get_instance_profile.return_value = {
            "InstanceProfile": {
                "Arn": "arn:aws:iam::123:instance-profile/blitzlog-agent-profile"
            }
        }
        mock_s3.put_object.return_value = {}

        from ec2 import launch_ec2_spot_instance
        from scripts.aws.autonomous import build_autonomous_user_data

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

    @patch.dict(
        os.environ,
        {
            "EC2_SECURITY_GROUP_ID": "sg-123",
            "EC2_SUBNET_ID": "subnet-123",
            "VPC_ID": "vpc-123",
            "S3_LOGS_BUCKET": "test-bucket",
            "EC2_INSTANCE_PROFILE_NAME": "blitzlog-agent-profile",
        },
    )
    @patch("provider.aws._boto3_client")
    def test_uploads_userdata_to_s3(self, mock_boto3_client):
        mock_ssm, mock_s3, mock_ec2, mock_iam = self._wire_clients(mock_boto3_client)
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "ami-12345"}}
        mock_ssm.delete_parameter.return_value = {}
        mock_ec2.describe_images.return_value = {"Images": [{"ImageId": "ami-12345"}]}
        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}
        mock_iam.get_instance_profile.return_value = {
            "InstanceProfile": {
                "Arn": "arn:aws:iam::123:instance-profile/blitzlog-agent-profile"
            }
        }
        mock_s3.put_object.return_value = {}

        from ec2 import launch_ec2_spot_instance
        from scripts.aws.assisted import build_assisted_user_data

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

    @patch.dict(
        os.environ,
        {
            "EC2_SECURITY_GROUP_ID": "sg-123",
            "EC2_SUBNET_ID": "subnet-123",
            "VPC_ID": "vpc-123",
            "S3_LOGS_BUCKET": "test-bucket",
            "EC2_INSTANCE_PROFILE_NAME": "blitzlog-agent-profile",
        },
    )
    @patch("provider.aws._boto3_client")
    def test_ec2_receives_downloader_not_full_script(self, mock_boto3_client):
        mock_ssm, mock_s3, mock_ec2, mock_iam = self._wire_clients(mock_boto3_client)
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "ami-12345"}}
        mock_ssm.delete_parameter.return_value = {}
        mock_ec2.describe_images.return_value = {"Images": [{"ImageId": "ami-12345"}]}
        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}
        mock_iam.get_instance_profile.return_value = {
            "InstanceProfile": {
                "Arn": "arn:aws:iam::123:instance-profile/blitzlog-agent-profile"
            }
        }
        mock_s3.put_object.return_value = {}

        from ec2 import launch_ec2_spot_instance
        from scripts.aws.autonomous import build_autonomous_user_data

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

    @patch.dict(
        os.environ,
        {
            "EC2_SECURITY_GROUP_ID": "sg-123",
            "EC2_SUBNET_ID": "subnet-123",
            "VPC_ID": "vpc-123",
            "S3_LOGS_BUCKET": "test-bucket",
            "EC2_INSTANCE_PROFILE_NAME": "blitzlog-agent-profile",
        },
    )
    @patch("provider.aws._boto3_client")
    def test_ec2_volume_size_is_20gb(self, mock_boto3_client):
        mock_ssm, mock_s3, mock_ec2, mock_iam = self._wire_clients(mock_boto3_client)
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "ami-12345"}}
        mock_ssm.delete_parameter.return_value = {}
        mock_ec2.describe_images.return_value = {"Images": [{"ImageId": "ami-12345"}]}
        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}
        mock_iam.get_instance_profile.return_value = {
            "InstanceProfile": {
                "Arn": "arn:aws:iam::123:instance-profile/blitzlog-agent-profile"
            }
        }
        mock_s3.put_object.return_value = {}

        from ec2 import launch_ec2_spot_instance
        from scripts.aws.autonomous import build_autonomous_user_data

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

    @patch.dict(
        os.environ,
        {
            "EC2_SECURITY_GROUP_ID": "sg-123",
            "EC2_SUBNET_ID": "subnet-123",
            "VPC_ID": "vpc-123",
            "S3_LOGS_BUCKET": "test-bucket",
            "BLITZLOG_ENV": "prod",
            "EC2_INSTANCE_PROFILE_NAME": "blitzlog-agent-profile",
        },
    )
    @patch("provider.aws._boto3_client")
    def test_instance_has_environment_tag(self, mock_boto3_client):
        """EC2 instances must carry an `Environment` tag matching the
        Lambda's BLITZLOG_ENV. The ec2_agent_role's `ec2:TerminateInstances`
        IAM condition (infra/aws/modules/core/iam.tf:178-190) keys on
        `ec2:ResourceTag/Environment = var.environment`; without this tag
        the watchdog's `aws ec2 terminate-instances` fails with
        `UnauthorizedOperation` and the instance waits the full 2-hour EC2
        lifecycle timeout.
        """
        mock_ssm, mock_s3, mock_ec2, mock_iam = self._wire_clients(mock_boto3_client)
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "ami-12345"}}
        mock_ssm.delete_parameter.return_value = {}
        mock_ec2.describe_images.return_value = {"Images": [{"ImageId": "ami-12345"}]}
        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}
        mock_iam.get_instance_profile.return_value = {
            "InstanceProfile": {
                "Arn": "arn:aws:iam::123:instance-profile/blitzlog-agent-profile"
            }
        }
        mock_s3.put_object.return_value = {}

        from ec2 import launch_ec2_spot_instance
        from scripts.aws.autonomous import build_autonomous_user_data

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
            "env by infra/aws/modules/core/lambda.tf:95) so prod and dev resources "
            "are isolated in IAM conditions.",
        )


class TestGetAgentAmi(unittest.TestCase):
    """Tests for get_agent_ami() (Ubuntu 26.04 only — issue #55)."""

    def setUp(self):
        provider.reset_provider_singletons()

    @patch("provider.aws._boto3_client")
    @patch.dict(os.environ, {"BLITZLOG_ENV": "dev"})
    def test_reads_docker_ubuntu_param(self, mock_boto3_client):
        from ec2 import get_agent_ami

        mock_ssm = MagicMock()
        mock_ec2 = MagicMock()
        mock_boto3_client.side_effect = _boto3_dispatch(ssm=mock_ssm, ec2=mock_ec2)
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "ami-ubuntu"}}
        mock_ec2.describe_images.return_value = {"Images": [{"ImageId": "ami-ubuntu"}]}

        result = get_agent_ami()
        self.assertEqual(result, "ami-ubuntu")
        # The AMI id is not a secret; AwsSecretStore.get passes
        # `WithDecryption=False` for the lookup. (The pre-#83 code
        # called `boto3.client("ssm").get_parameter(Name=...)`
        # without the WithDecryption kwarg — the AWS SDK defaulted
        # it to False. The new code makes the default explicit.)
        mock_ssm.get_parameter.assert_called_once_with(
            Name="/blitzlog/dev/agent-ami-id-docker-ubuntu",
            WithDecryption=False,
        )

    @patch("provider.aws._boto3_client")
    @patch.dict(os.environ, {"BLITZLOG_ENV": "prod"})
    def test_falls_back_to_upstream_ubuntu_when_param_missing(self, mock_boto3_client):
        from ec2 import get_agent_ami

        mock_ssm = MagicMock()
        mock_ec2 = MagicMock()
        mock_boto3_client.side_effect = _boto3_dispatch(ssm=mock_ssm, ec2=mock_ec2)
        # First call (primary): SSM param missing. Second call
        # (fallback via get_latest_ubuntu_ami): returns upstream AMI.
        mock_ssm.get_parameter.side_effect = [
            ClientError({"Error": {"Code": "ParameterNotFound"}}, "GetParameter"),
            {"Parameter": {"Value": "ami-upstream-ubuntu"}},
        ]

        result = get_agent_ami()
        self.assertEqual(result, "ami-upstream-ubuntu")
        self.assertEqual(mock_ssm.get_parameter.call_count, 2)

    @patch("provider.aws._boto3_client")
    @patch.dict(os.environ, {"BLITZLOG_ENV": "prod"})
    def test_clears_param_and_falls_back_when_ami_retired(self, mock_boto3_client):
        from ec2 import get_agent_ami

        mock_ssm = MagicMock()
        mock_ec2 = MagicMock()
        mock_boto3_client.side_effect = _boto3_dispatch(ssm=mock_ssm, ec2=mock_ec2)
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "ami-stale"}},
            {"Parameter": {"Value": "ami-upstream-ubuntu"}},
        ]
        mock_ec2.describe_images.side_effect = ClientError(
            {"Error": {"Code": "InvalidAMIID.NotFound"}}, "DescribeImages"
        )

        result = get_agent_ami()
        self.assertEqual(result, "ami-upstream-ubuntu")
        mock_ssm.delete_parameter.assert_called_once_with(
            Name="/blitzlog/prod/agent-ami-id-docker-ubuntu"
        )

    @patch("provider.aws._boto3_client")
    @patch.dict(os.environ, {"BLITZLOG_ENV": "prod"})
    def test_propagates_unexpected_client_error(self, mock_boto3_client):
        from ec2 import get_agent_ami

        mock_ssm = MagicMock()
        mock_ec2 = MagicMock()
        mock_boto3_client.side_effect = _boto3_dispatch(ssm=mock_ssm, ec2=mock_ec2)
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "GetParameter"
        )
        with self.assertRaises(ClientError):
            get_agent_ami()


class TestLoadSpotInstanceTypes(unittest.TestCase):
    """Cover the SPOT_INSTANCE_TYPES_JSON env var parser (issue #26).

    Terraform is the source of truth at deploy time, but we still need to
    behave sensibly when the env var is absent (unit tests), malformed
    (operator typo in tfvars), or has the wrong shape (e.g. accidentally
    passed a JSON object instead of a list).
    """

    def setUp(self):
        # Snapshot the module-level constant so we can restore it after each
        # test — the value is frozen at import time, but reload below is the
        # safer way to exercise the parser.
        self._original = ec2_module.SPOT_INSTANCE_TYPES
        self._saved_env = os.environ.pop("SPOT_INSTANCE_TYPES_JSON", None)

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("SPOT_INSTANCE_TYPES_JSON", None)
        else:
            os.environ["SPOT_INSTANCE_TYPES_JSON"] = self._saved_env
        ec2_module.SPOT_INSTANCE_TYPES = self._original

    def _reload(self):
        importlib.reload(ec2_module)

    def test_missing_env_falls_back_to_defaults(self):
        os.environ.pop("SPOT_INSTANCE_TYPES_JSON", None)
        self.assertEqual(
            _load_spot_instance_types(), list(_DEFAULT_SPOT_INSTANCE_TYPES)
        )

    def test_empty_string_falls_back_to_defaults(self):
        os.environ["SPOT_INSTANCE_TYPES_JSON"] = ""
        self.assertEqual(
            _load_spot_instance_types(), list(_DEFAULT_SPOT_INSTANCE_TYPES)
        )

    def test_whitespace_only_falls_back_to_defaults(self):
        os.environ["SPOT_INSTANCE_TYPES_JSON"] = "   "
        self.assertEqual(
            _load_spot_instance_types(), list(_DEFAULT_SPOT_INSTANCE_TYPES)
        )

    def test_valid_json_list_is_returned(self):
        os.environ["SPOT_INSTANCE_TYPES_JSON"] = json.dumps(
            ["m6i.large", "m6i.xlarge", "c6i.large"]
        )
        self.assertEqual(
            _load_spot_instance_types(),
            ["m6i.large", "m6i.xlarge", "c6i.large"],
        )

    def test_malformed_json_falls_back_to_defaults(self):
        os.environ["SPOT_INSTANCE_TYPES_JSON"] = "[t4g.medium, t4g.large"
        self.assertEqual(
            _load_spot_instance_types(), list(_DEFAULT_SPOT_INSTANCE_TYPES)
        )

    def test_non_list_json_falls_back_to_defaults(self):
        os.environ["SPOT_INSTANCE_TYPES_JSON"] = json.dumps({"types": ["t4g.medium"]})
        self.assertEqual(
            _load_spot_instance_types(), list(_DEFAULT_SPOT_INSTANCE_TYPES)
        )

    def test_empty_list_falls_back_to_defaults(self):
        os.environ["SPOT_INSTANCE_TYPES_JSON"] = "[]"
        self.assertEqual(
            _load_spot_instance_types(), list(_DEFAULT_SPOT_INSTANCE_TYPES)
        )

    def test_list_with_non_string_falls_back_to_defaults(self):
        os.environ["SPOT_INSTANCE_TYPES_JSON"] = json.dumps(["t4g.medium", 42])
        self.assertEqual(
            _load_spot_instance_types(), list(_DEFAULT_SPOT_INSTANCE_TYPES)
        )

    def test_list_with_empty_string_falls_back_to_defaults(self):
        os.environ["SPOT_INSTANCE_TYPES_JSON"] = json.dumps(["t4g.medium", ""])
        self.assertEqual(
            _load_spot_instance_types(), list(_DEFAULT_SPOT_INSTANCE_TYPES)
        )

    def test_module_level_constant_uses_env_var(self):
        """After import (or reload), SPOT_INSTANCE_TYPES must reflect the env var
        so launch_ec2_spot_instance picks up the operator's override."""
        os.environ["SPOT_INSTANCE_TYPES_JSON"] = json.dumps(["c7g.large"])
        self._reload()
        try:
            self.assertEqual(ec2_module.SPOT_INSTANCE_TYPES, ["c7g.large"])
        finally:
            # Reload with no env var so other tests in this module see the default.
            os.environ.pop("SPOT_INSTANCE_TYPES_JSON", None)
            self._reload()

    def test_module_level_constant_falls_back_without_env_var(self):
        """Bare import (no SPOT_INSTANCE_TYPES_JSON) must produce the defaults,
        matching the behaviour other modules and out-of-Lambda callers depend on."""
        os.environ.pop("SPOT_INSTANCE_TYPES_JSON", None)
        self._reload()
        self.assertEqual(
            ec2_module.SPOT_INSTANCE_TYPES, list(_DEFAULT_SPOT_INSTANCE_TYPES)
        )


if __name__ == "__main__":
    unittest.main()
