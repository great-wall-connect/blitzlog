"""Lambda webhook entrypoint.

Receives GitHub `issues.labeled` webhooks, verifies the HMAC-SHA256
signature, mints a short-lived GitHub App installation token, optionally
acquires a free bot from the user's per-env-independent pool (assisted
mode only), and launches an EC2 spot instance to run the agent.

`extract_event_data` normalizes both EventBridge-style payloads
(`{"detail": {...}}`) and direct webhook payloads (`{"action": ..., ...}`)
into a flat dict that the rest of the pipeline consumes.
"""

import base64
import json

from _env import logger
from auth import (
    get_github_app_token,
    get_ssm_param,
    verify_github_signature,
)
from bot_pool import (
    _update_lock_instance_id,
    acquire_bot_token,
    get_local_llm_config,
    get_telegram_user_id,
)
from botocore.exceptions import ClientError
from ec2 import launch_ec2_spot_instance
from scripts.assisted import build_assisted_user_data
from scripts.autonomous import build_autonomous_user_data


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
    except (
        Exception
    ) as e:  # noqa: BLE001 - GitHub App auth can raise many types; log + 500
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
