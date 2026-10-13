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

Why lazy-imported provider modules:

    PR #84 made the Alicloud implementations real (KMS + OSS +
    ECS via `oss2` / `alibabacloud_ecs20140526` /
    `alibabacloud_kms20160120` / `alibabacloud_tea_openapi`).
    PR #85 will do the same for Azure. An AWS cold start used
    to eagerly `from provider.alicloud import ...` (and, post-#85,
    `provider.azure`) to surface a typo in `BLITZLOG_PROVIDER` at
    cold start instead of at request time. The cost of that
    eagerness was that the AWS cold start paid the full
    Alicloud + Azure import cost on every cold start, blowing
    the 128 MB Lambda memory budget and OOMing.

    The factory now keeps the "typo surfaces at cold start"
    property via `importlib.util.find_spec(...)` (cheap — does
    not import the body) and lazy-imports the actual module on
    the first `get_*()` call. An AWS cold start no longer pulls
    `oss2` / `alibabacloud_ecs20140526` /
    `alibabacloud_kms20160120` / `alibabacloud_tea_openapi` /
    `provider.azure` into memory.
"""

from __future__ import annotations

import importlib
import importlib.util

from _env import _blitzlog_provider, logger
from provider.aws import AwsCompute, AwsObjectStore, AwsSecretStore
from provider.base import (
    ComputeProvider,
    LaunchSpec,
    ObjectStore,
    SecretStore,
    SpotQuote,
    Tag,
)

# Mapping of provider name → fully-qualified module path. Adding a
# new cloud = add a new entry here (and let the lazy import
# discover the class names via the `<Role>` convention below).
_PROVIDER_MODULES: dict[str, str] = {
    "aws": "provider.aws",
    "alicloud": "provider.alicloud",
    "azure": "provider.azure",
}

# Mapping of provider name → (compute class, secrets class, object-store
# class). The `aws` entry is pre-populated because the AWS classes
# are tiny (boto3-only, already paid at import time) and we want
# the AWS dispatch path to stay branch-free. The `alicloud` and
# `azure` entries are populated on first `get_*()` call.
_PROVIDERS: dict[
    str, tuple[type[ComputeProvider], type[SecretStore], type[ObjectStore]]
] = {
    "aws": (AwsCompute, AwsSecretStore, AwsObjectStore),
}

# Class names inside each provider module. The convention is
# `<Cloud>Compute` / `<Cloud>SecretStore` / `<Cloud>ObjectStore`
# (e.g. `AlicloudCompute`, `AzureSecretStore`). Adding a new cloud
# that follows the convention needs no changes here.
_ROLE_CLASS_NAMES: dict[str, tuple[str, str, str]] = {
    "aws": ("AwsCompute", "AwsSecretStore", "AwsObjectStore"),
    "alicloud": ("AlicloudCompute", "AlicloudSecretStore", "AlicloudObjectStore"),
    "azure": ("AzureCompute", "AzureSecretStore", "AzureObjectStore"),
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

    Cold-start typo check: we call
    `importlib.util.find_spec(...)` against the registered
    module path so a typo in `BLITZLOG_PROVIDER` (or a
    misconfigured Terraform variable that points at a non-
    existent module) surfaces as `KeyError` at cold start,
    matching the pre-#84 behaviour — but without paying the
    import cost of every other provider.

    `importlib.util.find_spec` raises `ModuleNotFoundError`
    when the *parent* package of the requested module is not
    on `sys.path` (e.g. `find_spec("does.not.exist")` raises
    `ModuleNotFoundError: No module named 'does'`). We catch
    that and re-raise as `KeyError` so a misconfigured
    provider is reported through the same exception channel
    the factory used pre-#84.
    """
    name = _blitzlog_provider()
    if name not in _PROVIDER_MODULES:
        raise KeyError(
            f"BLITZLOG_PROVIDER={name!r} is not registered; "
            f"known providers: {sorted(_PROVIDER_MODULES)}"
        )
    module_path = _PROVIDER_MODULES[name]
    try:
        spec = importlib.util.find_spec(module_path)
    except ModuleNotFoundError as exc:
        raise KeyError(
            f"BLITZLOG_PROVIDER={name!r} is registered but "
            f"the module {module_path!r} is not on sys.path "
            f"(parent package missing: {exc})"
        ) from exc
    if spec is None:
        raise KeyError(
            f"BLITZLOG_PROVIDER={name!r} is registered but "
            f"the module {module_path!r} is not on sys.path"
        )
    return name


def _load_provider_classes(
    name: str,
) -> tuple[type[ComputeProvider], type[SecretStore], type[ObjectStore]]:
    """Lazy-load the `(compute, secrets, object_store)` class
    triple for `name` and cache it in `_PROVIDERS`. The module
    is imported exactly once per Lambda container (subsequent
    `get_*()` calls hit the cache). The classes are looked up
    by name via `getattr` so the only thing we need to maintain
    is `_ROLE_CLASS_NAMES` — the module file itself is opaque
    to the factory.
    """
    if name in _PROVIDERS:
        return _PROVIDERS[name]
    module_path = _PROVIDER_MODULES[name]
    module = importlib.import_module(module_path)
    compute_name, secrets_name, object_store_name = _ROLE_CLASS_NAMES[name]
    classes = (
        getattr(module, compute_name),
        getattr(module, secrets_name),
        getattr(module, object_store_name),
    )
    _PROVIDERS[name] = classes
    return classes


def get_compute() -> ComputeProvider:
    """Return the memoised `ComputeProvider` for the configured
    cloud. Cold-start: instantiates once. Warm invocations:
    returns the cached instance.
    """
    global _compute
    if _compute is None:
        name = _resolve_provider_name()
        cls = _load_provider_classes(name)[0]
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
        cls = _load_provider_classes(name)[1]
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
        cls = _load_provider_classes(name)[2]
        logger.info("Initialising ObjectStore: %s", cls.__name__)
        _object_store = cls()
    return _object_store


def reset_provider_singletons() -> None:
    """Drop the memoised singletons + the lazy-imported class
    cache. Tests call this from a fixture to force the next
    `get_*` call to re-initialise against a freshly-patched
    boto3 client (and, in alicloud/azure tests, to drop the
    `provider.alicloud` / `provider.azure` module from the
    `_PROVIDERS` cache so a re-import happens on the next
    call).
    """
    global _compute, _secrets, _object_store
    _compute = None
    _secrets = None
    _object_store = None
    _PROVIDERS.pop("alicloud", None)
    _PROVIDERS.pop("azure", None)


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
