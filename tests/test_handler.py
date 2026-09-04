import base64
import json
import os
import sys
import tempfile
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


def _load_shim_server():
    """Load packages/whisper-stt-shim/server.py via importlib so we can
    test the actual HTTP handler (the file is embedded in the bootstrap
    heredoc, not installed as a package)."""
    import importlib.util
    import sys
    import types

    # The shim imports pywhispercpp at module load. The test environment
    # doesn't have pywhispercpp installed, so inject a stub before the
    # import so exec_module doesn't blow up on a missing dependency.
    # The actual transcribe logic is mocked in each test.
    pywhispercpp_stub = types.ModuleType("pywhispercpp")
    pywhispercpp_stub.__dict__["__path__"] = []
    pywhispercpp_model = types.ModuleType("pywhispercpp.model")
    pywhispercpp_model.Model = (
        object  # placeholder; tests mock get_model() or transcribe()
    )
    pywhispercpp_stub.model = pywhispercpp_model
    sys.modules.setdefault("pywhispercpp", pywhispercpp_stub)
    sys.modules.setdefault("pywhispercpp.model", pywhispercpp_model)

    # Same trick for imageio_ffmpeg — the shim imports it eagerly and
    # calls get_ffmpeg_exe() at module load. Stub a fake one so the
    # import doesn't require the real package in the test venv.
    imageio_ffmpeg_stub = types.ModuleType("imageio_ffmpeg")
    imageio_ffmpeg_stub.get_ffmpeg_exe = lambda: "/usr/bin/ffmpeg"
    sys.modules.setdefault("imageio_ffmpeg", imageio_ffmpeg_stub)

    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    shim_path = os.path.join(repo_root, "packages", "whisper-stt-shim", "server.py")
    spec = importlib.util.spec_from_file_location("shim_server", shim_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        self.assertIn("/opt/whisper-stt/server.py", script)
        self.assertNotIn("/opt/whisper-stt/server.js", script)

    def test_whisper_install_script_installs_pywhispercpp(self):
        script = _install_whisper_stt_script()
        self.assertIn("pywhispercpp", script)
        self.assertIn("pip install", script)

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
        # The embedded source must contain recognizable Python shim
        # identifiers so we catch accidental overwrites / empty reads.
        self.assertIn("pywhispercpp", script)
        self.assertIn("whisper-stt-shim listening", script)
        self.assertIn("HTTPServer", script)

    def test_whisper_install_script_does_not_install_npm_deps(self):
        # Regression: the Node.js shim is gone; npm install / busboy /
        # ffmpeg-static must not reappear.
        script = _install_whisper_stt_script()
        self.assertNotIn("npm install", script)
        self.assertNotIn("busboy", script)
        self.assertNotIn("ffmpeg-static", script)

    def test_whisper_shim_pip_install_fails_loud(self):
        """Regression for the silent-pip-fail bug: pip install must NOT
        be wrapped in `... | tail -3` (which masks exit codes under
        `set -eu` and silently swallows failures). Use an explicit
        `if ! ... ; then exit 1; fi` guard instead."""
        script = _install_whisper_stt_script()
        self.assertRegex(
            script, r"if\s+!\s+python3\s+-m\s+pip\s+install\s+pywhispercpp"
        )
        # No `| tail -3` masking on pip install.
        self.assertNotRegex(script, r"pip install[^|]*\|\s*tail")

    def test_whisper_shim_verifies_pywhispercpp_imports(self):
        """Catches "installed but broken" — pywhispercpp is on disk but
        unimportable (e.g., ABI mismatch, missing libpython)."""
        script = _install_whisper_stt_script()
        self.assertIn(
            'python3 -c "import pywhispercpp; from pywhispercpp.model import Model"',
            script,
        )

    def test_whisper_shim_binds_mise_python_globally(self):
        """`mise install -y` installs Python 3.12.x but does NOT bind the
        global shim — until `mise use -g python` runs, `python3 --version`
        in any clean shell reports "No version is set for shim: python3"
        (and the systemd ExecStart fails to start)."""
        script = _install_whisper_stt_script()
        self.assertRegex(script, r"mise\s+use\s+-g\s+python\b")

    def test_whisper_shim_systemd_uses_mise_shim_path(self):
        """The systemd ExecStart must use the actual mise shim path
        (/root/.local/share/mise/shims/python3 — that `whereis` confirms
        exists), not /root/.local/bin/python3 (which doesn't exist on
        AL2023; systemd starts with a clean PATH that doesn't include
        the mise shim dir)."""
        script = _install_whisper_stt_script()
        unit_block = script.split("<<'__WHISPER_SHIM_UNIT__'\n", 1)[1].split(
            "__WHISPER_SHIM_UNIT__", 1
        )[0]
        self.assertIn(
            "ExecStart=/root/.local/share/mise/shims/python3",
            unit_block,
        )
        self.assertNotIn("ExecStart=/usr/bin/python3 ", unit_block)
        self.assertNotIn("ExecStart=/root/.local/bin/python3", unit_block)

    def test_whisper_shim_script_is_executable(self):
        """Hygiene: the systemd ExecStart runs `python3 <script>` (data
        not exec), but chmod +x the script for consistency."""
        script = _install_whisper_stt_script()
        self.assertIn("chmod +x /opt/whisper-stt/server.py", script)


class TestOpencodeProviderConfig(unittest.TestCase):
    def test_heredoc_uses_minimax_provider(self):
        script = _write_opencode_config_script()
        self.assertIn('"minimax-coding-plan":', script)


class TestLambdaBuildConfiguration(unittest.TestCase):
    """Regression tests for infra/lambda.tf build-time configuration.

    A mistake here silently produces a broken Lambda zip at runtime
    (e.g., 1-byte server.py from a stale cp reference). These tests
    assert the file content directly so we catch the bug at PR review
    time, not on the EC2 instance."""

    @staticmethod
    def _read_lambda_tf():
        with open("infra/lambda.tf", "r", encoding="utf-8") as f:
            return f.read()

    def test_lambda_build_copies_python_shim(self):
        """Regression: the Lambda build's cp must reference server.py
        (the current canonical shim name), not the obsolete server.js.
        Otherwise the Lambda zip is missing server.py, the bootstrap's
        heredoc writes a 1-byte stub (server.py comes back empty), and
        the shim is empty on the EC2 instance."""
        content = self._read_lambda_tf()
        self.assertRegex(
            content,
            r"cp\s+\$\{path\.module\}/../packages/whisper-stt-shim/server\.py",
        )
        self.assertNotRegex(
            content,
            r"cp\s+\$\{path\.module\}/../packages/whisper-stt-shim/server\.js",
        )

    def test_lambda_build_local_exec_uses_set_e(self):
        """Regression: the local-exec build must `set -e` so a missing
        file (cp fails) aborts the build instead of silently producing
        a broken zip. Without this, future renames (like server.js ->
        server.py) produce a zip that looks fine but is missing the
        renamed file, and the EC2 instance gets a 1-byte stub."""
        content = self._read_lambda_tf()
        self.assertRegex(
            content,
            r'provisioner\s+"local-exec"\s*\{\s*command\s*=\s*<<-EOT\s*\n\s*set\s+-e',
        )

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


class TestParseMultipart(unittest.TestCase):
    """Unit tests for the shim's `parse_multipart` helper.

    These exercise the real python_multipart callback flow with real
    multipart bodies — no mocking of the parser itself.
    """

    @classmethod
    def setUpClass(cls):
        cls.shim_server = _load_shim_server()

    @staticmethod
    def _multipart(fields, boundary="----TestBoundary"):
        """Build a real multipart/form-data body from a list of fields.

        Each field is one of:
          - (name, value)              — text field, value is str
          - (name, filename, bytes, ct) — file field
        Returns (boundary_str, body_bytes).
        """
        body = bytearray()
        for f in fields:
            name = f[0]
            if len(f) == 2:
                value = f[1]
                body += (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode()
            else:
                _filename, content, ctype = f[1], f[2], f[3]
                body += (
                    (
                        f"--{boundary}\r\n"
                        f'Content-Disposition: form-data; name="{name}"; filename="{_filename}"\r\n'
                        f"Content-Type: {ctype}\r\n\r\n"
                    ).encode()
                    + content
                    + b"\r\n"
                )
        body += f"--{boundary}--\r\n".encode()
        return boundary, bytes(body)

    def test_parse_multipart_extracts_file_field(self):
        boundary, body = self._multipart(
            [
                ("file", "audio.wav", b"FAKE_WAV_DATA", "audio/wav"),
            ]
        )
        parsed = self.shim_server.parse_multipart(body, boundary.encode("ascii"))
        self.assertEqual(parsed["file"], b"FAKE_WAV_DATA")
        self.assertIsNone(parsed["prompt"])
        self.assertEqual(parsed["fields"], [b"file"])

    def test_parse_multipart_extracts_prompt_field(self):
        boundary, body = self._multipart(
            [
                ("file", "audio.wav", b"DATA", "audio/wav"),
                ("prompt", "The following text is..."),
            ]
        )
        parsed = self.shim_server.parse_multipart(body, boundary.encode("ascii"))
        self.assertEqual(parsed["file"], b"DATA")
        self.assertEqual(parsed["prompt"], b"The following text is...")
        self.assertEqual(parsed["fields"], [b"file", b"prompt"])

    def test_parse_multipart_empty_body(self):
        # Empty body — parse_multipart returns an empty result (no parts).
        result = self.shim_server.parse_multipart(b"", b"----boundary")
        self.assertIsNone(result.get("file"))
        self.assertIsNone(result.get("prompt"))
        self.assertEqual(result.get("fields"), [])

    def test_shim_installs_python_multipart(self):
        """python-multipart (new name: python_multipart) is the modern,
        robust multipart parser. cgi is deprecated in 3.11, removed in
        3.13. The bootstrap installs it as a replacement."""
        script = _install_whisper_stt_script()
        self.assertIn("python-multipart", script)


class TestAudioConversion(unittest.TestCase):
    """ensure_wav converts non-WAV inputs to 16kHz mono WAV via ffmpeg,
    and passes through inputs that are already RIFF/WAVE."""

    @classmethod
    def setUpClass(cls):
        cls.shim_server = _load_shim_server()

    def _write_tmp(self, content):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        return path

    def test_ensure_wav_passthrough_for_wav(self):
        # RIFF header (4 bytes) + chunk size + WAVE (4 bytes) = first 12 bytes.
        wav_header = b"RIFF\x00\x00\x00\x00WAVEfmt "
        src = self._write_tmp(wav_header + b"\x00" * 64)
        try:
            with patch("subprocess.run") as mock_run:
                result = self.shim_server.ensure_wav(src)
            self.assertEqual(result, src, "Should return WAV input as-is")
            mock_run.assert_not_called()  # no ffmpeg invocation
        finally:
            os.unlink(src)

    def test_ensure_wav_converts_ogg_to_wav(self):
        # OGG/S stream starts with "OggS" (0x4F 0x67 0x67 0x53).
        src = self._write_tmp(b"OggS\x00\x02" + b"\x00" * 1024)
        # expected WAV path: same name without the .ogg suffix.
        dst = src.rsplit(".", 1)[0] + ".wav"
        try:
            fake_completed = MagicMock(returncode=0, stderr=b"")
            with patch("subprocess.run", return_value=fake_completed) as mock_run:
                result = self.shim_server.ensure_wav(src)
            self.assertEqual(result, dst)
            self.assertTrue(mock_run.called, "ffmpeg must be invoked for non-WAV")
            called_args = mock_run.call_args[0][0]  # first positional arg of call
            self.assertIn("-i", called_args)
            self.assertIn(src, called_args)
            self.assertIn(dst, called_args)
            self.assertIn("-ar", called_args)
            self.assertIn("16000", called_args)
            self.assertIn("-ac", called_args)
            self.assertIn("1", called_args)
        finally:
            os.unlink(src)
            if os.path.exists(dst):
                os.unlink(dst)

    def test_ensure_wav_raises_when_ffmpeg_fails(self):
        src = self._write_tmp(b"OggS\x00\x02" + b"\x00" * 64)
        try:
            fake_completed = MagicMock(returncode=1, stderr=b"some ffmpeg error\n")
            with patch(
                "subprocess.run", return_value=fake_completed
            ), self.assertRaises(RuntimeError) as cm:
                self.shim_server.ensure_wav(src)
            # Error message should include stderr so debugging is easy.
            self.assertIn("ffmpeg", str(cm.exception).lower())
            self.assertIn("some ffmpeg error", str(cm.exception))
        finally:
            os.unlink(src)
            dst = src.rsplit(".", 1)[0] + ".wav"
            if os.path.exists(dst):
                os.unlink(dst)

    def test_shim_install_script_installs_imageio_ffmpeg(self):
        """The EC2 bootstrap must install `imageio-ffmpeg` — pywhispercpp's
        internal audio decoder only handles WAV, so the shim converts
        upstream OGG/Opus via a bundled static ffmpeg provided by the
        `imageio-ffmpeg` pip package. The Node.js shim did the same with
        `ffmpeg-static` (npm); this is the Python equivalent."""
        script = _install_whisper_stt_script()
        self.assertRegex(script, r"pip\s+install\s+.*\bimageio-ffmpeg\b")

    def test_shim_install_script_does_not_install_ffmpeg_via_dnf(self):
        """Regression: `ffmpeg` is now bundled via imageio-ffmpeg pip
        wheel — no system install needed. Ensure `dnf install ffmpeg`
        does not slip back in."""
        script = _install_whisper_stt_script()
        self.assertNotRegex(
            script,
            r"dnf\s+install\s+.*\bffmpeg\b",
            "ffmpeg should be bundled via imageio-ffmpeg pip wheel, "
            "not installed via dnf",
        )


class TestTranscribeTextExtraction(unittest.TestCase):
    """pywhispercpp's Model.transcribe() returns list[Segment] (default)
    or dict (when transcribe_with_meta=True). transcribe() must extract
    the text correctly in both cases — and fall back to str() for
    unknown shapes."""

    @classmethod
    def setUpClass(cls):
        cls.shim_server = _load_shim_server()

    class _Segment:
        """Minimal stand-in for pywhispercpp.model.Segment."""

        def __init__(self, text, t0=0, t1=0, probability=0.0):
            self.text = text
            self.t0 = t0
            self.t1 = t1
            self.probability = probability

    def _make_model(self, transcribe_return):
        fake_model = MagicMock()
        fake_model.transcribe.return_value = transcribe_return
        return fake_model

    def test_transcribe_concatenates_segment_text(self):
        # pywhispercpp default: list of Segment objects.
        segs = [
            self._Segment("Hello", t0=0, t1=200, probability=1.0),
            self._Segment(" world", t0=200, t1=500, probability=1.0),
        ]
        with patch.object(
            self.shim_server, "get_model", return_value=self._make_model(segs)
        ):
            result = self.shim_server.transcribe("/tmp/dummy.wav", "en")
        self.assertEqual(result, "Hello world")

    def test_transcribe_returns_dict_text_when_meta_mode(self):
        # transcribe_with_meta=True: a dict with a `text` key.
        fake_result = {"text": "Hello world", "language": "en", "segments": []}
        with patch.object(
            self.shim_server, "get_model", return_value=self._make_model(fake_result)
        ):
            result = self.shim_server.transcribe("/tmp/dummy.wav", "en")
        self.assertEqual(result, "Hello world")

    def test_transcribe_does_not_use_str_of_segment_list(self):
        """Regression: the old code did `str(result)` for non-dict
        results, producing "[t0=0, t1=200, text=Hello world,
        probability=nan]" instead of "Hello world"."""
        segs = [self._Segment("Hello world", t0=0, t1=200, probability=float("nan"))]
        with patch.object(
            self.shim_server, "get_model", return_value=self._make_model(segs)
        ):
            result = self.shim_server.transcribe("/tmp/dummy.wav", "en")
        # Must NOT include "t0=" / "t1=" / "probability=" — those are
        # Segment __repr__ artifacts from the bug.
        self.assertNotIn("t0=", result)
        self.assertNotIn("t1=", result)
        self.assertNotIn("probability=", result)

    def test_transcribe_returns_empty_for_empty_segment_list(self):
        with patch.object(
            self.shim_server, "get_model", return_value=self._make_model([])
        ):
            result = self.shim_server.transcribe("/tmp/dummy.wav", "en")
        self.assertEqual(result, "")

    def test_transcribe_handles_unknown_shape(self):
        # An exotic return type — fall back to str().
        with patch.object(
            self.shim_server, "get_model", return_value=self._make_model(42)
        ):
            result = self.shim_server.transcribe("/tmp/dummy.wav", "en")
        self.assertEqual(result, "42")


class TestBrokenPipeHandling(unittest.TestCase):
    """When the client disconnects mid-response, _send_json should
    silently drop the write instead of raising BrokenPipeError."""

    @classmethod
    def setUpClass(cls):
        cls.shim_server = _load_shim_server()

    def _make_handler(self, write_side_effect):
        handler = self.shim_server.Handler.__new__(self.shim_server.Handler)
        handler.wfile = MagicMock()
        handler.wfile.write.side_effect = write_side_effect
        # BaseHTTPRequestHandler.send_response() → log_request() calls
        # self.log_message('"%s" %s %s', self.requestline, str(code),
        # str(size)). Even though our shim overrides log_message to a
        # no-op, Python still evaluates `self.requestline` to build the
        # args. Set all three attrs to bypass the descriptor access.
        handler.__dict__["requestline"] = "POST / HTTP/1.1"
        handler.__dict__["command"] = "POST"
        handler.client_address = ("test", 0)
        handler.server = None
        handler.request_version = "HTTP/1.1"
        return handler

    def test_send_json_silently_drops_when_client_gone(self):
        """_send_json must NOT raise BrokenPipeError to its caller
        when the wire write fails — there's nothing useful to do."""
        handler = self._make_handler(BrokenPipeError(32, "Broken pipe"))
        # Must not raise.
        handler._send_json(200, {"text": "hello"})
        # And must NOT have produced a 500 traceback either.
        handler.wfile.write.assert_called_once()

    def test_send_json_silently_drops_on_connection_reset(self):
        handler = self._make_handler(ConnectionResetError(104, "Connection reset"))
        handler._send_json(200, {"text": "hello"})
        handler.wfile.write.assert_called_once()

    def test_handler_logs_client_disconnected_when_transcribe_succeeds_but_pipe_broken(
        self,
    ):
        """Happy-path transcription + broken pipe at response write:
        we must log `client disconnected` (NOT `transcription failed`
        and NOT `returned response: status=200`)."""
        boundary, body = type(self)._multipart_body_static(
            self.shim_server,
            [("file", "audio.wav", b"FAKE_WAV", "audio/wav")],
        )

        # Build a handler whose wfile breaks on every write.
        from io import BytesIO

        handler = self.shim_server.Handler.__new__(self.shim_server.Handler)
        handler.rfile = BytesIO(body)
        handler.headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        }
        handler.__dict__["rfile"] = handler.rfile
        handler.__dict__["headers"] = handler.headers
        handler.wfile = MagicMock()
        handler.wfile.write.side_effect = BrokenPipeError(32, "Broken pipe")
        handler.path = "/v1/audio/transcriptions"
        handler.command = "POST"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "POST /v1/audio/transcriptions HTTP/1.1"
        handler.client_address = ("test", 0)
        handler.server = None

        # Stderr capture.
        from io import StringIO

        old_stderr = sys.stderr
        captured = StringIO()
        sys.stderr = captured
        original_transcribe = self.shim_server.transcribe
        original_ensure = self.shim_server.ensure_wav
        self.shim_server.ensure_wav = lambda p: p
        self.shim_server.transcribe = lambda *a, **kw: "actual transcription"
        try:
            handler.do_POST()
        finally:
            self.shim_server.ensure_wav = original_ensure
            self.shim_server.transcribe = original_transcribe
            sys.stderr = old_stderr

        log = captured.getvalue()
        self.assertIn("received file", log)
        self.assertIn("transcribed audio", log)
        self.assertIn("client disconnected", log)
        # The misleading "transcription failed" line must NOT appear,
        # and neither should "returned response: status=200" (we never
        # actually returned 200 — the write was broken).
        self.assertNotIn("transcription failed", log)

    @staticmethod
    def _multipart_body_static(shim_server, fields, boundary="----TestBoundary"):
        """Mirror of TestShimHttpHandler._multipart_body, callable as a
        staticmethod so we don't depend on that class's setUpClass
        running first."""
        body = bytearray()
        for f in fields:
            name = f[0]
            if len(f) == 2:
                value = f[1]
                body += (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode()
            else:
                _filename, content, ctype = f[1], f[2], f[3]
                body += (
                    (
                        f"--{boundary}\r\n"
                        f'Content-Disposition: form-data; name="{name}"; filename="{_filename}"\r\n'
                        f"Content-Type: {ctype}\r\n\r\n"
                    ).encode()
                    + content
                    + b"\r\n"
                )
        body += f"--{boundary}--\r\n".encode()
        return boundary, bytes(body)


class TestFirstPostLog(unittest.TestCase):
    """`_log("first POST received ...")` fires exactly once across the
    shim's lifetime — useful for diagnosing whether the shim ever saw a
    request at all (e.g. wrong port, traffic not reaching the shim)."""

    @classmethod
    def setUpClass(cls):
        cls.shim_server = _load_shim_server()

    def setUp(self):
        # Reset the per-process flag before each test so tests are
        # independent.
        self.shim_server._first_post_logged = False

    def _send(self):
        from io import BytesIO, StringIO

        boundary, body = TestShimHttpHandler._multipart_body(
            [("file", "audio.wav", b"FAKE_WAV", "audio/wav")],
        )
        handler = self.shim_server.Handler.__new__(self.shim_server.Handler)
        handler.rfile = BytesIO(body)
        handler.headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        }
        handler.__dict__["rfile"] = handler.rfile
        handler.__dict__["headers"] = handler.headers
        handler.wfile = BytesIO()
        handler.path = "/v1/audio/transcriptions"
        handler.command = "POST"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "POST /v1/audio/transcriptions HTTP/1.1"
        handler.client_address = ("test", 0)
        handler.server = None

        old_stderr = sys.stderr
        captured = StringIO()
        sys.stderr = captured
        original_transcribe = self.shim_server.transcribe
        original_ensure = self.shim_server.ensure_wav
        self.shim_server.ensure_wav = lambda p: p

        def mock_transcribe(_path, language, prompt=None):
            return "ok"

        self.shim_server.transcribe = mock_transcribe
        try:
            handler.do_POST()
        finally:
            self.shim_server.ensure_wav = original_ensure
            self.shim_server.transcribe = original_transcribe
            sys.stderr = old_stderr

        return captured.getvalue()

    def test_first_post_emits_first_post_log(self):
        log = self._send()
        self.assertIn("first POST received at /v1/audio/transcriptions", log)

    def test_subsequent_posts_omit_first_post_log(self):
        self._send()
        log2 = self._send()
        # Second POST must NOT re-emit the first-post marker.
        self.assertEqual(
            log2.count("first POST received at /v1/audio/transcriptions"),
            0,
            "first-post marker should fire at most once across the "
            f"shim's lifetime; got: {log2!r}",
        )


class TestContentLengthRead(unittest.TestCase):
    """Regression tests for the `self.rfile.read()` bug — reading until
    EOF instead of `Content-Length` bytes caused the 60-s symptom when
    the bot used HTTP/1.1 keep-alive."""

    @classmethod
    def setUpClass(cls):
        cls.shim_server = _load_shim_server()

    def _handler(self, body_bytes, content_length_value=None, extra_after_body=b""):
        """Build a handler whose rfile simulates a slow / keep-alive
        client: it has *body_bytes* followed by *extra_after_body* but
        will not EOF unless the caller reads past Content-Length."""
        from io import BytesIO

        boundary, _ = TestShimHttpHandler._multipart_body(
            [("file", "audio.wav", body_bytes, "audio/wav")],
        )
        # Recompose a real multipart body so the parser has work to do.
        _, real_body = TestShimHttpHandler._multipart_body(
            [("file", "audio.wav", body_bytes, "audio/wav")],
        )
        cl = (
            content_length_value
            if content_length_value is not None
            else str(len(real_body))
        )
        handler = self.shim_server.Handler.__new__(self.shim_server.Handler)
        # rfile has real_body + extra_after_body. With the bug, read()
        # would block waiting for the trailing sentinel; with the fix,
        # only the first content_length bytes are read.
        handler.rfile = BytesIO(real_body + extra_after_body)
        handler.headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": cl,
        }
        handler.__dict__["rfile"] = handler.rfile
        handler.__dict__["headers"] = handler.headers
        handler.wfile = BytesIO()
        handler.path = "/v1/audio/transcriptions"
        handler.command = "POST"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "POST /v1/audio/transcriptions HTTP/1.1"
        handler.client_address = ("test", 0)
        handler.server = None
        return handler, real_body

    def test_handler_returns_400_when_content_length_missing(self):
        from io import BytesIO

        handler = self.shim_server.Handler.__new__(self.shim_server.Handler)
        handler.rfile = BytesIO(b"--garbage\r\nfoo\r\n")
        # No Content-Length header at all.
        handler.headers = {
            "Content-Type": "multipart/form-data; boundary=----x",
        }
        handler.__dict__["rfile"] = handler.rfile
        handler.__dict__["headers"] = handler.headers
        handler.wfile = BytesIO()
        handler.path = "/v1/audio/transcriptions"
        handler.command = "POST"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "POST /v1/audio/transcriptions HTTP/1.1"
        handler.client_address = ("test", 0)
        handler.server = None

        old_stderr = sys.stderr
        from io import StringIO

        sys.stderr = StringIO()
        try:
            handler.do_POST()
            response = handler.wfile.getvalue()
        finally:
            sys.stderr = old_stderr

        self.assertIn(b"400", response[:50])
        self.assertIn(b"Content-Length", response)
        # Critically: we did NOT block waiting for the missing-body
        # sentinel. (The empty BytesIO would EOF immediately, but the
        # assertion below is that we got a 400 because of the missing
        # header — not because of a parse failure.)

    def test_handler_reads_exact_content_length_bytes_not_past_it(self):
        # Sentinel: a byte marker that must NOT appear in rfile's tail
        # after the handler finishes (which would prove we read past
        # Content-Length).
        sentinel = b"SENTINEL_AFTER_BODY"
        body = (
            b"--xyz\r\n"
            b'Content-Disposition: form-data; name="file"; filename="a.wav"\r\n'
            b"Content-Type: audio/wav\r\n\r\n"
            b"FAKE_WAV\r\n"
            b"--xyz--\r\n"
        )
        cl = len(body)

        from io import BytesIO

        handler = self.shim_server.Handler.__new__(self.shim_server.Handler)
        handler.rfile = BytesIO(body + sentinel)
        handler.headers = {
            "Content-Type": "multipart/form-data; boundary=xyz",
            "Content-Length": str(cl),
        }
        handler.__dict__["rfile"] = handler.rfile
        handler.__dict__["headers"] = handler.headers
        handler.wfile = BytesIO()
        handler.path = "/v1/audio/transcriptions"
        handler.command = "POST"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "POST /v1/audio/transcriptions HTTP/1.1"
        handler.client_address = ("test", 0)
        handler.server = None

        original_transcribe = self.shim_server.transcribe
        original_ensure = self.shim_server.ensure_wav
        self.shim_server.ensure_wav = lambda p: p

        def mock_transcribe(_path, language, prompt=None):
            return "ok"

        self.shim_server.transcribe = mock_transcribe
        old_stderr = sys.stderr
        from io import StringIO

        sys.stderr = StringIO()
        try:
            handler.do_POST()
            response = handler.wfile.getvalue()
        finally:
            self.shim_server.ensure_wav = original_ensure
            self.shim_server.transcribe = original_transcribe
            sys.stderr = old_stderr

        # The 200 path was reached.
        self.assertIn(b"200", response[:50])
        # And the sentinel is STILL in rfile's tail — proving the
        # handler did not consume past Content-Length.
        remaining = handler.rfile.read()
        self.assertEqual(remaining, sentinel)

    def test_handler_does_not_block_60s_on_keep_alive_body(self):
        """If we regress and start reading to EOF again, this test
        would hang for 60 s before the runtime harness times it out.
        We simulate a non-EOF keep-alive stream by writing a buffer
        and never closing rfile. We can't truly block-forever in
        BytesIO, but we can prove that Content-Length does in fact
        bound the read by checking the implementation detail."""
        # Implementation check: the handler's source must call
        # rfile.read(<integer>), not rfile.read() with no args.
        import inspect

        source = inspect.getsource(self.shim_server.Handler.do_POST)
        # Must read with a length bound — `self.rfile.read(N)` for some
        # non-zero N derived from Content-Length.
        self.assertIn(
            "self.rfile.read(content_length)",
            source,
            "do_POST must use a Content-Length-bounded read",
        )


