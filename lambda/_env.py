"""Env-scoped SSM namespaces + pre-loaded shim source.

Shared by every other module in the package. Lives in its own module
(no other dependencies) so the rest of the package can import these
constants without creating a circular dependency with `handler.py`.

The Terraform-deployed Lambda zip has `lambda/` at its task root and
invokes `lambda.handler.lambda_handler`. The shim source is read once
at cold start and embedded into the bootstrap heredoc, avoiding a
second network fetch from inside the EC2 instance.
"""

import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _blitzlog_env() -> str:
    return os.environ.get("BLITZLOG_ENV", "prod")


def _ssm_root() -> str:
    return f"/blitzlog/{_blitzlog_env()}"


# Environment name passed in by Terraform as a Lambda env var
# (infra/modules/core/lambda.tf). Used to namespace all per-env SSM
# parameters under /blitzlog/<env>/... so prod and dev can coexist in
# the same AWS account without collision.
# Defaults to "prod" so tests / out-of-Lambda callers continue to work
# without the BLITZLOG_ENV env var being explicitly set.
BLITZLOG_ENV = _blitzlog_env()
SSM_PATH = _ssm_root()

# Per-user data (bot pools, local LLM config) is env-independent — a user
# has one Telegram bot pool and one local LLM endpoint, not one per env.
# Both prod and dev Lambda instances read from this single namespace.
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
