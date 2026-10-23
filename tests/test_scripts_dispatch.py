"""Regression tests for the BLITZLOG_PROVIDER dispatcher in
`lambda/scripts/__init__.py` (issue #126).

The dispatcher picks `scripts.aws.*` or `scripts.alicloud.*` based
on the `BLITZLOG_PROVIDER` env var (default `"aws"`). Each
subpackage exposes the same public functions
(`build_autonomous_user_data`, `build_assisted_user_data`,
`script_header`, ...) so callers (`lambda/handler.py`) can do
`from scripts import ...` without per-cloud branching.

These tests are static — they import the dispatcher with different
`BLITZLOG_PROVIDER` values and assert the right subpackage was
loaded. They re-import the package via `importlib.reload` so the
top-level env var change takes effect (Python caches the import
state on first load; the reload re-runs the dispatcher block).

The dispatcher's env-var read happens at import time (one-shot),
matching the existing `lambda/provider/__init__.py` factory's
behaviour. A Lambda cold start is the only natural boundary; the
provider is fixed at function boot.
"""

import importlib
import os
import sys
import unittest


def _reload_scripts_with_provider(provider: str | None) -> object:
    """Reload `scripts` with BLITZLOG_PROVIDER set (or unset) in the env.

    Returns the freshly-loaded `scripts` module. The reload re-runs
    the top-level dispatcher block, so a different provider value
    re-binds the public symbols to the new subpackage's functions.
    """
    if provider is None:
        os.environ.pop("BLITZLOG_PROVIDER", None)
    else:
        os.environ["BLITZLOG_PROVIDER"] = provider
    # Drop any cached module references; `importlib.reload` only
    # works on the same module object, and tests in this file run
    # in arbitrary order, so we wipe the cache to be safe.
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            del sys.modules[name]
    return importlib.import_module("scripts")


class TestScriptsDispatch(unittest.TestCase):
    """Verify the BLITZLOG_PROVIDER dispatcher binds the right
    subpackage's public symbols.
    """

    def setUp(self):
        # Snapshot the env so each test can set its own value and
        # restore on tearDown — `conftest.py` sets BLITZLOG_PROVIDER=aws
        # at module load time; the dispatcher re-reads at reload.
        self._saved_provider = os.environ.get("BLITZLOG_PROVIDER")

    def tearDown(self):
        if self._saved_provider is None:
            os.environ.pop("BLITZLOG_PROVIDER", None)
        else:
            os.environ["BLITZLOG_PROVIDER"] = self._saved_provider

    def test_default_provider_is_aws(self):
        """`BLITZLOG_PROVIDER` unset → AWS (pre-#126 default)."""
        scripts = _reload_scripts_with_provider(None)
        self.assertEqual(
            scripts.build_autonomous_user_data.__module__,
            "scripts.aws.autonomous",
            "default BLITZLOG_PROVIDER must dispatch to scripts.aws.autonomous",
        )
        self.assertEqual(
            scripts.build_assisted_user_data.__module__,
            "scripts.aws.assisted",
            "default BLITZLOG_PROVIDER must dispatch to scripts.aws.assisted",
        )

    def test_explicit_aws_provider(self):
        """`BLITZLOG_PROVIDER=aws` → AWS subpackage."""
        scripts = _reload_scripts_with_provider("aws")
        self.assertEqual(
            scripts.build_autonomous_user_data.__module__,
            "scripts.aws.autonomous",
        )

    def test_explicit_alicloud_provider(self):
        """`BLITZLOG_PROVIDER=alicloud` → Alicloud subpackage."""
        scripts = _reload_scripts_with_provider("alicloud")
        self.assertEqual(
            scripts.build_autonomous_user_data.__module__,
            "scripts.alicloud.autonomous",
            "BLITZLOG_PROVIDER=alicloud must dispatch to scripts.alicloud.autonomous",
        )
        self.assertEqual(
            scripts.build_assisted_user_data.__module__,
            "scripts.alicloud.assisted",
        )

    def test_unknown_provider_falls_back_to_aws(self):
        """`BLITZLOG_PROVIDER=garbage` → AWS with a stderr warning.

        The dispatcher prints a warning to stderr and falls back to
        `aws`. We don't assert the warning (stderr capture isn't
        worth the boilerplate here); we just check the dispatch
        outcome is the AWS subpackage.
        """
        scripts = _reload_scripts_with_provider("garbage")
        self.assertEqual(
            scripts.build_autonomous_user_data.__module__,
            "scripts.aws.autonomous",
            "unknown BLITZLOG_PROVIDER must fall back to scripts.aws.autonomous",
        )

    def test_empty_provider_falls_back_to_aws(self):
        """`BLITZLOG_PROVIDER=` (empty string) → AWS (default)."""
        scripts = _reload_scripts_with_provider("")
        self.assertEqual(
            scripts.build_autonomous_user_data.__module__,
            "scripts.aws.autonomous",
            "empty BLITZLOG_PROVIDER must fall back to the AWS subpackage",
        )


if __name__ == "__main__":
    unittest.main()
