"""Re-export the package's SemVer version literal.

The literal lives in ``lambda/version.py`` so that the release
workflow (``release.yml``) can edit it in place via a one-line
``sed`` (preserving the file structure and only changing the
version line). cocogitto handles the bump-type detection in the
Detect step; this file is the source of truth that the Bump step
writes to. This module exposes the literal as ``__version__`` and
``get_version()`` (the stable import surface for test mocks and
the handler's startup log).

The ``from .version`` form is a relative import that resolves
within the ``lambda`` package, so it works at Lambda runtime where
the zip's task root only contains ``lambda/...`` and the bare name
``version`` is NOT on ``sys.path``. A bare ``from version``
import would fail with ``ModuleNotFoundError: No module named
'version'`` on Lambda — the runtime error from the dev deployment
that prompted this fix.

The ``except ImportError`` fallback lets the test suite (which
loads ``_version`` as a top-level module after pytest's
``sys.modules`` isolation clears the package cache) still find
``lambda/version.py`` via the absolute name on ``sys.path`` (the
conftest adds ``lambda/``). At runtime the relative form succeeds
first and the absolute fallback is never hit.
"""

try:
    from .version import __version__
except ImportError:
    from version import __version__


def get_version() -> str:
    """Return the package's SemVer version string (e.g. ``"0.1.0"``)."""
    return __version__
