import base64
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

from handler import (
    _IDLE_WATCHDOG_PLUGIN_JS,
    _PERIODIC_AUTOSAVE_PLUGIN_JS,
    _SHUTDOWN_TOOL_JS,
    _SPOT_WATCHDOG_PLUGIN_JS,
    MISE_VERSION,
    _build_s3_downloader_script,
    _configure_git_script,
    _decode_api_errors_script,
    _install_toolchain_script,
    _install_whisper_stt_script,
    _read_secrets_from_ssm_script,
    _write_opencode_config_script,
    _write_periodic_autosave_plugin_script,
    _write_spot_watchdog_plugin_script,
    acquire_bot_token,
    build_assisted_user_data,
    build_autonomous_user_data,
    get_az_subnet_map,
    get_spot_prices,
)


class TestGetSpotPrices(unittest.TestCase):
    @patch("handler.ec2")
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

    @patch("handler.ec2")
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

    @patch("handler.ec2")
    def test_empty_response(self, mock_ec2):
        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        result = get_spot_prices(["t4g.medium"])
        self.assertEqual(result, [])

    @patch("handler.ec2")
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
    @patch("handler.ec2")
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
    @patch("handler.ec2")
    def test_empty_subnets(self, mock_ec2):
        mock_ec2.describe_subnets.return_value = {"Subnets": []}
        result = get_az_subnet_map()
        self.assertEqual(result, {})

    @patch.dict(os.environ, {"VPC_ID": ""})
    def test_missing_vpc_id(self):
        result = get_az_subnet_map()
        self.assertEqual(result, {})

    @patch.dict(os.environ, {"VPC_ID": "vpc-12345"})
    @patch("handler.ec2")
    def test_api_failure_returns_empty(self, mock_ec2):
        from botocore.exceptions import ClientError

        mock_ec2.describe_subnets.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "boom"}}, "DescribeSubnets"
        )
        result = get_az_subnet_map()
        self.assertEqual(result, {})


