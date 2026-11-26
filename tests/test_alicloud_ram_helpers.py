"""Tests for the RAM helpers the deploy legs run.

Two scripts turn a failure into an answer:

* `scripts/decode_ram_diagnostics.sh` — a RAM denial names the
  ACTION but not the resource the check ran against. That resource is
  in the API's `AccessDeniedDetail.EncodedDiagnosticMessage`, a
  base64 blob the error never mentions, and it is the fact a grant
  is scoped against.
* `scripts/guard_kms_key_destruction.sh` — refuses a plan that would
  destroy the CMK the OOS parameters are encrypted with.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DECODER = REPO_ROOT / "scripts" / "decode_ram_diagnostics.sh"
GUARD = REPO_ROOT / "scripts" / "guard_kms_key_destruction.sh"

# A fake `aliyun` that answers the decode call with a fixed answer.
FAKE_ALIYUN = """#!/usr/bin/env python3
import json
import sys

if "DecodeDiagnosticMessage" in sys.argv:
    print(json.dumps({
        "DecodedDiagnosticMessage": {
            "AuthAction": "oos:ListSecretParameters",
            "AuthResource": "acs:oos:cn-hongkong:1234:secretparameter/*",
        }
    }))
    sys.exit(0)
sys.exit(1)
"""

# A fake `terraform` whose `show -json` prints the plan JSON it is
# handed.
FAKE_TERRAFORM = """#!/usr/bin/env python3
import json
import os
import sys

if "show" in sys.argv and "-json" in sys.argv:
    with open(os.environ["FAKE_PLAN_JSON"], encoding="utf-8") as fh:
        print(fh.read())
    sys.exit(0)
sys.exit(0)
"""


def _write_fake(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body)
    path.chmod(0o755)
    return path


class TestTheDecoder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.aliyun = _write_fake(self.dir, "aliyun", FAKE_ALIYUN)

    def _run(self, text: str):
        proc = subprocess.run(
            [str(DECODER)],
            input=text,
            capture_output=True,
            text=True,
            env={**os.environ, "ALICLOUD_CLI": str(self.aliyun)},
            check=False,
        )
        return proc

    def test_it_prints_the_action_and_the_resource(self):
        blob = "QUJDREVGR0g="
        text = (
            'AccessDeniedDetail:{"AuthAction":"oos:ListSecretParameters",'
            f'"EncodedDiagnosticMessage":"{blob}"}}\n'
        )
        proc = self._run(text)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("oos:ListSecretParameters", proc.stdout)
        self.assertIn(
            "acs:oos:cn-hongkong:1234:secretparameter/*",
            proc.stdout,
            "the resource the check ran against is the fact a grant is "
            "scoped against — it has to be in the output",
        )

    def test_it_is_a_no_op_without_a_denial(self):
        proc = self._run("nothing to see here\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("RAM denials", proc.stdout)


class TestTheGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.terraform = _write_fake(self.dir, "terraform", FAKE_TERRAFORM)
        self.plan = self.dir / "tfplan"
        self.plan.write_text("")

    def _run(self, plan_json: dict):
        plan_json_path = self.dir / "plan.json"
        plan_json_path.write_text(json.dumps(plan_json))
        proc = subprocess.run(
            [str(GUARD), str(self.plan)],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{self.dir}:{os.environ['PATH']}",
                "FAKE_PLAN_JSON": str(plan_json_path),
            },
            check=False,
        )
        return proc

    @staticmethod
    def _plan(actions, resource_type="alicloud_kms_key"):
        return {
            "resource_changes": [
                {
                    "address": "alicloud_kms_key.secrets",
                    "type": resource_type,
                    "change": {"actions": actions},
                }
            ]
        }

    def test_it_refuses_a_plan_that_destroys_a_key(self):
        proc = self._run(self._plan(["delete", "create"]))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("would destroy a KMS key", proc.stderr)
        self.assertIn("alicloud_kms_key.secrets", proc.stderr)

    def test_it_refuses_a_plan_that_only_deletes_a_key(self):
        proc = self._run(self._plan(["delete"]))
        self.assertNotEqual(proc.returncode, 0)

    def test_it_allows_a_plan_that_updates_a_key(self):
        proc = self._run(self._plan(["update"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_it_allows_a_plan_that_destroys_something_else(self):
        proc = self._run(self._plan(["delete"], resource_type="alicloud_oss_bucket"))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_it_fails_when_the_plan_is_missing(self):
        proc = subprocess.run(
            [str(GUARD), str(self.dir / "nope")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
