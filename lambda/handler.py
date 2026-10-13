"""Lambda webhook entrypoint.

Receives two flavours of invocation:

  1. **GitHub webhook** (API Gateway → Lambda) — the original flow.
     `issues.labeled` triggers the handler, which verifies the
     HMAC-SHA256 signature, mints a GitHub App installation token
     (lifetime configured by the GITHUB_TOKEN_LIFETIME_HOURS env
     var, default 8 h — see `lambda/auth.py`), optionally acquires
     a free bot from the user's per-env-independent pool (assisted
     mode only), launches an EC2 spot instance to run the agent, and
     schedules the first token-refresh event (issue #145).

  2. **EventBridge Scheduler direct invocation** (issue #145) — the
     scheduler fires the Lambda with `{"action": "refresh", ...}` to
     rotate the GitHub App installation token before it expires.
     GitHub caps installation tokens to 1 h regardless of the
     requested lifetime, so the watchdog on each EC2 instance runs a
     5-min poll loop that picks up the new value from SSM and
     rewrites the bind-mounted credential files. The refresh handler
     re-uses `get_github_app_token()` (the same path the webhook
     uses) and reschedules itself until iteration 9 — a 10-iteration
     chain gives ~8.3 h of token freshness, well over the longest
     realistic assisted run, with a hard upper bound so a buggy
     schedule can't accumulate indefinitely.

`extract_event_data` normalizes both EventBridge-style payloads
(`{"detail": {...}}`) and direct webhook payloads (`{"action": ..., ...}`)
into a flat dict that the rest of the pipeline consumes.
"""

import base64
import json
import os
from datetime import datetime, timedelta, timezone

import boto3
from _env import SSM_PATH, logger
from _version import get_version
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
from provider import get_secrets
from scripts.assisted import build_assisted_user_data
from scripts.autonomous import build_autonomous_user_data

logger.info("blitzlog-agent %s", get_version())

# Issue #145: how many one-time refresh events to chain. 10 × 50 min =
# ~8.3 h of token freshness — well over the longest realistic assisted
# run (the EC2 `timeout` + the `idle_watchdog` 3 h hard-shutdown).
# Hard upper bound so a buggy schedule cannot accumulate indefinitely.
TOKEN_REFRESH_MAX_ITERATIONS = 10
# Gap between the current refresh and the next scheduled event. Sized
# to land ~10 min before the new token would expire (1 h cap), with
# slack for clock skew on the watchdog's 5-min poll loop.
TOKEN_REFRESH_INTERVAL_SECONDS = 3000  # 50 min

# Module-level boto3 clients. Same rationale as the singletons in
# `lambda/provider/__init__.py` — the Lambda runtime re-uses a warm
# container for up to ~15 min, and a fresh client per call would
# re-establish the connection. Initialised lazily on first use so a
# test that doesn't touch the refresh path doesn't pay the cost.
_scheduler_client = None
_sts_client = None
_account_id_cache: str | None = None


def _get_scheduler_client():
    global _scheduler_client
    if _scheduler_client is None:
        _scheduler_client = boto3.client("scheduler")
    return _scheduler_client


