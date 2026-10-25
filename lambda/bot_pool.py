"""Per-user bot pool, telegram user lookup, local LLM config.

Responsibilities:
    list_bot_pool              — read /blitzlog/users/<login>/telegram/pool/*
    get_telegram_user_id       — read /blitzlog/users/<login>/telegram/allowed-user-id
    parse_telegram_decision    — pull the first matching callback_query from a getUpdates payload
    get_local_llm_config       — read /blitzlog/users/<login>/local-llm/* and validate the endpoint
                                 IP via llm_guard (loopback/IMDS hard-rejected; RFC1918/ULA/CGNAT
                                 opt-in only; public IPs hard-rejected)
    get_opencode_profile       — read /blitzlog/users/<login>/opencode/* (issue #165): the
                                 per-user default model + the per-user credential. The credential
                                 itself never leaves this module — only a boolean does.
    acquire_bot_token          — find a free bot in the pool, write a TTL'd S3 lock, return (name, token)
    _get_lock                  — read an S3 lock with TTL semantics (4 h)
    _update_lock_instance_id   — overwrite the lock with the real EC2 instance id after launch

Per-user data lives under /blitzlog/users/... (env-independent namespace) so
prod and dev Lambda instances read the same bot pool — there's one Telegram
bot pool per user, not per env.

After #83, this module no longer instantiates its own boto3 clients.
SSM reads go through `provider.get_secrets()` and S3 lock writes go
through `provider.get_object_store()`. The wire shape — parameter
names, lock keys, error codes — is byte-identical to the pre-#83
behaviour so the test suite and the consumer in
`lambda/handler.py` are unaffected.
"""

import ipaddress
import json
import os
import socket
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from _env import BOT_POOL_SSM_PATH, logger
from botocore.exceptions import ClientError
from llm_guard import (
    _ip_always_blocked,
    _ip_is_opt_in_only,
    _resolve_endpoint_ips,
    validate_byo_endpoint,
)
from model_select import UserOpencodeProfile
from provider import get_object_store, get_secrets

BOT_POOL_LOCK_PREFIX = "bot-pool-locks"
BOT_POOL_LOCK_TTL_HOURS = 4


def list_bot_pool(sender_login: str) -> dict[str, str]:
    params = get_secrets().get_by_path(
        f"{BOT_POOL_SSM_PATH}/{sender_login}/telegram/pool",
        decrypt=True,
    )
    return dict(params)