class TestNoSecretsInUserData(unittest.TestCase):
    def test_autonomous_no_embedded_token(self):
        script = build_autonomous_user_data("org/repo", 42, "octocat", "12345")
        self.assertNotIn("ghp_", script)
        self.assertNotIn("x-access-token:ghp_", script)
        self.assertIn("${_CC_GITHUB_TOKEN}", script)
        self.assertIn('user.name "octocat"', script)
        self.assertIn('user.email "12345+octocat@users.noreply.github.com"', script)

    def test_assisted_no_embedded_token(self):
        script = build_assisted_user_data(
            "org/repo",
            42,
            "octocat",
            "12345",
            bot_name="escobar",
            bot_token="123456:ABC",
            telegram_user_id="99999",
        )
        self.assertNotIn("ghp_", script)
        self.assertNotIn("x-access-token:ghp_", script)
        self.assertIn("${_CC_GITHUB_TOKEN}", script)
        self.assertIn('user.name "octocat"', script)
        self.assertIn('user.email "12345+octocat@users.noreply.github.com"', script)
        self.assertIn("escobar", script)

    @patch.dict(
        os.environ,
        {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"},
    )
    def test_assisted_no_embedded_stt_api_key(self):
        # STT_API_KEY is a SecureString; the user-data must reference the
        # runtime-fetched shell variable rather than bake a literal value.
        script = build_assisted_user_data(
            "org/repo",
            42,
            "octocat",
            "12345",
            bot_name="escobar",
            bot_token="123456:ABC",
            telegram_user_id="99999",
        )
        self.assertIn("STT_API_KEY=${STT_API_KEY}", script)
        self.assertIn("export STT_API_KEY", script)
        # The bot .env heredoc assigns from the runtime shell variable only;
        # no literal value should be embedded.
        self.assertNotRegex(script, r"STT_API_KEY=[^$\n][^\n]*")

    def test_no_identity_without_sender(self):
        script = build_autonomous_user_data("org/repo", 42)
        self.assertNotIn("user.name", script)
        self.assertNotIn("user.email", script)


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


class TestSSMSecretsScript(unittest.TestCase):
    def test_fetches_github_token_from_ephemeral_param(self):
        script = _read_secrets_from_ssm_script(42)
        self.assertIn("/blitzlog/ephemeral/github-token-42", script)
        self.assertIn("export _CC_GITHUB_TOKEN", script)
        self.assertIn("export OPENCODE_API_KEY", script)

    def test_different_issue_numbers(self):
        script13 = _read_secrets_from_ssm_script(13)
        script99 = _read_secrets_from_ssm_script(99)
        self.assertIn("github-token-13", script13)
        self.assertIn("github-token-99", script99)
        self.assertNotIn("github-token-99", script13)

    def test_fetches_stt_params(self):
        script = _read_secrets_from_ssm_script(42)
        self.assertIn("/blitzlog/stt/api-url", script)
        self.assertIn("/blitzlog/stt/api-key", script)
        self.assertIn("/blitzlog/stt/model", script)
        self.assertIn("/blitzlog/stt/language", script)
        self.assertIn("/blitzlog/stt/models-bucket", script)
        self.assertIn("export STT_API_URL", script)
        self.assertIn("export STT_API_KEY", script)
        self.assertIn("export STT_MODEL", script)
        self.assertIn("export STT_LANGUAGE", script)
        self.assertIn("export STT_MODELS_BUCKET", script)

    def test_stt_api_key_uses_with_decryption(self):
        script = _read_secrets_from_ssm_script(42)
        stt_key_idx = script.find("/blitzlog/stt/api-key")
        self.assertNotEqual(stt_key_idx, -1)
        self.assertIn("--with-decryption", script[stt_key_idx : stt_key_idx + 200])


class TestSTTInBotConfig(unittest.TestCase):
    @patch.dict(
        os.environ,
        {
            "S3_LOGS_BUCKET": "test-bucket",
            "OPENCODE_MODEL": "minimax-coding-plan/MiniMax-M3",
        },
    )
    def test_bot_env_has_stt_api_url(self):
        user_data = build_assisted_user_data(
            "owner/repo",
            42,
            sender_login="octocat",
            bot_name="escobar",
            bot_token="123:ABC",
            telegram_user_id="99999",
        )
        self.assertIn("STT_API_URL=${STT_API_URL}", user_data)

    @patch.dict(
        os.environ,
        {
            "S3_LOGS_BUCKET": "test-bucket",
            "OPENCODE_MODEL": "minimax-coding-plan/MiniMax-M3",
        },
    )
    def test_bot_env_has_stt_api_key(self):
        user_data = build_assisted_user_data(
            "owner/repo",
            42,
            sender_login="octocat",
            bot_name="escobar",
            bot_token="123:ABC",
            telegram_user_id="99999",
        )
        self.assertIn("STT_API_KEY=${STT_API_KEY}", user_data)

    @patch.dict(
        os.environ,
        {
            "S3_LOGS_BUCKET": "test-bucket",
            "OPENCODE_MODEL": "minimax-coding-plan/MiniMax-M3",
        },
    )
    def test_bot_env_has_stt_model_and_language(self):
        user_data = build_assisted_user_data(
            "owner/repo",
            42,
            sender_login="octocat",
            bot_name="escobar",
            bot_token="123:ABC",
            telegram_user_id="99999",
        )
        self.assertIn("STT_MODEL=${STT_MODEL}", user_data)
        self.assertIn("STT_LANGUAGE=${STT_LANGUAGE}", user_data)
        self.assertIn("STT_REQUEST_FORMAT=multipart", user_data)

    def test_whisper_install_script_downloads_from_github_release(self):
        script = _install_whisper_stt_script()
        self.assertIn("github.com/ggml-org/whisper.cpp/releases/download", script)
        self.assertIn("whisper-bin-aarch64-linux-gnu", script)

    def test_whisper_install_script_falls_back_to_source_build(self):
        script = _install_whisper_stt_script()
        self.assertIn("building whisper.cpp from source", script)
        self.assertIn("cmake -S", script)

    def test_whisper_install_script_downloads_model_from_s3(self):
        script = _install_whisper_stt_script()
        self.assertIn("aws s3 cp", script)
        self.assertIn("s3://${STT_MODELS_BUCKET}/models/", script)
        self.assertIn("ggml-${STT_MODEL}.bin", script)

    def test_whisper_install_script_writes_shim_source(self):
        script = _install_whisper_stt_script()
        self.assertIn("/opt/whisper-stt/server.js", script)
        self.assertIn('require("busboy")', script)
        self.assertIn("ffmpeg-static", script)

    def test_whisper_install_script_writes_package_json(self):
        script = _install_whisper_stt_script()
        self.assertIn("/opt/whisper-stt/package.json", script)
        self.assertIn('"busboy"', script)
        self.assertIn('"ffmpeg-static"', script)
        self.assertIn("npm install", script)

    def test_whisper_install_script_installs_systemd_unit(self):
        script = _install_whisper_stt_script()
        self.assertIn("/etc/systemd/system/whisper-stt-shim.service", script)
        self.assertIn("systemctl enable whisper-stt-shim.service", script)
        self.assertIn("systemctl restart whisper-stt-shim.service", script)

    def test_whisper_install_script_health_checks_before_bot(self):
        script = _install_whisper_stt_script()
        self.assertIn("http://127.0.0.1:7878/healthz", script)
        self.assertIn("curl -sf", script)

    def test_whisper_install_script_embeds_loaded_shim_source(self):
        script = _install_whisper_stt_script()
        # The embedded source must contain recognizable shim identifiers so we
        # catch accidental overwrites / empty reads during refactors.
        self.assertIn('require("busboy")', script)
        self.assertIn('require("ffmpeg-static")', script)
        self.assertIn("whisper-stt-shim listening", script)

    def test_whisper_install_script_writes_clean_server_heredoc(self):
        # Heredocs with quoted delimiters take the body literally. The body
        # must NOT be wrapped in stray single quotes (that bug produces a 3-byte
        # file and breaks the bot). Regression for issue surfaced on EC2.
        script = _install_whisper_stt_script()
        shim_block = script.split("<<'__WHISPER_SHIM_JS__'\n", 1)[1].split(
            "__WHISPER_SHIM_JS__", 1
        )[0]
        self.assertFalse(
            shim_block.startswith("'"),
            f"server.js heredoc body must not be wrapped in stray quotes; got: {shim_block[:60]!r}",
        )
        self.assertGreater(
            len(shim_block), 1000, "shim body should be ~5KB; got suspiciously short"
        )
        self.assertIn("const http = require(", shim_block)
        self.assertIn("server.listen(PORT, HOST", shim_block)

    def test_whisper_install_script_writes_clean_package_json_heredoc(self):
        # The package.json heredoc body must be valid JSON, not wrapped in
        # quotes. The bug produces invalid JSON like:
        #   '{\n  "name": "@blitzlog/whisper-stt-shim",\n...
        # which npm correctly fails to parse with EJSONPARSE.
        script = _install_whisper_stt_script()
        pkg_block = script.split("<<'__WHISPER_SHIM_PKG__'\n", 1)[1].split(
            "__WHISPER_SHIM_PKG__", 1
        )[0]
        self.assertFalse(
            pkg_block.startswith("'"),
            f"package.json heredoc body must not be wrapped in stray quotes; got: {pkg_block[:60]!r}",
        )
        self.assertTrue(pkg_block.lstrip().startswith("{"))
        self.assertTrue(pkg_block.rstrip().endswith("}"))
        # Parses as JSON
        import json as _json

        parsed = _json.loads(pkg_block)
        self.assertEqual(parsed["name"], "@blitzlog/whisper-stt-shim")

    def test_whisper_install_script_writes_clean_systemd_heredoc(self):
        script = _install_whisper_stt_script()
        unit_block = script.split("<<'__WHISPER_SHIM_UNIT__'\n", 1)[1].split(
            "__WHISPER_SHIM_UNIT__", 1
        )[0]
        self.assertFalse(
            unit_block.startswith("'"),
            f"systemd unit heredoc body must not be wrapped in stray quotes; got: {unit_block[:60]!r}",
        )
        self.assertTrue(unit_block.lstrip().startswith("[Unit]"))
        self.assertIn("ExecStart=/usr/bin/node server.js", unit_block)
        self.assertIn("WantedBy=multi-user.target", unit_block)

    def test_whisper_install_script_does_not_use_npm_silent(self):
        # --silent hides npm errors. Drop it so future failures surface.
        script = _install_whisper_stt_script()
        self.assertNotIn("--silent", script)
        self.assertIn("npm install", script)


class TestOpencodeProviderConfig(unittest.TestCase):
    def test_heredoc_uses_minimax_provider(self):
        script = _write_opencode_config_script()
        self.assertIn('"minimax-coding-plan":', script)

    def test_heredoc_provider_block_has_no_legacy_providers(self):
        script = _write_opencode_config_script()
        provider_block = script.split('"provider":', 1)[1].split("}", 1)[0]
        self.assertNotIn("zai", provider_block)
        self.assertNotIn("glm", provider_block)

    def test_heredoc_injects_api_key_from_env(self):
        script = _write_opencode_config_script()
        self.assertIn("{env:OPENCODE_API_KEY}", script)

    @patch.dict(
        os.environ,
        {
            "S3_LOGS_BUCKET": "test-bucket",
            "OPENCODE_MODEL": "minimax-coding-plan/MiniMax-M3",
        },
    )
    def test_autonomous_user_data_uses_minimax_model(self):
        user_data = build_autonomous_user_data("owner/repo", 42)
        self.assertIn("minimax-coding-plan", user_data)
        self.assertIn("OPENCODE_MODEL", user_data)

    @patch.dict(
        os.environ,
        {
            "S3_LOGS_BUCKET": "test-bucket",
            "OPENCODE_MODEL": "minimax-coding-plan/MiniMax-M3",
        },
    )
    def test_assisted_user_data_uses_minimax_model(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("minimax-coding-plan", user_data)
        self.assertIn("OPENCODE_MODEL_PROVIDER=minimax-coding-plan", user_data)

    @patch.dict(
        os.environ,
        {
            "S3_LOGS_BUCKET": "test-bucket",
            "OPENCODE_MODEL": "minimax-coding-plan/MiniMax-M3",
        },
    )
    def test_autonomous_user_data_logs_config_diagnostic(self):
        user_data = build_autonomous_user_data("owner/repo", 42)
        self.assertIn("Effective opencode config", user_data)
        self.assertIn("api_key_prefix", user_data)

    @patch.dict(
        os.environ,
        {
            "S3_LOGS_BUCKET": "test-bucket",
            "OPENCODE_MODEL": "minimax-coding-plan/MiniMax-M3",
        },
    )
    def test_assisted_user_data_logs_config_diagnostic(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("Effective opencode config", user_data)
        self.assertIn("api_key_prefix", user_data)

    def test_default_opencode_model_is_minimax(self):
        with patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"}, clear=True):
            self.assertIn(
                "minimax-coding-plan/MiniMax-M3",
                build_autonomous_user_data("owner/repo", 1),
            )
            self.assertIn(
                "minimax-coding-plan/MiniMax-M3",
                build_assisted_user_data("owner/repo", 1),
            )


class TestDecodeApiErrorsScript(unittest.TestCase):
    def test_decodes_insufficient_balance_1008(self):
        script = _decode_api_errors_script()
        self.assertIn("1008", script)
        self.assertIn("insufficient", script)
        self.assertIn("platform.minimax.io", script)

    def test_decodes_unauthorized_401(self):
        script = _decode_api_errors_script()
        self.assertIn("Unauthorized", script)
        self.assertIn("401", script)
        self.assertIn("opencode/api-key", script)

    def test_decodes_rate_limit_429(self):
        script = _decode_api_errors_script()
        self.assertIn("429", script)
        self.assertIn("rate", script.lower())

    def test_watchdog_invokes_decoder(self):
        with patch.dict(
            os.environ,
            {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"},
        ):
            user_data = build_autonomous_user_data("owner/repo", 42)
        self.assertIn("ACTIONABLE", user_data)
        self.assertIn("insufficient_balance", user_data)
        self.assertIn("platform.minimax.io", user_data)


class TestConfigureGitScript(unittest.TestCase):
    def test_uses_env_var_not_literal(self):
        script = _configure_git_script()
        self.assertIn("${_CC_GITHUB_TOKEN}", script)
        self.assertNotIn("x-access-token:ghp_", script)

    def test_no_identity_when_no_sender(self):
        script = _configure_git_script()
        self.assertNotIn("user.name", script)
        self.assertNotIn("user.email", script)

    def test_sets_identity_with_sender_info(self):
        script = _configure_git_script(
            "octocat", "12345+octocat@users.noreply.github.com"
        )
        self.assertIn('git config --global user.name "octocat"', script)
        self.assertIn(
            'git config --global user.email "12345+octocat@users.noreply.github.com"',
            script,
        )

    def test_no_identity_with_empty_login(self):
        script = _configure_git_script("", "12345")
        self.assertNotIn("user.name", script)
        self.assertNotIn("user.email", script)


class TestLaunchEc2SpotInstance(unittest.TestCase):
    @patch(
        "handler.get_instance_profile_arn",
        return_value="arn:aws:iam::123:instance-profile/test",
    )
    @patch("handler.get_latest_al2023_ami", return_value="ami-12345")
    @patch("handler.s3")
    @patch("handler.ssm")
    @patch("handler.ec2")
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
        self, mock_ec2, mock_ssm, mock_s3, mock_ami, mock_profile
    ):
        from handler import launch_ec2_spot_instance

        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}

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
        self.assertEqual(call_args[1]["Name"], "/blitzlog/ephemeral/github-token-42")
        self.assertEqual(call_args[1]["Value"], "ghp_testtoken")
        self.assertEqual(call_args[1]["Type"], "SecureString")

    @patch(
        "handler.get_instance_profile_arn",
        return_value="arn:aws:iam::123:instance-profile/test",
    )
    @patch("handler.get_latest_al2023_ami", return_value="ami-12345")
    @patch("handler.s3")
    @patch("handler.ssm")
    @patch("handler.ec2")
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
        self, mock_ec2, mock_ssm, mock_s3, mock_ami, mock_profile
    ):
        from handler import launch_ec2_spot_instance

        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}

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
        self.assertIn('user.name "octocat"', body)
        self.assertIn('user.email "12345+octocat@users.noreply.github.com"', body)

    @patch(
        "handler.get_instance_profile_arn",
        return_value="arn:aws:iam::123:instance-profile/test",
    )
    @patch("handler.get_latest_al2023_ami", return_value="ami-12345")
    @patch("handler.s3")
    @patch("handler.ssm")
    @patch("handler.ec2")
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
        self, mock_ec2, mock_ssm, mock_s3, mock_ami, mock_profile
    ):
        from handler import launch_ec2_spot_instance

        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}

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
        "handler.get_instance_profile_arn",
        return_value="arn:aws:iam::123:instance-profile/test",
    )
    @patch("handler.get_latest_al2023_ami", return_value="ami-12345")
    @patch("handler.s3")
    @patch("handler.ssm")
    @patch("handler.ec2")
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
        self, mock_ec2, mock_ssm, mock_s3, mock_ami, mock_profile
    ):
        from handler import launch_ec2_spot_instance

        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-123"}]}

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


