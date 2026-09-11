"""GitHub App authentication + webhook signature verification.

Two responsibilities:

    verify_github_signature — constant-time HMAC-SHA256 comparison of the
    `X-Hub-Signature-256` header against the per-repo webhook secret stored
    in SSM.

    get_github_app_token — mints a short-lived (1 h) installation token
    scoped to a single repository. Builds a GitHub App JWT with the App's
    private key, then POSTs to `/app/installations/<id>/access_tokens`
    with `{"repositories": [<repo_name>]}` so the issued token has only
    the permissions the worker needs.
"""

import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import boto3
import jwt
import requests
from botocore.config import Config

from . import SSM_PATH, logger

_ssm = boto3.client("ssm", config=Config(retries={"max_attempts": 1}))


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
    request_body = {"repositories": [repo_name]}

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
