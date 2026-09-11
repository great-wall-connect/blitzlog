"""Lambda webhook entrypoint + shared package-level constants.

The other modules in this package (`auth`, `bot_pool`, `ec2`,
`plugins`, `scripts.*`) import shared constants from this module
directly. We use absolute imports (no leading dot) so that the test
suite — which adds `lambda/` to `sys.path` — can import every
submodule as a top-level module.

For AWS Lambda deployment, the Terraform build step copies the whole
`lambda/` directory into `build/lambda/` and invokes the handler as
`lambda.handler.lambda_handler`. The handler path string is a runtime
identifier, not Python source, so `lambda` being a reserved keyword
in Python source is not a problem for the AWS Lambda runtime — only
for tests, which is why tests use absolute imports via `sys.path`.

Module-level constants defined here:
    BLITZLOG_ENV, SSM_PATH, BOT_POOL_SSM_PATH - env-scoped SSM namespaces
    WHISPER_STT_SHIM_SOURCE - pre-loaded whisper-stt-shim server.py
    logger                                    - root logger

Submodule layout:
    auth        - GitHub App JWT minting + webhook signature verification
    bot_pool    - Telegram bot pool / per-user bot acquisition + locks
    llm_guard   - IP-safety guard for user-configured local LLM endpoints
    ec2         - EC2 spot instance launch + subnet/AMI/price helpers
    plugins     - JS plugin .js loaders + heredoc-emit helpers
    scripts     - User-data bootstrap script builders (autonomous / assisted / shared)
"""

import base64
import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Env-scoped SSM namespaces + pre-loaded shim source
# ---------------------------------------------------------------------------

# Environment name passed in by Terraform as a Lambda env var (infra/modules/core/lambda.tf).
# Used to namespace all per-env SSM parameters under /blitzlog/<env>/... so prod and dev
# can coexist in the same AWS account without collision.
# Defaults to "prod" so tests / out-of-Lambda callers continue to work without
# the BLITZLOG_ENV env var being explicitly set.


def _blitzlog_env() -> str:
    return os.environ.get("BLITZLOG_ENV", "prod")


def _ssm_root() -> str:
    return f"/blitzlog/{_blitzlog_env()}"


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


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------

from botocore.exceptions import ClientError  # noqa: E402

from auth import (  # noqa: E402
    get_github_app_token,
    get_ssm_param,
    verify_github_signature,
)
from bot_pool import (  # noqa: E402
    _update_lock_instance_id,
    acquire_bot_token,
    get_local_llm_config,
    get_telegram_user_id,
)
from ec2 import launch_ec2_spot_instance  # noqa: E402
from scripts.assisted import build_assisted_user_data  # noqa: E402
from scripts.autonomous import build_autonomous_user_data  # noqa: E402


def extract_event_data(payload: dict) -> dict | None:
    if "detail" in payload:
        detail = payload["detail"]
        action = detail.get("action", "")
        issue = detail.get("issue", {})
        repo = payload.get("repository", detail.get("repository", {}))
        sender = detail.get("sender", payload.get("sender", {}))
    else:
        action = payload.get("action", "")
        issue = payload.get("issue", {})
        repo = payload.get("repository", {})
        sender = payload.get("sender", {})

    if not action or not issue:
        return None

    return {
        "action": action,
        "issue_number": issue.get("number"),
        "repo_full_name": repo.get("full_name", ""),
        "labels": {label["name"] for label in issue.get("labels", [])},
        "sender_login": sender.get("login", ""),
        "sender_id": sender.get("id", ""),
    }


