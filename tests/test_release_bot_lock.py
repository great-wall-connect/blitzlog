import importlib
import io
import json
import sys
import unittest
from unittest.mock import patch

import boto3
from moto import mock_aws

# Add scripts directory to path to import
sys.path.insert(0, "scripts")
release_bot_lock = importlib.import_module("release-bot-lock")


@mock_aws
class TestReleaseBotLock(unittest.TestCase):
    BUCKET = "test-agent-logs"
    SENDER = "octocat"
    REGION = "us-east-1"

    def setUp(self):
        self.s3 = boto3.client("s3", region_name=self.REGION)
        self.s3.create_bucket(Bucket=self.BUCKET)

    def _put_lock(
        self,
        bot: str,
        acquired_at: str = "2026-08-30T10:00:00+00:00",
        repo: str = "org/repo",
        issue: int = 42,
    ):
        key = f"bot-pool-locks/{self.SENDER}/{bot}.json"
        data = {
            "bot_name": bot,
            "acquired_at": acquired_at,
            "repo": repo,
            "issue_number": issue,
            "instance_id": "i-1234567890",
        }
        self.s3.put_object(
            Bucket=self.BUCKET,
            Key=key,
            Body=json.dumps(data).encode("utf-8"),
        )

    def test_missing_bucket_fails(self):
        with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            code = release_bot_lock.main(["--sender", self.SENDER, "--bot", "bot1"])
            self.assertEqual(code, 1)
            self.assertIn("S3 bucket not specified", mock_stderr.getvalue())

    def test_no_lock_found_for_single_bot(self):
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            code = release_bot_lock.main(
                [
                    "--sender",
                    self.SENDER,
                    "--bot",
                    "nonexistent",
                    "--bucket",
                    self.BUCKET,
                    "--region",
                    self.REGION,
                ]
            )
            self.assertEqual(code, 0)
            self.assertIn("No active locks found", mock_stdout.getvalue())

    def test_release_single_bot_confirmed(self):
        self._put_lock("bot1")
        with patch("builtins.input", return_value="y"), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as mock_stdout:
            code = release_bot_lock.main(
                [
                    "--sender",
                    self.SENDER,
                    "--bot",
                    "bot1",
                    "--bucket",
                    self.BUCKET,
                    "--region",
                    self.REGION,
                ]
            )
            self.assertEqual(code, 0)
            self.assertIn("Successfully released 1 lock", mock_stdout.getvalue())

        # Verify object deleted
        resp = self.s3.list_objects_v2(
            Bucket=self.BUCKET, Prefix=f"bot-pool-locks/{self.SENDER}/"
        )
        self.assertEqual(resp.get("KeyCount", 0), 0)

    def test_release_single_bot_yes_flag(self):
        self._put_lock("bot1")
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            code = release_bot_lock.main(
                [
                    "--sender",
                    self.SENDER,
                    "--bot",
                    "bot1",
                    "--bucket",
                    self.BUCKET,
                    "--region",
                    self.REGION,
                    "-y",
                ]
            )
            self.assertEqual(code, 0)
            self.assertIn("Successfully released 1 lock", mock_stdout.getvalue())

        resp = self.s3.list_objects_v2(
            Bucket=self.BUCKET, Prefix=f"bot-pool-locks/{self.SENDER}/"
        )
        self.assertEqual(resp.get("KeyCount", 0), 0)

    def test_release_abort(self):
        self._put_lock("bot1")
        with patch("builtins.input", return_value="n"), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as mock_stdout:
            code = release_bot_lock.main(
                [
                    "--sender",
                    self.SENDER,
                    "--bot",
                    "bot1",
                    "--bucket",
                    self.BUCKET,
                    "--region",
                    self.REGION,
                ]
            )
            self.assertEqual(code, 0)
            self.assertIn("Aborted", mock_stdout.getvalue())

        # Verify object still exists
        resp = self.s3.list_objects_v2(
            Bucket=self.BUCKET, Prefix=f"bot-pool-locks/{self.SENDER}/"
        )
        self.assertEqual(resp.get("KeyCount", 0), 1)

    def test_release_all_locks_for_sender(self):
        self._put_lock("bot1")
        self._put_lock("bot2")
        self._put_lock("bot3")

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            code = release_bot_lock.main(
                [
                    "--sender",
                    self.SENDER,
                    "--all",
                    "--bucket",
                    self.BUCKET,
                    "--region",
                    self.REGION,
                    "--yes",
                ]
            )
            self.assertEqual(code, 0)
            self.assertIn("Found 3 lock(s)", mock_stdout.getvalue())
            self.assertIn("Successfully released 3 lock", mock_stdout.getvalue())

        resp = self.s3.list_objects_v2(
            Bucket=self.BUCKET, Prefix=f"bot-pool-locks/{self.SENDER}/"
        )
        self.assertEqual(resp.get("KeyCount", 0), 0)


if __name__ == "__main__":
    unittest.main()
