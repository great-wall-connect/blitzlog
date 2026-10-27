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
import hashlib
import json
import os
import time
from datetime import UTC, datetime, timedelta

import boto3
from _env import ephemeral_token_ssm_param, logger
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
from botocore.exceptions import BotoCoreError, ClientError
from ec2 import launch_ec2_spot_instance
from provider import get_secrets

# Issue #126: scripts.{assisted,autonomous} now live under
# `scripts.{aws,alicloud}/` and are dispatched by `scripts/__init__.py`
# on the `BLITZLOG_PROVIDER` env var. `from scripts import ...` resolves
# to the right cloud's `build_*_user_data` at function cold start.
from scripts import build_assisted_user_data, build_autonomous_user_data

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

# Issue #145: how many times to retry a `CreateSchedule` that failed
# for a recoverable reason (the group is mid-deletion, the schedule
# name is still held by a leftover). Each retry sleeps at most
# `SCHEDULE_CREATE_MAX_BACKOFF_SECONDS`; the whole loop must finish
# well inside the Lambda's 180 s timeout.
SCHEDULE_CREATE_MAX_ATTEMPTS = 6
SCHEDULE_CREATE_MAX_BACKOFF_SECONDS = 8

# Module-level boto3 clients. Same rationale as the singletons in
# `lambda/provider/__init__.py` — the Lambda runtime re-uses a warm
# container for up to ~15 min, and a fresh client per call would
# re-establish the connection. Initialised lazily on first use so a
# test that doesn't touch the refresh path doesn't pay the cost.
_scheduler_client = None
_sts_client = None
_ec2_client = None
_account_id_cache: str | None = None


def _get_scheduler_client():
    global _scheduler_client
    if _scheduler_client is None:
        _scheduler_client = boto3.client("scheduler")
    return _scheduler_client


def _get_ec2_client():
    global _ec2_client
    if _ec2_client is None:
        _ec2_client = boto3.client("ec2")
    return _ec2_client


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
#      /blitzlog/<env>/ephemeral/gh/<owner>/<repo>/<issue> with overwrite=True.
#   3. If iteration < 9, schedules the next event 50 min out.
#
# The chain is the entire refresh story. No central registry, no
# continuous always-on schedule, no per-instance marker namespace.
# If the EC2 instance dies before the chain ends, the remaining events
# fire and no-op against the missing SSM parameter (~one SSM
# ListParameters + one missing-param 400, both ~50 ms and ~$0.0000023).


def _refresh_schedule_group_name(repo: str, issue: int) -> str:
    r"""Per-(env, owner, repo, issue) schedule group name.

    Each running instance gets its own group; deleting the group at
    instance teardown cascade-deletes all chain events for that
    instance, so the watchdog's cleanup is one AWS API call (vs.
    iterating through iterations 0..9). Per-issue scope also means
    one instance's cleanup cannot nuke another instance's chain.

    Group names are capped at 64 chars and must match
    `[.\-_A-Za-z0-9]+`, so we can't just append the full
    `owner/repo/issue`. We hash the three-tuple with SHA-256 and
    take 16 hex chars (64 bits of collision space — well beyond
    what a single account+region will ever see). Mirrors the
    per-issue SSM namespace shape:
        /blitzlog/<env>/ephemeral/gh/<owner>/<repo>/<issue>
    """
    env = os.environ.get("BLITZLOG_ENV", "prod")
    digest = hashlib.sha256(f"{repo}|{issue}".encode()).hexdigest()[:16]
    return f"blitzlog-{env}-token-refresh-{digest}"


def _refresh_schedule_name(issue: int, iteration: int) -> str:
    """Per-(issue, iteration) schedule name. Names must be unique within
    a schedule group; the iteration suffix lets two events in the same
    chain coexist (EventBridge rejects duplicate names)."""
    return f"refresh-issue-{issue}-iter-{iteration}"


