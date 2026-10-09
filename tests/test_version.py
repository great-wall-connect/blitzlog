"""Regression guards for the SemVer version source of truth and the
release-please config that owns it.

These tests are static (string-level checks on version.py,
lambda/_version.py, and release-please-config.json, plus import-level
checks for ``__version__`` and ``get_version()``). They mirror
tests/test_entrypoint_readiness.py's pattern: read a config file,
assert the shapes that prevent the bug from regressing. No shell, no
docker, no AWS.

What this guards against:

- ``version.__version__`` (the canonical release-please target)
  drifting away from the release-please seed.
- release-please losing its ``version-file`` entry (so a bump no
  longer edits ``version.py``).
- release-please losing its pre-major bump flags (so a `feat:` on the
  0.x line jumps a published minor instead of releasing 1.x
  prematurely).
- ``lambda._version.__version__`` falling out of sync with
  ``version.__version__`` (re-export chain broken).
- ``get_version()`` and ``__version__`` falling out of sync.
- ``lambda.__init__`` losing the ``__version__`` re-export, so callers
  that ``import lambda`` no longer see ``lambda.__version__``.
- The ``null_resource.lambda_build`` filemd5 list in `lambda.tf` losing
  `version.py`, so changing the version doesn't trigger a rebuild.
"""

import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAMBDA_INIT = REPO_ROOT / "lambda" / "__init__.py"
LAMBDA_VERSION = REPO_ROOT / "lambda" / "_version.py"
VERSION_PY = REPO_ROOT / "version.py"
LAMBDA_TF = REPO_ROOT / "infra" / "modules" / "core" / "lambda.tf"
RELEASE_PLEASE_CONFIG = REPO_ROOT / "release-please-config.json"
RELEASE_PLEASE_MANIFEST = REPO_ROOT / ".release-please-manifest.json"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class TestVersionSource(unittest.TestCase):
    """The canonical version literal MUST live in ``version.py`` at the
    repo root, where release-please's ``PythonFileWithVersion`` updater
    preserves the file structure and only edits the
    ``__version__ = "..."`` line. Earlier configs pointed at
    ``lambda/_version.py`` (a non-standard path that release-please's
    python release-type does not recognize, so it fell back to the
    generic updater, which wiped the file on every bump — see #109).
    """

    @classmethod
    def setUpClass(cls):
        # Importing ``version`` requires the repo root on sys.path.
        # The conftest adds ``lambda/`` and ``lambda/scripts/`` but not
        # the repo root, so we add it here for the test process.
        # (Lambda runtime: the zip's task root IS on sys.path, so
        # ``from version import __version__`` works there without
        # any conftest-style fixup.)
        repo_root = str(REPO_ROOT)
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        cls.version_text = VERSION_PY.read_text()

    def test_version_is_semver_string(self):
        """``version.__version__`` MUST match ^\\d+\\.\\d+\\.\\d+$.

        release-please will only edit it if it's a plain SemVer
        string; anything else (a tuple, a leading 'v', a PEP 440
        '0.1.0a1' suffix) trips its parser and either no-ops or
        errors. Anchored regex, not ``re.search`` — we want the
        entire string to be the version.
        """
        m = re.search(
            r'(?m)^__version__\s*=\s*["\'](\d+\.\d+\.\d+)["\']', self.version_text
        )
        self.assertIsNotNone(
            m,
            f"__version__ literal missing from {VERSION_PY}",
        )
        self.assertRegex(
            m.group(1),
            SEMVER_RE,
            f"__version__ {m.group(1)!r} is not a SemVer string",
        )


class TestLambdaVersion(unittest.TestCase):
    """``lambda/_version.py`` re-exports from ``version.py`` and adds
    a stable ``get_version()`` helper. The literal MUST NOT live in
    ``_version.py`` itself — release-please only knows how to edit
    the canonical version file (``version.py``); if the literal
    moved to ``_version.py``, the bump would no-op.
    """

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

    def test_version_module_constant_present_in_version_module(self):
        """``__version__`` re-export MUST live in ``lambda/_version.py``.

        release-please's ``version-file`` config points at the root
        ``version.py``; ``_version.py`` re-exports that literal so
        existing import paths (``lambda.__version__``,
        ``from _version import __version__``) keep working. If the
        re-export breaks, the handler's startup log and the test
        mocks stop matching the manifest version.
        """
        self.assertRegex(
            self.version_text,
            r"(?m)^from version import .*\b__version__\b",
            "lambda/_version.py must re-export __version__ from version.py",
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
        # In manifest mode the per-package settings live under
        # `packages.{".": {...}}`; release-type / version-file /
        # extra-files are scoped to the package entry. Top-level keys
        # still hold the bump flags and (deprecated) shared options.
        cls.packages = cls.config.get("packages", {}).get(".", {})

    def test_release_type_is_python(self):
        """release-type MUST be `python` for the per-package entry.

        With ``version-file: version.py`` at the repo root, only the
        python release-type's ``PythonFileWithVersion`` updater is
        used (preserves file structure, edits only the version line).
        ``release-type: simple`` falls back to the generic updater
        and wipes the file — see #109 for the failure mode.
        """
        self.assertEqual(
            self.packages.get("release-type"),
            "python",
            "release-please per-package release-type must be 'python' "
            "(simple mode wipes the version file on every bump)",
        )

    def test_version_file_points_at_root(self):
        """``version-file`` MUST point at ``version.py`` at the repo root.

        The release-please Python file updater recognizes
        ``<prefix>/version.py`` (where prefix is the package path).
        With ``packages.{".": {...}}`` and ``prefix = "."``, the
        canonical file is ``./version.py``. ``lambda/_version.py``
        does not match the Python updater's pattern; the generic
        updater is then selected and the file is wiped (see #109).
        """
        self.assertEqual(
            self.packages.get("version-file"),
            "version.py",
            "release-please per-package version-file must be 'version.py' "
            "(a path that PythonFileWithVersion recognizes)",
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
