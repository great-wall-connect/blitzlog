"""Pytest config shared by every test module in this directory.

Adds `lambda/` and `lambda/scripts/` to `sys.path` so the package's
absolute imports (e.g. `from auth import get_github_app_token`,
`from scripts.autonomous import build_autonomous_user_data`) resolve
correctly without per-test boilerplate. This matches the layout the
AWS Lambda runtime uses after Terraform bundles `lambda/` as a
directory under the zip task root.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lambda"))
sys.path.insert(0, os.path.join(HERE, "..", "lambda", "scripts"))
