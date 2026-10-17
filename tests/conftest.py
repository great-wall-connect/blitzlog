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

Registers the `integration` marker and auto-skips
`@pytest.mark.integration` tests unless one of:

  * `BLITZLOG_RUN_INTEGRATION=1` is set in the env, or
  * `--run-integration` is passed on the pytest CLI

so the default `pytest` run is fast and AWS-free. The
`tests/test_watchdog_credential_poll_e2e.py::test_watchdog_poll_loop_rewrites_against_real_ssm`
test exercises the watchdog's poll library against the real AWS
SSM service; it is the Level 2 test in the 3-level testing
convention documented in `docs/WATCHDOG_TOKEN_REFRESH.md`.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lambda"))
sys.path.insert(0, os.path.join(HERE, "..", "lambda", "scripts"))

# Pin the provider before any module under test is imported. The
# factory reads this on cold start; if the first import happens to
# be from `provider/__init__.py` (via a transitive import of
# `lambda/auth.py` or `lambda/bot_pool.py`), the singleton would
# otherwise lock in whatever value was in the parent process env.
os.environ.setdefault("BLITZLOG_PROVIDER", "aws")


def pytest_configure(config: pytest.Config) -> None:
    """Register the `integration` marker so `pytest --markers` lists
    it and the unknown-marker warning at collection time goes away.
    """
    config.addinivalue_line(
        "markers",
        "integration: requires real AWS credentials; skipped "
        "unless BLITZLOG_RUN_INTEGRATION=1 or --run-integration "
        "is set (Level 2 of the 3-level testing convention; see "
        "docs/WATCHDOG_TOKEN_REFRESH.md).",
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add the `--run-integration` CLI flag as an explicit opt-in
    alongside the `BLITZLOG_RUN_INTEGRATION=1` env var. The flag is
    a convenience for workstations where the operator already has
    AWS_PROFILE set in the shell and wants a one-arg invocation.
    """
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help=(
            "Run tests marked @pytest.mark.integration. Requires "
            "AWS credentials with ssm:GetParameter, ssm:PutParameter, "
            "ssm:DeleteParameter on the test prefix "
            "(/blitzlog/<env>/ephemeral/gh/integration-test/repo/integration-test-*). "
            "Equivalent to setting BLITZLOG_RUN_INTEGRATION=1."
        ),
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Auto-skip `@pytest.mark.integration` tests unless the
    operator has explicitly opted in. The default `pytest` run
    stays fast and AWS-free; the lint CI workflow needs no change
    because it does not set the env var. Workstation runs opt in
    via `mise run test-integration` (which sets
    `BLITZLOG_RUN_INTEGRATION=1`) or the `--run-integration` flag;
    the nightly `.github/workflows/integration-test.yml` job sets
    the env var explicitly.
    """
    if config.getoption("--run-integration"):
        return
    if os.environ.get("BLITZLOG_RUN_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(
        reason=(
            "BLITZLOG_RUN_INTEGRATION=1 not set; pass "
            "--run-integration or set the env var to run this test "
            "(Level 2 of the 3-level testing convention)"
        )
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
