"""Tests for the OOS operator scripts.

`scripts/migrate_oos_parameters_to_cmk.sh` and
`scripts/sync_oos_parameters.sh` delete and re-create parameters in
the OOS Parameter Store. They run with the operator's own credentials
and they touch live secrets, so their behaviour is pinned here
against a fake `aliyun` CLI:

* a dry run changes nothing,
* the migration preserves the value — that is the whole point of it,
* the migration moves the parameter onto the CMK,
* the sync copies values in both directions and creates the target
  when it is missing.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATE = REPO_ROOT / "scripts" / "migrate_oos_parameters_to_cmk.sh"
SYNC = REPO_ROOT / "scripts" / "sync_oos_parameters.sh"

# A stand-in for the aliyun CLI: it reads and writes a JSON file that
# stands in for the parameter store.
FAKE_CLI = """#!/usr/bin/env python3
import json
import os
import sys

store_path = os.environ["FAKE_OOS_STORE"]


def load():
    if not os.path.exists(store_path):
        return {"secret": {}, "plain": {}}
    with open(store_path, encoding="utf-8") as fh:
        return json.load(fh)


def save(store):
    with open(store_path, "w", encoding="utf-8") as fh:
        json.dump(store, fh)


def opts_from(args):
    opts = {}
    index = 2
    while index < len(args):
        if args[index].startswith("--"):
            key = args[index][2:]
            # The real CLI takes the next argument as the value even
            # when it starts with "-" (a PEM starts with "-----").
            if index + 1 < len(args):
                opts[key] = args[index + 1]
                index += 2
            else:
                opts[key] = "true"
                index += 1
        else:
            index += 1
    return opts


args = sys.argv[1:]
action = args[1] if len(args) > 1 else ""
opts = opts_from(args)
name = opts.get("Name", "")
store = load()

if action == "GetSecretParameter":
    entry = store["secret"].get(name)
    if entry is None:
        print(json.dumps({"error_code": "ParameterNotFound", "message": "no"}))
        sys.exit(0)
    param = {"Type": "Secret", "KeyId": entry.get("KeyId", "")}
    if opts.get("WithDecryption") == "true":
        param["Value"] = entry.get("Value", "")
    param["Description"] = entry.get("Description", "")
    print(json.dumps({"Parameter": param}))
elif action == "GetParameter":
    entry = store["plain"].get(name)
    if entry is None:
        print(json.dumps({"error_code": "ParameterNotFound", "message": "no"}))
        sys.exit(0)
    print(json.dumps({"Parameter": {"Type": "String", "Value": entry.get("Value", "")}}))
elif action == "CreateSecretParameter":
    store["secret"][name] = {
        "Value": opts.get("Value", ""),
        "KeyId": opts.get("KeyId", ""),
        "Description": opts.get("Description", ""),
    }
    save(store)
    print(json.dumps({"RequestId": "fake"}))
elif action == "UpdateSecretParameter":
    entry = store["secret"].get(name)
    if entry is None:
        print(json.dumps({"error_code": "ParameterNotFound", "message": "no"}))
        sys.exit(0)
    entry["Value"] = opts.get("Value", "")
    save(store)
    print(json.dumps({"RequestId": "fake"}))
elif action == "DeleteSecretParameter":
    store["secret"].pop(name, None)
    save(store)
    print(json.dumps({"RequestId": "fake"}))
else:
    sys.exit("the fake CLI does not implement %s" % action)
