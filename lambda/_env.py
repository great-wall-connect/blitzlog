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


def _blitzlog_provider() -> str:
    """Cloud provider key. Set by Terraform as a Lambda env var
    (`infra/modules/core/lambda.tf`). Drives the dispatch in
    `lambda/provider/__init__.py`.

    Supported values:

      * `"aws"` — the production provider. Defaults to this so
        tests and out-of-Lambda callers continue to work without
        the env var being explicitly set.
      * `"alicloud"` — Phase 2 (#84) ships full bodies for the
        Alicloud-side `SecretStore` / `ObjectStore` and the ECS
        preemptible `Compute` so `BLITZLOG_PROVIDER=alicloud` is
        a fully working deploy on Alicloud.
      * `"azure"` — Phase 3 ships the Azure implementations; for
        now the factory hands back stubs that raise
        `NotImplementedError` (issue #84).

    The check is lower-cased + empty-string-falls-back-to-aws so
    a stray `" "` or `""` value (e.g. from a half-applied
    Terraform) doesn't disable the Lambda entirely.
    """
    return os.environ.get("BLITZLOG_PROVIDER", "aws").strip().lower() or "aws"


# ---------------------------------------------------------------------------
# Alicloud env vars (Phase 2 / #84).
#
# Only the four below are read. Azure-specific env vars land in Phase 3
# alongside the Azure SDK implementations. The `aws_*` env vars are read
# directly by boto3 and never touch this module.
# ---------------------------------------------------------------------------


def _aliyun_region() -> str:
    """Aliyun region id, e.g. `"cn-hangzhou"`. Required when
    `BLITZLOG_PROVIDER=alicloud`; missing otherwise the SDK client
    fails at first invocation (caught and surfaced as a 500).
    """
    return os.environ.get("ALIYUN_REGION", "").strip()


def _aliyun_access_key_id() -> str:
    """Aliyun AccessKey ID. In production this would come from
    the ECS instance RAM role (no static key on the box); the
    Lambda-side ALI access keys are a stop-gap until Terraform
    ships the InstanceRole assumption path (Phase 4+).
    """
    return os.environ.get("ALIYUN_ACCESS_KEY_ID", "").strip()


def _aliyun_access_key_secret() -> str:
    """Aliyun AccessKey secret. Pair to `_aliyun_access_key_id`."""
    return os.environ.get("ALIYUN_ACCESS_KEY_SECRET", "").strip()


def _aliyun_kms_key_id() -> str:
    """Customer-managed CMK id used to encrypt secrets at rest in
    KMS. Optional: when unset the secrets are encrypted with the
    service default key. `AwsSecretStore` reads the equivalent
    from the SSM parameter's encryption flag, so this is the
    Alicloud-side knob to keep parity with the AWS SSM
    `KMSKeyID` parameter.
    """
    return os.environ.get("ALIYUN_KMS_KEY_ID", "").strip()


def _ssm_root() -> str:
    return f"/blitzlog/{_blitzlog_env()}"


# Environment name passed in by Terraform as a Lambda env var
# (infra/modules/core/lambda.tf). Used to namespace all per-env SSM
# parameters under /blitzlog/<env>/... so prod and dev can coexist in
# the same AWS account without collision.
# Defaults to "prod" so tests / out-of-Lambda callers continue to work
# without the BLITZLOG_ENV env var being explicitly set.
BLITZLOG_ENV = _blitzlog_env()
BLITZLOG_PROVIDER = _blitzlog_provider()
SSM_PATH = _ssm_root()


def ephemeral_token_ssm_param(repo: str, issue: int) -> str:
    """Per-(env, owner, repo, issue) SSM path for the GitHub App
    installation token the watchdog reads.

    Was historically ``{SSM_PATH}/ephemeral/github-token-{issue}``,
    which collided between two issues numbered the same in different
    repos (e.g. blitzlog-example#2 and blitzlog-foo#2 would both write
    to ``/blitzlog/dev/ephemeral/github-token-2``). The new
    hierarchical namespace scopes by (owner, repo, issue) so
    concurrent dev sessions in different repos don't trample each
    other's tokens.

    ``repo`` is the full name (``great-wall-connect/blitzlog-example``).
    Returns ``{SSM_PATH}/ephemeral/gh/{owner}/{repo}/{issue}``.
    """
    owner, _, name = repo.partition("/")
    if not name:
        raise ValueError(f"repo must be in 'owner/name' form; got {repo!r}")
    return f"{SSM_PATH}/ephemeral/gh/{owner}/{name}/{issue}"


# Alicloud configuration (Phase 2 / #84). The four env vars
# below are read on demand by `provider/alicloud.py` via the
# `_aliyun_region()` / `_aliyun_access_key_id()` /
# `_aliyun_access_key_secret()` / `_aliyun_kms_key_id()` shims
# defined above. Mirrors the call-time read of
# `_blitzlog_provider` so a test can flip the env vars between
# calls without re-importing the module.

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
