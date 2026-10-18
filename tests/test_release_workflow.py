"""Regression guards for the GitHub Actions release workflows.

These tests are static (string-level checks on .github/workflows/*.yml).
They mirror tests/test_entrypoint_readiness.py's pattern: read a config
file, assert the shapes that prevent the bug from regressing. No shell, no
docker, no AWS.

What this guards against:

- ``release.yml`` losing its `workflow_dispatch` trigger — the
  flow is fully manual. There is no `push: tags:` trigger.
- ``release.yml`` accidentally keeping the obsolete `mode` / `bump`
  / `suffix` dispatch inputs. The design auto-detects the mode
  from the ref's open-PR status and the bump type from the
  conventional commits in the diff (via cocogitto). The only
  operator input is ``ref``; everything else is computed.
- ``release.yml`` losing the cocogitto-based bump detection
  (regression to a hand-rolled bash parser that got the
  conventional-commit rules wrong).
- ``release.yml`` losing the tag/version-file alignment check
  (the runtime invariant that fails loud when the last v* tag
  and lambda/version.py disagree).
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

    def test_no_mode_dispatch_input(self):
        """release.yml MUST NOT have a `mode` dispatch input.

        The mode (pr-test vs release) is auto-detected from the ref's
        open-PR status. A `mode` input is overengineered — it would
        re-introduce the dual-button UX the user explicitly rejected.
        The dispatch's `inputs:` block must declare only `ref` and
        nothing else. Assert that by asserting the only `inputs.X`
        sub-key is `ref:`.
        """
        # Find the `inputs:` block and look at what's under it.
        m = re.search(
            r"(?ms)^  workflow_dispatch:\s*\n\s+inputs:\s*\n(.*?)(?=^permissions:)",
            self.text,
        )
        self.assertIsNotNone(
            m,
            "release.yml must have a workflow_dispatch.inputs block",
        )
        inputs_block = m.group(1)
        # The only top-level input should be `ref:`. Specifically, there
        # must NOT be a `mode:` line at the same indent as `ref:`.
        self.assertNotRegex(
            inputs_block,
            r"(?m)^\s+mode:",
            "release.yml's workflow_dispatch inputs must NOT declare a "
            "`mode:` input; mode is auto-detected from $BUILD_REF's "
            "open-PR status",
        )

    def test_no_bump_dispatch_input(self):
        """release.yml MUST NOT have a `bump` dispatch input.

        The bump type is auto-detected from the conventional commits in
        the diff (last release tag -> $BUILD_REF). The operator does
        not pick patch/minor/major; the commits decide.
        """
        m = re.search(
            r"(?ms)^  workflow_dispatch:\s*\n\s+inputs:\s*\n(.*?)(?=^permissions:)",
            self.text,
        )
        self.assertIsNotNone(
            m,
            "release.yml must have a workflow_dispatch.inputs block",
        )
        inputs_block = m.group(1)
        self.assertNotRegex(
            inputs_block,
            r"(?m)^\s+bump:",
            "release.yml's workflow_dispatch inputs must NOT declare a "
            "`bump:` input; bump type is auto-detected from conventional "
            "commits",
        )

    def test_no_suffix_dispatch_input(self):
        """release.yml MUST NOT have a `suffix` dispatch input.

        The image tag suffix is computed: pr-test -> `-pr{N}` from
        the open PR number; release -> empty (v{X} + :latest).
        """
        m = re.search(
            r"(?ms)^  workflow_dispatch:\s*\n\s+inputs:\s*\n(.*?)(?=^permissions:)",
            self.text,
        )
        self.assertIsNotNone(
            m,
            "release.yml must have a workflow_dispatch.inputs block",
        )
        inputs_block = m.group(1)
        self.assertNotRegex(
            inputs_block,
            r"(?m)^\s+suffix:",
            "release.yml's workflow_dispatch inputs must NOT declare a "
            "`suffix:` input; suffix is derived from mode + pr_number",
        )

    def test_dispatch_has_ref_input(self):
        """release.yml's workflow_dispatch MUST have a `ref` input.

        `ref` selects the branch to build. Defaults to the
        workflow's ref_name if blank. This is the only operator
        input — the mode and bump are auto-detected.
        """
        self.assertRegex(
            self.text,
            r"(?ms)^  workflow_dispatch:.*?inputs:.*?ref:",
            "release.yml's workflow_dispatch must declare a `ref` input",
        )

    def test_detect_mode_step_present(self):
        """release.yml MUST have a step that detects mode (pr-test vs
        release) from the ref's open-PR status.

        The Detect step's bash script must call `gh pr list` against
        ``$BUILD_REF`` and set the `mode` output (and the `bump` and
        `new_version` outputs) based on the result.
        """
        # The Detect step is the canonical source of mode/bump/new_version.
        self.assertRegex(
            self.text,
            r"id:\s*detect",
            "release.yml must have a Detect step (id: detect)",
        )
        self.assertRegex(
            self.text,
            r"gh pr list",
            "release.yml's Detect step must call `gh pr list` to detect the mode",
        )
        self.assertRegex(
            self.text,
            r"echo \"mode=",
            "release.yml's Detect step must echo `mode=pr-test|release` as an output",
        )
        self.assertRegex(
            self.text,
            r"echo \"bump=",
            "release.yml's Detect step must echo `bump=patch|minor|major` as an output",
        )
        self.assertRegex(
            self.text,
            r"echo \"new_version=",
            "release.yml's Detect step must echo `new_version=X.Y.Z` as an output",
        )

    def test_detect_mode_pr_test_when_pr_exists(self):
        """When `gh pr list` returns an open PR for $BUILD_REF, the Detect
        step must set MODE=pr-test.
        """
        self.assertRegex(
            self.text,
            r"if \[ -n \"\$\{?PR_NUMBER\}?\" \]; then\s*\n\s*MODE=pr-test",
            "release.yml's Detect step must set MODE=pr-test when an open PR is found",
        )

    def test_checkout_fetches_tags(self):
        """The Checkout step MUST have ``fetch-tags: true``.

        git-cliff and `git describe` both need the v* tag history to
        find the bump base. Without it the Detect step sees an empty
        `LAST_TAG` and fails loud — but we lock the cause in here so a
        future refactor that drops the flag is caught at PR time.
        """
        # Scope to the Checkout step's `with:` block so a stray
        # `fetch-tags: true` in some other step can't pass this test.
        m = re.search(
            r"(?ms)- name: Checkout\s*\n\s+uses: actions/checkout@v\d+\s*\n\s+with:\s*\n(?P<with>(?:\s+[^\n]*\n)+)",
            self.text,
        )
        self.assertIsNotNone(
            m,
            "release.yml must have a Checkout step with a `with:` block",
        )
        self.assertRegex(
            m.group("with"),
            r"(?m)^\s+fetch-tags:\s+true\s*$",
            "release.yml's Checkout step must have `fetch-tags: true` "
            "(git-cliff and `git describe` need the v* tag history to "
            "find the bump base)",
        )

    def test_git_cliff_installed_in_workflow(self):
        """The Detect step must install git-cliff before using it.

        Pins four things that have all been wrong at various points
        in this workflow's history:
        - the asset name embeds the CLIFF_VERSION (so a CLIFF_VERSION
          bump can't ship with a stale asset name)
        - the URL targets aarch64 (the runner is `ubuntu-26.04-arm`,
          not x86_64 — the wrong arch gives Exec format error)
        - the URL targets gnu libc (Ubuntu uses glibc)
        - the URL DOES have a `v` prefix on the tag path (git-cliff
          tags use `v2.14.2`; without the `v` returns 404)
        - the tar extract must use `--strip-components=1` (the
          tarball's top-level directory is `git-cliff-${CLIFF_VERSION}/`,
          so without the strip the binary lands at the wrong path
          and the workflow fails with `tar: git-cliff: Not found in
          archive`)
        - the CLIFF_VERSION variable is a deliberate pin (not
          `/latest/`) so the runner is hermetic

        The curl invocation may span multiple lines (line-continuation
        backslashes), so we collapse whitespace before searching.

        The URL uses the bash variable `${CLIFF_VERSION}` (not the
        literal value), so the regex matches the variable reference
        rather than the resolved value.
        """
        collapsed = re.sub(r"\s+", " ", self.text)
        # Confirm a CLIFF_VERSION variable is declared (the pin).
        self.assertRegex(
            self.text,
            r'CLIFF_VERSION="[^"]+"',
            "release.yml must declare a CLIFF_VERSION variable for the "
            "git-cliff install (no `/latest/` — pinned for hermeticity)",
        )
        # The curl URL must:
        # - be a git-cliff releases download URL
        # - have a `v` prefix on the tag path (git-cliff convention)
        # - target the git-cliff-${CLIFF_VERSION}-{arch} tarball
        # - use the right arch (aarch64) and libc (gnu) for the runner
        self.assertRegex(
            collapsed,
            r"curl [^\n]*?orhun/git-cliff/releases/download/v"
            r"\$\{CLIFF_VERSION\}/git-cliff-\$\{CLIFF_VERSION\}"
            r"-aarch64-unknown-linux-gnu\.tar\.gz",
            "release.yml's Detect step must install git-cliff via curl "
            "from `releases/download/v${CLIFF_VERSION}/git-cliff-${CLIFF_VERSION}-aarch64-unknown-linux-gnu.tar.gz` "
            "(version interpolated in the URL, `v` prefix on tag path, "
            "ARM64 arch, glibc).",
        )
        # The tar extract must drop the leading `git-cliff-${VERSION}/`
        # directory; otherwise the binary lands at a wrong path and
        # the workflow fails with `tar: git-cliff: Not found in archive`.
        self.assertRegex(
            collapsed,
            r"tar [^\n]*?--strip-components=1[^\n]*?-C /usr/local/bin",
            "release.yml's tar extract must use `--strip-components=1` "
            "so the `git-cliff` binary lands at `/usr/local/bin/git-cliff` "
            "(the tarball's top-level directory is `git-cliff-${VERSION}/`)",
        )

    def test_detect_step_uses_git_cliff_bumped_version(self):
        """The Detect step must compute the new version via
        `git-cliff --bumped-version`. This is the call that actually
        matters — we use git-cliff because its `--bumped-version`
        correctly handles BREAKING CHANGE: / `feat!:` / post-breaking
        `fix:` (which the prior cocogitto 7.0.0 `--auto` did not).
        """
        m = re.search(
            r"(?ms)- name: Detect mode and bump type.*?run: \|\n(?P<body>(?:          .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(
            m,
            "release.yml must have a 'Detect mode and bump type' step",
        )
        body = m.group("body")
        # Find the line that actually invokes `git-cliff --bumped-version`
        # (assigned to NEW_VERSION). The Detect step also has lines that
        # *mention* git-cliff in comments (which we don't want).
        invocation = next(
            (ln for ln in body.splitlines() if "git-cliff --bumped-version" in ln),
            None,
        )
        self.assertIsNotNone(
            invocation,
            "Detect step must invoke `git-cliff --bumped-version` "
            "(the subcommand that prints the next SemVer for unreleased "
            "commits on stdout). The prior cocogitto 7.0.0 `--auto` "
            "flag did not handle BREAKING CHANGE: or `feat!:` correctly.",
        )
        # git-cliff echoes the tag's prefix (so for tag `v0.1.1` it
        # returns `v0.1.2`); the Detect step must strip the `v` so
        # the SemVer regex below matches. Regression guard: a future
        # refactor that drops the strip will fail the regex below.
        self.assertRegex(
            invocation,
            r"sed ['\"]s/\^v//['\"]",
            "Detect step must strip the `v` prefix from git-cliff's "
            "`--bumped-version` output (git-cliff echoes the tag prefix; "
            "without the strip, the SemVer regex below rejects the result)",
        )

    def test_detect_step_fails_loudly_on_missing_tag(self):
        """If no v* tag is reachable from $BUILD_REF, the Detect step
        must fail with a clear error — NOT silently fall back to the
        manifest value (which isn't a valid git ref and was the
        root cause of a prior broken-bump regression).
        """
        m = re.search(
            r"(?ms)- name: Detect mode and bump type.*?run: \|\n(?P<body>(?:          .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(
            m,
            "release.yml must have a 'Detect mode and bump type' step",
        )
        body = m.group("body")
        self.assertIn(
            "FATAL: no v* release tag reachable",
            body,
            "release.yml's Detect step must fail loud when no v* tag is "
            "reachable from $BUILD_REF (the prior manifest-value fallback "
            "is broken because the manifest value isn't a ref)",
        )

    def test_detect_step_aligns_tag_and_version_file(self):
        """The Detect step must compare the last v* tag's version to
        the literal in lambda/version.py and exit 1 on mismatch.

        This is the runtime alignment check that catches drift where
        someone tags or bumps one without the other, before the Bump
        step makes any changes.
        """
        m = re.search(
            r"(?ms)- name: Detect mode and bump type.*?run: \|\n(?P<body>(?:          .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(
            m,
            "release.yml must have a 'Detect mode and bump type' step",
        )
        body = m.group("body")
        self.assertIn(
            "tag/version drift",
            body,
            "release.yml's Detect step must print 'tag/version drift' "
            "when the last v* tag and lambda/version.py disagree",
        )
        self.assertRegex(
            body,
            r'\[ "\$FILE_VERSION" != "\$BUMP_BASE" \]',
            "release.yml's Detect step must compare FILE_VERSION to "
            "BUMP_BASE and exit 1 on mismatch",
        )

    def test_image_tag_pr_test_uses_pr_number_suffix(self):
        """In pr-test mode, the Build step's IMAGE_TAG env must be
        `v{new_version}-pr{pr_number}` so concurrent PRs each have a
        unique tag (no conflict when multiple PRs build the same bumped
        version).
        """
        # The file uses a GH Actions `format()` expression:
        #   SUFFIX_TO_USE: ${{ steps.detect.outputs.mode == 'pr-test'
        #                       && format('-pr{0}', steps.detect.outputs.pr_number)
        #                       || '' }}
        # The regex below matches the `format('-pr{0}', pr_number)` part.
        self.assertRegex(
            self.text,
            r"format\('-pr\{0\}',\s*steps\.detect\.outputs\.pr_number\)",
            "release.yml's Build step SUFFIX_TO_USE must use `format('-pr{0}', "
            "steps.detect.outputs.pr_number)` in pr-test mode (avoids "
            "concurrent-PR tag conflicts)",
        )

    def test_image_tag_release_omits_pr_suffix(self):
        """In release mode, the Build step pushes v{new_version} + :latest
        with no PR-number suffix. The canonical release tag is just
        v{version}.
        """
        # Verify the build step's SUFFIX_TO_USE has the pr-test path
        # behind the mode check; if the test above is the only match,
        # the else branch (empty suffix) is implicit. Make the
        # ternary explicit here.
        self.assertRegex(
            self.text,
            r"SUFFIX_TO_USE.*==\s*'pr-test'",
            "release.yml's Build step SUFFIX_TO_USE must be mode-gated "
            "(suffix applies only to pr-test)",
        )

    def test_image_tag_uses_resolved_version(self):
        """release.yml's image tag MUST use the resolved version
        (``steps.detect.outputs.new_version``), not a hardcoded
        string.

        The resolved version comes from auto-detecting the bump
        type and incrementing the current lambda/version.py literal.
        If a future change hard-codes the tag, the workflow ships a
        stale version.
        """
        self.assertRegex(
            self.text,
            r"steps\.detect\.outputs\.new_version",
            "release.yml's image tag must use steps.detect.outputs.new_version, "
            "not a hardcoded string",
        )

    def test_no_latest_push_in_pr_test_mode(self):
        """release.yml MUST NOT push ``:latest`` on the pr-test path.

        ``:latest`` is reserved for the release-mode build (which
        has a real version-bump commit behind it). A pr-test build
        pushing ``:latest`` would silently promote a pre-merge image
        to the production tag.

        The new release.yml uses a bash ``if [ "$MODE" = ... ]``
        conditional that adds the ``:latest`` line only in release
        mode. The integration we care about is that ``:latest`` is
        gated on the mode string; a future change that hard-codes
        it will fail this assertion.
        """
        # The image-push step's `if` line on the :latest push.
        self.assertRegex(
            self.text,
            r'if\s+\[\s*"\${{ steps\.detect\.outputs\.mode }}"\s*=\s*"release"\s*\]',
            "release.yml's :latest push must be gated on "
            "steps.detect.outputs.mode == 'release'",
        )
        # The literal :latest must not appear as a bare docker buildx
        # -t line. It must always be inside the conditional.
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
        """release.yml MUST upload the Lambda zip to a release asset
        (release mode only). The ``Upload Lambda zip to release``
        step must be gated on ``steps.detect.outputs.mode ==
        'release'`` so the pr-test path doesn't accidentally publish
        a draft release asset.
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
        # Gate: the upload step must be `if: steps.detect.outputs.mode ==
        # 'release'` so the pr-test path doesn't accidentally publish
        # a release.
        self.assertRegex(
            self.text,
            r"-\s+name:\s*Upload Lambda zip to release\s*\n\s+if:\s*steps\.detect\.outputs\.mode\s*==\s*'release'",
            "release.yml's 'Upload Lambda zip to release' step must be "
            "gated on steps.detect.outputs.mode == 'release'",
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
        # The release-branch name is constructed by bash as
        # `release/v${NEW_VERSION}-$(date +%s)` (a shell variable
        # substitution, not a GH Actions template). The regex below
        # matches the literal bash-prefix the script uses.
        self.assertRegex(
            self.text,
            r"release/v\$\{NEW_VERSION\}",
            "release.yml's fallback release branch name must include the bumped version (bash var)",
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
        # The direct-push path uses GH Actions template syntax
        # (the runner interpolates `${{ env.BUILD_REF }}` before
        # bash sees the line). The fallback path uses a plain bash
        # variable (`$RELEASE_BRANCH`) because that name is
        # already a shell var at that point.
        self.assertRegex(
            self.text,
            r"echo \"release_ref=\$\{\{ env\.BUILD_REF \}\}\"",
            "release.yml's direct-push path must echo release_ref=<BUILD_REF> "
            "(GH Actions template)",
        )
        self.assertRegex(
            self.text,
            r"echo \"release_ref=\$RELEASE_BRANCH\"",
            "release.yml's fallback path must echo release_ref=<RELEASE_BRANCH> (bash var)",
        )

    def test_release_mode_tags_the_release(self):
        """release.yml's release-mode path MUST create a git tag."""
        self.assertIn(
            "Tag the release",
            self.text,
            "release.yml must have a step that creates the git tag",
        )
        # The tag step uses a bash var substitution (${NEW_VERSION}),
        # not a GH Actions template — the regex must match the
        # literal bash-prefix the script uses.
        self.assertRegex(
            self.text,
            r"git tag -a \"v\$\{NEW_VERSION\}\"",
            "release.yml's tag step must create vX.Y.Z from the resolved version (bash var)",
        )
        # The Tag step's `git checkout` interpolates a GH Actions
        # expression (`${{ steps.bump.outputs.release_ref }}`) so
        # the runner resolves the right ref (direct or fallback
        # branch) before bash sees the command.
        self.assertRegex(
            self.text,
            r"git checkout \"\$\{\{ steps\.bump\.outputs\.release_ref \}\}\"",
            "release.yml's Tag step must checkout the release_ref output (GH Actions template)",
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

    def test_bump_step_sed_pattern_matches_digits(self):
        """release.yml's Bump step updates lambda/version.py via sed. The
        version literal is ``__version__ = "X.Y.Z"`` and the sed regex
        must match the three numeric parts.

        Regression guard: an earlier version used ``\\d+`` in the
        pattern, which in GNU sed ERE matches a literal ``d``, not a
        digit. The sed then silently no-op'd, ``git commit`` had
        nothing to stage, and the Bump step exited 1 under
        ``set -e``. The pattern must use POSIX character classes
        (``[0-9]+``) instead.
        """
        # Pull the Bump step's `run:` block and inspect the sed line.
        # The block is the one that contains the `sed -i -E` command.
        m = re.search(
            r"(?ms)- name: Bump version in source.*?run: \|\n(?P<body>(?:          .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(
            m,
            "release.yml must have a 'Bump version in source' step",
        )
        sed_line = next(
            (ln for ln in m.group("body").splitlines() if "sed -i -E" in ln),
            None,
        )
        self.assertIsNotNone(
            sed_line,
            "Bump step must contain a `sed -i -E` command that updates "
            "lambda/version.py",
        )
        # Negative: the sed pattern must NOT use `\d` (matches literal d
        # in GNU sed ERE, not a digit).
        self.assertNotIn(
            "\\d",
            sed_line,
            "release.yml's Bump-step sed must not use `\\d` in the "
            "version regex — in GNU sed ERE, `\\d` matches a literal "
            "`d`, not a digit. Use `[0-9]+` instead.",
        )
        # Positive: the sed pattern must use `[0-9]+` (POSIX class) for
        # each of the three numeric parts.
        self.assertIn(
            "[0-9]+",
            sed_line,
            "release.yml's Bump-step sed must use `[0-9]+` for the "
            "numeric parts of the version regex",
        )

    def test_bump_step_reads_git_identity_from_ssm(self):
        """Issue #119: the Bump step's commit author comes from
        /blitzlog/users/<OWNER_LOGIN>/git-author/{name,email} in SSM
        (same params the EC2 agent runtime reads), NOT from a
        GHA repo variable. Pin the SSM-fetch shape so the
        audit-trail identity is sourced from a single
        operator-managed location.
        """
        m = re.search(
            r"(?ms)- name: Bump version in source.*?run: \|\n(?P<body>(?:          .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(m)
        body = m.group("body")
        # Collapse the YAML continuation-indent (the long
        # aws-cli lines wrap with `\` at the YAML column, so the
        # next line starts with the same 10-space indent) into a
        # single space. Without this, a contiguously-keyed
        # assertIn against a single-line aws-cli invocation
        # would fail because the args are split across lines
        # with double-space glue.
        flat = re.sub(r"\\\n          ", " ", body)
        flat = re.sub(r"  +", " ", flat)
        self.assertIn(
            'aws ssm get-parameter --name "/blitzlog/users/${OWNER_LOGIN}/git-author/name"',
            flat,
            "Bump step must read git author name from "
            "/blitzlog/users/${OWNER_LOGIN}/git-author/name in SSM",
        )
        self.assertIn(
            'aws ssm get-parameter --name "/blitzlog/users/${OWNER_LOGIN}/git-author/email"',
            flat,
            "Bump step must read git author email from "
            "/blitzlog/users/${OWNER_LOGIN}/git-author/email in SSM",
        )
        self.assertIn(
            'git config user.name "${GIT_AUTHOR_NAME}"',
            body,
            "Bump step must use the SSM-fetched GIT_AUTHOR_NAME for the commit",
        )
        self.assertIn(
            'git config user.email "${GIT_AUTHOR_EMAIL}"',
            body,
            "Bump step must use the SSM-fetched GIT_AUTHOR_EMAIL for the commit",
        )

    def test_bump_step_uses_owner_login_repo_var(self):
        """The Bump step identifies the pool to read git-author
        params from via `vars.OWNER_LOGIN`. The deploy-role's
        SSMGitAuthorIdentityRead grant (in
        infra/aws/bootstrap/deploy-role.tf) targets the same
        /blitzlog/users/*/git-author/* subtree the packer role
        needs to read. OWNER_LOGIN is the only new repo variable
        the operator must set — terraform-apply-dev.yml doesn't
        need any git-identity plumbing.
        """
        m = re.search(
            r"(?ms)- name: Bump version in source.*?env:\n(?P<env>(?:          .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(m, "Bump step must have an env block")
        self.assertIn(
            "OWNER_LOGIN: ${{ vars.OWNER_LOGIN }}",
            m.group("env"),
            "Bump step env must source OWNER_LOGIN from vars.OWNER_LOGIN",
        )

    def test_bump_step_fails_loud_when_ssm_param_missing(self):
        """When /blitzlog/users/<OWNER_LOGIN>/git-author/{name,email}
        isn't set in SSM (operator hasn't run `terraform apply`
        in infra/user-pool, or the user-pool stack is stale),
        the Bump step must abort with a clear setup error
        rather than land the commit under a fictional identity
        (the runner hostname). The `FATAL:` line and the
        `cd infra/user-pool && terraform apply` hint are the
        contract.
        """
        m = re.search(
            r"(?ms)- name: Bump version in source.*?run: \|\n(?P<body>(?:          .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(m)
        body = m.group("body")
        self.assertIn(
            "FATAL:", body, "Bump step must emit a FATAL: line on missing SSM params"
        )
        self.assertIn(
            "terraform apply",
            body,
            "Bump step's FATAL hint must point the operator at `terraform apply` "
            "in infra/user-pool (the single source of truth for the identity)",
        )

    def test_release_workflow_does_not_use_gha_git_author_vars(self):
        """Issue #119: the GHA worker identity is sourced from SSM
        (per the test_bump_step_reads_git_identity_from_ssm
        contract), not from a GHA repo variable like
        `vars.GIT_AUTHOR_NAME` / `vars.GIT_AUTHOR_EMAIL`. The
        release.yml contract has no `vars.GIT_AUTHOR_*` references
        anywhere — keeping the operator setup in terraform
        (infra/user-pool/terraform.tfvars) rather than scattered
        between terraform and `gh variable set` calls.
        """
        self.assertNotIn(
            "vars.GIT_AUTHOR_NAME",
            self.text,
            "release.yml must not read vars.GIT_AUTHOR_NAME — the "
            "identity is sourced from SSM (issue #119)",
        )
        self.assertNotIn(
            "vars.GIT_AUTHOR_EMAIL",
            self.text,
            "release.yml must not read vars.GIT_AUTHOR_EMAIL — the "
            "identity is sourced from SSM (issue #119)",
        )

    def test_bump_step_falls_back_to_release_bot_when_owner_login_unset(self):
        """When `vars.OWNER_LOGIN` is unset the Bump step must
        fall back to the historical `release-bot` identity
        (issue #119 legacy carve-out) so the canonical release
        pipeline works on a repo that hasn't yet provisioned a
        user-pool stack. The strict fail-loud from PR #128 broke
        release #31 on main; this is the relaxation that unblocks
        it without sacrificing the operator-managed-identity path
        (which still triggers when OWNER_LOGIN is set).

        Regression guard: a future refactor that drops the
        fallback will surface here, not in a 17-minute release
        run on main.
        """
        m = re.search(
            r"(?ms)- name: Bump version in source.*?run: \|\n(?P<body>(?:          .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(m)
        body = m.group("body")
        # The fallback branch must be guarded by a string-empty
        # OWNER_LOGIN test (not a numeric / a different var).
        self.assertRegex(
            body,
            r"if \[ -n \"\$\{OWNER_LOGIN:-?\}\" \]",
            "Bump step's operator-identity path must be guarded by "
            '[ -n "${OWNER_LOGIN}" ] (so the empty-string unset '
            "case falls through to the release-bot default).",
        )
        # The else branch must set the historical identity verbatim.
        self.assertIn(
            'GIT_AUTHOR_NAME="release-bot"',
            body,
            "Bump step's fallback must use the historical "
            "'release-bot' name (matches the pre-#119 Bump step "
            "verbatim so any reviewer can map the Bump commit to a "
            "stable bot identity).",
        )
        self.assertIn(
            'GIT_AUTHOR_EMAIL="release-bot@users.noreply.github.com"',
            body,
            "Bump step's fallback must use the historical "
            "'release-bot@users.noreply.github.com' email.",
        )
        # The fallback path is a WARN, not a FATAL — the canonical
        # release pipeline must not be blocked by an unset
        # OWNER_LOGIN. Operators who want the strict path opt in
        # by setting the var.
        self.assertIn(
            "WARN: vars.OWNER_LOGIN is unset",
            body,
            "Bump step's fallback must log a WARN (not FATAL) when "
            "OWNER_LOGIN is unset so the canonical release pipeline "
            "keeps working without operator setup. The migration "
            "path (set OWNER_LOGIN + run infra/user-pool apply) is "
            "documented in the WARN line.",
        )

    def test_bump_step_still_fails_loud_when_ssm_param_missing(self):
        """The strict-FATAL branch must still trigger when
        OWNER_LOGIN is set but the SSM git-author params are
        missing — that's the operator-actionable misconfiguration.
        The fix relaxes the OWNER_LOGIN-unset case to a WARN,
        NOT the SSM-missing case. Pin the SSM-missing contract.
        """
        m = re.search(
            r"(?ms)- name: Bump version in source.*?run: \|\n(?P<body>(?:          .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(m)
        body = m.group("body")
        # The SSM-missing case is still a fatal with a
        # `terraform apply` hint.
        self.assertIn(
            "FATAL: /blitzlog/users/${OWNER_LOGIN}/git-author",
            body,
            "Bump step must still FATAL when OWNER_LOGIN is set but "
            "the SSM git-author params are missing (the operator-"
            "actionable misconfiguration).",
        )
        self.assertIn(
            "terraform apply",
            body,
            "Bump step's SSM-missing FATAL hint must still point the "
            "operator at `terraform apply` in infra/user-pool.",
        )

    def test_ref_gate_mode_release_on_main(self):
        """Issue #131: BUILD_REF=main with no open PR must produce
        MODE=release (canonical release path). Pin the case branch
        + the absence-of-PR -> release fallback.
        """
        # The Detect step must have a `case "$BUILD_REF"` (or
        # equivalent shell-switch) that explicitly enumerates
        # `main` -> release.
        m = re.search(
            r"(?ms)- name: Detect mode and bump type.*?run: \|\n(?P<body>(?:          .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(m)
        body = m.group("body")
        # The case statement must be present.
        self.assertRegex(
            body,
            r'case\s+"?\$\{\{\s*env\.BUILD_REF\s*\}\}"?\s+in',
            "Detect step must dispatch on BUILD_REF via a `case` "
            "statement (the ref-gate is the primary safety mechanism).",
        )
        # The `main` case must set MODE=release when there's no PR
        # (canonical release path). The contract is the literal
        # `main)` token followed (across the if/else block) by
        # `MODE=release`. Use [\s\S] to span newlines (assertRegex
        # uses re.search which is single-line by default).
        self.assertRegex(
            body,
            r"main\)[\s\S]*?Canonical release path[\s\S]*?MODE=release",
            "The `main` case branch must set MODE=release when no PR "
            "is open (canonical release path).",
        )

    def test_ref_gate_mode_pr_test_on_main_with_open_pr(self):
        """Issue #131: BUILD_REF=main with an open PR must produce
        MODE=pr-test (the operator is testing a PR against the main
        tip — same behavior as before the ref-gate, just expressed
        via the new `case` statement).
        """
        m = re.search(
            r"(?ms)- name: Detect mode and bump type.*?run: \|\n(?P<body>(?:          .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(m)
        body = m.group("body")
        # The `main` case must downgrade to pr-test when an open PR
        # exists for the dispatched ref.
        self.assertRegex(
            body,
            r"main\)[\s\S]*?if \[ -n \"\$PR_NUMBER\" \]; then\s*\n\s*MODE=pr-test",
            "The `main` case must set MODE=pr-test when an open PR "
            "exists for BUILD_REF=main (downgrade to PR-test, not "
            "release-on-PR-head which would deploy that PR's code "
            "to prod).",
        )

    def test_ref_gate_mode_release_on_release_branch(self):
        """Issue #131: BUILD_REF=release/* must produce MODE=release
        (the fallback release-branch path used when direct push
        to main fails — the Bump step's `gh pr create` flow
        creates these branches).
        """
        m = re.search(
            r"(?ms)- name: Detect mode and bump type.*?run: \|\n(?P<body>(?:          .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(m)
        body = m.group("body")
        # The `release/*` case must set MODE=release.
        self.assertRegex(
            body,
            r"release/\*\)[\s\S]*?MODE=release",
            "The `release/*` case branch must set MODE=release "
            "(fallback release-branch path for when direct push "
            "to main fails).",
        )

    def test_ref_gate_mode_pr_test_on_feature_branch_with_pr(self):
        """Issue #131: BUILD_REF=feature/foo (or any other non-release
        ref) with an open PR must produce MODE=pr-test. The
        pr-test image goes to /blitzlog/dev/agent-ami-id-docker-ubuntu
        (no prod apply, no tag, no version bump).
        """
        m = re.search(
            r"(?ms)- name: Detect mode and bump type.*?run: \|\n(?P<body>(?:          .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(m)
        body = m.group("body")
        # The catch-all `*)` case must downgrade to pr-test when
        # an open PR exists.
        self.assertRegex(
            body,
            r"\)[\s\S]*?#?\s*Any other ref[\s\S]*?if \[ -n \"\$PR_NUMBER\" \]; then\s*\n\s*MODE=pr-test",
            "The catch-all case branch must set MODE=pr-test when "
            "an open PR exists on a non-release ref (the pr-test "
            "image path).",
        )

    def test_ref_gate_fatal_on_feature_branch_without_pr(self):
        """Issue #131: BUILD_REF=feature/foo (or any other non-release
        ref) WITHOUT an open PR must abort the workflow with a clear
        FATAL message. This is the regression guard for release
        #32 (where dispatching on a feature branch without an open
        PR silently fell into release mode and deployed to prod).
        """
        m = re.search(
            r"(?ms)- name: Detect mode and bump type.*?run: \|\n(?P<body>(?:          .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(m)
        body = m.group("body")
        # The catch-all `*)` case must exit 1 when no PR is open.
        # The exit must be guarded by an `if [ -n "$PR_NUMBER" ];
        # ...; else; exit 1` pattern (the PR-empty branch).
        self.assertRegex(
            body,
            r"if \[ -n \"\$PR_NUMBER\" \]; then\s*\n\s*MODE=pr-test\s*\n\s*else\s*\n\s*echo \"FATAL",
            "The catch-all case branch must exit 1 with a FATAL "
            "message when no open PR exists on a non-release ref "
            "(the regression guard for issue #131).",
        )
        # The FATAL message must be operator-actionable: it must
        # mention the allowed release refs (main, release/*) and
        # the migration paths (open a PR, dispatch from main).
        self.assertIn(
            "Allowed release refs: 'main'",
            body,
            "FATAL message must enumerate the allowed release refs "
            "(main, release/*) so the operator knows where to dispatch.",
        )
        self.assertIn(
            "issue #131",
            body,
            "FATAL message must reference issue #131 (the audit-trail "
            "for why this gate exists).",
        )

    def test_pr_list_uses_exact_match_not_head_filter(self):
        """Issue #131: the `gh pr list` call must use the
        exact-match `... | jq select(.headRefName == $r) | .number`
        form, not the broken `--head "$ref"` filter. The `--head`
        form has a known prefix-collision failure mode (Gemini
        analysis, 2026-10-12): it performs a `startsWith`-style
        match and silently returns empty for fork PRs.

        Regression guard for release #32: the bad run had an open
        PR on the dispatched ref, but `gh pr list --head` returned
        empty, so mode silently fell into release and deployed to
        prod. The `gh pr list --json | jq select` form below does
        an exact-string match on `headRefName` and is the safe
        replacement.

        The pipe shape (NOT `--jq --arg r` inside `gh pr list`)
        is pinned by `test_pr_list_pipes_to_jq_not_uses_gh_arg`.
        """
        m = re.search(
            r"(?ms)- name: Detect mode and bump type.*?run: \|\n(?P<body>(?:          .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(m)
        body = m.group("body")
        # The exact-match jq form must be present. Match
        # across the bash line-continuation (backslash +
        # newline) by allowing arbitrary whitespace between
        # `gh pr list --state open` and `--json number,headRefName`.
        self.assertRegex(
            body,
            r"gh pr list --state open[\s\S]*?--json number,headRefName",
            "Detect step must use `gh pr list --json number,headRefName` "
            "for the exact-match form (instead of `--head` which has a "
            "known prefix-collision failure mode).",
        )
        # The jq `select(.headRefName == $r)` form must be present
        # (the exact-string match). Match across whitespace.
        # Assert it appears AFTER the `gh pr list` pipe (i.e.
        # `gh ... | jq ...`) — `assertNotIn` for `--head` doesn't
        # catch the broken `--arg`-inside-`gh` form.
        self.assertRegex(
            body,
            r"\|[\s\S]*?jq[\s\S]*?select\(\.headRefName == \$r\)",
            "Detect step must filter with `jq select(.headRefName == $r)` "
            "AFTER a `|` from `gh pr list` (the pipe-to-jq form). "
            "The broken `--jq --arg r ...` shape (where `--arg` "
            "stays inside `gh pr list`) would NOT match this regex "
            "because the `|` must come before `jq`.",
        )
        # The broken `--head` filter must be GONE.
        self.assertNotIn(
            '--head "${{ env.BUILD_REF }}"',
            body,
            'Detect step must NOT use `gh pr list --head "${{ env.BUILD_REF }}"` '
            "anymore — that filter has a known prefix-collision failure "
            "mode (issue #131).",
        )
        # Defensive: also assert there's no `--head` form left at
        # all in the Detect step. (Future refactors that reintroduce
        # it will fail this test.)
        self.assertNotRegex(
            body,
            r"--head\s+\"\$\{\{\s*env\.BUILD_REF\s*\}\}\"",
            'Detect step must not use any `--head "${{ env.BUILD_REF }}"` '
            "form (exact-string match is the only safe contract).",
        )

    def test_pr_list_pipes_to_jq_not_uses_gh_arg(self):
        """Issue #131: the PR query must pipe `gh pr list` output to
        `jq --arg`, NOT pass `--arg` to `gh` (which doesn't accept
        it as a flag and errors with `unknown arguments ["r" ...]`).

        Regression guard for run #38261612554 (Oct 12 14:15 UTC):
        the original PR #132 implementation had
        `gh pr list --json ... --jq --arg r "$BUILD_REF" EXPR`,
        but `--arg` is a `jq` flag, not a `gh` flag. The broken
        call produced a cobra error, `PR_NUMBER` ended up empty
        (after the `|| true` swallow), the ref-gate's catch-all
        `*)` case correctly emitted the FATAL — but the
        triggering condition was the broken PR query, not the
        actual "no open PR" detection. Fix: pipe the JSON
        output to a real `jq --arg r` invocation.

        The ref-gate contract is sound; this is purely a
        `gh`/`jq` flag-routing bug.
        """
        m = re.search(
            r"(?ms)- name: Detect mode and bump type.*?run: \|\n(?P<body>(?:          .*\n)+)",
            self.text,
        )
        self.assertIsNotNone(m)
        body = m.group("body")
        # The Detect step must use the pipe-to-jq form
        # (`gh pr list ... | jq --arg r "$BUILD_REF" EXPR`).
        # The pipe and the `jq --arg r` must appear in that
        # order after the `gh pr list` call.
        self.assertRegex(
            body,
            r"gh pr list[\s\S]*?\|\s*jq --arg r",
            "Detect step must pipe `gh pr list` output to "
            '`jq --arg r "$BUILD_REF"` (NOT pass `--arg` to `gh` '
            "directly, which errors with 'unknown arguments' "
            "— issue #131).",
        )
        # Defensive: the broken `--arg` inside `gh pr list`
        # must be GONE. This is the exact shape that produced
        # the cobra error in run #38261612554.
        self.assertNotRegex(
            body,
            r"gh pr list[\s\S]*?--jq\s+--arg r",
            "Detect step must NOT have `--jq --arg r ...` after "
            "`gh pr list` — `--arg` is a jq flag, not a gh flag. "
            "Use `gh pr list ... | jq --arg r ...` instead (issue #131).",
        )

    def test_bump_step_does_not_push_in_pr_test_mode(self):
        """Issue #131: the Bump step's `git push origin "$BUILD_REF"`
        must never run in pr-test mode (pr-test must not push
        back to the dispatch ref, otherwise the Bump step's
        `chore(release): vX.Y.Z` commit gets stranded on a side
        branch — see release #32 where the `a7c03e6` v0.4.0 Bump
        commit landed on `fix/issue-128-release-bump-fallback`
        and was never reachable from main).

        The step-level `if: steps.detect.outputs.mode == 'release'`
        guard is the contract. With the ref-gate in place, the
        only way to reach `MODE=release` is via `main` or
        `release/*`, so the push is only ever to a known-safe ref.
        """
        m = re.search(
            r"(?ms)- name: Bump version in source.*?(?=- name:|\Z)",
            self.text,
        )
        self.assertIsNotNone(
            m,
            "release.yml must have a 'Bump version in source' step",
        )
        bump_step = m.group(0)
        # The step must have `if: steps.detect.outputs.mode == 'release'`
        # (the Bump step only runs in release mode, never in pr-test).
        self.assertRegex(
            bump_step,
            r"if:\s*steps\.detect\.outputs\.mode == 'release'",
            "Bump step must be gated on `if: steps.detect.outputs.mode == 'release'` "
            "so it never runs in pr-test mode (which would push a "
            "version bump commit to a feature branch — the release "
            "#32 regression).",
        )
        # The step name must signal the gating (the "(release only)"
        # suffix is a smell-test for future readers).
        self.assertIn(
            "(release only)",
            bump_step,
            "Bump step name must include '(release only)' to make "
            "the gating visible at a glance.",
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


class TestAlicloudProviderDispatch(unittest.TestCase):
    """Regression guards for the BLITZLOG_PROVIDER dispatch in
    ``release.yml`` (issue #126).

    Most of the dispatch shape is asserted in
    ``test_terraform_env_isolation.py::TestAlicloudProviderDispatch``
    (where the cross-file invariants live). This class covers the
    additional shape that lives specifically in release.yml:

    - the `Validate provider` step explicitly fails fast when
      BLITZLOG_PROVIDER=alicloud (Packer is AWS-only in this PR).
    - the `Render TF_VAR_* env from SSM` step is gated on
      `BLITZLOG_PROVIDER == 'aws'` so it doesn't run on the
      Alicloud path.
    - the `terraform plan` step uses `${BLITZLOG_PROVIDER}.tfvars`
      so the per-provider tfvars file is passed.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = RELEASE_YML.read_text()

    def test_validate_provider_step_present(self):
        """The deploy job must include a Validate provider step
        that fails fast on alicloud.
        """
        self.assertRegex(
            self.text,
            r"(?ms)^\s+- name: Validate provider\s*\n"
            r"\s+env:\s*\n"
            r"\s+BLITZLOG_PROVIDER:.*\$\{\{ env\.BLITZLOG_PROVIDER \}\}",
            "release.yml must include a 'Validate provider' step that reads "
            "env.BLITZLOG_PROVIDER",
        )
        # The case statement for alicloud must fail with a clear error.
        self.assertIn(
            "BLITZLOG_PROVIDER=alicloud is not yet supported by the Packer step",
            self.text,
            "release.yml must fail fast when BLITZLOG_PROVIDER=alicloud",
        )

    def test_ssm_render_step_gated_to_aws(self):
        """The 'Render TF_VAR_* env from SSM' step must be gated on
        `BLITZLOG_PROVIDER == 'aws'` so it doesn't run on the
        Alicloud path.
        """
        # Find the step's `if:` clause by looking at the line right
        # after the step name. We accept either a same-line
        # `if:` or a block-style `if:` on the next line.
        self.assertRegex(
            self.text,
            r"(?ms)Render TF_VAR_\* env from SSM\s*\n"
            r"\s+if: env\.BLITZLOG_PROVIDER == 'aws'",
            "release.yml must gate 'Render TF_VAR_* env from SSM' on "
            "BLITZLOG_PROVIDER == 'aws' so the step doesn't run for Alicloud",
        )

    def test_terraform_plan_uses_per_provider_tfvars(self):
        """The `terraform plan` step must pass
        `-var-file=${BLITZLOG_PROVIDER}.tfvars` so the per-provider
        tfvars file is used.
        """
        # The plan step body contains the `TFVARS_FILE=...` line
        # and the `-var-file=...` reference. We just check both
        # are present; a finer-grained regex was brittle against
        # the YAML whitespace/indentation.
        plan_block_match = re.search(
            r"(?ms)- name: terraform plan\b.*?(?=\n\s*-\s|\Z)", self.text
        )
        self.assertIsNotNone(
            plan_block_match,
            "release.yml must have a 'terraform plan' step",
        )
        plan_block = plan_block_match.group(0)
        self.assertIn(
            "TFVARS_FILE=",
            plan_block,
            "terraform plan step must derive TFVARS_FILE from BLITZLOG_PROVIDER",
        )
        self.assertIn(
            "${BLITZLOG_PROVIDER}.tfvars",
            plan_block,
            "terraform plan step must use ${BLITZLOG_PROVIDER}.tfvars "
            "as the -var-file argument",
        )

    def test_terraform_init_switches_backend_by_provider(self):
        """The `terraform init` step must switch between S3 (AWS)
        and OSS (Alicloud) backends based on BLITZLOG_PROVIDER.
        """
        self.assertIn(
            "TF_BACKEND_BUCKET_OSS",
            self.text,
            "release.yml terraform init must reference TF_BACKEND_BUCKET_OSS "
            "for the Alicloud path",
        )
        # The init step has both branches. We don't regex-match the
        # full step body (the heredoc-style commands make that
        # brittle); we just check the two key flags are referenced.
        self.assertIn(
            "region=ap-east-1",
            self.text,
            "release.yml terraform init must use region=ap-east-1 for the AWS S3 backend",
        )
        self.assertIn(
            "region=cn-hangzhou",
            self.text,
            "release.yml terraform init must use region=cn-hangzhou for the Alicloud OSS backend",
        )


if __name__ == "__main__":
    unittest.main()
