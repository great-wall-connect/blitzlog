"""Single source of truth for the package's SemVer version.

`release-please` (release-please-config.json, extra-files) edits this
file on every release PR. The constant is also re-exported from
``lambda.__init__`` so callers that ``import lambda`` still see
``lambda.__version__`` without needing to import the submodule by name
(which is awkward: ``lambda`` is a reserved word and can't appear in
``import`` statements).

Keep the literal in this file in sync with the ``version`` seed in
release-please-config.json — they should always match between releases
(release-please updates both atomically).

Module-level ``__version__`` is the contract; ``get_version()`` exists
so callers (handler, tests, scripts) have a stable import surface and
can mock the version in tests without touching the global.
"""

__version__ = "0.1.0"


def get_version() -> str:
    """Return the package's SemVer version string (e.g. ``"0.1.0"``)."""
    return __version__
