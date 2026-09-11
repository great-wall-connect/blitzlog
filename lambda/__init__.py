"""Blitzlog Lambda package.

Top-level package init. Defines the env-scoped SSM path constants
shared by every submodule, and pre-loads the whisper-stt-shim server
source at cold-start so the bootstrap can drop it into /opt without
needing the EC2 to fetch it from anywhere.

Module layout:
    handler     - Lambda entrypoint (lambda_handler) and event extraction
    auth        - GitHub App JWT minting + webhook signature verification
    bot_pool    - Telegram bot pool / per-user bot acquisition + locks
    llm_guard   - IP-safety guard for user-configured local LLM endpoints
    ec2         - EC2 spot instance launch + subnet/AMI/price helpers
    plugins     - JS plugin .js loaders + heredoc-emit helpers
    scripts     - User-data bootstrap script builders (autonomous / assisted / shared)

The Terraform-deployed Lambda zip has `lambda/` at its task root, so
the runtime invokes this entrypoint as `lambda.handler.lambda_handler`.
"""

import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _blitzlog_env() -> str:
    return os.environ.get("BLITZLOG_ENV", "prod")


def _ssm_root() -> str:
    return f"/blitzlog/{_blitzlog_env()}"


# Environment name passed in by Terraform as a Lambda env var (infra/modules/core/lambda.tf).
# Used to namespace all per-env SSM parameters under /blitzlog/<env>/... so prod and dev
# can coexist in the same AWS account without collision.
# Defaults to "prod" so tests / out-of-Lambda callers continue to work without
# the BLITZLOG_ENV env var being explicitly set.
BLITZLOG_ENV = _blitzlog_env()
SSM_PATH = _ssm_root()

# Per-user data (bot pools, local LLM config) is env-independent — a user has
# one Telegram bot pool and one local LLM endpoint, not one per env. Both prod
# and dev Lambda instances read from this single namespace.
BOT_POOL_SSM_PATH = "/blitzlog/users"


def _load_shim_source() -> str:
    """Read packages/whisper-stt-shim/server.py at cold start so the
    bootstrap can drop it into /opt/whisper-stt/server.py without an
    extra network hop. Tries two relative paths because the file lives
    outside this package — one is correct in the Terraform-bundled zip
    layout (lambda/packages/...), one in the source-tree layout used by
    unit tests (../packages/...).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(here, "packages", "whisper-stt-shim", "server.py"),
        os.path.join(here, "..", "packages", "whisper-stt-shim", "server.py"),
    )
    for candidate in candidates:
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            continue
    return ""


WHISPER_STT_SHIM_SOURCE = _load_shim_source()