def get_telegram_user_id(sender_login: str) -> str | None:
    try:
        return get_secrets().get(
            f"{BOT_POOL_SSM_PATH}/{sender_login}/telegram/allowed-user-id",
            decrypt=False,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ParameterNotFound", "404"):
            return None
        raise


def parse_telegram_decision(
    get_updates_payload: dict, allowed_callbacks: set[str]
) -> str | None:
    """Return the first matching callback_query.data decision in a Telegram
    getUpdates response, or None if none of the updates carry an allowed
    callback. Free-text messages are ignored.
    """
    for update in get_updates_payload.get("result", []) or []:
        callback = update.get("callback_query") or {}
        data = callback.get("data") or ""
        if data in allowed_callbacks:
            return data
    return None


def get_local_llm_config(sender_login: str) -> dict | None:
    """Read this user's local-llm SSM params and validate the endpoint URL.

    Returns a dict with keys {endpoint, model, api_key, allow_private, fallback}
    if a valid local LLM is configured, or None if no params exist, the endpoint
    fails the safety guard, or required fields are missing.

    Safety guard rules:
      - Always blocked: loopback, link-local (covers IMDS).
      - Opt-in (requires local_llm_endpoint_allow_private_cidrs == true):
        RFC1918 (10/8, 172.16/12, 192.168/16), ULA (fc00::/7),
        Tailscale CGNAT (100.64.0.0/10).
      - Anything else (public IPs, including DNS that resolves to a public IP)
        is hard-rejected with no opt-in.
    """
    if not sender_login:
        return None

    try:
        params = get_secrets().get_by_path(
            f"{BOT_POOL_SSM_PATH}/{sender_login}/local-llm",
            decrypt=True,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ParameterNotFound", "404"):
            return None
        raise

    endpoint = (params.get("endpoint") or "").strip()
    if not endpoint:
        return None

    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https"):
        logger.warning(
            "Local LLM endpoint %s rejected: non-http(s) scheme %r",
            endpoint,
            parsed.scheme,
        )
        return None

    if not parsed.hostname:
        logger.warning("Local LLM endpoint %s rejected: no hostname", endpoint)
        return None

    try:
        resolved_ips = _resolve_endpoint_ips(endpoint)
    except (socket.gaierror, ValueError) as e:
        logger.warning(
            "Local LLM endpoint %s rejected: DNS resolution failed: %s",
            endpoint,
            e,
        )
        return None

    if not resolved_ips:
        logger.warning(
            "Local LLM endpoint %s rejected: no resolved addresses", endpoint
        )
        return None

    allow_private = (params.get("allow-private-cidrs") or "").strip().lower() in (
        "true",
        "1",
        "yes",
    )

    for ip_str in resolved_ips:
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            logger.warning(
                "Local LLM endpoint %s rejected: cannot parse resolved IP %r",
                endpoint,
                ip_str,
            )
            return None

        if _ip_always_blocked(ip_obj):
            logger.warning(
                "Local LLM endpoint %s rejected: resolves to always-blocked IP %s "
                "(loopback or link-local)",
                endpoint,
                ip_obj,
            )
            return None

        if _ip_is_opt_in_only(ip_obj):
            if not allow_private:
                logger.warning(
                    "Local LLM endpoint %s rejected: resolves to private IP %s "
                    "but local_llm_endpoint_allow_private_cidrs is not enabled",
                    endpoint,
                    ip_obj,
                )
                return None
            continue

        logger.warning(
            "Local LLM endpoint %s rejected: resolves to public IP %s",
            endpoint,
            ip_obj,
        )
        return None

    model = (params.get("model") or "").strip()
    if not model:
        logger.warning("Local LLM endpoint %s rejected: model is empty", endpoint)
        return None

    fallback = (params.get("fallback") or "closed").strip().lower()
    if fallback not in ("closed", "cloud"):
        fallback = "closed"

    return {
        "endpoint": endpoint,
        "model": model,
        "api_key": params.get("api-key") or "",
        "allow_private": allow_private,
        "fallback": fallback,
        "tailscale_auth_key": (params.get("tailscale-auth-key") or "").strip(),
    }


def get_opencode_profile(sender_login: str) -> UserOpencodeProfile | None:
    """Read this user's per-user opencode config (issue #165).

    The namespace is `/blitzlog/users/<login>/opencode/*` — it mirrors
    the org layout `/blitzlog/<env>/opencode/api-key` and, like every
    other per-user namespace, carries no env prefix: a buyer has one
    model default and one credential, not one per env.

    Parameters:
        model               — the buyer's default model (`provider/model`)
        api-key             — the buyer's own credential (SecureString)
        provider            — the provider the credential belongs to
        base-url            — an optional self-hosted endpoint for the model
        allow-private-cidrs — opt-in for a private (Tailnet/VPC) base URL

    Returns `None` when the user has no opencode config at all (the
    common case — the org default model and the org credential are
    then used, exactly as before issue #165).

    The API KEY ITSELF NEVER LEAVES THIS FUNCTION. The caller only
    learns whether a credential exists (`has_api_key`); the key is
    fetched on the EC2 host from a server-derived secret-store path.
    That keeps key material out of the Lambda's memory, its logs, and
    the rendered user-data.

    A `base-url` that fails the safety guard rejects the WHOLE
    profile rather than silently dropping the base URL: the endpoint
    is where the credential gets sent, so falling back to the
    provider's default endpoint would move the user's key to a host
    the user did not choose.
    """
    if not sender_login:
        return None

    try:
        params = get_secrets().get_by_path(
            f"{BOT_POOL_SSM_PATH}/{sender_login}/opencode",
            decrypt=True,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ParameterNotFound", "404"):
            return None
        raise

    model = (params.get("model") or "").strip()
    api_key = (params.get("api-key") or "").strip()
    provider = (params.get("provider") or "").strip()
    base_url = (params.get("base-url") or "").strip()

    if not any((model, api_key, provider, base_url)):
        return None

    if base_url:
        allow_private = (params.get("allow-private-cidrs") or "").strip().lower() in (
            "true",
            "1",
            "yes",
        )
        ok, reason = validate_byo_endpoint(base_url, allow_private_cidrs=allow_private)
        if not ok:
            logger.warning(
                "Opencode base URL %s rejected for %s: %s — the per-user profile is "
                "ignored for this run",
                base_url,
                sender_login,
                reason,
            )
            return None

    return UserOpencodeProfile(
        model=model,
        has_api_key=bool(api_key),
        provider=provider,
        base_url=base_url,
    )


def _lock_key(sender_login: str, bot_name: str) -> str:
    return f"{BOT_POOL_LOCK_PREFIX}/{sender_login}/{bot_name}.json"


def _get_lock(sender_login: str, bot_name: str, bucket: str) -> dict | None:
    try:
        body = get_object_store().get(
            bucket,
            _lock_key(sender_login, bot_name),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    data = json.loads(body.decode())
    acquired_at = datetime.fromisoformat(data["acquired_at"])
    if datetime.now(UTC) - acquired_at > timedelta(hours=BOT_POOL_LOCK_TTL_HOURS):
        logger.info(
            "Stale lock for %s (acquired %s), treating as free",
            bot_name,
            data["acquired_at"],
        )
        return None
    return data


def acquire_bot_token(
    sender_login: str, instance_id: str, repo: str, issue_number: int
) -> tuple[str, str] | None:
    bucket = os.environ.get("S3_LOGS_BUCKET", "<your-agent-logs-bucket>")
    pool = list_bot_pool(sender_login)
    if not pool:
        logger.error("No bot pool configured for user %s", sender_login)
        return None

    for bot_name in sorted(pool.keys()):
        lock = _get_lock(sender_login, bot_name, bucket)
        if lock is not None:
            logger.info(
                "Bot %s (user %s) is locked by %s",
                bot_name,
                sender_login,
                lock.get("instance_id", "unknown"),
            )
            continue

        lock_payload = json.dumps(
            {
                "instance_id": instance_id,
                "issue_number": issue_number,
                "repo": repo,
                "acquired_at": datetime.now(UTC).isoformat(),
            }
        )
        try:
            get_object_store().put(
                bucket,
                _lock_key(sender_login, bot_name),
                lock_payload.encode(),
            )
        except ClientError as e:
            logger.warning(
                "Failed to write lock for %s/%s: %s", sender_login, bot_name, e
            )
            continue

        logger.info(
            "Acquired bot %s for user %s, issue #%d",
            bot_name,
            sender_login,
            issue_number,
        )
        return (bot_name, pool[bot_name])

    logger.warning("All bots in pool for user %s are locked", sender_login)
    return None


def _update_lock_instance_id(
    sender_login: str, bot_name: str, instance_id: str, repo: str, issue_number: int
) -> None:
    bucket = os.environ.get("S3_LOGS_BUCKET", "<your-agent-logs-bucket>")
    lock_payload = json.dumps(
        {
            "instance_id": instance_id,
            "issue_number": issue_number,
            "repo": repo,
            "acquired_at": datetime.now(UTC).isoformat(),
        }
    )
    try:
        get_object_store().put(
            bucket,
            _lock_key(sender_login, bot_name),
            lock_payload.encode(),
        )
    except ClientError as e:
        logger.warning("Failed to update lock for %s/%s: %s", sender_login, bot_name, e)
