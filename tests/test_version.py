"""Regression guards for the SemVer version source of truth and the
release configuration that owns it.

These tests are static (string-level checks on lambda/version.py,
lambda/_version.py, and cog.toml, plus import-level checks for
``__version__`` and ``get_version()``). They mirror
tests/test_entrypoint_readiness.py's pattern: read a config file,
assert the shapes that prevent the bug from regressing. No shell, no
docker, no AWS.

What this guards against:

- ``lambda.version.__version__`` (the canonical cocogitto target
  the workflow's Bump step edits) drifting away from a valid
  SemVer string.
- ``lambda._version.__version__`` falling out of sync with
  ``lambda.version.__version__`` (re-export chain broken).
- ``get_version()`` and ``__version__`` falling out of sync.
- ``lambda.__init__`` losing the ``__version__`` re-export, so callers
  that ``import lambda`` no longer see ``lambda.__version__``.
- ``version-manifest.json`` (the workflow's bump-target file) losing
  the ``lambda/`` key or having a non-SemVer value.
- ``cog.toml`` drifting from the file (tag_prefix mismatch, or
  initial_tag != lambda/version.py when no v* tag exists).
- The ``null_resource.lambda_build`` filemd5 list in `lambda.tf` losing
  ``lambda/version.py``, so changing the version doesn't trigger a
  rebuild.
- A bare ``from version`` import in ``lambda/_version.py`` (which
  fails at Lambda runtime because the zip's task root only contains
  ``lambda/...`` — the actual error from the dev deployment that
  prompted the lambda/ prefix move).
"""

import json
import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAMBDA_INIT = REPO_ROOT / "lambda" / "__init__.py"
LAMBDA_VERSION = REPO_ROOT / "lambda" / "_version.py"
LAMBDA_VERSION_PY = REPO_ROOT / "lambda" / "version.py"
LAMBDA_TF = REPO_ROOT / "infra" / "modules" / "core" / "lambda.tf"
COG_TOML = REPO_ROOT / "cog.toml"
VERSION_MANIFEST = REPO_ROOT / "version-manifest.json"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class TestVersionSource(unittest.TestCase):
    """The canonical version literal MUST live in ``lambda/version.py``,
    where the release workflow's Bump step edits it in place via a
    one-line ``sed`` (preserves the file structure and only edits the
    ``__version__ = "..."`` line). cocogitto handles the bump-type
    detection in the Detect step; ``lambda/version.py`` is the
    source of truth that the Bump step writes to.

    Earlier configs pointed at a root-level ``version.py`` (a path the
    zip build excludes — the runtime error from the dev deployment that
    prompted the lambda/ prefix move) or at ``lambda/_version.py`` (a
    re-export module whose docstring we don't want the bump step
    reading).
    """

    @classmethod
    def setUpClass(cls):
        cls.version_text = LAMBDA_VERSION_PY.read_text()

    def test_version_is_semver_string(self):
        """``lambda.version.__version__`` MUST match ^\\d+\\.\\d+\\.\\d+$.

        The Bump step's sed only matches a plain SemVer string;
        anything else (a tuple, a leading 'v', a PEP 440 '0.1.0a1'
        suffix) trips the sed and the Bump step silently no-ops.
        Anchored regex, not ``re.search`` — we want the entire string
        to be the version.
        """
        m = re.search(
            r'(?m)^__version__\s*=\s*["\'](\d+\.\d+\.\d+)["\']', self.version_text
        )
        self.assertIsNotNone(
            m,
            f"__version__ literal missing from {LAMBDA_VERSION_PY}",
        )
        self.assertRegex(
            m.group(1),
            SEMVER_RE,
            f"__version__ {m.group(1)!r} is not a SemVer string",
        )


