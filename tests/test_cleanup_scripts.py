"""Regression guards for scripts/clean_ghcr.sh and scripts/clean_amis.sh.

These scripts are destructive: a wrong flag or a date-math bug silently
wipes prod packages or production AMIs. The tests are static checks on
the script sources — no live gh/aws calls, no network, no shelling out.
Pattern follows tests/test_entrypoint_readiness.py.

What we guard:
- The scripts exist, are executable, and parse cleanly under bash -n.
- Both default to dry-run (a missing --yes must NOT touch anything).
- Both accept --days N and reject non-positive integers.
- Both fail closed when auth is missing (exit 2 with a clear hint).
- The AMIs script deletes the snapshot *after* deregistering the AMI
  (the inverse ordering orphans the AMI's EBS volume and leaves a
  dangling snapshot reference).
- The GHCR script deletes per-version, not per-package — keeping the
  package name alive is the whole reason we can re-push the same tag.
- The GHCR script refuses to run without an explicit --image filter
  (no accidental namespace wipes).
- The GHCR script --image filter converts literal names to anchored
  exact-match regexes and `*`-containing patterns to anchored globs.
- Exact-match --image values use the direct /packages/container/{pkg}/versions
  endpoint (bypassing the broken /packages list endpoint); globs go
  through the list endpoint.
- The SEEN_VER dedupe key includes api_root so the same name in user
  namespace and an org namespace don't double-delete.
"""

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GHCR = REPO_ROOT / "scripts" / "clean_ghcr.sh"
AMIS = REPO_ROOT / "scripts" / "clean_amis.sh"