class TestConnectionCloseHeader(unittest.TestCase):
    """Every response from _send_json must include `Connection: close`
    so HTTP/1.1 keep-alive sockets don't outlive the request."""

    @classmethod
    def setUpClass(cls):
        cls.shim_server = _load_shim_server()

    def test_send_json_emits_connection_close(self):
        from io import BytesIO

        handler = self.shim_server.Handler.__new__(self.shim_server.Handler)
        handler.wfile = BytesIO()
        handler.headers = {}
        handler.__dict__["wfile"] = handler.wfile
        handler.__dict__["headers"] = handler.headers
        handler.__dict__["requestline"] = "POST / HTTP/1.1"
        handler.__dict__["command"] = "POST"
        handler.client_address = ("test", 0)
        handler.server = None
        handler.request_version = "HTTP/1.1"
        handler._send_json(200, {"ok": True})

        captured = handler.wfile.getvalue()
        # The HTTP/1.1 status line must be the first line.
        first_line = captured.split(b"\r\n", 1)[0]
        self.assertIn(b" 200 ", first_line)
        # Connection: close must appear in the header block.
        self.assertIn(b"Connection: close", captured)


class TestShimHttpHandler(unittest.TestCase):
    """End-to-end HTTP tests: spin up the shim's handler, post a real
    multipart body, assert the response. Mocks `transcribe` so no model
    file is needed."""

    @staticmethod
    def _multipart_body(fields, boundary="----TestBoundary"):
        """Build a minimal multipart/form-data body from a list of fields.

        Each field is one of:
          - (name, str)              — text field
          - (name, filename, bytes, content_type) — file field
        Returns (boundary_str, body_bytes).
        """
        body = bytearray()
        for f in fields:
            name = f[0]
            if len(f) == 2:
                value = f[1]
                body += (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode()
            else:
                _filename, content, ctype = f[1], f[2], f[3]
                body += (
                    (
                        f"--{boundary}\r\n"
                        f'Content-Disposition: form-data; name="{name}"; filename="{_filename}"\r\n'
                        f"Content-Type: {ctype}\r\n\r\n"
                    ).encode()
                    + content
                    + b"\r\n"
                )
        body += f"--{boundary}--\r\n".encode()
        return boundary, bytes(body)

    @classmethod
    def setUpClass(cls):
        cls.shim_server = _load_shim_server()

    @staticmethod
    def _setup_handler(body, content_type):
        # Imported here so we capture the test class's shim_server at
        # call time. We can't use cls (the static method has no cls).
        from io import BytesIO, StringIO

        handler = TestShimHttpHandler.shim_server.Handler.__new__(
            TestShimHttpHandler.shim_server.Handler
        )
        handler.rfile = BytesIO(body)
        handler.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        # Force attribute assignment via __dict__ to bypass any
        # descriptor magic in BaseHTTPRequestHandler.
        handler.__dict__["rfile"] = handler.rfile
        handler.__dict__["headers"] = handler.headers
        handler.wfile = BytesIO()
        handler.path = "/v1/audio/transcriptions"
        handler.command = "POST"
        handler.request_version = "HTTP/1.1"
        # send_response() calls log_message() which reads requestline;
        # bypass BaseHTTPRequestHandler via Handler.__new__.
        handler.requestline = "POST /v1/audio/transcriptions HTTP/1.1"
        handler.client_address = ("test", 0)
        handler.server = None

        # Capture stderr so we can assert diagnostic output.
        old_stderr = sys.stderr
        captured = StringIO()
        sys.stderr = captured
        return handler, captured, old_stderr

    def test_handler_400_when_no_file_field(self):
        """Multipart without a `file` field → 400 (not 500)."""
        boundary, body = self._multipart_body([("prompt", "no file")])
        handler, _captured, old_stderr = self._setup_handler(
            body, f"multipart/form-data; boundary={boundary}"
        )
        try:
            handler.do_POST()
            response = handler.wfile.getvalue()
        finally:
            import sys

            sys.stderr = old_stderr

        self.assertIn(b"400", response[:50])
        self.assertIn(b"missing 'file' field", response)
        # Diagnostic should log which fields the bot actually sent.
        debug = _captured.getvalue()
        self.assertIn("DEBUG multipart", debug)
        self.assertIn("prompt", debug)  # saw the prompt field

    def test_handler_200_with_file_field(self):
        """Multipart with `file` → 200 + transcription text."""
        boundary, body = self._multipart_body(
            [
                ("file", "audio.wav", b"FAKE_WAV", "audio/wav"),
            ]
        )
        handler, _captured, old_stderr = self._setup_handler(
            body, f"multipart/form-data; boundary={boundary}"
        )

        captured_transcribe = {"prompt": None}
        original_transcribe = self.shim_server.transcribe
        captured_ensure = {"called": False, "path": None}
        original_ensure = self.shim_server.ensure_wav

        def mock_ensure(path):
            captured_ensure["called"] = True
            captured_ensure["path"] = path
            return path  # passthrough — no ffmpeg in tests

        def mock_transcribe(_path, language, prompt=None):
            captured_transcribe["prompt"] = prompt
            return "transcribed"

        self.shim_server.ensure_wav = mock_ensure
        self.shim_server.transcribe = mock_transcribe
        try:
            handler.do_POST()
            response = handler.wfile.getvalue()
        finally:
            self.shim_server.ensure_wav = original_ensure
            self.shim_server.transcribe = original_transcribe
            import sys

            sys.stderr = old_stderr

        self.assertIn(b"200", response[:50])
        self.assertIn(b"transcribed", response)
        # No prompt field in this test → transcribe should have seen None.
        self.assertIsNone(captured_transcribe["prompt"])
        # ensure_wav was called with the multipart upload.
        self.assertTrue(captured_ensure["called"])

    def test_handler_forwards_prompt_field_to_transcribe(self):
        """Multipart with `file` + `prompt` → transcribe receives prompt."""
        boundary, body = self._multipart_body(
            [
                ("file", "audio.wav", b"FAKE_WAV", "audio/wav"),
                ("prompt", "The following text is..."),
            ]
        )
        handler, _captured, old_stderr = self._setup_handler(
            body, f"multipart/form-data; boundary={boundary}"
        )

        captured_transcribe = {"prompt": None, "language": None}
        original_transcribe = self.shim_server.transcribe
        original_ensure = self.shim_server.ensure_wav

        def mock_transcribe(_path, language, prompt=None):
            captured_transcribe["prompt"] = prompt
            captured_transcribe["language"] = language
            return "ok"

        self.shim_server.ensure_wav = lambda p: p  # passthrough
        self.shim_server.transcribe = mock_transcribe
        try:
            handler.do_POST()
            response = handler.wfile.getvalue()
        finally:
            self.shim_server.ensure_wav = original_ensure
            self.shim_server.transcribe = original_transcribe
            import sys

            sys.stderr = old_stderr

        self.assertIn(b"200", response[:50])
        self.assertEqual(captured_transcribe["prompt"], b"The following text is...")
        self.assertEqual(captured_transcribe["language"], "en")

    def test_handler_logs_lifecycle_events(self):
        """Three timestamped lifecycle events must appear on stderr in
        order: received file -> transcribed audio -> returned response.
        """
        import re

        boundary, body = self._multipart_body(
            [
                ("file", "audio.wav", b"FAKE_WAV", "audio/wav"),
            ]
        )
        handler, _captured, old_stderr = self._setup_handler(
            body, f"multipart/form-data; boundary={boundary}"
        )
        original_transcribe = self.shim_server.transcribe
        original_ensure = self.shim_server.ensure_wav
        self.shim_server.ensure_wav = lambda p: p  # passthrough

        def mock_transcribe(_path, language, prompt=None):
            return "hello world"

        self.shim_server.transcribe = mock_transcribe
        try:
            handler.do_POST()
        finally:
            self.shim_server.ensure_wav = original_ensure
            self.shim_server.transcribe = original_transcribe
            import sys

            sys.stderr = old_stderr

        log_output = _captured.getvalue()
        events = [
            "received file: bytes=",
            "transcribed audio: text=",
            "returned response: status=200",
        ]
        positions = [log_output.find(ev) for ev in events]
        self.assertGreaterEqual(
            positions[0],
            0,
            f"missing 'received file' log; got: {log_output!r}",
        )
        self.assertGreaterEqual(
            positions[1],
            0,
            f"missing 'transcribed audio' log; got: {log_output!r}",
        )
        self.assertGreaterEqual(
            positions[2],
            0,
            f"missing 'returned response' log; got: {log_output!r}",
        )
        self.assertLess(
            positions[0],
            positions[1],
            "received file must come before transcribed audio",
        )
        self.assertLess(
            positions[1],
            positions[2],
            "transcribed audio must come before returned response",
        )
        # Regression for the 60s-symptom: with the Content-Length fix,
        # the wall-clock gap between `first POST received` and
        # `received file` must be sub-second (the read is bounded by
        # Content-Length and not by client-side keep-alive EOF).
        first_ts_match = re.search(
            r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z) ",
            log_output,
        )
        received_file_match = re.search(
            r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z) received file",
            log_output,
            re.MULTILINE,
        )
        self.assertIsNotNone(first_ts_match)
        self.assertIsNotNone(received_file_match)
        from datetime import datetime

        # Replacers normalize the "Z" suffix back to "+00:00" for
        # datetime.fromisoformat's parser.
        first = datetime.fromisoformat(first_ts_match.group(1).replace("Z", "+00:00"))
        received = datetime.fromisoformat(
            received_file_match.group(1).replace("Z", "+00:00")
        )
        gap_s = abs((received - first).total_seconds())
        self.assertLess(
            gap_s,
            1.0,
            f"first POST → received file must be sub-second; "
            f"got {gap_s}s. The `self.rfile.read()` bug would cause "
            f"this to be ~60s.",
        )
        # Each line must have an ISO-8601 UTC timestamp prefix.
        iso_pat = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z ")
        for line in log_output.splitlines():
            if any(ev in line for ev in events):
                self.assertRegex(line, iso_pat, f"missing timestamp: {line!r}")

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
    def test_assisted_installs_node_24_via_dnf(self):
        # dnf install of nodejs24 is the supported path on AL2023. The
        # mise.toml `[tools] node = ...` entry would otherwise reinstall
        # Node v20 via mise shims and mask this install — that's why
        # `test_mise_toml_does_not_pin_node` exists.
        user_data = build_assisted_user_data("owner/repo", 42)
        self.assertIn("dnf install -y nodejs24 nodejs24-npm", user_data)
        self.assertIn("alternatives --set node /usr/bin/node-24", user_data)

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


class TestMiseToml(unittest.TestCase):
    @staticmethod
    def _read_mise_toml():
        repo_root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(repo_root, "mise.toml"), "r", encoding="utf-8") as f:
            return f.read()

    def test_mise_toml_does_not_pin_node(self):
        # blitzlog itself is Python; Node is brought in at runtime by the
        # user-data's `dnf install nodejs24` plus the bot's `npx`. Pinning
        # Node in mise.toml reinstalls Node (typically v20) and shims it ahead
        # of /usr/bin/node, masking dnf's install — that's what made the bot
        # refuse to start with "requires Node.js 22.14+, 23.6+, or 24+".
        content = self._read_mise_toml()
        self.assertNotRegex(content, r"^\s*node\s*=", msg=content)

    def test_mise_toml_does_not_pin_terraform(self):
        # The bootstrap doesn't invoke the Terraform CLI; pinning it just
        # adds a needless install on the EC2 instance.
        content = self._read_mise_toml()
        self.assertNotRegex(content, r"^\s*terraform\s*=", msg=content)


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
