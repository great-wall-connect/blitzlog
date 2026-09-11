# ruff: noqa: N999
"""Make the package importable at AWS Lambda runtime.

When AWS Lambda imports ``lambda.handler`` (handler =
``lambda.handler.lambda_handler``), the Python runtime puts the zip's
task root (e.g. ``/var/task``) on ``sys.path``. The package's
submodules (``_env``, ``auth``, ``bot_pool``, ``ec2``, ``llm_guard``,
``plugins``) live inside ``lambda/`` — i.e. one directory below the task
root. ``scripts/_common``, ``scripts/autonomous``, and ``scripts/assisted``
live one level deeper at ``lambda/scripts/``. The modules use bare-name
imports (``from _env import logger``, ``from auth import
get_github_app_token``, ``from _common import …``) rather than
``lambda.X`` because ``lambda`` is a Python reserved word and cannot
appear in ``import`` statements.

Without this shim, ``import lambda.handler`` would resolve ``handler.py``
and then immediately fail with ``ModuleNotFoundError: No module named
'_env'`` at runtime, even though the test suite passes (the test
conftest adds ``lambda/`` and ``lambda/scripts/`` to ``sys.path``
directly, hiding the bug).

Inserting the package's own directory and the ``scripts/`` subdirectory
onto ``sys.path`` lets the existing bare-name imports resolve at
runtime, with zero changes to the module sources or to the existing
test layout.

This file is intentionally minimal: presence alone turns ``lambda`` from
a PEP 420 namespace package into a regular package, which is what the
runtime import path assumes. The ``sys.path`` mutation runs only when
``import lambda`` (or any submodule via the dotted path) actually
happens — tests that load modules by sys.path lookup
(``import auth``) never trigger this code.
"""

import os
import sys

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PACKAGE_DIR)
sys.path.insert(0, os.path.join(_PACKAGE_DIR, "scripts"))
