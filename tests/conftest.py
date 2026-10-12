"""Pytest config shared by every test module in this directory.

Adds `lambda/` and `lambda/scripts/` to `sys.path` so the package's
absolute imports (e.g. `from auth import get_github_app_token`,
`from scripts.autonomous import build_autonomous_user_data`) resolve
correctly without per-test boilerplate. This matches the layout the
AWS Lambda runtime uses after Terraform bundles `lambda/` as a
directory under the zip task root.

Also pins `BLITZLOG_PROVIDER=aws` so the factory in
`lambda/provider/__init__.py` loads the AWS implementations by
default. Individual tests that want to exercise the Alicloud stub
override the env var in their own setup.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lambda"))
sys.path.insert(0, os.path.join(HERE, "..", "lambda", "scripts"))

# Pin the provider before any module under test is imported. The
# factory reads this on cold start; if the first import happens to
# be from `provider/__init__.py` (via a transitive import of
# `lambda/auth.py` or `lambda/bot_pool.py`), the singleton would
# otherwise lock in whatever value was in the parent process env.
os.environ.setdefault("BLITZLOG_PROVIDER", "aws")
