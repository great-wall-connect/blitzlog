"""Regression guards for the GitHub Actions release workflows.

These tests are static (string-level checks on .github/workflows/*.yml).
They mirror tests/test_entrypoint_readiness.py's pattern: read a config
file, assert the shapes that prevent the bug from regressing. No shell, no
docker, no AWS.

What this guards against:

- ``release-please.yml`` losing its `workflow_dispatch` trigger
  (operators need a way to drive release-please against a PR
  branch without merging first).
- ``release.yml`` losing its `workflow_dispatch` trigger or the
  `mode` / `ref` / `suffix` inputs (the new manual release
  flow). The flow is fully manual — no automatic `push: tags:`
  trigger — so every release is an explicit operator action.
- ``release.yml`` accidentally pushing the wrong image tag, or
  failing to skip ``:latest`` on the pr-test path.
- ``docker-images.yml`` losing its ``workflow_dispatch`` block or
  its ``packages: write`` permission — both are required for
  ``gh workflow run docker-images.yml -f image_tag=<tag>`` to push
  the agent image to GHCR (PR-test image path).
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

    def test_has_workflow_dispatch_input(self):
        """release-please.yml MUST have a workflow_dispatch trigger with
        a `ref` input so operators can drive release-please against a
        PR branch without merging first.
        """
        self.assertIn(
            "workflow_dispatch",
            self.text,
            "release-please.yml must have a workflow_dispatch trigger",
        )
        self.assertRegex(
            self.text,
            r"(?ms)^  workflow_dispatch:.*?inputs:.*?ref:",
            "release-please.yml's workflow_dispatch must declare a `ref` input",
        )


class TestReleaseWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = RELEASE_YML.read_text()
        cls.on_block = _section(cls.text, "on")

    def test_triggered_only_by_workflow_dispatch(self):
        """release.yml MUST fire only via workflow_dispatch.

        The release flow is fully manual. There is no automatic
        `push: tags:` trigger — every release is an explicit operator
        action. If a `push:` block were added back, a tag push
        would silently re-run the release flow on the canonical
        path, racing the workflow_dispatch.
        """
        self.assertNotIn(
            "push:",
            self.on_block,
            "release.yml must not have a `push:` trigger; releases are manual via workflow_dispatch",
        )

    def test_dispatch_has_mode_input(self):
        """release.yml's workflow_dispatch MUST have a `mode` input.

        `mode` discriminates pr-test (PR image, rc1 suffix, no
        :latest) from release (main image, full :latest, source
        modification). The `mode` choice is the simplest guard
        against accidentally triggering a source-modifying release.
        """
        self.assertRegex(
            self.text,
            r"(?ms)^  workflow_dispatch:.*?inputs:.*?mode:",
            "release.yml's workflow_dispatch must declare a `mode` input",
        )

    def test_dispatch_has_ref_input(self):
        """release.yml's workflow_dispatch MUST have a `ref` input.

        `ref` selects the branch to build. Defaults to the
        workflow's ref_name if blank.
        """
        self.assertRegex(
            self.text,
            r"(?ms)^  workflow_dispatch:.*?inputs:.*?ref:",
            "release.yml's workflow_dispatch must declare a `ref` input",
        )

    def test_dispatch_has_suffix_input(self):
        """release.yml's workflow_dispatch MUST have a `suffix` input
        defaulting to ``rc1`` for the pr-test image tag suffix.
        """
        self.assertRegex(
            self.text,
            r"(?ms)^  workflow_dispatch:.*?inputs:.*?suffix:",
            "release.yml's workflow_dispatch must declare a `suffix` input",
        )
        self.assertRegex(
            self.text,
            r"(?ms)suffix:.*?default:\s*[\"']rc1[\"']",
            "release.yml's `suffix` input must default to 'rc1' for the pr-test image tag",
        )

    def test_image_tag_uses_resolved_version(self):
        """release.yml's image tag MUST use the resolved version
        (``steps.version.outputs.version``), not a hardcoded string.

        The resolved version comes from ``lambda/version.py`` at the
        chosen ref (with manifest fallback + manual patch bump). If
        a future change hard-codes the tag, the workflow ships a stale
        version.
        """
        self.assertRegex(
            self.text,
            r"steps\.version\.outputs\.version",
            "release.yml's image tag must use the resolved version output, "
            "not a hardcoded string",
        )

    def test_no_latest_push_in_pr_test_mode(self):
        """release.yml MUST NOT push ``:latest`` on the pr-test path.

        ``:latest`` is reserved for the release-mode build (which
        has a real version-bump commit behind it). A pr-test build
        pushing ``:latest`` would silently promote a pre-merge image
        to the production tag.

        The new release.yml uses a bash ``run:`` step with an
        explicit ``if [ "$MODE" = "release" ]`` conditional that
        adds the ``:latest`` line only in release mode. The
        integration we care about is that ``:latest`` is gated on
        the mode string; a future change that hard-codes it will
        fail this assertion.
        """
        # The image-push step's `if` line on the :latest push.
        self.assertRegex(
            self.text,
            r'if\s+\[\s*"\${{ env\.MODE }}"\s*=\s*"release"\s*\]',
            "release.yml's :latest push must be gated on env.MODE == 'release'",
        )
        # The literal :latest must not appear as a bare docker buildx
        # -t line. It must always be inside the conditional.
        # Find every line starting with "-t " or "  -t " and check
        # that the only :latest appears in the conditional branch.
        # The actual safe pattern: the :latest -t line is built via
        # the IMAGE_LATEST shell variable inside the `if` branch, so
        # the YAML literal -t ":latest" line is absent.
        m = re.search(
            r"^\s*-t\s+[\"']?:latest[\"']?",
            self.text,
            re.MULTILINE,
        )
        self.assertIsNone(
            m,
            "release.yml must not contain a bare YAML-level `-t :latest` "
            "line; :latest must only appear inside the conditional bash branch",
        )

    def test_no_rolling_tags(self):
        """release.yml MUST NOT push rolling tags (0.Y, MAJOR).

        The new design uses ``docker buildx`` with explicit -t flags
        rather than a ``tags: |`` block. The guard is on the
        generated tag strings inside the bash ``run:`` step.
        """
        # No YAML-level 0.Y, MAJOR, or major tags should appear.
        for forbidden in (":0.", ":MAJOR", ":major"):
            self.assertNotRegex(
                self.text,
                rf"^\s*-t\s+[\"']?{re.escape(forbidden)}",
                f"release.yml must not push rolling tag matching {forbidden!r}",
            )

    def test_uploads_lambda_zip_as_release_asset(self):
        """release.yml MUST upload the Lambda zip as a release asset
        (release mode only). The ``Upload Lambda zip to release``
        step must be gated on ``env.MODE == 'release'`` so the
        pr-test path doesn't accidentally publish a draft release
        asset.
        """
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
        # Gate: the upload step must be `if: env.MODE == 'release'`
        # so the pr-test path doesn't accidentally publish a release.
        self.assertRegex(
            self.text,
            r"-\s+name:\s*Upload Lambda zip to release\s*\n\s+if:\s*env\.MODE\s*==\s*'release'",
            "release.yml's 'Upload Lambda zip to release' step must be "
            "gated on env.MODE == 'release'",
        )

    def test_release_mode_bumps_lambda_version(self):
        """release.yml's release-mode path MUST update lambda/version.py
        and commit the bump.

        The release-mode path is the canonical release flow:
        it bumps the version literal in ``lambda/version.py`` (and the
        manifest), commits, pushes, and creates a git tag. A future
        change that drops the source-modification step silently
        leaves the next release without a version commit.
        """
        self.assertIn(
            "Bump version in source",
            self.text,
            "release.yml must have a step that bumps lambda/version.py",
        )
        self.assertIn(
            "lambda/version.py",
            self.text,
            "release.yml's bump step must edit lambda/version.py",
        )

    def test_release_mode_falls_back_to_release_branch_pr(self):
        """release.yml's bump step MUST fall back to a release branch
        + PR when the direct push to BUILD_REF fails.

        ``main`` is typically protected against direct pushes. When the
        operator runs the workflow against main, the ``git push``
        fails and the workflow must create a ``release/vX.Y.Z-<ts>``
        branch, push the bump commit to it, and open a PR back to
        BUILD_REF. A future change that drops the fallback path leaves
        the workflow unable to release from main.
        """
        # The bump step's bash script must contain a `git push` and an
        # `else` branch that creates a release branch and runs
        # `gh pr create`. Use re.DOTALL so the regex spans newlines.
        self.assertRegex(
            self.text,
            r"if git push origin",
            "release.yml's bump step must have a git push that's gated on a branch",
        )
        # The 'else' branch creates the fallback release branch + PR.
        # Use DOTALL so the regex can span newlines between `if` and
        # `else`. The literal `else` keyword is the simplest signal.
        self.assertRegex(
            self.text,
            r"(?ms)if git push origin.*?\belse\b",
            "release.yml's bump step must have an else branch (the "
            "release-branch + PR fallback path)",
        )
        self.assertIn(
            "gh pr create",
            self.text,
            "release.yml's fallback path must call `gh pr create`",
        )
        self.assertRegex(
            self.text,
            r"release/v\$\{\{ steps\.version\.outputs\.version \}\}",
            "release.yml's release branch name must include the version",
        )

    def test_release_mode_uses_release_ref_output(self):
        """The Bump step's release_ref output must be consumed by the
        Tag step (and the checkout in the Tag step).

        The fallback path sets ``release_ref`` to the new
        release/vX.Y.Z-<ts> branch instead of BUILD_REF. The Tag
        step's ``git checkout "${{ steps.bump.outputs.release_ref }}"``
        ensures the tag attaches to the right commit.
        """
        self.assertRegex(
            self.text,
            r"steps\.bump\.outputs\.release_ref",
            "release.yml must consume steps.bump.outputs.release_ref "
            "to handle the fallback branch",
        )
        # The fallback path must echo the ref name as an output.
        self.assertRegex(
            self.text,
            r"echo \"release_ref=\$\{\{ env\.BUILD_REF \}\}\"",
            "release.yml's direct-push path must echo release_ref=<BUILD_REF>",
        )
        self.assertRegex(
            self.text,
            r"echo \"release_ref=\$RELEASE_BRANCH\"",
            "release.yml's fallback path must echo release_ref=<RELEASE_BRANCH>",
        )

    def test_release_mode_tags_the_release(self):
        """release.yml's release-mode path MUST create a git tag."""
        self.assertIn(
            "Tag the release",
            self.text,
            "release.yml must have a step that creates the git tag",
        )
        self.assertRegex(
            self.text,
            r"git tag -a \"v\$\{\{ steps\.version\.outputs\.version \}\}\"",
            "release.yml's tag step must create vX.Y.Z from the resolved version",
        )
        # The Tag step's checkout targets the release_ref (direct or
        # fallback branch). It runs after the Bump step in the same
        # job (jobs run steps sequentially; the `if: env.MODE ==
        # 'release' && steps.bump.outputs.release_ref` guard ensures
        # it's a no-op when the Bump step didn't run).
        self.assertRegex(
            self.text,
            r"git checkout \"\$\{\{ steps\.bump\.outputs\.release_ref \}\}\"",
            "release.yml's Tag step must checkout the release_ref output",
        )

    def test_release_workflow_pull_requests_write_permission(self):
        """release.yml's permissions MUST include ``pull-requests: write``
        so the fallback path can call ``gh pr create`` from the Bump
        step.
        """
        # The release.yml file content has already been loaded into
        # cls.text via the TestReleaseWorkflow setUpClass. Re-read it
        # from disk to get the permissions block.
        perms = _section(RELEASE_YML.read_text(), "permissions")
        self.assertRegex(
            perms,
            r"(?m)^\s+pull-requests:\s+write\s*$",
            "release.yml must request pull-requests: write (needed for the "
            "fallback `gh pr create` path in the Bump step)",
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

    def test_has_workflow_dispatch_trigger_with_image_tag_input(self):
        """docker-images.yml MUST have a `workflow_dispatch` trigger
        with an `image_tag` input.

        Operators use the manual dispatcher to push an agent image
        under a custom tag (e.g. ``pr-100-final``) so Packer can pull
        it during a dev-AMI bake. release.yml covers release
        publishing; this dispatcher covers ad-hoc PR image testing.
        Both publish paths coexist deliberately.
        """
        self.assertIn(
            "workflow_dispatch",
            self.text,
            "docker-images.yml must have a workflow_dispatch trigger",
        )
        self.assertRegex(
            self.text,
            r"(?ms)^  workflow_dispatch:.*?inputs:.*?image_tag:",
            "docker-images.yml's workflow_dispatch must declare an `image_tag` input",
        )

    def test_has_packages_write_permission(self):
        """docker-images.yml MUST request `packages: write` so the
        manual-dispatch step can push the agent image to GHCR."""
        self.assertRegex(
            self.permissions_block,
            r"(?m)^\s+packages:\s+write\s*$",
            "docker-images.yml must request packages: write (needed for the manual-dispatch push)",
        )

    def test_manual_dispatch_logs_into_ghcr(self):
        """docker-images.yml MUST log into GHCR on the non-PR path so
        buildx can push to a private package."""
        self.assertRegex(
            self.text,
            r"(?ms)^\s+- name: Login to GHCR\s*\n"
            r"\s+if: github\.event_name != 'pull_request'\s*\n"
            r"\s+uses: docker/login-action@v\d+",
            "docker-images.yml must log into GHCR on the non-PR path (workflow_dispatch / push)",
        )


if __name__ == "__main__":
    unittest.main()