class TestShutdownTool(unittest.TestCase):
    def test_tool_uses_plain_object_export(self):
        self.assertIn("export default", _SHUTDOWN_TOOL_JS)

    def test_tool_has_no_external_imports(self):
        self.assertNotIn("import", _SHUTDOWN_TOOL_JS)

    def test_tool_calls_shutdown_script(self):
        self.assertIn("/usr/local/bin/assisted-shutdown.sh", _SHUTDOWN_TOOL_JS)

    def test_tool_has_description(self):
        self.assertIn("Shut down this assisted agent instance", _SHUTDOWN_TOOL_JS)

    def test_tool_has_args(self):
        self.assertIn("args: {}", _SHUTDOWN_TOOL_JS)

    def test_tool_has_execute(self):
        self.assertIn("async execute()", _SHUTDOWN_TOOL_JS)


class TestAssistedShutdownInUserData(unittest.TestCase):
    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_user_data_contains_shutdown_tool(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("shutdown.js", user_data)
        self.assertIn("SHUTDOWN_TOOL_JS", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_autonomous_user_data_excludes_shutdown_tool(self):
        user_data = build_autonomous_user_data("owner/repo", 42)
        self.assertNotIn("shutdown.js", user_data)
        self.assertNotIn("SHUTDOWN_TOOL_JS", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_user_data_contains_shutdown_script(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("assisted-shutdown.sh", user_data)
        self.assertIn("shutdown -h now", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_plugins_use_global_directory(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("/root/.config/opencode/tools/shutdown.js", user_data)
        self.assertIn("/root/.config/opencode/plugins/idle-watchdog.js", user_data)
        self.assertNotIn("/workspace/repo/.opencode/plugins/", user_data)


class TestToolchainBootstrapScript(unittest.TestCase):
    def test_session_archive_uses_global_directory(self):
        from handler import _write_session_archive_plugin_script

        script = _write_session_archive_plugin_script()
        self.assertIn("/root/.config/opencode/plugins/session-archive.js", script)
        self.assertNotIn("/workspace/repo/.opencode/plugins", script)

    def test_script_installs_mise(self):
        script = _install_toolchain_script()
        self.assertIn("mise.run", script)
        self.assertIn("mise install", script)
        self.assertIn(f'MISE_VERSION="{MISE_VERSION}"', script)

    def test_mise_version_constant_is_pinned(self):

        self.assertRegex(MISE_VERSION, r"\Av\d{4}\.\d+\.\d+\Z")
        self.assertNotEqual(
            MISE_VERSION,
            "v2026.7.0",
            "v2026.7.0 requires GLIBC_2.38/2.39 and breaks AL2023",
        )

    def test_script_pins_mise_version_env(self):
        script = _install_toolchain_script()
        self.assertIn("MISE_VERSION=", script)
        self.assertNotIn('MISE_VERSION="${MISE_VERSION}"', script)

    def test_script_checks_config_files(self):
        script = _install_toolchain_script()
        self.assertIn("mise.toml", script)
        self.assertIn(".tool-versions", script)

    def test_script_trusts_config(self):
        script = _install_toolchain_script()
        self.assertIn("mise trust", script)

    def test_script_sets_up_shims_path(self):
        script = _install_toolchain_script()
        self.assertIn("mise/shims", script)
        self.assertIn("/etc/profile.d/mise.sh", script)

    def test_script_handles_missing_config(self):
        script = _install_toolchain_script()
        self.assertIn("No mise.toml or .tool-versions found", script)

    def test_toolchain_runs_bootstrap_if_present(self):
        script = _install_toolchain_script()
        self.assertIn("bootstrap", script)
        self.assertIn("mise tasks --name-only", script)

    def test_no_secrets_in_toolchain_script(self):
        script = _install_toolchain_script()
        self.assertNotIn("ghp_", script)
        self.assertNotIn("sk-", script)


class TestToolchainInUserData(unittest.TestCase):
    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_autonomous_includes_toolchain(self):
        user_data = build_autonomous_user_data("owner/repo", 42)
        self.assertIn("mise install", user_data)
        self.assertIn("mise.toml", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_includes_toolchain(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("mise install", user_data)
        self.assertIn("mise.toml", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_toolchain_runs_after_clone(self):
        user_data = build_autonomous_user_data("owner/repo", 42)
        clone_pos = user_data.index("git clone")
        mise_pos = user_data.index("mise install")
        config_pos = user_data.index("Writing opencode config")
        self.assertGreater(mise_pos, clone_pos)
        self.assertLess(mise_pos, config_pos)


class TestShutdownReasonDetection(unittest.TestCase):
    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_shutdown_script_detects_reason(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("SHUTDOWN_REASON", user_data)
        self.assertIn("spot_interruption", user_data)
        self.assertIn("system_shutdown", user_data)
        self.assertIn("unknown", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_shutdown_script_checks_spot_instance_action(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("spot/instance-action", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_shutdown_script_checks_shutdown_reason_env(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("_SHUTDOWN_REASON", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_telegram_message_includes_reason(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("reason:", user_data)
        self.assertIn("SHUTDOWN_REASON", user_data)


class TestShutdownToolPassesReason(unittest.TestCase):
    def test_tool_passes_agent_requested_reason(self):
        self.assertIn("agent_requested", _SHUTDOWN_TOOL_JS)

    def test_tool_passes_reason_via_env(self):
        self.assertIn("_SHUTDOWN_REASON", _SHUTDOWN_TOOL_JS)


class TestIdleWatchdogPlugin(unittest.TestCase):
    def test_plugin_exports_named_function(self):
        self.assertIn("export const IdleWatchdog", _IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_handles_session_idle(self):
        self.assertIn("session.idle", _IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_handles_session_updated(self):
        self.assertIn("message.part.updated", _IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_handles_session_deleted(self):
        self.assertIn("session.deleted", _IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_sets_autosave_timer(self):
        self.assertIn("5 * 60 * 1000", _IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_sets_ping_timer(self):
        self.assertIn("35 * 60 * 1000", _IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_sets_shutdown_timer(self):
        self.assertIn("3 * 60 * 60 * 1000", _IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_autosave_branch(self):
        self.assertIn("autosave/issue-", _IDLE_WATCHDOG_PLUGIN_JS)
        self.assertIn("ISSUE_NUMBER", _IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_force_pushes(self):
        self.assertIn("push --force", _IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_passes_idle_timeout_reason(self):
        self.assertIn("idle_timeout", _IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_uses_telegram_env_vars(self):
        self.assertIn("TELEGRAM_BOT_TOKEN", _IDLE_WATCHDOG_PLUGIN_JS)
        self.assertIn("TELEGRAM_USER_ID", _IDLE_WATCHDOG_PLUGIN_JS)

    def test_plugin_clears_timers(self):
        self.assertIn("clearTimers", _IDLE_WATCHDOG_PLUGIN_JS)
        self.assertIn("clearTimeout", _IDLE_WATCHDOG_PLUGIN_JS)

    def test_no_secrets_in_plugin(self):
        self.assertNotIn("ghp_", _IDLE_WATCHDOG_PLUGIN_JS)
        self.assertNotIn("sk-", _IDLE_WATCHDOG_PLUGIN_JS)


class TestIdleWatchdogInUserData(unittest.TestCase):
    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_user_data_contains_idle_watchdog_plugin(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("idle-watchdog.js", user_data)
        self.assertIn("IdleWatchdog", user_data)
        self.assertIn("IDLE_WATCHDOG_PLUGIN_JS", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_autonomous_user_data_excludes_idle_watchdog_plugin(self):
        user_data = build_autonomous_user_data("owner/repo", 42)
        self.assertNotIn("idle-watchdog.js", user_data)
        self.assertNotIn("IdleWatchdog", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_telegram_vars_exported_before_opencode(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        export_pos = user_data.index("export TELEGRAM_BOT_TOKEN TELEGRAM_USER_ID")
        serve_pos = user_data.index("opencode serve")
        self.assertLess(export_pos, serve_pos)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_blitzlog_env_includes_telegram_vars(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        env_start = user_data.index("cat > /etc/blitzlog.env")
        env_section = user_data[env_start : env_start + 500]
        self.assertIn("TELEGRAM_BOT_TOKEN", env_section)
        self.assertIn("TELEGRAM_USER_ID", env_section)


class TestSpotWatchdogPlugin(unittest.TestCase):
    def test_plugin_exports_named_function(self):
        self.assertIn("export const SpotWatchdog", _SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_handles_session_created(self):
        self.assertIn("session.created", _SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_handles_session_deleted(self):
        self.assertIn("session.deleted", _SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_polls_imds_spot_action(self):
        self.assertIn("spot/instance-action", _SPOT_WATCHDOG_PLUGIN_JS)
        self.assertIn("169.254.169.254", _SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_uses_set_interval(self):
        self.assertIn("setInterval", _SPOT_WATCHDOG_PLUGIN_JS)
        self.assertIn("5000", _SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_clears_interval_on_deleted(self):
        self.assertIn("clearInterval", _SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_triggers_emergency_save(self):
        self.assertIn("emergencySave", _SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_uses_interruption_branch_name(self):
        self.assertIn("autosave/issue-", _SPOT_WATCHDOG_PLUGIN_JS)
        self.assertIn("interruption-", _SPOT_WATCHDOG_PLUGIN_JS)
        self.assertIn("ISSUE_NUMBER", _SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_force_pushes(self):
        self.assertIn("push --force", _SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_archives_session_to_s3(self):
        self.assertIn("SESSION_ARCHIVE_BUCKET", _SPOT_WATCHDOG_PLUGIN_JS)
        self.assertIn("aws s3 cp", _SPOT_WATCHDOG_PLUGIN_JS)
        self.assertIn("opencode export", _SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_prevents_double_trigger(self):
        self.assertIn("emergencySaveTriggered", _SPOT_WATCHDOG_PLUGIN_JS)

    def test_plugin_logs_via_client(self):
        self.assertIn("client.app.log", _SPOT_WATCHDOG_PLUGIN_JS)
        self.assertIn("spot-watchdog", _SPOT_WATCHDOG_PLUGIN_JS)

    def test_no_secrets_in_plugin(self):
        self.assertNotIn("ghp_", _SPOT_WATCHDOG_PLUGIN_JS)
        self.assertNotIn("sk-", _SPOT_WATCHDOG_PLUGIN_JS)


class TestPeriodicAutosavePlugin(unittest.TestCase):
    def test_plugin_exports_named_function(self):
        self.assertIn("export const PeriodicAutosave", _PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_plugin_handles_session_created(self):
        self.assertIn("session.created", _PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_plugin_handles_session_deleted(self):
        self.assertIn("session.deleted", _PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_plugin_uses_set_interval(self):
        self.assertIn("setInterval", _PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_plugin_uses_5_minute_interval(self):
        self.assertIn("5 * 60 * 1000", _PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_plugin_clears_interval_on_deleted(self):
        self.assertIn("clearInterval", _PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_plugin_uses_stable_branch_name(self):
        self.assertIn("autosave/issue-", _PERIODIC_AUTOSAVE_PLUGIN_JS)
        self.assertIn("-latest", _PERIODIC_AUTOSAVE_PLUGIN_JS)
        self.assertIn("ISSUE_NUMBER", _PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_plugin_force_pushes(self):
        self.assertIn("push --force", _PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_plugin_uses_allow_empty_commit(self):
        self.assertIn("--allow-empty", _PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_plugin_logs_via_client(self):
        self.assertIn("client.app.log", _PERIODIC_AUTOSAVE_PLUGIN_JS)
        self.assertIn("periodic-autosave", _PERIODIC_AUTOSAVE_PLUGIN_JS)

    def test_no_secrets_in_plugin(self):
        self.assertNotIn("ghp_", _PERIODIC_AUTOSAVE_PLUGIN_JS)
        self.assertNotIn("sk-", _PERIODIC_AUTOSAVE_PLUGIN_JS)


class TestSpotWatchdogInUserData(unittest.TestCase):
    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_autonomous_user_data_contains_spot_watchdog_plugin(self):
        user_data = build_autonomous_user_data("owner/repo", 42)
        self.assertIn("spot-watchdog.js", user_data)
        self.assertIn("SpotWatchdog", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_user_data_contains_spot_watchdog_plugin(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("spot-watchdog.js", user_data)
        self.assertIn("SpotWatchdog", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_spot_watchdog_uses_global_directory(self):
        script = _write_spot_watchdog_plugin_script()
        self.assertIn("/root/.config/opencode/plugins/spot-watchdog.js", script)
        self.assertNotIn("/workspace/repo/.opencode/plugins", script)


class TestPeriodicAutosaveInUserData(unittest.TestCase):
    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_autonomous_user_data_contains_periodic_autosave_plugin(self):
        user_data = build_autonomous_user_data("owner/repo", 42)
        self.assertIn("periodic-autosave.js", user_data)
        self.assertIn("PeriodicAutosave", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_user_data_contains_periodic_autosave_plugin(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("periodic-autosave.js", user_data)
        self.assertIn("PeriodicAutosave", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_periodic_autosave_uses_global_directory(self):
        script = _write_periodic_autosave_plugin_script()
        self.assertIn("/root/.config/opencode/plugins/periodic-autosave.js", script)
        self.assertNotIn("/workspace/repo/.opencode/plugins", script)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_autonomous_plugins_after_session_archive(self):
        user_data = build_autonomous_user_data("owner/repo", 42)
        archive_pos = user_data.index("session-archive.js")
        spot_pos = user_data.index("spot-watchdog.js")
        periodic_pos = user_data.index("periodic-autosave.js")
        self.assertGreater(spot_pos, archive_pos)
        self.assertGreater(periodic_pos, archive_pos)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_plugins_after_session_archive(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        archive_pos = user_data.index("session-archive.js")
        spot_pos = user_data.index("spot-watchdog.js")
        periodic_pos = user_data.index("periodic-autosave.js")
        self.assertGreater(spot_pos, archive_pos)
        self.assertGreater(periodic_pos, archive_pos)


class TestAcquireBotToken(unittest.TestCase):
    SENDER = "octocat"

    def _pool_pages(self, names_and_tokens):
        return [
            {
                "Parameters": [
                    {
                        "Name": f"/blitzlog/users/{self.SENDER}/telegram/pool/{name}",
                        "Value": token,
                    }
                    for name, token in names_and_tokens
                ]
            }
        ]

    @patch("handler.s3")
    @patch("handler.ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_all_bots_free_returns_first_sorted(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = self._pool_pages(
            [
                ("escobar", "token1"),
                ("chaparro", "token2"),
                ("frank", "token3"),
            ]
        )
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey"}}, "GetObject"
        )
        mock_s3.put_object.return_value = {}

        result = acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)
        self.assertIsNotNone(result)
        bot_name, bot_token = result
        self.assertEqual(bot_name, "chaparro")
        self.assertEqual(bot_token, "token2")

    @patch("handler.s3")
    @patch("handler.ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_some_bots_locked_skips_to_free(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = self._pool_pages(
            [
                ("escobar", "token1"),
                ("chaparro", "token2"),
                ("frank", "token3"),
            ]
        )

        now = datetime.now(timezone.utc).isoformat()
        lock_body = json.dumps(
            {
                "instance_id": "i-other",
                "issue_number": 1,
                "repo": "org/repo",
                "acquired_at": now,
            }
        ).encode()
        mock_s3.get_object.side_effect = [
            {"Body": MagicMock(read=MagicMock(return_value=lock_body))},
            ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject"),
            ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject"),
        ]
        mock_s3.put_object.return_value = {}

        result = acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)
        self.assertIsNotNone(result)
        bot_name, _ = result
        self.assertEqual(bot_name, "escobar")

    @patch("handler.s3")
    @patch("handler.ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_all_bots_locked_returns_none(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = self._pool_pages(
            [
                ("escobar", "token1"),
                ("frank", "token2"),
            ]
        )

        now = datetime.now(timezone.utc).isoformat()
        lock_body = json.dumps(
            {
                "instance_id": "i-other",
                "issue_number": 1,
                "repo": "org/repo",
                "acquired_at": now,
            }
        ).encode()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=lock_body))
        }

        result = acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)
        self.assertIsNone(result)

    @patch("handler.s3")
    @patch("handler.ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_stale_lock_overwritten(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = self._pool_pages(
            [
                ("escobar", "token1"),
            ]
        )

        stale_time = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        lock_body = json.dumps(
            {
                "instance_id": "i-crashed",
                "issue_number": 1,
                "repo": "org/repo",
                "acquired_at": stale_time,
            }
        ).encode()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=lock_body))
        }
        mock_s3.put_object.return_value = {}

        result = acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)
        self.assertIsNotNone(result)
        bot_name, _ = result
        self.assertEqual(bot_name, "escobar")

    @patch("handler.s3")
    @patch("handler.ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_recent_lock_not_overwritten(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = self._pool_pages(
            [
                ("escobar", "token1"),
            ]
        )

        recent_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        lock_body = json.dumps(
            {
                "instance_id": "i-active",
                "issue_number": 1,
                "repo": "org/repo",
                "acquired_at": recent_time,
            }
        ).encode()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=lock_body))
        }

        result = acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)
        self.assertIsNone(result)

    @patch("handler.ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_empty_pool_returns_none(self, mock_ssm):
        mock_ssm.get_paginator.return_value.paginate.return_value = [{"Parameters": []}]

        result = acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)
        self.assertIsNone(result)

    @patch("handler.s3")
    @patch("handler.ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_lock_key_is_sender_scoped(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = self._pool_pages(
            [
                ("escobar", "token1"),
            ]
        )
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey"}}, "GetObject"
        )
        mock_s3.put_object.return_value = {}

        acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)

        put_call = mock_s3.put_object.call_args
        self.assertEqual(
            put_call[1]["Key"],
            f"bot-pool-locks/{self.SENDER}/escobar.json",
        )

    @patch("handler.s3")
    @patch("handler.ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_get_lock_reads_sender_scoped_key(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = self._pool_pages(
            [
                ("escobar", "token1"),
            ]
        )
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey"}}, "GetObject"
        )
        mock_s3.put_object.return_value = {}

        acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)

        get_call = mock_s3.get_object.call_args
        self.assertEqual(
            get_call[1]["Key"],
            f"bot-pool-locks/{self.SENDER}/escobar.json",
        )

    @patch("handler.s3")
    @patch("handler.ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_pool_pagination_scoped_to_user(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = [{"Parameters": []}]
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey"}}, "GetObject"
        )

        acquire_bot_token(self.SENDER, "i-test", "org/repo", 42)

        paginator_call = mock_ssm.get_paginator.return_value.paginate.call_args
        self.assertEqual(
            paginator_call[1]["Path"],
            f"/blitzlog/users/{self.SENDER}/telegram/pool",
        )

    @patch("handler.s3")
    @patch("handler.ssm")
    @patch.dict(os.environ, {"S3_LOGS_BUCKET": "test-bucket"})
    def test_user_without_pool_returns_none(self, mock_ssm, mock_s3):
        mock_ssm.get_paginator.return_value.paginate.return_value = [{"Parameters": []}]

        result = acquire_bot_token("lonely-user", "i-test", "org/repo", 42)
        self.assertIsNone(result)


class TestGetTelegramUserId(unittest.TestCase):
    @patch("handler.ssm")
    def test_returns_value_when_present(self, mock_ssm):
        from handler import get_telegram_user_id

        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "12345"}}
        self.assertEqual(get_telegram_user_id("octocat"), "12345")
        call = mock_ssm.get_parameter.call_args
        self.assertEqual(
            call[1]["Name"], "/blitzlog/users/octocat/telegram/allowed-user-id"
        )

    @patch("handler.ssm")
    def test_returns_none_when_missing(self, mock_ssm):
        from handler import get_telegram_user_id

        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound"}}, "GetParameter"
        )
        self.assertIsNone(get_telegram_user_id("nobody"))


class TestBotPoolInUserData(unittest.TestCase):
    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_user_data_contains_bot_name(self):
        user_data = build_assisted_user_data(
            "owner/repo",
            42,
            sender_login="octocat",
            bot_name="escobar",
            bot_token="123:ABC",
            telegram_user_id="99999",
        )
        self.assertIn("[Bot: escobar]", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_user_data_injects_token_directly(self):
        user_data = build_assisted_user_data(
            "owner/repo",
            42,
            sender_login="octocat",
            bot_name="escobar",
            bot_token="123:ABC",
            telegram_user_id="99999",
        )
        self.assertIn('TELEGRAM_BOT_TOKEN="123:ABC"', user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_user_data_injects_user_id_directly(self):
        user_data = build_assisted_user_data(
            "owner/repo",
            42,
            sender_login="octocat",
            bot_name="escobar",
            bot_token="123:ABC",
            telegram_user_id="99999",
        )
        self.assertIn('TELEGRAM_USER_ID="99999"', user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_user_data_no_ssm_telegram_reads(self):
        user_data = build_assisted_user_data(
            "owner/repo",
            42,
            sender_login="octocat",
            bot_name="escobar",
            bot_token="123:ABC",
            telegram_user_id="99999",
        )
        self.assertNotIn("/blitzlog/telegram/bot-token", user_data)
        self.assertNotIn("/blitzlog/telegram/allowed-user-id", user_data)
        self.assertNotIn("/blitzlog/users/octocat/telegram/allowed-user-id", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_shutdown_releases_sender_scoped_lock(self):
        user_data = build_assisted_user_data(
            "owner/repo",
            42,
            sender_login="octocat",
            bot_name="escobar",
            bot_token="123:ABC",
            telegram_user_id="99999",
        )
        self.assertIn("bot-pool-locks/octocat/escobar.json", user_data)
        self.assertIn("Released bot pool lock for octocat/escobar", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_shutdown_notification_includes_bot_name(self):
        user_data = build_assisted_user_data(
            "owner/repo",
            42,
            sender_login="octocat",
            bot_name="escobar",
            bot_token="123:ABC",
            telegram_user_id="99999",
        )
        self.assertIn("[Bot: escobar]", user_data)


class TestGetGithubAppToken(unittest.TestCase):
    @patch("handler.jwt.encode", return_value="fake.jwt.token")
    @patch("handler.requests.post")
    @patch("handler.get_ssm_param")
    def test_posts_repo_scoped_body_for_long_lived_token(
        self, mock_ssm, mock_post, mock_jwt_encode
    ):
        from handler import get_github_app_token

        mock_ssm.side_effect = lambda name, with_decryption=True: {
            "github-app/id": "12345",
            "github-app/private-key": base64.b64encode(b"fake-key").decode(),
            "github-app/installation-id": "67890",
        }[name]
        mock_post.return_value.ok = True
        mock_post.return_value.json.return_value = {"token": "ghp_abc123"}
        mock_post.return_value.raise_for_status = MagicMock()

        result = get_github_app_token("owner/repo")

        self.assertEqual(result, "ghp_abc123")
        mock_jwt_encode.assert_called_once()
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(call_kwargs["json"], {"repositories": ["repo"]})
        url = mock_post.call_args.args[0]
        self.assertIn("/installations/67890/access_tokens", url)

    @patch("handler.jwt.encode", return_value="fake.jwt.token")
    @patch("handler.requests.post")
    @patch("handler.get_ssm_param")
    def test_scopes_to_single_repo_for_eight_hour_lifetime(
        self, mock_ssm, mock_post, mock_jwt_encode
    ):
        from handler import get_github_app_token

        mock_ssm.side_effect = lambda name, with_decryption=True: {
            "github-app/id": "1",
            "github-app/private-key": base64.b64encode(b"k").decode(),
            "github-app/installation-id": "2",
        }[name]
        mock_post.return_value.ok = True
        mock_post.return_value.json.return_value = {"token": "t"}
        mock_post.return_value.raise_for_status = MagicMock()

        get_github_app_token("org/very-specific-repo")

        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(len(body["repositories"]), 1)
        self.assertEqual(body["repositories"][0], "very-specific-repo")

    @patch("handler.jwt.encode", return_value="fake.jwt.token")
    @patch("handler.requests.post")
    @patch("handler.get_ssm_param")
    def test_logs_response_body_and_raises_on_422(
        self, mock_ssm, mock_post, mock_jwt_encode
    ):
        import requests as real_requests
        from handler import get_github_app_token

        mock_ssm.side_effect = lambda name, with_decryption=True: {
            "github-app/id": "12345",
            "github-app/private-key": base64.b64encode(b"fake-key").decode(),
            "github-app/installation-id": "67890",
        }[name]

        error_response = real_requests.Response()
        error_response.status_code = 422
        error_response._content = (
            b'{"message":"Validation Failed","errors":["Bad repository name"]}'
        )
        mock_post.return_value = error_response

        with self.assertRaises(real_requests.HTTPError):
            get_github_app_token("owner/repo")

        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(call_kwargs["json"], {"repositories": ["repo"]})


class TestNodeVersionGuard(unittest.TestCase):
    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_installs_node_24_via_tarball(self):
        # Direct tarball from nodejs.org — bypasses AL2023 dnf repo gaps that
        # left the previous dnf install silently failing on some AMIs (set -eu
        # with `dnf install | tail -5` returned tail's exit code, masking dnf
        # failures; the bot then ran on the AL2023 default Node v20, which
        # the upstream Telegram bot rejects with "requires Node.js 22.14+").
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn(
            "https://nodejs.org/dist/v24.6.0/node-v24.6.0-linux-arm64.tar.xz",
            user_data,
        )
        self.assertIn("tar -xJ -C /usr/local --strip-components=1", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_guards_node_install_by_major_version(self):
        # Idempotency: skip the download when the AL2023 base image already
        # ships a new-enough Node (e.g., future AMIs).
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("NODE_MAJOR=$(node --version", user_data)
        self.assertIn('if [ "$NODE_MAJOR" -lt 24 ]', user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_no_dnf_node_install(self):
        # Regression guard: the previous dnf-based install silently failed on
        # some AL2023 AMIs (nodejs24 not in default repos + `| tail -5`
        # masking the exit code). If anyone reverts to dnf, this fires.
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertNotIn("dnf install -y nodejs24", user_data)
        self.assertNotIn("dnf install -y nodejs22", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_invokes_bot_via_npx(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("npx -y @grinev/opencode-telegram-bot@latest start", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_no_bot_install_line(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertNotIn("npm install -g @grinev/opencode-telegram-bot", user_data)
        self.assertNotIn("npm-22 install -g @grinev/opencode-telegram-bot", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_no_build_tools_install(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertNotIn("dnf install -y gcc-c++ make python3", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_no_hardcoded_cli_path(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertNotIn(
            "/usr/local/lib/node_modules/@grinev/opencode-telegram-bot/dist/cli.js",
            user_data,
        )

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_no_shebang_patch(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertNotIn("sed -i '1c", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_pre_warms_npx_cache(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("npx -y @grinev/opencode-telegram-bot@latest status", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_pre_warm_before_notification(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        pre_warm_pos = user_data.index("Pre-warming opencode-telegram-bot")
        notification_pos = user_data.index("Sending Telegram notification")
        self.assertLess(pre_warm_pos, notification_pos)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_pre_warm_runs_once(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertEqual(
            user_data.count("npx -y @grinev/opencode-telegram-bot@latest status"),
            1,
        )

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_pre_warm_uses_status_subcommand(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("npx -y @grinev/opencode-telegram-bot@latest status", user_data)
        self.assertNotIn(
            "npx -y @grinev/opencode-telegram-bot@latest --help", user_data
        )

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_pre_warm_captures_exit_code(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("PRE_WARM_EXIT=$?", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_pre_warm_failure_sends_telegram(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        guard_pos = user_data.index('"$PRE_WARM_EXIT" -ne 0')
        failure_block = user_data[guard_pos:]
        self.assertIn("Assisted agent cannot be started", failure_block)
        self.assertIn("sendMessage", failure_block)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_pre_warm_uses_real_chat_id(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        guard_pos = user_data.index('"$PRE_WARM_EXIT" -ne 0')
        failure_block = user_data[guard_pos:]
        self.assertIn('chat_id="${TELEGRAM_USER_ID}"', failure_block)
        self.assertIn("bot${TELEGRAM_BOT_TOKEN}", failure_block)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_pre_warm_continues_on_failure(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        failure_end = user_data.index("Sending Telegram notification")
        bot_install = user_data.index(
            "npx -y @grinev/opencode-telegram-bot@latest start", failure_end
        )
        self.assertGreater(bot_install, failure_end)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_pre_warm_log_written_to_var_log(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("> /var/log/pre-warm.log", user_data)
        self.assertNotIn("/tmp/pre-warm.log", user_data)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_pre_warm_failure_includes_repo_context(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        guard_pos = user_data.index('"$PRE_WARM_EXIT" -ne 0')
        failure_block = user_data[guard_pos : guard_pos + 1500]
        self.assertIn("Repo: ${REPO}", failure_block)
        self.assertIn(
            "[Issue #${ISSUE_NUMBER}: ${ISSUE_TITLE}]",
            failure_block,
        )
        self.assertIn("Mode: Assisted (interactive via Telegram)", failure_block)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_pre_warm_failure_includes_resume_status(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        guard_pos = user_data.index('"$PRE_WARM_EXIT" -ne 0')
        failure_block = user_data[guard_pos : guard_pos + 1500]
        self.assertIn("$RESUME_STATUS", failure_block)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_pre_warm_failure_uses_markdown(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        guard_pos = user_data.index('"$PRE_WARM_EXIT" -ne 0')
        failure_block = user_data[guard_pos : guard_pos + 1500]
        self.assertIn('parse_mode="Markdown"', failure_block)

    @patch.dict(
        os.environ, {"S3_LOGS_BUCKET": "test-bucket", "OPENCODE_MODEL": "test/model"}
    )
    def test_assisted_pre_warm_failure_omits_log_path_hint(self):
        user_data = build_assisted_user_data("owner/repo", 42)
        guard_pos = user_data.index('"$PRE_WARM_EXIT" -ne 0')
        failure_block = user_data[guard_pos : guard_pos + 1500]
        self.assertNotIn("/var/log", failure_block)


class TestLambdaHandlerBotPool(unittest.TestCase):
    @patch("handler._update_lock_instance_id")
    @patch("handler.launch_ec2_spot_instance", return_value="i-123")
    @patch("handler.acquire_bot_token")
    @patch("handler.get_telegram_user_id", return_value="99999")
    @patch("handler.get_github_app_token", return_value="ghp_test")
    @patch("handler.verify_github_signature", return_value=True)
    @patch("handler.get_ssm_param", return_value="secret")
    def test_assisted_calls_acquire_bot_token_with_sender(
        self,
        mock_ssm,
        mock_sig,
        mock_gh,
        mock_tg_user,
        mock_acquire,
        mock_launch,
        mock_update_lock,
    ):
        from handler import lambda_handler

        mock_acquire.return_value = ("escobar", "token123")

        event = {
            "body": json.dumps(
                {
                    "action": "labeled",
                    "issue": {"number": 42, "labels": [{"name": "assisted"}]},
                    "repository": {"full_name": "org/repo"},
                    "sender": {"login": "octocat", "id": "123"},
                }
            ),
            "headers": {"x-hub-signature-256": "sha256=abc"},
        }

        result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        mock_tg_user.assert_called_once_with("octocat")
        mock_acquire.assert_called_once()
        acquire_args = mock_acquire.call_args
        self.assertEqual(acquire_args[0][0], "octocat")
        update_args = mock_update_lock.call_args
        self.assertEqual(update_args[0][0], "octocat")
        self.assertEqual(update_args[0][1], "escobar")

    @patch("handler.get_telegram_user_id", return_value="99999")
    @patch("handler.get_github_app_token", return_value="ghp_test")
    @patch("handler.verify_github_signature", return_value=True)
    @patch("handler.get_ssm_param", return_value="secret")
    @patch("handler.acquire_bot_token", return_value=None)
    def test_pool_exhausted_returns_503(
        self, mock_acquire, mock_ssm, mock_sig, mock_gh, mock_tg_user
    ):
        from handler import lambda_handler

        event = {
            "body": json.dumps(
                {
                    "action": "labeled",
                    "issue": {"number": 42, "labels": [{"name": "assisted"}]},
                    "repository": {"full_name": "org/repo"},
                    "sender": {"login": "octocat", "id": "123"},
                }
            ),
            "headers": {"x-hub-signature-256": "sha256=abc"},
        }

        result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 503)
        self.assertIn("octocat", result["body"])

    @patch("handler.get_github_app_token", return_value="ghp_test")
    @patch("handler.verify_github_signature", return_value=True)
    @patch("handler.get_ssm_param", return_value="secret")
    @patch("handler.get_telegram_user_id", return_value=None)
    def test_user_without_telegram_id_returns_503(
        self, mock_tg_user, mock_ssm, mock_sig, mock_gh
    ):
        from handler import lambda_handler

        event = {
            "body": json.dumps(
                {
                    "action": "labeled",
                    "issue": {"number": 42, "labels": [{"name": "assisted"}]},
                    "repository": {"full_name": "org/repo"},
                    "sender": {"login": "lonely", "id": "9"},
                }
            ),
            "headers": {"x-hub-signature-256": "sha256=abc"},
        }

        result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 503)
        self.assertIn("lonely", result["body"])

    @patch("handler.launch_ec2_spot_instance", return_value="i-123")
    @patch("handler.get_github_app_token", return_value="ghp_test")
    @patch("handler.verify_github_signature", return_value=True)
    @patch("handler.get_ssm_param", return_value="secret")
    @patch("handler.list_bot_pool")
    def test_autonomous_does_not_call_acquire(
        self, mock_pool, mock_ssm, mock_sig, mock_gh, mock_launch
    ):
        from handler import lambda_handler

        event = {
            "body": json.dumps(
                {
                    "action": "labeled",
                    "issue": {"number": 42, "labels": [{"name": "autonomous"}]},
                    "repository": {"full_name": "org/repo"},
                    "sender": {"login": "octocat", "id": "123"},
                }
            ),
            "headers": {"x-hub-signature-256": "sha256=abc"},
        }

        result = lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        mock_pool.assert_not_called()


if __name__ == "__main__":
    unittest.main()
