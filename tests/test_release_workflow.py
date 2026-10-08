"""Regression guards for the GitHub Actions release workflows.

These tests are static (string-level checks on .github/workflows/*.yml).
They mirror tests/test_entrypoint_readiness.py's pattern: read a config
file, assert the shapes that prevent the bug from regressing. No shell,
no docker, no AWS.

What this guards against:

- ``release-please.yml`` losing its `push: branches: [main]` trigger
  (so release-please stops opening release PRs).
- ``release.yml`` accidentally pushing rolling tags (``0.Y`` /
  ``MAJOR``) alongside ``vX.Y.Z`` and ``latest``. Rolling tags
  collide with the snok retention policy that prunes by digest.
- ``release.yml`` losing its tag-push trigger.
- ``docker-images.yml`` regaining a ``push: branches: [main]`` build,
  which would publish the same SHA twice on merge (once via the PR
  path's no-push step and once via the new push path).

We parse the YAML with regex rather than pyyaml so we don't pull a new
dev dependency in for what is fundamentally a set of shape assertions
on small, stable workflows. The pattern matches below are intentionally
loose — they're guards, not parsers — but they catch every realistic
regression (typos, deleted lines, swapped keys).
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_PLEASE_YML = REPO_ROOT / ".github" / "workflows" / "release-please.yml"
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"
DOCKER_IMAGES_YML = REPO_ROOT / ".github" / "workflows" / "docker-images.yml"


def _section(text: str, key: str) -> str:
    """Return the indented block under ``key:`` at top-of-file scope.

    Captures until the next sibling top-level key or end-of-file. Used to
    pull out the `on:` block and the `permissions:` block without a
    full YAML parse.
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


class TestReleasePleaseWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = RELEASE_PLEASE_YML.read_text()
        cls.on_block = _section(cls.text, "on")

    def test_triggers_on_main_push(self):
        """release-please.yml MUST trigger on push to main.

        Without this trigger, release-please never opens a release PR
        and the project stays unversioned indefinitely.
        """
        self.assertRegex(
            self.on_block,
            r"(?m)^\s+branches:\s*\[main\]\s*$",
            "release-please.yml must trigger on push to main",
        )

    def test_uses_official_release_please_action(self):
        """release-please.yml MUST invoke googleapis/release-please-action@v4.

        The repo name is the source of truth for which action the workflow
        actually runs. Switching it (e.g. to a fork) silently changes
        release semantics.
        """
        self.assertRegex(
            self.text,
            r"(?m)^\s+uses:\s+googleapis/release-please-action@v\d+",
            "release-please.yml must use googleapis/release-please-action",
        )

    def test_references_repo_config(self):
        """release-please.yml MUST pass config-file: release-please-config.json."""
        self.assertIn(
            "config-file: release-please-config.json",
            self.text,
            "release-please.yml must reference release-please-config.json",
        )


class TestReleaseWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = RELEASE_YML.read_text()
        cls.on_block = _section(cls.text, "on")

    def test_triggers_on_tag_push(self):
        """release.yml MUST trigger on push of v* tags.

        Triggers only on tag push so it doesn't race with PR validation
        builds (which are PR-time only).
        """
        self.assertRegex(
            self.on_block,
            r"(?m)^\s+tags:\s*\[.*v\*.*\]\s*$",
            "release.yml must trigger on push of v* tags",
        )

    def test_does_not_trigger_on_main_push(self):
        """release.yml MUST NOT also list main in its branches.

        Tag-only triggers prevent racing with docker-images.yml's
        PR validation flow and keep `release.yml` semantics strictly
        "release artifacts only".
        """
        self.assertNotRegex(
            self.on_block,
            r"(?m)^\s+branches:",
            "release.yml must not list branches under `when.push`",
        )

    def test_pushes_only_two_image_tags(self):
        """release.yml MUST push exactly two image tags: vX.Y.Z and latest.

        Rolling tags (0.Y, MAJOR) collide with the snok retention
        policy that prunes by digest — they go dangling between
        retention and the next re-tag. The issue (#93) explicitly
        rejects them.
        """
        # Locate the tags: | scalar block inside docker/build-push-action.
        # re.DOTALL so `.*?` crosses newlines between `uses:` and `tags:`.
        m = re.search(
            r"uses:\s+docker/build-push-action@v\d+.*?tags:\s*\|\n((?:[ \t].*\n)+)",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(
            m,
            "release.yml must contain a docker/build-push-action step with a tags: | block",
        )
        tags_block = m.group(1)
        self.assertIn(
            "v${{ steps.version.outputs.version }}",
            tags_block,
            "release.yml must push a vX.Y.Z tag derived from the pushed tag",
        )
        self.assertIn(
            ":latest",
            tags_block,
            "release.yml must push a :latest tag",
        )
        # No rolling tags.
        for forbidden in (":0.", ":MAJOR", ":major"):
            self.assertNotIn(
                forbidden,
                tags_block,
                f"release.yml must not push rolling tag matching {forbidden!r}",
            )

    def test_uploads_lambda_zip_as_release_asset(self):
        """release.yml MUST upload the Lambda zip as a release asset."""
        self.assertIn(
            "softprops/action-gh-release",
            self.text,
            "release.yml must use softprops/action-gh-release to upload the Lambda zip",
        )
        self.assertIn(
            "blitzlog-lambda.zip",
            self.text,
            "release.yml must upload the Lambda zip named blitzlog-lambda.zip",
        )

    def test_derives_version_from_tag(self):
        """release.yml MUST derive X.Y.Z from GITHUB_REF_NAME.

        The image tag and the GitHub Release must both come from the
        pushed tag, not a hard-coded version or an environment input.
        """
        self.assertIn(
            "GITHUB_REF_NAME",
            self.text,
            "release.yml must derive version from GITHUB_REF_NAME",
        )


class TestDockerImagesWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = DOCKER_IMAGES_YML.read_text()
        cls.on_block = _section(cls.text, "on")
        cls.permissions_block = _section(cls.text, "permissions")

    def test_pr_trigger_present(self):
        """docker-images.yml MUST still build on pull_request.

        PR-time validation (size gate + smoke test) is the whole point
        of this workflow; if it loses the PR trigger, broken images
        ship to releases.
        """
        self.assertRegex(
            self.on_block,
            r"(?m)^\s+pull_request:\s*$",
            "docker-images.yml must still trigger on pull_request",
        )

    def test_no_push_to_main_trigger(self):
        """docker-images.yml MUST NOT trigger on push to main.

        Push-to-main builds are handled by release.yml on tag push.
        Keeping a push-to-main trigger here would double-publish every
        merge (once here, once via the release tag). Specifically:
        there must be no `push:` block under `on:` whose sub-keys
        include `branches: [main]`. (pull_request's `branches: [main]`
        is the PR validation path and must stay.)
        """
        self.assertNotRegex(
            self.on_block,
            r"(?m)^\s+push:\s*\n(?:\s+\S.*\n)*?\s+branches:\s*\[main\]",
            "docker-images.yml must not have a `push:` block with branches: [main]",
        )

    def test_no_workflow_dispatch_trigger(self):
        """docker-images.yml MUST NOT have a workflow_dispatch trigger.

        Manual image builds are handled by release.yml (cut a tag) —
        keeping a dispatch trigger here duplicates the publish
        pathway.
        """
        self.assertNotIn(
            "workflow_dispatch",
            self.text,
            "docker-images.yml must not have a workflow_dispatch trigger",
        )

    def test_no_packages_write_permission(self):
        """docker-images.yml MUST NOT request packages: write.

        The PR-only build never pushes to GHCR (push: false on the
        docker/build-push-action step). Requesting packages: write
        expands the blast radius of any token compromise for no
        benefit.
        """
        self.assertNotRegex(
            self.permissions_block,
            r"(?m)^\s+packages:\s+write\s*$",
            "docker-images.yml must not request packages: write",
        )


if __name__ == "__main__":
    unittest.main()
