"""Regression guards for the SemVer version source of truth and the
release-please config that owns it.

These tests are static (string-level checks on lambda/_version.py and
release-please-config.json, plus import-level checks for ``__version__``
and ``get_version()``). They mirror tests/test_entrypoint_readiness.py's
pattern: read a config file, assert the shapes that prevent the bug
from regressing. No shell, no docker, no AWS.

What this guards against:

- ``lambda._version.__version__`` drifting away from the release-please
  seed.
- release-please losing its ``extra-files`` entry (so a bump no longer
  edits ``lambda/_version.py``).
- release-please losing its pre-major bump flags (so a `feat:` on the
  0.x line jumps a published minor instead of releasing 1.x prematurely).
- ``get_version()`` and ``__version__`` falling out of sync (one updated
  without the other).
- ``lambda.__init__`` losing the ``__version__`` re-export, so callers
  that ``import lambda`` no longer see ``lambda.__version__``.
- The ``null_resource.lambda_build`` filemd5 list in `lambda.tf` losing
  `_version.py`, so changing the version doesn't trigger a rebuild.
"""

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAMBDA_INIT = REPO_ROOT / "lambda" / "__init__.py"
LAMBDA_VERSION = REPO_ROOT / "lambda" / "_version.py"
LAMBDA_TF = REPO_ROOT / "infra" / "modules" / "core" / "lambda.tf"
RELEASE_PLEASE_CONFIG = REPO_ROOT / "release-please-config.json"
RELEASE_PLEASE_MANIFEST = REPO_ROOT / ".release-please-manifest.json"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class TestLambdaVersion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Trigger the sys.path shim + module-level __version__ assignment
        # via a fresh import. conftest already inserted lambda/ onto
        # sys.path, so importlib resolves `lambda` from disk.
        # `lambda` is a Python reserved word, so the `import` statement
        # form `import lambda as ...` is a SyntaxError — import_module()
        # is the only valid way to load it by name.
        import importlib

        cls.pkg = importlib.import_module("lambda")
        cls.init_text = LAMBDA_INIT.read_text()
        cls.version_text = LAMBDA_VERSION.read_text()

    def test_version_is_semver_string(self):
        """__version__ MUST match ^\\d+\\.\\d+\\.\\d+$.

        release-please will only edit it if it's a plain SemVer string;
        anything else (a tuple, a leading 'v', a PEP 440 '0.1.0a1' suffix)
        trips its parser and either no-ops or errors. Anchored regex,
        not `re.search` — we want the entire string to be the version.
        """
        self.assertRegex(
            self.pkg.__version__,
            SEMVER_RE,
            f"__version__ {self.pkg.__version__!r} is not a SemVer string",
        )

    def test_version_module_constant_present_in_version_module(self):
        """__version__ assignment MUST live in ``lambda/_version.py``.

        release-please's ``extra-files`` config points at exactly this
        file. If the assignment moves (e.g. back into ``handler.py``)
        the config needs to be updated in lockstep or release-please
        silently no-ops on bumps. This test forces the change to fail
        loudly instead of silently breaking the release pipeline.
        """
        self.assertRegex(
            self.version_text,
            r'(?m)^__version__\s*=\s*["\']\d+\.\d+\.\d+["\']',
            "__version__ assignment missing from lambda/_version.py",
        )

    def test_get_version_helper_present(self):
        """get_version() helper MUST live in ``lambda/_version.py``."""
        self.assertRegex(
            self.version_text,
            r"(?m)^def get_version\b",
            "get_version() missing from lambda/_version.py",
        )

    def test_get_version_returns_module_constant(self):
        """get_version() MUST return the same value as __version__.

        They should never diverge — the helper is just a stable import
        surface (test mocks, handler logs) over the module constant.

        Imported here (not bound on the class) because unittest binds
        function attributes to ``self`` on lookup, which would pass the
        test instance as the first positional argument.
        """
        from _version import get_version

        self.assertEqual(
            get_version(),
            self.pkg.__version__,
            "get_version() and __version__ out of sync",
        )

    def test_init_reexports_version(self):
        """``lambda.__init__`` MUST re-export ``__version__`` from ``_version``.

        Callers that ``import lambda`` expect ``lambda.__version__`` to
        work without importing the submodule by name (which would
        require ``import lambda._version`` — and ``lambda`` is a
        reserved word in Python, so the only valid form is the relative
        import inside the package).
        """
        self.assertRegex(
            self.init_text,
            r"(?m)^from \._version import .*\b__version__\b",
            "lambda/__init__.py must re-export __version__ via `from ._version`",
        )


class TestReleasePleaseConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads(RELEASE_PLEASE_CONFIG.read_text())

    def test_release_type_is_python(self):
        """release-type MUST be python.

        Drives the changelog section ordering and the bump logic. Switching
        to `go` / `node` / etc. silently breaks the version parsing.
        """
        self.assertEqual(self.config.get("release-type"), "python")

    def test_extra_files_includes_lambda_version(self):
        """extra-files MUST list ``lambda/_version.py``.

        release-please only edits files explicitly listed here. Without
        this entry, the version string in ``lambda/_version.py`` is
        frozen forever and release-please opens release PRs with no
        actual version bump.
        """
        extra_files = self.config.get("extra-files", [])
        self.assertIn(
            "lambda/_version.py",
            extra_files,
            "release-please extra-files missing lambda/_version.py",
        )

    def test_pre_major_minor_bump_is_enabled(self):
        """bump-minor-pre-major MUST be true so 0.1.0 → 0.2.0 on `feat:`.

        Without it, release-please under 0.x treats `feat:` as a major
        bump (0.1.0 → 1.0.0), which violates the project's stated
        "stay on 0.x until API stability" contract.
        """
        self.assertTrue(
            self.config.get("bump-minor-pre-major"),
            "release-please bump-minor-pre-major must be true under 0.x",
        )

    def test_pre_major_patch_bump_is_enabled(self):
        """bump-patch-for-minor-pre-major MUST be true so 0.1.0 → 0.1.1 on `fix:`.

        Without it, release-please under 0.x treats `fix:` as a minor
        bump (0.1.0 → 0.2.0), inflating patch-only fixes into
        feature bumps.
        """
        self.assertTrue(
            self.config.get("bump-patch-for-minor-pre-major"),
            "release-please bump-patch-for-minor-pre-major must be true under 0.x",
        )

    def test_manifest_file_exists_and_is_json(self):
        """.release-please-manifest.json MUST exist and parse as JSON.

        ``googleapis/release-please-action@v4`` defaults to manifest mode
        and hard-errors with "Missing required manifest versions" when
        this file is absent. The manifest is the canonical source of
        truth for the current package version; release-please edits it
        on every release, and reviewers rely on its API in PRs to spot
        version drift.
        """
        self.assertTrue(
            RELEASE_PLEASE_MANIFEST.is_file(),
            f"{RELEASE_PLEASE_MANIFEST} must exist; release-please-action@v4 "
            "fails without it",
        )
        try:
            parsed = json.loads(RELEASE_PLEASE_MANIFEST.read_text())
        except json.JSONDecodeError as exc:
            self.fail(f"{RELEASE_PLEASE_MANIFEST} is not valid JSON: {exc}")
        self.assertIsInstance(
            parsed,
            dict,
            f"{RELEASE_PLEASE_MANIFEST} must be a JSON object at the top level",
        )
        self.assertNotEqual(
            parsed,
            {},
            f"{RELEASE_PLEASE_MANIFEST} must declare at least one package version",
        )

    def test_seed_version_is_semver(self):
        """The seeded version in the manifest MUST be a plain SemVer string.

        ``release-please-config.json`` no longer carries a top-level
        ``"version"`` key — that field was the legacy non-manifest
        convention and is ignored when ``release-please-action@v4``
        runs in manifest mode. The seed now lives in
        ``.release-please-manifest.json`` and must match ``^\\d+\\.\\d+\\.\\d+$``;
        anything else (a tuple, a leading 'v', a PEP 440 '0.1.0a1'
        suffix) trips release-please's parser and either no-ops or
        errors.
        """
        self.assertTrue(
            RELEASE_PLEASE_MANIFEST.is_file(),
            f"{RELEASE_PLEASE_MANIFEST} must exist; see test_manifest_file_exists_and_is_json",
        )
        manifest = json.loads(RELEASE_PLEASE_MANIFEST.read_text())
        # Single-package repo: the version lives under the "." key.
        version = manifest.get(".")
        self.assertIsNotNone(
            version,
            f"{RELEASE_PLEASE_MANIFEST} must declare a version under the '.' key",
        )
        self.assertRegex(
            version,
            SEMVER_RE,
            f"manifest version {version!r} is not a SemVer string",
        )


class TestHandlerVersionLog(unittest.TestCase):
    """The handler MUST log the version at module load.

    CloudWatch log correlation: when an issue happens at 02:00 UTC, the
    operator needs to know which build they were looking at. A single
    `logger.info("blitzlog-agent %s", ...)` line at module import is
    cheap and survives every subsequent log group rotation.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = (REPO_ROOT / "lambda" / "handler.py").read_text()

    def test_handler_imports_get_version(self):
        """handler.py MUST import get_version from _version."""
        self.assertRegex(
            self.text,
            r"(?m)^from _version import .*\bget_version\b",
            "handler.py must import get_version from _version",
        )

    def test_handler_logs_version_at_module_load(self):
        """handler.py MUST log the version at module load."""
        self.assertRegex(
            self.text,
            r"logger\.info\(\s*[\"']blitzlog-agent %s[\"'],\s*get_version\(\)\s*\)",
            "handler.py must log blitzlog-agent <version> at module load",
        )


class TestLambdaTfTriggersOnVersionChange(unittest.TestCase):
    """The Terraform ``null_resource.lambda_build`` MUST watch ``_version.py``.

    Without a ``filemd5`` trigger on ``_version.py``, bumping the version
    string won't invalidate the ``null_resource`` and the Lambda zip
    won't be rebuilt with the new version baked in. (The function
    ``lambda`` runtime never reads ``__version__`` at execution, but the
    value is logged at module load — CloudWatch log correlation breaks
    if the rebuild doesn't happen.)
    """

    @classmethod
    def setUpClass(cls):
        cls.text = LAMBDA_TF.read_text()

    def test_version_py_in_filemd5_triggers(self):
        """lambda.tf MUST list ``_version.py`` in null_resource.lambda_build triggers."""
        self.assertRegex(
            self.text,
            r"version_py\s*=\s*filemd5\(.*lambda/_version\.py",
            "lambda.tf must filemd5() _version.py in null_resource.lambda_build triggers",
        )


if __name__ == "__main__":
    unittest.main()