"""


class FakeOosTestCase(unittest.TestCase):
    """A parameter store on disk and the scripts pointed at it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store_path = Path(self.tmp.name) / "store.json"
        self.store = {
            "secret": {},
            # The bootstrap publishes the CMK's id here; the migration
            # script reads it to know which key to move onto.
            "plain": {"blitzlog/shared/kms-key-id": {"Value": "cmk-1"}},
        }
        self._write_store()

        cli = Path(self.tmp.name) / "aliyun"
        cli.write_text(FAKE_CLI)
        cli.chmod(0o755)

    def _write_store(self):
        self.store_path.write_text(json.dumps(self.store))

    def _read_store(self):
        return json.loads(self.store_path.read_text())

    def _seed(self, name, value, key_id="", description=""):
        self.store["secret"][name] = {
            "Value": value,
            "KeyId": key_id,
            "Description": description,
        }
        self._write_store()

    def _run(self, script, *args):
        env = dict(os.environ)
        env["ALICLOUD_CLI"] = str(Path(self.tmp.name) / "aliyun")
        env["FAKE_OOS_STORE"] = str(self.store_path)
        env["ALICLOUD_REGION"] = "cn-hongkong"
        return subprocess.run(
            [str(script), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )


class TestTheMigrationScript(FakeOosTestCase):
    def test_a_dry_run_changes_nothing(self):
        self._seed("blitzlog/dev/github-app/id", "the-secret", "old-key")
        proc = self._run(MIGRATE, "blitzlog/dev/github-app/id")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DRY RUN", proc.stdout)
        entry = self._read_store()["secret"]["blitzlog/dev/github-app/id"]
        self.assertEqual(entry["Value"], "the-secret")
        self.assertEqual(entry["KeyId"], "old-key")

    def test_the_value_survives_the_migration(self):
        self._seed("blitzlog/dev/github-app/id", "the-secret", "old-key")
        proc = self._run(MIGRATE, "--apply", "blitzlog/dev/github-app/id")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        entry = self._read_store()["secret"]["blitzlog/dev/github-app/id"]
        self.assertEqual(
            entry["Value"],
            "the-secret",
            "the migration must not lose the value — that is the only " "copy of it",
        )
        self.assertEqual(entry["KeyId"], "cmk-1")

    def test_a_parameter_already_on_the_cmk_is_left_alone(self):
        self._seed("blitzlog/dev/stt/model", "base.en", "cmk-1")
        proc = self._run(MIGRATE, "--apply", "blitzlog/dev/stt/model")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("already on the CMK", proc.stdout)

    def test_a_multi_line_value_survives(self):
        pem = "-----BEGIN KEY-----\nline one\nline two\n-----END KEY-----\n"
        self._seed("blitzlog/dev/github-app/private-key", pem, "old-key")
        proc = self._run(MIGRATE, "--apply", "blitzlog/dev/github-app/private-key")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = self._read_store()["secret"]["blitzlog/dev/github-app/private-key"]
        self.assertIn("line one", entry["Value"])
        self.assertIn("line two", entry["Value"])

    def test_it_fails_loudly_without_a_cmk_id(self):
        self._seed("blitzlog/dev/stt/model", "base.en", "old-key")
        # The bootstrap has not published the key's id yet.
        self.store["plain"] = {}
        self._write_store()
        proc = self._run(MIGRATE, "--apply", "blitzlog/dev/stt/model")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("blitzlog/shared/kms-key-id", proc.stderr)


class TestTheSyncScript(FakeOosTestCase):
    def _seed_both_sets(self):
        self._seed("blitzlog/dev/github-app/id", "the-secret", "cmk-1")
        self._seed("blitzlog/dev/github-app-private-key", "the-key", "cmk-1")

    def test_to_deploy_copies_the_per_env_values(self):
        self._seed_both_sets()
        proc = self._run(SYNC, "--to-deploy", "dev", "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        store = self._read_store()["secret"]
        self.assertEqual(store["blitzlog/dev/github-app-id"]["Value"], "the-secret")
        self.assertEqual(
            store["blitzlog/dev/github-app-private-key"]["Value"], "the-key"
        )

    def test_to_runtime_copies_the_deploy_time_values(self):
        self._seed("blitzlog/dev/github-app-id", "the-secret", "cmk-1")
        proc = self._run(SYNC, "--to-runtime", "dev", "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        store = self._read_store()["secret"]
        self.assertEqual(store["blitzlog/dev/github-app/id"]["Value"], "the-secret")

    def test_a_dry_run_changes_nothing(self):
        self._seed("blitzlog/dev/github-app/id", "the-secret", "cmk-1")
        proc = self._run(SYNC, "--to-deploy", "dev")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("blitzlog/dev/github-app-id", self._read_store()["secret"])

    def test_it_creates_a_target_that_is_missing(self):
        self._seed("blitzlog/dev/stt/model", "base.en", "cmk-1")
        proc = self._run(SYNC, "--to-deploy", "dev", "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self._read_store()["secret"]["blitzlog/dev/stt-model"]["Value"],
            "base.en",
        )

    def test_it_needs_a_direction_and_an_environment(self):
        proc = self._run(SYNC)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--to-runtime", proc.stderr)


if __name__ == "__main__":
    unittest.main()
