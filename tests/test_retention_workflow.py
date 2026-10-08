"""Regression guards for the GHCR retention workflow.

These tests are static (string-level checks on
.github/workflows/retention.yml). They mirror
tests/test_entrypoint_readiness.py's pattern: read a config file,
assert the shapes that prevent the bug from regressing. No shell, no
docker, no AWS.

What this guards against:

- The schedule trigger being removed (so retention stops running).
- The image-name changing off ``blitzlog-agent`` (so retention would
  prune a different image or no-op silently).
- ``keep-n-most-recent`` or ``cut-off`` being deleted (so retention
  either keeps nothing or everything).
- ``token`` regressing to a non-existent secret like ``PAT_TOKEN``
  (so retention fails with a 403 every run).

We parse the YAML with regex rather than pyyaml so we don't pull a new
dev dependency in for what is fundamentally a set of shape assertions
on a small, stable workflow.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RETENTION_YML = REPO_ROOT / ".github" / "workflows" / "retention.yml"


def _section(text: str, key: str) -> str:
    """Return the indented block under ``key:`` at top-of-file scope.

    Captures until the next sibling top-level key or end-of-file.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(key)}:\s*$", line):
            start = i + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start, len(lines)):
        if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*:\s*", lines[j]) and not lines[
            j
        ].startswith(" "):
            end = j
            break
    return "\n".join(lines[start:end])


def _step_with_block(text: str, action_prefix: str) -> str:
    """Return the ``with:`` block of the step that ``uses:`` the given action.

    Returns an empty string if the step isn't found or has no ``with:``
    block.
    """
    # Find the line `uses: <prefix>@...`
    m = re.search(
        rf"^\s+uses:\s+{re.escape(action_prefix)}@.*\n((?:[ \t].*\n)*)",
        text,
        re.MULTILINE,
    )
    if not m:
        return ""
    step_body = m.group(1)
    # Within the step body, pull the with: block.
    wm = re.search(
        r"^\s+with:\s*\n((?:[ \t]+\S.*\n)+)",
        step_body,
        re.MULTILINE,
    )
    if not wm:
        return ""
    return wm.group(1)


class TestRetentionWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = RETENTION_YML.read_text()
        cls.on_block = _section(cls.text, "on")
        cls.permissions_block = _section(cls.text, "permissions")
        cls.with_block = _step_with_block(cls.text, "snok/container-retention-policy")

    def test_schedule_trigger_present(self):
        """retention.yml MUST trigger on a daily cron schedule."""
        self.assertRegex(
            self.on_block,
            r"(?m)^\s+schedule:\s*$",
            "retention.yml must trigger on a cron schedule",
        )
        # YAML list syntax: `schedule:\n  - cron: '...'`. Allow the
        # leading `- ` of the list item. Capture the entire cron
        # expression (which contains spaces) to the end of the line.
        cron_match = re.search(
            r"(?m)^\s+-?\s*cron:\s*['\"]?(.+?)['\"]?\s*$",
            self.on_block,
        )
        self.assertIsNotNone(
            cron_match,
            "retention.yml must define a cron: under schedule:",
        )
        # 5-field cron (minute hour dom month dow)
        self.assertRegex(
            cron_match.group(1),
            r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+$",
            f"retention.yml cron is malformed: {cron_match.group(1)!r}",
        )

    def test_uses_container_retention_policy_action(self):
        """retention.yml MUST use snok/container-retention-policy.

        The retention action is what actually deletes GHCR tags; a
        different action (or no action at all) would no-op silently.
        """
        self.assertRegex(
            self.text,
            r"(?m)^\s+uses:\s+snok/container-retention-policy@v[\d.]+",
            "retention.yml must use snok/container-retention-policy",
        )

    def test_targets_blitzlog_agent(self):
        """retention.yml MUST prune blitzlog-agent (not my-app-image)."""
        self.assertRegex(
            self.with_block,
            r"(?m)^\s+image-names:\s+blitzlog-agent\s*$",
            "retention.yml must prune the blitzlog-agent image",
        )

    def test_keep_n_most_recent_set(self):
        """retention.yml MUST set keep-n-most-recent to a positive integer."""
        m = re.search(
            r"(?m)^\s+keep-n-most-recent:\s+(\d+)\s*$",
            self.with_block,
        )
        self.assertIsNotNone(
            m,
            "retention.yml must set keep-n-most-recent",
        )
        self.assertEqual(
            int(m.group(1)),
            10,
            "retention.yml keep-n-most-recent must be 10",
        )

    def test_cut_off_set(self):
        """retention.yml MUST set cut-off to 30d."""
        self.assertRegex(
            self.with_block,
            r"(?m)^\s+cut-off:\s+30d\s*$",
            "retention.yml cut-off must be 30d",
        )

    def test_uses_github_token_not_pat_token(self):
        """retention.yml MUST use secrets.GITHUB_TOKEN, not a PAT_TOKEN.

        Blitzlog and the `blitzlog-agent` package are in the same
        organization, so GITHUB_TOKEN has delete rights on GHCR.
        Requiring a PAT_TOKEN would silently break retention on
        token-rotation day.
        """
        self.assertIn(
            "secrets.GITHUB_TOKEN",
            self.text,
            "retention.yml must use secrets.GITHUB_TOKEN",
        )
        self.assertNotIn(
            "PAT_TOKEN",
            self.text,
            "retention.yml must not reference a PAT_TOKEN (regression guard)",
        )

    def test_packages_write_permission(self):
        """retention.yml MUST request packages: write.

        GHCR package deletion requires write scope on `packages`. The
        repo's ``Settings → Actions → Workflow permissions`` setting must
        also be ``Read and write permissions`` (documented in the PR
        description as a one-time repo setting).
        """
        self.assertRegex(
            self.permissions_block,
            r"(?m)^\s+packages:\s+write\s*$",
            "retention.yml must request packages: write to delete GHCR tags",
        )


if __name__ == "__main__":
    unittest.main()
