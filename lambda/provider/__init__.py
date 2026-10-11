# ruff: noqa: N999
"""Cloud-provider factory.

Three memoised singletons (`_compute`, `_secrets`, `_object_store`),
keyed on the `BLITZLOG_PROVIDER` env var (default `"aws"`).
Every module that needs a cloud client calls one of the three
`get_*` helpers instead of importing boto3 directly.

Why module-level singletons:

    The Lambda runtime re-uses a warm container for up to ~15
    minutes between invocations. A fresh boto3 client per call
    would re-establish the connection each time. The singleton
    pattern matches what `lambda/auth.py` and `lambda/bot_pool.py`
    did at module import time before #83.

    The trade-off: a unit test that wants a fresh factory must
    reset the singletons. `tests/_fakes.py` exposes a
    `reset_provider_singletons()` helper for that.

Phase 1 of #83: AWS is the only fully-implemented provider.
Alicloud classes exist in `lambda/provider/alicloud.py` but
their methods raise `NotImplementedError` until #84 / #124 land.
The factory still imports them so a typo in the provider name
surfaces as `KeyError` at cold start, not as a half-initialised
factory at request time.
"""

from __future__ import annotations

from _env import _blitzlog_provider, logger
from provider.alicloud import (
    AlicloudCompute,
    AlicloudObjectStore,
    AlicloudSecretStore,
)
from provider.aws import AwsCompute, AwsObjectStore, AwsSecretStore
from provider.base import (
    ComputeProvider,
    LaunchSpec,
    ObjectStore,
    SecretStore,
    SpotQuote,
    Tag,
)

# Mapping of provider name → (compute class, secrets class, object-store class).
# Adding a new cloud = add a new entry here and ship a new module
# alongside `aws.py` / `alicloud.py`.
_PROVIDERS: dict[
    str, tuple[type[ComputeProvider], type[SecretStore], type[ObjectStore]]
] = {
    "aws": (AwsCompute, AwsSecretStore, AwsObjectStore),
    "alicloud": (AlicloudCompute, AlicloudSecretStore, AlicloudObjectStore),
}

_compute: ComputeProvider | None = None
_secrets: SecretStore | None = None
_object_store: ObjectStore | None = None


def _resolve_provider_name() -> str:
    """Return the configured provider name. Reads the env var at
    call time (not import time) so a test that flips
    `BLITZLOG_PROVIDER` between calls exercises the right
    dispatch path. The Lambda runtime sets the var once at
    cold start, so the call-time read is free.
    """
    name = _blitzlog_provider()
    if name not in _PROVIDERS:
        raise KeyError(
            f"BLITZLOG_PROVIDER={name!r} is not registered; "
            f"known providers: {sorted(_PROVIDERS)}"
        )
    return name


def get_compute() -> ComputeProvider:
    """Return the memoised `ComputeProvider` for the configured
    cloud. Cold-start: instantiates once. Warm invocations:
    returns the cached instance.
    """
    global _compute
    if _compute is None:
        name = _resolve_provider_name()
        cls = _PROVIDERS[name][0]
        logger.info("Initialising ComputeProvider: %s", cls.__name__)
        _compute = cls()
    return _compute


def get_secrets() -> SecretStore:
    """Return the memoised `SecretStore` for the configured
    cloud. Used by `lambda/auth.py::get_ssm_param` and
    `lambda/bot_pool.py`.
    """
    global _secrets
    if _secrets is None:
        name = _resolve_provider_name()
        cls = _PROVIDERS[name][1]
        logger.info("Initialising SecretStore: %s", cls.__name__)
        _secrets = cls()
    return _secrets


def get_object_store() -> ObjectStore:
    """Return the memoised `ObjectStore` for the configured
    cloud. Used by `lambda/bot_pool.py` (bot-pool locks) and
    `lambda/ec2.py` (user-data upload).
    """
    global _object_store
    if _object_store is None:
        name = _resolve_provider_name()
        cls = _PROVIDERS[name][2]
        logger.info("Initialising ObjectStore: %s", cls.__name__)
        _object_store = cls()
    return _object_store


def reset_provider_singletons() -> None:
    """Drop the memoised singletons. Tests call this from a
    fixture to force the next `get_*` call to re-initialise
    against a freshly-patched boto3 client.
    """
    global _compute, _secrets, _object_store
    _compute = None
    _secrets = None
    _object_store = None


__all__ = [
    "ComputeProvider",
    "LaunchSpec",
    "ObjectStore",
    "SecretStore",
    "SpotQuote",
    "Tag",
    "get_compute",
    "get_object_store",
    "get_secrets",
    "reset_provider_singletons",
]
