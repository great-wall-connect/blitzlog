"""Tests for scripts/oos_read_parameter.py.

The aliyun CLI answers an API error with a JSON document on stdout
and a ZERO exit code. A caller that only looks for `Parameter.Value`
therefore reads "the parameter is missing or empty" when the truth is
"you are not allowed to read it" — which is exactly what the deploy
legs reported while the OOS read was failing on a KMS permission.

The script is the one place that parses the response, so these tests
pin what it tells the caller: the value on stdout, the diagnostic (and
the parameter's CMK) on stderr, and a non-zero exit with the API's own
error message when the read was refused.
"""

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "oos_read_parameter.py"


def _load():
    spec = importlib.util.spec_from_file_location("oos_read_parameter", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestOosReadParameter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load()

    def _run(self, payload: str):
        """Run the script over `payload`; return (rc, stdout, stderr)."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_the_value_goes_to_stdout(self):
        rc, out, _err = self._run(
            json.dumps({"Parameter": {"Value": "s3cr3t", "KeyId": "key-123"}})
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out, "s3cr3t")

    def test_a_multi_line_value_survives(self):
        """The GitHub App private key is a PEM — the newlines are the
        value, not formatting."""
        pem = "-----BEGIN PRIVATE KEY-----\nline one\nline two\n-----END PRIVATE KEY-----\n"
        rc, out, _ = self._run(json.dumps({"Parameter": {"Value": pem}}))
        self.assertEqual(rc, 0)
        self.assertEqual(out, pem)

    def test_the_key_id_is_reported_on_stderr(self):
        """The CMK a parameter is encrypted with is only visible in the
        read response — the terraform provider reads the parameter
        back WITHOUT decryption, so its state copy is empty."""
        rc, _, err = self._run(
            json.dumps({"Parameter": {"Value": "v", "KeyId": "key-123"}})
        )
        self.assertEqual(rc, 0)
        self.assertIn("key-123", err)

    def test_a_parameter_without_a_key_id_says_so(self):
        rc, _, err = self._run(json.dumps({"Parameter": {"Value": "v"}}))
        self.assertEqual(rc, 0)
        self.assertIn("service CMK", err)

    def test_an_api_error_fails_with_the_api_s_message(self):
        """The CLI exits 0 on an API error, so the script is what turns
        it into a failure the caller cannot miss."""
        payload = json.dumps(
            {
                "error_code": "Kms.SecretFailed",
                "message": "This operation for acs:kms:...:secret/oos/... is not allowed",
                "request_id": "01A2-3",
            }
        )
        rc, out, err = self._run(payload)
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("Kms.SecretFailed", err)
        self.assertIn("secret/oos", err)

    def test_an_empty_parameter_is_not_an_error(self):
        """An existing parameter with an empty value is a different
        problem from an unreadable one — the caller reports it."""
        rc, out, _ = self._run(json.dumps({"Parameter": {"Value": ""}}))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_non_json_output_fails_loudly(self):
        rc, out, err = self._run("ERROR: 'oos' is not a valid command or product")
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("did not return JSON", err)

    def test_the_module_is_importable_and_exposes_main(self):
        self.assertTrue(callable(self.module.main))


class TestOosReadParameterInTheWorkflows(unittest.TestCase):
    """Both deploy legs must use the shared reader — a second copy of
    the parsing is a second chance to read an API error as "missing"."""

    WORKFLOWS = (
        REPO_ROOT / ".github" / "workflows" / "release.yml",
        REPO_ROOT / ".github" / "workflows" / "terraform-apply-dev.yml",
    )

    def test_the_reader_is_addressed_by_an_absolute_path(self):
        """A step that `cd`s anywhere makes a relative path resolve
        somewhere else.

        The aliyun CLI install does `cd /tmp` — and the render step
        ran the reader afterwards as `python3 scripts/...`, which
        resolved to /tmp/scripts/... and failed with "can't open
        file". The path is absolute for that reason."""
        for path in self.WORKFLOWS:
            text = path.read_text()
            self.assertNotIn(
                "python3 scripts/oos_read_parameter.py",
                text,
                f"{path.name} must address the reader by an absolute "
                "path — a step that changes directory would break it",
            )
            self.assertIn(
                'python3 "$GITHUB_WORKSPACE/scripts/oos_read_parameter.py"',
                text,
                f"{path.name} must address the reader by an absolute path",
            )

    def test_both_legs_call_the_shared_reader(self):
        for path in self.WORKFLOWS:
            text = path.read_text()
            self.assertIn(
                "scripts/oos_read_parameter.py",
                text,
                f"{path.name} must read OOS parameters through the shared " "reader",
            )
            self.assertNotIn(
                'json.load(sys.stdin)["Parameter"]["Value"]',
                text,
                f"{path.name} still parses the CLI's output inline — an API "
                "error would be read as a missing parameter",
            )


if __name__ == "__main__":
    unittest.main()
