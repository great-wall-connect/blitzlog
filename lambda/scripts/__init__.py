"""Provider-dispatched user-data script package."""

# N999 (invalid module name `lambda`) is a pre-existing project-wide
# decision — the package directory is named `lambda/`, which shadows
# a Python keyword. The original `lambda/handler.py`, `lambda/_env.py`,
# etc. are also affected (or would be, if they were sub-modules); they
# pass ruff today only because they're at the package root and ruff
# doesn't flag the root name. After issue #126 added sub-modules
# (lambda/scripts/{aws,alicloud}/__init__.py), ruff started flagging
# those as N999 because the *parent* package name `lambda` is a
# keyword. See the AGENTS.md note about the `lambda/` project
# directory decision. The noqa below silences the noise; the
# directory rename is out of scope for #126.
# ruff: noqa: N999  (lambda/ is a pre-existing project directory name; not addressed by this PR)

import os

# Mirrors the factory in `lambda/provider/__init__.py` (`_resolve_provider_name`).
# Default to "aws" preserves the pre-#126 behaviour; an explicit "alicloud"
# picks the Alicloud tree. Unknown values fall back to "aws" with a clear
# log line so a typo doesn't silently dispatch to the wrong cloud.
_PROVIDER = os.environ.get("BLITZLOG_PROVIDER", "aws").strip().lower() or "aws"
if _PROVIDER not in ("aws", "alicloud"):
    # We don't have a logger here at import time (handler.py's `logger`
    # is configured later). Best-effort: print to stderr so the dispatch
    # surprise is visible in CloudWatch. The factory in
    # `lambda/provider/__init__.py` does the same for its own dispatch.
    import sys

    print(
        f"WARNING: BLITZLOG_PROVIDER={_PROVIDER!r} is not a recognised "
        f"provider; falling back to 'aws'. Valid: 'aws', 'alicloud'.",
        file=sys.stderr,
    )
    _PROVIDER = "aws"

if _PROVIDER == "alicloud":
    from scripts.alicloud.assisted import build_assisted_user_data
    from scripts.alicloud.autonomous import build_autonomous_user_data
else:
    from scripts.aws.assisted import build_assisted_user_data
    from scripts.aws.autonomous import build_autonomous_user_data


__all__ = [
    "build_assisted_user_data",
    "build_autonomous_user_data",
]