def lambda_handler(event, context):
    logger.info("Lambda invoked")
    raw_body = event.get("body", "")
    logger.info("isBase64Encoded: %s", event.get("isBase64Encoded", False))

    if event.get("isBase64Encoded", False):
        body_bytes = base64.b64decode(raw_body)
    else:
        body_bytes = raw_body.encode()

    payload = json.loads(body_bytes)

    headers = event.get("headers", {})
    signature = headers.get("x-hub-signature-256") or headers.get(
        "X-Hub-Signature-256", ""
    )
    logger.info("Signature present: %s", bool(signature))

    try:
        webhook_secret = get_ssm_param("github-webhook/secret")
    except ClientError as e:
        logger.error("Failed to read webhook secret from SSM: %s", e)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"SSM read failed: {e}"}),
        }

    if not verify_github_signature(webhook_secret, body_bytes, signature):
        logger.warning("Signature verification failed")
        return {"statusCode": 401, "body": json.dumps({"error": "Invalid signature"})}

    event_data = extract_event_data(payload)
    if not event_data:
        return {"statusCode": 400, "body": json.dumps({"error": "Malformed event"})}

    logger.info("Action: %s, Labels: %s", event_data["action"], event_data["labels"])

    if event_data["action"] != "labeled":
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "Not a label action"}),
        }

    labels = event_data["labels"]

    if "autonomous" in labels:
        mode = "autonomous"
    elif "assisted" in labels:
        mode = "assisted"
    else:
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "No relevant label"}),
        }

    issue_number = event_data["issue_number"]
    repo_full_name = event_data["repo_full_name"]
    sender_login = event_data.get("sender_login", "")
    sender_id = event_data.get("sender_id", "")

    try:
        github_token = get_github_app_token(repo_full_name)
    except Exception as e:
        logger.exception("GitHub App auth failed")

        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"GitHub App auth failed: {e}"}),
        }

    builder = (
        build_autonomous_user_data if mode == "autonomous" else build_assisted_user_data
    )

    bot_name = ""
    bot_token = ""
    telegram_user_id = ""

    if mode == "assisted":
        if not sender_login:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {"error": "Cannot determine sender for assisted mode"}
                ),
            }
        telegram_user_id = get_telegram_user_id(sender_login) or ""
        if not telegram_user_id:
            logger.warning("No Telegram user ID configured for %s", sender_login)
            return {
                "statusCode": 503,
                "body": json.dumps(
                    {"error": f"No bot pool configured for user {sender_login}"}
                ),
            }
        import uuid as _uuid

        tentative_id = f"i-{_uuid.uuid4().hex[:8]}"
        pool_result = acquire_bot_token(
            sender_login, tentative_id, repo_full_name, issue_number
        )
        if pool_result is None:
            logger.warning(
                "Bot pool exhausted for user %s, returning 503", sender_login
            )
            return {
                "statusCode": 503,
                "body": json.dumps(
                    {"error": f"No bot pool configured for user {sender_login}"}
                ),
            }
        bot_name, bot_token = pool_result

    local_llm = None
    if sender_login:
        try:
            local_llm = get_local_llm_config(sender_login)
        except (ClientError, ValueError, OSError) as e:
            logger.warning(
                "Failed to read local LLM config for %s: %s", sender_login, e
            )
            local_llm = None

    def _builder(repo, issue_number, sender_login="", sender_id=""):
        if mode == "assisted":
            return builder(
                repo,
                issue_number,
                sender_login,
                sender_id,
                bot_name=bot_name,
                bot_token=bot_token,
                telegram_user_id=telegram_user_id,
                local_llm=local_llm,
            )
        return builder(repo, issue_number, sender_login, sender_id, local_llm=local_llm)

    try:
        instance_id = launch_ec2_spot_instance(
            repo_full_name,
            issue_number,
            github_token,
            mode,
            _builder,
            sender_login=sender_login,
            sender_id=sender_id,
        )
        logger.info("Launched EC2 instance (%s): %s", mode, instance_id)
        if mode == "assisted" and bot_name:
            _update_lock_instance_id(
                sender_login, bot_name, instance_id, repo_full_name, issue_number
            )
    except ClientError as e:
        logger.exception("EC2 launch failed")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"EC2 launch failed: {e}"}),
        }

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "message": f"EC2 spot instance launched ({mode}) for issue #{issue_number}",
                "instance_id": instance_id,
                "mode": mode,
            }
        ),
    }
