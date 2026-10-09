"""Re-export the package's SemVer version literal.

The literal lives in ``version.py`` at the repo root — the standard
release-please Python target. release-please's ``PythonFileWithVersion``
updater preserves the file structure and only edits the
``__version__ = "..."`` line. This module exposes the literal as
``__version__`` (for direct imports) and ``get_version()`` (the
stable import surface for test mocks and the handler's startup log).
"""

from version import __version__


def get_version() -> str:
    """Return the package's SemVer version string (e.g. ``"0.1.0"``)."""
    return __version__