def _schedule_create_kwargs(
    *,
    schedule_name: str,
    schedule_group: str,
    fire_at: datetime,
    target_arn: str,
    scheduler_role_arn: str,
    input_payload: str,
) -> dict:
    """The `CreateSchedule` argument bag.

    Factored out so the retry paths (group-create, conflict-recovery)
    cannot drift apart from the happy path.
    """
    return {
        "Name": schedule_name,
        "GroupName": schedule_group,
        "ScheduleExpression": f"at({fire_at.strftime('%Y-%m-%dT%H:%M:%S')})",
        "ScheduleExpressionTimezone": "UTC",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": {
            "Arn": target_arn,
            "RoleArn": scheduler_role_arn,
            "Input": input_payload,
        },
        "State": "ENABLED",
    }


def _sweep_stale_schedule_group(schedule_group: str) -> None:
    """FALLBACK cleanup — delete a leftover per-issue schedule group.

    The watchdog's teardown is the PRIMARY cleanup path: it deletes
    the group when the agent run ends (see
    `infra/aws/packer/scripts-docker-ubuntu/watchdog.sh`). This sweep
    only matters when that teardown did not happen — the instance was
    killed hard, or the EC2 role was missing the
    `scheduler:DeleteSchedule` grant that the group delete needs.

    A surviving group is not harmless. EventBridge Scheduler does NOT
    delete a one-time `at()` schedule after it fires, so the previous
    run's `refresh-issue-<N>-iter-0` lingers in the group and the
    next run's first `CreateSchedule` fails with `ConflictException`.
    The chain then never starts and the agent's token dies 1 h after
    the launch-time mint.

    Best-effort by design: a failure here must not stop the chain
    from starting, so every error is logged and swallowed.
    """
    client = _get_scheduler_client()
    try:
        client.delete_schedule_group(Name=schedule_group)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code == "ResourceNotFoundException":
            # The healthy case: the watchdog's teardown already
            # removed the group. Nothing to sweep.
            return
        logger.warning(
            "Could not sweep leftover schedule group %s (%s) — continuing; "
            "the first CreateSchedule may collide with a leftover schedule.",
            schedule_group,
            code or e,
        )
        return
    logger.info(
        "Swept leftover schedule group %s — the watchdog teardown should "
        "have removed it; check the EC2 role's scheduler:DeleteSchedule "
        "grant (see docs/WATCHDOG_TOKEN_REFRESH.md).",
        schedule_group,
    )


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
            "Scheduler target. Set it in infra/aws/modules/core/lambda.tf."
        )

    region = os.environ.get("AWS_REGION") or os.environ.get(
        "AWS_DEFAULT_REGION", "us-east-1"
    )
    account_id = _get_account_id()
    target_arn = f"arn:aws:lambda:{region}:{account_id}:function:{function_name}"

    fire_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
    schedule_name = _refresh_schedule_name(issue, iteration)
    schedule_group = _refresh_schedule_group_name(repo, issue)

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
    create_kwargs = _schedule_create_kwargs(
        schedule_name=schedule_name,
        schedule_group=schedule_group,
        fire_at=fire_at,
        target_arn=target_arn,
        scheduler_role_arn=scheduler_role_arn,
        input_payload=input_payload,
    )

    client = _get_scheduler_client()

    # FALLBACK cleanup. The watchdog's teardown is the primary path
    # (see `_sweep_stale_schedule_group`'s docstring); this only
    # covers the case where it did not run. Only the first hop
    # sweeps — iterations 1..9 ARE the chain and must not delete
    # the group they live in.
    if iteration == 0:
        _sweep_stale_schedule_group(schedule_group)

    # Idempotent create. Three failure modes are recoverable, and
    # each one is a race with the scheduler's own bookkeeping:
    #
    #   ResourceNotFoundException — the per-issue group doesn't
    #     exist yet (first run for this issue, or the fallback
    #     sweep above just removed it). Create it, then retry.
    #   ConflictException — a schedule with this name already
    #     exists: a leftover from a previous run whose teardown
    #     never happened. Delete it, then retry.
    #   ValidationException ("... is being deleted") — the group
    #     delete is still in flight. EventBridge Scheduler is
    #     eventually consistent here; wait it out, then retry.
    #
    # Anything else propagates so the chain's failure logging in
    # `_handle_refresh` fires.
    last_error: ClientError | None = None
    for attempt in range(1, SCHEDULE_CREATE_MAX_ATTEMPTS + 1):
        try:
            client.create_schedule(**create_kwargs)
            return
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            message = e.response.get("Error", {}).get("Message", "") or ""
            last_error = e
            if code == "ResourceNotFoundException":
                logger.info(
                    "Schedule group %s not found; creating then retrying CreateSchedule",
                    schedule_group,
                )
                try:
                    client.create_schedule_group(Name=schedule_group)
                except ClientError as group_error:
                    if (
                        group_error.response.get("Error", {}).get("Code")
                        != "ConflictException"
                    ):
                        raise
                continue
            if code == "ConflictException":
                logger.warning(
                    "Schedule %s already exists; deleting the leftover and retrying",
                    schedule_name,
                )
                try:
                    client.delete_schedule(Name=schedule_name, GroupName=schedule_group)
                except ClientError as delete_error:
                    if (
                        delete_error.response.get("Error", {}).get("Code")
                        != "ResourceNotFoundException"
                    ):
                        raise
                continue
            if code == "ValidationException" and "being deleted" in message:
                delay = min(2**attempt, SCHEDULE_CREATE_MAX_BACKOFF_SECONDS)
                logger.info(
                    "Schedule group %s is still being deleted; retrying in %ds",
                    schedule_group,
                    delay,
                )
                time.sleep(delay)
                continue
            raise
    raise last_error  # pragma: no cover - the loop always returns or raises


