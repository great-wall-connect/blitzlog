"""GitHub App authentication + webhook signature verification.

Two responsibilities:

    verify_github_signature — constant-time HMAC-SHA256 comparison of the
    `X-Hub-Signature-256` header against the per-repo webhook secret stored
    in SSM.

    get_github_app_token — mints an installation token scoped to a single
    repository, with a custom lifetime (default 8 h, configurable via the
    GITHUB_TOKEN_LIFETIME_HOURS Lambda env var). Builds a GitHub App JWT
    with the App's private key, then POSTs to
    `/app/installations/<id>/access_tokens` with
    `{"repositories": [<repo_name>], "expires_at": "<ISO 8601>"}` so the
    issued token has only the permissions the worker needs and lasts
    long enough to cover an overnight assisted run.

    The 8 h default is wider than the watchdog (the EC2 `timeout` plus
    the `idle_watchdog` 3 h hard-shutdown) on purpose: a single static
    token keeps `git push` and `gh auth login` both alive without giving
    the agent AWS credentials to refresh them.
"""

import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import boto3
import jwt
import requests
from _env import SSM_PATH, logger
from botocore.config import Config

_ssm = boto3.client("ssm", config=Config(retries={"max_attempts": 1}))

# Installation-token lifetime in hours. Read from the Lambda env on every
# call so the Terraform `github_token_lifetime_hours` variable can tune
# it per-env (e.g. below 8 h for GitHub Apps whose org policy caps
# installation tokens). Default matches the README's "up to 8h lifetime"
# promise. Must be >= 1 — non-positive values fall back to the default
# with a warning so a misconfigured env var can't mint a 0-second token.
GITHUB_TOKEN_LIFETIME_HOURS_DEFAULT = 8


def _get_github_token_lifetime_hours() -> int:
    raw = os.environ.get("GITHUB_TOKEN_LIFETIME_HOURS")
    if raw is None or raw == "":
        return GITHUB_TOKEN_LIFETIME_HOURS_DEFAULT
    try:
        hours = int(raw)
    except ValueError:
        logger.warning(
            "GITHUB_TOKEN_LIFETIME_HOURS=%r is not an integer; falling back to %d",
            raw,
            GITHUB_TOKEN_LIFETIME_HOURS_DEFAULT,
        )
        return GITHUB_TOKEN_LIFETIME_HOURS_DEFAULT
    if hours < 1:
        logger.warning(
            "GITHUB_TOKEN_LIFETIME_HOURS=%d must be >= 1; falling back to %d",
            hours,
            GITHUB_TOKEN_LIFETIME_HOURS_DEFAULT,
        )
        return GITHUB_TOKEN_LIFETIME_HOURS_DEFAULT
    return hours


def get_ssm_param(name: str, with_decryption: bool = True) -> str:
    resp = _ssm.get_parameter(
        Name=f"{SSM_PATH}/{name}",
        WithDecryption=with_decryption,
    )
    return resp["Parameter"]["Value"]


def get_github_app_token(repo: str) -> str:
    app_id = get_ssm_param("github-app/id", with_decryption=False)
    private_key_b64 = get_ssm_param("github-app/private-key")
    private_key = base64.b64decode(private_key_b64).decode()
    installation_id = get_ssm_param("github-app/installation-id", with_decryption=False)

    now = datetime.now(timezone.utc)
    app_jwt = jwt.encode(
        {
            "iat": now,
            "exp": now + timedelta(minutes=10),
            "iss": app_id,
        },
        private_key,
        algorithm="RS256",
    )

    repo_name = repo.split("/", 1)[-1] if "/" in repo else repo
    lifetime_hours = _get_github_token_lifetime_hours()
    expires_at = now + timedelta(hours=lifetime_hours)
    request_body = {
        "repositories": [repo_name],
        "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    resp = requests.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=request_body,
    )
    if not resp.ok:
        body_snippet = resp.text[:500] if resp.text else ""
        logger.error(
            "GitHub App access_tokens request failed: status=%s app_id=%s "
            "installation_id=%s repo=%s request_body=%s response_body=%s",
            resp.status_code,
            app_id,
            installation_id,
            repo,
            request_body,
            body_snippet,
        )
        resp.raise_for_status()
    return resp.json()["token"]


def verify_github_signature(secret: str, payload: bytes, signature: str) -> bool:
    if not signature.startswith("sha256="):
        return False
    expected = (
        "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature)