def _get_account_id() -> str:
    """Resolve the AWS account id once per cold start. Used to build
    the Lambda function ARN that EventBridge Scheduler targets. The
    `aws sts get-caller-identity` round-trip is ~30 ms and we want
    to avoid running it for every refresh event in the chain.
    """
    global _account_id_cache, _sts_client
    if _account_id_cache is not None:
        return _account_id_cache
    if _sts_client is None:
        _sts_client = boto3.client("sts")
    _account_id_cache = _sts_client.get_caller_identity()["Account"]
    return _account_id_cache


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

    # Issue #145: direct EventBridge Scheduler invocation for token
    # refresh. Detection: when the scheduler targets the Lambda
    # directly (rather than via API Gateway), the payload IS the
    # event dict, not a wrapped API Gateway request — so a top-level
    # `action == "refresh"` is the unambiguous signal. We do this
    # check BEFORE the signature verification path because the
    # scheduler invocation does not carry an `X-Hub-Signature-256`
    # header.
    if isinstance(event, dict) and event.get("action") == "refresh":
        return _handle_refresh(event)

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
    except Exception as e:  # noqa: BLE001
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

    # Issue #145: schedule the first 50-min-out token refresh. We do
    # this AFTER the EC2 launch (not before) so a launch failure
    # doesn't leave a dangling schedule. The Lambda schedules the
    # entire chain from cold-start — the EC2 host has no scheduler
    # permissions, which is the cleanest least-privilege boundary.
    # A failure here does NOT fail the launch: the static token in
    # SSM still covers the first ~50 min, and a single missed refresh
    # is far less bad than a webhook that 500s.
    try:
        schedule_next_refresh(
            issue=issue_number,
            instance=instance_id,
            iteration=0,
            repo=repo_full_name,
        )
        logger.info(
            "Scheduled initial token refresh for issue #%d (instance=%s)",
            issue_number,
            instance_id,
        )
    except (ClientError, RuntimeError) as e:
        logger.error(
            "Failed to schedule initial token refresh — agent will lose "
            "push access in ~50 min if this isn't fixed: %s",
            e,
        )

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


# ---------------------------------------------------------------------------
# Issue #145: scheduled GitHub App token refresh
# ---------------------------------------------------------------------------
#
# GitHub caps installation tokens to 1 h regardless of the requested
# `expires_at` (issue #145's empirical confirmation). To keep the
# blast-radius posture (a token in the LLM's reach is at most 1 h old)
# while making the 1 h operationally invisible to the agent, the
# Lambda schedules a chain of one-time EventBridge Scheduler events.
# Each event:
#   1. Mints a fresh token via the existing get_github_app_token path
#      (re-uses the battle-tested code; no risk of drift).
#   2. Overwrites the SecureString at
#      /blitzlog/<env>/ephemeral/github-token-<N> with overwrite=True.
#   3. If iteration < 9, schedules the next event 50 min out.
#
# The chain is the entire refresh story. No central registry, no
# continuous always-on schedule, no per-instance marker namespace.
# If the EC2 instance dies before the chain ends, the remaining events
# fire and no-op against the missing SSM parameter (~one SSM
# ListParameters + one missing-param 400, both ~50 ms and ~$0.0000023).


def _refresh_schedule_group_name() -> str:
    """Schedule group name for refresh events. Per-env so prod and dev
    chains never collide. Mirrors the `blitzlog-<env>` naming pattern
    used elsewhere in the module (resource names, SSM paths)."""
    return f"blitzlog-{os.environ.get('BLITZLOG_ENV', 'prod')}-token-refresh"


def _refresh_schedule_name(issue: int, iteration: int) -> str:
    """Per-(issue, iteration) schedule name. Names must be unique within
    a schedule group; the iteration suffix lets two events in the same
    chain coexist (EventBridge rejects duplicate names)."""
    return f"refresh-issue-{issue}-iter-{iteration}"