def _instance_is_alive(instance: str) -> bool | None:
    """Is the agent's EC2 instance still alive?

    Returns True/False, or None when the check itself failed. The
    caller treats None as "don't know" and carries on — a transient
    EC2 error must never kill the refresh chain, because a dead
    chain costs the agent its GitHub access.

    A terminated instance is reported as False: EC2 keeps it visible
    for ~1 h after termination and then answers
    `InvalidInstanceID.NotFound`, which means the same thing.
    """
    try:
        response = _get_ec2_client().describe_instances(InstanceIds=[instance])
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code == "InvalidInstanceID.NotFound":
            # The instance is gone (terminated long enough ago that
            # EC2 no longer describes it).
            return False
        logger.warning(
            "Could not describe instance %s (continuing the refresh "
            "chain anyway): %s",
            instance,
            e,
        )
        return None
    except BotoCoreError as e:
        # No credentials, throttling, endpoint trouble — "don't know".
        logger.warning(
            "Could not describe instance %s (continuing the refresh "
            "chain anyway): %s",
            instance,
            e,
        )
        return None
    for reservation in response.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            state = inst.get("State", {}).get("Name")
            return state in ("pending", "running")
    return False


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

    # Instance-liveness guard. The watchdog's teardown deletes the
    # schedule group, so the chain normally dies with the run. But
    # if that cleanup failed (or the instance was killed hard), the
    # chain would keep re-minting tokens into SSM for hours after
    # the agent is gone — a zombie token nobody uses. If the
    # instance is no longer alive, stop the chain here: no token
    # write, no next hop.
    alive = _instance_is_alive(instance)
    if alive is False:
        logger.info(
            "Instance %s is no longer alive — stopping the token-refresh "
            "chain (iteration=%d). Not writing a token, not scheduling "
            "the next hop.",
            instance,
            iteration,
        )
        return {
            "statusCode": 200,
            "body": json.dumps({"status": "instance-gone"}),
        }

    try:
        new_token = get_github_app_token(repo)
    except Exception as e:  # noqa: BLE001
        logger.exception("Refresh: get_github_app_token failed")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"mint failed: {e}"}),
        }

    ephemeral_param = ephemeral_token_ssm_param(repo, issue)
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

    logger.info("Refresh: wrote token to %s (iteration=%d)", ephemeral_param, iteration)

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
            "Refresh: chain ended at iteration %d " "(TOKEN_REFRESH_MAX_ITERATIONS=%d)",
            iteration,
            TOKEN_REFRESH_MAX_ITERATIONS,
        )

    return {"statusCode": 200, "body": json.dumps({"status": "refreshed"})}