def _bash_n(path: Path) -> None:
    """Run `bash -n path` and fail with a useful message on syntax error."""
    result = subprocess.run(
        ["bash", "-n", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"{path.name} failed bash -n (rc={result.returncode}):\n"
            f"stderr: {result.stderr}"
        )


def _regex_of(path: Path, value: str) -> str:
    """Return the regex produced by glob_to_regex() when called with `value`."""
    text = path.read_text()
    start = text.index("glob_to_regex() {")
    end = text.index("\n}\n", start)
    body = text[start : end + 2]
    driver = body + f'\nprintf "%s\\n" "$(glob_to_regex "{value}")"\n'
    proc = subprocess.run(
        ["bash", "-c", driver],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


class TestScriptsPresent(unittest.TestCase):
    def test_clean_ghcr_exists_and_executable(self):
        self.assertTrue(GHCR.exists(), f"{GHCR} missing")
        self.assertTrue(
            GHCR.stat().st_mode & 0o111,
            f"{GHCR} is not executable (mode={oct(GHCR.stat().st_mode)})",
        )

    def test_clean_amis_exists_and_executable(self):
        self.assertTrue(AMIS.exists(), f"{AMIS} missing")
        self.assertTrue(
            AMIS.stat().st_mode & 0o111,
            f"{AMIS} is not executable (mode={oct(AMIS.stat().st_mode)})",
        )

    def test_clean_ghcr_parses(self):
        _bash_n(GHCR)

    def test_clean_amis_parses(self):
        _bash_n(AMIS)


class TestCleanGhcrStatic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = GHCR.read_text()

    def test_defaults_to_dry_run(self):
        """No --yes must mean no `gh api -X DELETE` runs."""
        self.assertIn("APPLY=0", self.text)
        self.assertIn("--yes", self.text)

    def test_deletes_versions_not_packages(self):
        """The endpoint must hit /versions/, not the package itself. Deleting
        the whole package makes the package name unrecoverable until a
        re-publish and breaks every tag the user hasn't migrated yet.
        """
        # Both the direct-fetch and list-endpoint paths must hit
        # /packages/container/{pkg}/versions/{ver_id} (no /packages/container/{pkg}
        # alone, which is the delete-the-whole-package endpoint).
        self.assertIn("/packages/container/${pkg}/versions/${ver_id}", self.text)
        self.assertNotRegex(
            self.text,
            r"DELETE\s+\"\$\{?USER_LOGIN\}?/packages/container/\$\{pkg\}\"",
        )

    def test_validates_days_is_positive_integer(self):
        """`--days abc` or `--days -1` must exit 2 with a clear error.

        Both shapes must appear: the regex test for non-digits and the
        arithmetic test for `< 1` (catches '-1' which would pass `^[0-9]+$`
        if we forgot the regex anchor on '-').
        """
        self.assertIn('"$DAYS" =~ ^[0-9]+$', self.text)
        self.assertIn("DAYS < 1", self.text)

    def test_uses_utc_for_cutoff(self):
        """GitHub's created_at is UTC; a local-time cutoff would let AMIs
        drift across the boundary when the user's TZ isn't UTC."""
        self.assertIn("date -u -d", self.text)
        # Negative: bare `date -d` without -u must not appear.
        self.assertNotRegex(self.text, r"date -d \"?\\$\\{?DAYS")

    def test_fails_closed_when_gh_not_authenticated(self):
        self.assertIn("gh auth status", self.text)
        # Should exit 2 (auth error per the AGENTS.md design).
        self.assertIn("exit 2", self.text)

    def test_handles_non_array_response(self):
        """A 403 from GHCR returns an error object, not an array. The
        script must validate `type == 'array'` before iterating so a
        missing read:packages scope doesn't crash with `Cannot index`.
        """
        self.assertIn('type == "array"', self.text)

    def test_requires_image_flag(self):
        """A full-namespace wipe is too easy a typo to allow. The script
        must refuse to run without at least one --image argument.
        """
        # Source must reference the IMAGES array.
        self.assertIn("IMAGES", self.text)
        # Must check IMAGES length and refuse when it's zero.
        self.assertIn("${#IMAGES[@]} == 0", self.text)
        # Error message must mention --image so the operator sees the hint.
        self.assertIn("--image", self.text)

    def test_supports_org_flag(self):
        """The script must support `--org NAME` (repeatable) and route
        the API calls to /orgs/{NAME}/... when set.
        """
        self.assertIn("ORGS+=", self.text)
        self.assertIn("--org", self.text)
        # Source must build the org API root used by the scanners.
        self.assertIn("/orgs/${org}", self.text)
        self.assertIn("api_root", self.text)

    def test_routes_exact_to_direct_fetch(self):
        """Exact-match --image values must hit /packages/container/{name}/versions
        directly. The GitHub REST API has a known edge case where
        /orgs/{org}/packages returns [] even when specific packages do exist,
        so the script must skip that endpoint for exact matches.

        Catches a regression where someone removes scan_version_endpoint or
        short-circuits exact matches back through the list endpoint.
        """
        # The direct-fetch scanner must exist.
        self.assertIn("scan_version_endpoint()", self.text)
        # And it must hit the version endpoint directly.
        self.assertIn(
            '"${api_root}/packages/container/${pkg}/versions"',
            self.text,
        )

    def test_routes_glob_to_list_endpoint(self):
        """Glob --image values go through the list endpoint (we have no
        way to enumerate candidate package names otherwise). The list
        path must still exist.
        """
        self.assertIn("scan_namespace_list()", self.text)
        # List endpoint URL must appear (query string is part of it).
        self.assertIn("/packages?package_type=container", self.text)

    def test_seen_ver_keys_include_api_root(self):
        """Dedupe keys for version deletion must include api_root so
        the same package in both /user/... and /orgs/{org}/... are not
        treated as one.
        """
        self.assertIn("${api_root}::${pkg}::${ver_id}", self.text)

    def test_glob_to_regex_exact_match(self):
        """--image blitzlog-agent → anchored exact-match regex. Catches
        a regression where someone forgot to wrap with ^…$ or escapes
        the regex anchors.
        """
        self.assertEqual(_regex_of(GHCR, "blitzlog-agent"), r"^blitzlog-agent$")

    def test_glob_to_regex_glob(self):
        """--image 'blitzlog-*' → anchored glob regex. Catches a regression
        where someone forgot to convert `*` to `.*` or escapes the
        wrong chars.
        """
        self.assertEqual(_regex_of(GHCR, "blitzlog-*"), r"^blitzlog-.*$")

    def test_glob_to_regex_escapes_metachars(self):
        """Regex metachars in the literal portion must be escaped, not
        passed through. A `+` in --image must not match against one or
        more of the preceding char (the default PCRE behavior).
        """
        self.assertEqual(
            _regex_of(GHCR, "blitzlog+agent"),
            r"^blitzlog\+agent$",
        )


class TestCleanAmisStatic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = AMIS.read_text()

    def test_defaults_to_dry_run(self):
        self.assertIn("APPLY=0", self.text)
        self.assertIn("--yes", self.text)

    def test_validates_days_is_positive_integer(self):
        """Same guard as test_clean_ghcr for the days argument."""
        self.assertIn('"$DAYS" =~ ^[0-9]+$', self.text)
        self.assertIn("DAYS < 1", self.text)

    def test_uses_utc_for_cutoff(self):
        self.assertIn("date -u -d", self.text)
        self.assertNotRegex(self.text, r"date -d \"?\\$\\{?DAYS")

    def test_uses_only_self_owned_amis(self):
        """`--owners self` is the only scope that prevents this script
        from nuking marketplace or community AMIs. Any other owner flag
        is a foot-gun.
        """
        self.assertIn("--owners self", self.text)
        # Negative: the script must not allow overriding to "all" or
        # an account id via flag (the user wanted "self" only).
        self.assertNotIn("--owners all", self.text)
        self.assertNotIn("--owner-ids", self.text)

    def test_deregisters_before_deleting_snapshot(self):
        """Deregister-image before delete-snapshot. The reverse order
        leaves the AMI pointing at a phantom EBS volume and the snapshot
        may refuse (or worse, succeed silently on an unrelated snapshot
        that just happens to share an id pattern — though that's
        impossible in practice, the principle still holds).
        """
        deregister_pos = self.text.index("deregister-image")
        delete_pos = self.text.index("delete-snapshot")
        self.assertLess(
            deregister_pos,
            delete_pos,
            "deregister-image must appear in the source before delete-snapshot",
        )

    def test_fails_closed_when_aws_not_configured(self):
        """Missing credentials must exit 2 before any destructive call."""
        self.assertIn("aws sts get-caller-identity", self.text)
        self.assertIn("exit 2", self.text)

    def test_region_resolution_order(self):
        """Region resolution: explicit --region > env > aws-cli default."""
        self.assertIn("--region", self.text)
        self.assertIn("AWS_REGION", self.text)
        self.assertIn("aws configure get region", self.text)


if __name__ == "__main__":
    unittest.main()