def schedule_next_refresh(
    *,
    issue: int,
    instance: str,
    iteration: int,
    repo: str,
    delay_seconds: int = TOKEN_REFRESH_INTERVAL_SECONDS,
) -> None:
    """Create a one-time EventBridge Scheduler event `delay_seconds`
    from now that re-invokes this Lambda with the same payload but
    `iteration + 1`. The Lambda is its own target (no separate
    consumer), so the chain self-perpetuates.

    Raises `ClientError` if the schedule cannot be created. The caller
    in `lambda_handler` catches the error and logs loudly without
    failing the webhook flow — the original launch + 1 h token still
    cover the first 50 min.
    """
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if not function_name:
        raise RuntimeError(
            "AWS_LAMBDA_FUNCTION_NAME is not set; cannot build target ARN"
        )
    scheduler_role_arn = os.environ.get("SCHEDULER_ROLE_ARN")
    if not scheduler_role_arn:
        raise RuntimeError(
            "SCHEDULER_ROLE_ARN is not set; cannot build EventBridge "
            "Scheduler target. Set it in infra/modules/core/lambda.tf."
        )

    region = os.environ.get("AWS_REGION") or os.environ.get(
        "AWS_DEFAULT_REGION", "us-east-1"
    )
    account_id = _get_account_id()
    target_arn = f"arn:aws:lambda:{region}:{account_id}:function:{function_name}"

    fire_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    schedule_name = _refresh_schedule_name(issue, iteration)
    schedule_group = _refresh_schedule_group_name()

    input_payload = json.dumps(
        {
            "action": "refresh",
            "issue": issue,
            "instance": instance,
            "iteration": iteration,
            "repo": repo,
        }
    )

    logger.info(
        "Scheduling refresh event: name=%s group=%s fire_at=%s iteration=%d",
        schedule_name,
        schedule_group,
        fire_at.isoformat(),
        iteration,
    )
    _get_scheduler_client().create_schedule(
        Name=schedule_name,
        GroupName=schedule_group,
        ScheduleExpression=f"at({fire_at.strftime('%Y-%m-%dT%H:%M:%S')})",
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={
            "Arn": target_arn,
            "RoleArn": scheduler_role_arn,
            "Input": input_payload,
        },
        State="ENABLED",
    )


def _handle_refresh(event: dict) -> dict:
    """Handle a direct EventBridge Scheduler invocation.

    The handler is the only legitimate producer of refresh payloads, so
    we trust the event content (no signature check). The IAM grant that
    the scheduler's role needs (`lambda:InvokeFunction` on this
    function) is the perimeter; EventBridge enforces it.
    """
    try:
        issue = int(event["issue"])
    except (KeyError, TypeError, ValueError):
        logger.error("Refresh event missing/invalid 'issue': %r", event)
        return {"statusCode": 400, "body": "missing issue"}
    try:
        instance = str(event["instance"])
    except (KeyError, TypeError):
        logger.error("Refresh event missing/invalid 'instance': %r", event)
        return {"statusCode": 400, "body": "missing instance"}
    repo = str(event.get("repo", ""))
    if not repo:
        logger.error("Refresh event missing 'repo': %r", event)
        return {"statusCode": 400, "body": "missing repo"}
    try:
        iteration = int(event.get("iteration", 0))
    except (TypeError, ValueError):
        iteration = 0

    logger.info(
        "Refresh event received: issue=%d instance=%s iteration=%d repo=%s",
        issue,
        instance,
        iteration,
        repo,
    )

    try:
        new_token = get_github_app_token(repo)
    except Exception as e:  # noqa: BLE001
        logger.exception("Refresh: get_github_app_token failed")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"mint failed: {e}"}),
        }

    ephemeral_param = f"{SSM_PATH}/ephemeral/github-token-{issue}"
    try:
        get_secrets().put(
            ephemeral_param,
            new_token,
            type="SecureString",
            overwrite=True,
        )
    except ClientError as e:
        logger.exception("Refresh: SSM PutParameter failed")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"ssm put failed: {e}"}),
        }

    logger.info(
        "Refresh: wrote token to %s (iteration=%d)", ephemeral_param, iteration
    )

    if iteration < TOKEN_REFRESH_MAX_ITERATIONS - 1:
        try:
            schedule_next_refresh(
                issue=issue,
                instance=instance,
                iteration=iteration + 1,
                repo=repo,
            )
        except ClientError as e:
            # The token is already written; we just couldn't schedule
            # the next hop. The watchdog will keep using the current
            # token for up to 1 h; the next refresh would only be 50
            # min away anyway. Log loudly so an operator notices a
            # misconfigured scheduler policy.
            logger.error(
                "Refresh: failed to schedule next iteration "
                "(token is written; current value still good for 1 h): %s",
                e,
            )
    else:
        logger.info(
            "Refresh: chain ended at iteration %d "
            "(TOKEN_REFRESH_MAX_ITERATIONS=%d)",
            iteration,
            TOKEN_REFRESH_MAX_ITERATIONS,
        )

    return {"statusCode": 200, "body": json.dumps({"status": "refreshed"})}