class TestLambdaVersion(unittest.TestCase):
    """``lambda/_version.py`` re-exports from ``lambda.version`` and
    adds a stable ``get_version()`` helper. The literal MUST NOT live
    in ``_version.py`` itself — the release workflow's Bump step only
    knows how to edit the canonical version file (``lambda/version.py``);
    if the literal moved to ``_version.py``, the bump would no-op.
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

    def test_version_module_uses_relative_import(self):
        """``lambda/_version.py`` MUST prefer a relative import for ``__version__``.

        The lambda zip task root only contains ``lambda/...`` (the
        build does ``cp -r lambda/ build/``), so a bare
        ``from version import __version__`` would fail at runtime
        with ``ModuleNotFoundError: No module named 'version'`` —
        the actual error that triggered this fix. The relative
        ``from .version`` form resolves within the ``lambda`` package
        regardless of what's on ``sys.path``.

        The try/except ImportError fallback (a bare ``from version``
        import) is for the test suite only, where pytest's
        ``sys.modules`` isolation clears the package cache between
        tests and ``_version`` gets re-imported as a top-level
        module. The relative form works at runtime; the absolute
        form keeps the tests working. The test pins the runtime
        form as the primary path.
        """
        m = re.search(
            r"(?m)^\s*from \.version import .*\b__version__\b",
            self.version_text,
        )
        self.assertIsNotNone(
            m,
            "lambda/_version.py must re-export __version__ via "
            "`from .version` (relative import; bare `from version` fails "
            "at lambda runtime). An absolute `from version` fallback is "
            "allowed for test contexts where the module is loaded as a "
            "top-level after pytest's sys.modules isolation, but the "
            "runtime form must be the primary path.",
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


class TestVersionManifest(unittest.TestCase):
    """``version-manifest.json`` is the workflow's bump-target file.

    The Bump step's python line writes the new version into
    ``manifest["lambda/"]`` (the package path, with the trailing
    slash). The manifest is the source of truth that downstream
    tooling can read to discover the current published version.
    """

    def test_manifest_file_exists_and_is_json(self):
        """version-manifest.json MUST exist and parse as JSON."""
        self.assertTrue(
            VERSION_MANIFEST.is_file(),
            f"{VERSION_MANIFEST} must exist; the release workflow's "
            "Bump step writes to it on every release",
        )
        try:
            parsed = json.loads(VERSION_MANIFEST.read_text())
        except json.JSONDecodeError as exc:
            self.fail(f"{VERSION_MANIFEST} is not valid JSON: {exc}")
        self.assertIsInstance(
            parsed,
            dict,
            f"{VERSION_MANIFEST} must be a JSON object at the top level",
        )
        self.assertNotEqual(
            parsed,
            {},
            f"{VERSION_MANIFEST} must declare at least one package version",
        )

    def test_seed_version_is_semver(self):
        """The seeded version in the manifest MUST be a plain SemVer string.

        The Bump step's python line writes the new version as a
        SemVer string. The seed must match ``^\\d+\\.\\d+\\.\\d+$``;
        anything else (a tuple, a leading 'v', a PEP 440 '0.1.0a1'
        suffix) breaks downstream consumers.
        """
        self.assertTrue(
            VERSION_MANIFEST.is_file(),
            f"{VERSION_MANIFEST} must exist; see test_manifest_file_exists_and_is_json",
        )
        manifest = json.loads(VERSION_MANIFEST.read_text())
        # Single-package repo: the version lives under the "lambda/"
        # key (the package path), not the root "." key.
        version = manifest.get("lambda/")
        self.assertIsNotNone(
            version,
            f"{VERSION_MANIFEST} must declare a version under the 'lambda/' key",
        )
        self.assertRegex(
            version,
            SEMVER_RE,
            f"manifest version {version!r} is not a SemVer string",
        )


class TestCogAlignment(unittest.TestCase):
    """Static checks that lock in the alignment between ``cog.toml``,
    ``release.yml``'s Tag step, and ``lambda/version.py``.

    These are the Layer-1 alignment invariants. The runtime check
    (Layer 2) lives in ``release.yml``'s Detect step and fires on
    every dispatch; these tests catch the config drift at PR time.
    """

    @classmethod
    def setUpClass(cls):
        cls.cog_text = COG_TOML.read_text() if COG_TOML.is_file() else ""
        cls.release_yml_text = (
            REPO_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text()
        # Resolve the version literal in lambda/version.py once.
        m = re.search(
            r'(?m)^__version__\s*=\s*["\'](\d+\.\d+\.\d+)["\']',
            LAMBDA_VERSION_PY.read_text(),
        )
        cls.file_version = m.group(1) if m else None

    def test_cog_toml_tag_prefix_matches_workflow_tag_step(self):
        """``cog.toml::[bump].tag_prefix`` MUST match the prefix
        release.yml's Tag step writes.

        cocogitto reads ``LAST_TAG`` via ``git describe`` using its
        configured tag_prefix. release.yml's Tag step writes
        ``v${NEW_VERSION}``. If they disagree, cocogitto never sees
        the workflow's tags and falls into the no-tag branch.
        """
        m = re.search(r'(?m)^\s*tag_prefix\s*=\s*"([^"]+)"', self.cog_text)
        self.assertIsNotNone(
            m,
            f'{COG_TOML} must declare a tag_prefix (e.g. "v")',
        )
        cog_prefix = m.group(1)

        tag_step = re.search(
            r"(?ms)- name: Tag the release.*?run: \|\n(?P<body>(?:          .*\n)+)",
            self.release_yml_text,
        )
        self.assertIsNotNone(
            tag_step,
            "release.yml must have a 'Tag the release' step",
        )
        self.assertIn(
            f'git tag -a "{cog_prefix}',
            tag_step.group("body"),
            f"release.yml's Tag step must use tag_prefix {cog_prefix!r} "
            f"to match cog.toml",
        )

    def test_cog_toml_initial_tag_matches_version_file_when_no_v_tag(self):
        """When no v* tag exists, cocogitto uses ``[bump].initial_tag``
        as the bump base. That base MUST equal the version in
        ``lambda/version.py`` so a fresh-clone bump doesn't compute
        a backwards version.

        When a v* tag IS reachable, the runtime drift check in
        release.yml's Detect step covers that case — this test
        is a no-op then.
        """
        if self.file_version is None:
            self.skipTest(f"{LAMBDA_VERSION_PY} has no parseable __version__ literal")

        # Is a v* tag reachable? If yes, skip — the runtime check
        # in release.yml covers the in-tag case.
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip().startswith("v"):
            self.skipTest(
                "v* tag exists; runtime alignment check in release.yml's "
                "Detect step covers this case"
            )

        # No v* tag: assert equality between initial_tag and the file.
        m = re.search(r'(?m)^\s*initial_tag\s*=\s*"([^"]+)"', self.cog_text)
        self.assertIsNotNone(
            m,
            f"{COG_TOML} must declare initial_tag for the no-v-tag case",
        )
        initial_tag = m.group(1)
        self.assertRegex(
            initial_tag,
            SEMVER_RE,
            f"cog.toml initial_tag {initial_tag!r} is not a SemVer string",
        )
        self.assertEqual(
            initial_tag,
            self.file_version,
            f"cog.toml initial_tag {initial_tag!r} must equal "
            f"__version__ in lambda/version.py ({self.file_version!r}) "
            "so a fresh-clone bump doesn't go backwards",
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
            (REPO_ROOT / "lambda" / "handler.py").read_text(),
            r"(?m)^from _version import .*\bget_version\b",
            "handler.py must import get_version from _version",
        )

    def test_handler_logs_version_at_module_load(self):
        """handler.py MUST log the version at module load."""
        self.assertRegex(
            (REPO_ROOT / "lambda" / "handler.py").read_text(),
            r"logger\.info\(\s*[\"']blitzlog-agent %s[\"'],\s*get_version\(\)\s*\)",
            "handler.py must log blitzlog-agent <version> at module load",
        )


class TestLambdaTfTriggersOnVersionChange(unittest.TestCase):
    """The Terraform ``null_resource.lambda_build`` MUST watch the
    canonical version file so a bump invalidates the ``null_resource``
    and the Lambda zip gets rebuilt with the new version baked in.

    The function ``lambda`` runtime never reads ``__version__`` at
    execution, but the value is logged at module load — CloudWatch
    log correlation breaks if the rebuild doesn't happen.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = LAMBDA_TF.read_text()

    def test_lambda_version_py_in_filemd5_triggers(self):
        """lambda.tf MUST list ``lambda/version.py`` in
        ``null_resource.lambda_build`` triggers."""
        self.assertRegex(
            self.text,
            r"version_py\s*=\s*filemd5\(.*lambda/version\.py",
            "lambda.tf must filemd5() lambda/version.py in null_resource.lambda_build triggers",
        )

    def test_lambda_version_re_export_in_filemd5_triggers(self):
        """lambda.tf MUST also list ``lambda/_version.py`` (the re-export
        module) so changes to its body invalidate the zip too."""
        self.assertRegex(
            self.text,
            r"lambda_version_py\s*=\s*filemd5\(.*lambda/_version\.py",
            "lambda.tf must filemd5() lambda/_version.py in null_resource.lambda_build triggers",
        )


if __name__ == "__main__":
    unittest.main()
