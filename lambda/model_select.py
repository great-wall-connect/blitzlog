"""Per-run model + credential resolution (issue #165).

Three levels of model selection, resolved per run, the higher
superseding the lower when it is set:

    task label  ->  per-user default  ->  org default

The credential resolves on its own axis and travels with the user:

    per-user credential  ->  org credential

Two independent axes, one binding rule:

  * A **model** is what the agent reasons with.
  * A **credential** is what authenticates the call.
  * A credential is used ONLY with a model whose provider matches
    the credential's provider. On a mismatch the run fails fast
    with an explicit message — the agent must never send a key to
    a provider it does not belong to.

A task label selects a **model**, never a secret: labels are
visible to everyone with repo read access, so they cannot carry
credentials.

Levels
------

**Level 1 — per task: the `model:<value>` issue label.**

`<value>` resolves in two steps:

  1. `<value>` matches an **alias** in the operator's profile
     allowlist (`OPENCODE_MODEL_PROFILES`) -> that profile is used.
  2. Otherwise `<value>` parses as `<provider>/<model>` AND
     `<provider>` is on the operator's **approved-provider list**
     (`OPENCODE_ALLOWED_PROVIDERS`) -> used raw.

Anything else: the label is ignored, resolution falls through to
the next level, and the rejected value is logged by name.

Raw strings are restricted to the approved-provider list precisely
because the run's credential rides along with the model — a
labeler must not be able to point a run at an arbitrary endpoint.

**Level 2 — per user.** The buyer's own default, read from
`/blitzlog/users/<login>/opencode/{model,base-url}` by
`bot_pool.get_opencode_profile`.

**Level 3 — per org.** The deployment default (`var.opencode_model`),
unchanged.

The local LLM keeps its current top precedence (`local_llm["model"]`
wins) — existing local-LLM users see no behaviour change; the
chain in this module applies only when there is no local-LLM
config, so the caller short-circuits before calling
`resolve_selection`.

Org-key reachability
--------------------

An operator-registered **alias** may run on the org credential: the
operator chose what the alias points at. A **raw** `provider/model`
label may NOT: the raw string is chosen by whoever can label an
issue, so the org's shared key must never be reachable from it. A
raw label therefore requires a per-user credential, and fails fast
with an actionable message when none is configured.

Security notes
--------------

* The label value is untrusted input that ends up inside a bash
  script (the EC2 user-data). `is_valid_model` /
  `is_valid_base_url` are strict allowlists that reject anything
  with shell- or JSON-significant characters. A value that fails
  them is never rendered.
* The credential **path** is always server-derived, never
  user-supplied text, so there is no injection surface and the
  Lambda never holds key material — it resolves a path and the
  host resolves the key.
"""

import json
import os
import re
from dataclasses import dataclass, field

from _env import logger

# The label prefix that selects a model for a single task.
MODEL_LABEL_PREFIX = "model:"

# Source markers on a `Selection`, for logging + tests.
SOURCE_LABEL_ALIAS = "label-alias"
SOURCE_LABEL_RAW = "label-raw"
SOURCE_USER = "user"
SOURCE_ORG = "org"

# Strict allowlist for a model string. The value is interpolated
# into the EC2 user-data (`OPENCODE_MODEL="..."`) and into the
# container's opencode.json, so it must not be able to carry shell
# metacharacters, quotes, or backslashes.
#
# Shape: `<provider>/<model>`. The provider segment is
# `[A-Za-z0-9._-]+` (opencode's provider ids are lowercase
# alphanumerics with dashes). The model segment additionally
# allows `.`, `:`, and `/` — model ids legitimately contain them
# (`qwen2.5-coder:32b`, `qwen/qwen3.8-27b`).
_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._:/-]+$")

# Strict allowlist for a base URL. The value is interpolated into
# the EC2 user-data (`OPENCODE_BASE_URL="..."`) and into the
# container's opencode.json, so it must not contain `"`, `'`,
# backtick, `$`, `\`, whitespace, or any other shell-significant
# character. Query strings are allowed with a restricted charset
# because some gateways require them.
_BASE_URL_RE = re.compile(
    r"^https?://[A-Za-z0-9._-]+(?::[0-9]{1,5})?"
    r"(?:/[A-Za-z0-9._~%/-]*)?"
    r"(?:\?[A-Za-z0-9._~%&=+-]*)?$"
)


class ModelSelectionError(Exception):
    """Raised when the requested model + credential combination
    cannot be honoured safely.

    The message is operator-facing: it names the rejected value,
    the two providers, and the knob to fix. The webhook handler
    turns this into a 422 so no instance is launched.
    """


@dataclass(frozen=True)
class ModelProfile:
    """An operator-registered profile, selectable per task via the
    `model:<alias>` issue label.

    `model` is the opencode model string (`provider/model`).
    `provider` is optional and overrides the provider derived from
    `model` — used when the profile names a model id without a
    provider prefix. `base_url` is optional: it is how an org
    registers a buyer's self-hosted endpoint WITHOUT the label
    being able to.
    """

    alias: str
    model: str
    base_url: str = ""
    provider: str = ""


@dataclass(frozen=True)
class UserOpencodeProfile:
    """The per-user default, read from
    `/blitzlog/users/<login>/opencode/*`.

    `has_api_key` is a boolean rather than the key itself: the key
    material must not cross back into the resolution layer, where
    it could end up in a log line or a rendered script.
    """

    model: str = ""
    has_api_key: bool = False
    provider: str = ""
    base_url: str = ""


@dataclass(frozen=True)
class Selection:
    """The resolved model + credential for one run.

    `credential_path` is a server-derived secret-store path (never
    key material, never user-supplied text). `credential_provider`
    is the provider the credential belongs to — the binding rule
    compares it against the selected model's provider.
    """

    model: str
    provider: str
    base_url: str
    credential_path: str
    credential_provider: str
    source: str
    warnings: tuple[str, ...] = field(default=())


def provider_of(model: str) -> str:
    """Return the provider segment of a `provider/model` string.

    opencode parses the FIRST slash as the separator, so the
    provider is everything before it. An empty string is returned
    for a model with no slash (no provider) — the caller decides
    whether that is acceptable.
    """
    if "/" not in model:
        return ""
    return model.split("/", 1)[0]


def is_valid_model(model: str) -> bool:
    """True when `model` is safe to render into the user-data and
    the container's opencode.json. See `_MODEL_RE`.
    """
    return bool(model) and bool(_MODEL_RE.match(model))


def is_valid_base_url(url: str) -> bool:
    """True when `url` is safe to render into the user-data and the
    container's opencode.json. See `_BASE_URL_RE`.
    """
    return bool(url) and bool(_BASE_URL_RE.match(url))


def parse_model_labels(labels) -> list[str]:
    """Return the `model:` label values, sorted alphabetically.

    Sorting makes the pick deterministic when an issue carries
    several `model:` labels: the alphabetically-first value that
    RESOLVES wins (a value that does not resolve is skipped and
    logged, not silently honoured). Sorting is over the raw values
    (after the prefix), which is the same order as sorting the full
    labels — the prefix is common.
    """
    values = []
    for label in labels or ():
        if not isinstance(label, str) or not label.startswith(MODEL_LABEL_PREFIX):
            continue
        value = label[len(MODEL_LABEL_PREFIX) :].strip()
        if value:
            values.append(value)
    return sorted(set(values))


def load_model_profiles(raw: str | None = None) -> dict[str, ModelProfile]:
    """Parse the `OPENCODE_MODEL_PROFILES` env var into profiles.

    Shape (set by Terraform from `var.opencode_model_profiles`):

        {"fast": {"model": "anthropic/claude-haiku",
                  "base_url": "https://llm.internal/v1",
                  "provider": "anthropic"}}

    A malformed value degrades to an empty map with a loud error —
    a bad profile map must not break the webhook; the org default
    model still works.
    """
    if raw is None:
        raw = os.environ.get("OPENCODE_MODEL_PROFILES", "")
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.error(
            "OPENCODE_MODEL_PROFILES is not valid JSON (%s); "
            "per-task model aliases are disabled for this run",
            e,
        )
        return {}
    if not isinstance(parsed, dict):
        logger.error(
            "OPENCODE_MODEL_PROFILES must be a JSON object of alias -> profile; "
            "got %s; per-task model aliases are disabled for this run",
            type(parsed).__name__,
        )
        return {}

    profiles: dict[str, ModelProfile] = {}
    for alias, spec in parsed.items():
        if not isinstance(alias, str) or not alias:
            continue
        if not isinstance(spec, dict):
            logger.error(
                "OPENCODE_MODEL_PROFILES[%r] is not an object; alias ignored", alias
            )
            continue
        model = str(spec.get("model") or "").strip()
        if not model:
            logger.error(
                "OPENCODE_MODEL_PROFILES[%r] has no 'model'; alias ignored", alias
            )
            continue
        profile_provider = str(spec.get("provider") or "").strip()
        if not is_valid_model(model) and not (profile_provider and "/" not in model):
            # A profile whose model cannot be rendered safely into the
            # EC2 user-data is skipped rather than failing the run:
            # the label falls through to the next level, with a
            # warning naming the rejected value.
            logger.error(
                "OPENCODE_MODEL_PROFILES[%r] has an invalid model %r; alias ignored",
                alias,
                model,
            )
            continue
        provider = str(spec.get("provider") or "").strip()
        base_url = str(spec.get("base_url") or "").strip()
        if base_url and not is_valid_base_url(base_url):
            logger.error(
                "OPENCODE_MODEL_PROFILES[%r] has an invalid base_url; alias ignored",
                alias,
            )
            continue
        profiles[alias] = ModelProfile(
            alias=alias,
            model=model,
            base_url=base_url,
            provider=provider,
        )
    return profiles


def load_allowed_providers(raw: str | None = None) -> list[str]:
    """Parse the `OPENCODE_ALLOWED_PROVIDERS` env var into a list of
    provider names a raw `model:<provider>/<model>` label may name.

    A malformed value degrades to an empty list with a loud error —
    which means raw labels are rejected (the fail-closed direction).
    """
    if raw is None:
        raw = os.environ.get("OPENCODE_ALLOWED_PROVIDERS", "")
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.error(
            "OPENCODE_ALLOWED_PROVIDERS is not valid JSON (%s); "
            "raw per-task model labels are disabled for this run",
            e,
        )
        return []
    if not isinstance(parsed, list):
        logger.error(
            "OPENCODE_ALLOWED_PROVIDERS must be a JSON array of provider names; "
            "got %s; raw per-task model labels are disabled for this run",
            type(parsed).__name__,
        )
        return []
    return [p.strip() for p in parsed if isinstance(p, str) and p.strip()]


def _normalize(model: str, provider_override: str = "") -> tuple[str, str]:
    """Return `(model, provider)` for a candidate model string.

    A profile may name a model id without a provider prefix and
    declare the provider separately; in that case the rendered
    model gets the provider prefix so opencode can resolve it.
    """
    provider = (provider_override or provider_of(model)).strip()
    if provider and "/" not in model:
        model = f"{provider}/{model}"
    return model, provider


def resolve_selection(
    *,
    labels=None,
    org_model: str,
    org_credential_provider: str = "",
    org_credential_path: str,
    user_profile: UserOpencodeProfile | None = None,
    user_credential_path: str = "",
    profiles: dict[str, ModelProfile] | None = None,
    allowed_providers: list[str] | None = None,
    user_login: str = "",
) -> Selection:
    """Resolve the model + credential for one run.

    Raises `ModelSelectionError` when the combination cannot be
    honoured safely — the caller must fail the run fast rather than
    fall back to a different provider or credential.
    """
    profiles = profiles or {}
    allowed = list(allowed_providers or [])
    user = user_profile or UserOpencodeProfile()

    warnings: list[str] = []

    model = ""
    provider = ""
    base_url = ""
    source = ""
    raw_label_value = ""
    needs_user_credential = False

    candidates = parse_model_labels(labels)
    if len(candidates) > 1:
        warnings.append(
            f"issue carries {len(candidates)} model: labels "
            f"({', '.join(candidates)}); picking the first that resolves"
        )

    for value in candidates:
        profile = profiles.get(value)
        if profile is not None:
            model, provider = _normalize(profile.model, profile.provider)
            base_url = profile.base_url
            source = SOURCE_LABEL_ALIAS
            break
        if not is_valid_model(value):
            warnings.append(
                f"model label {value!r} rejected: not a valid '<provider>/<model>' value"
            )
            logger.warning(
                "model label %r rejected: not a valid '<provider>/<model>' value", value
            )
            continue
        raw_provider = provider_of(value)
        if raw_provider not in allowed:
            warnings.append(
                f"model label {value!r} rejected: provider {raw_provider!r} is not "
                f"on the approved-provider list"
            )
            logger.warning(
                "model label %r rejected: provider %r is not on the "
                "approved-provider list",
                value,
                raw_provider,
            )
            continue
        model, provider = value, raw_provider
        base_url = ""
        source = SOURCE_LABEL_RAW
        raw_label_value = value
        needs_user_credential = True
        break

    if not model:
        if user.model:
            # The user's model is `provider/model`, like the org's.
            # The user's `provider` field declares the provider their
            # CREDENTIAL belongs to — it is not a model-provider
            # override, so it is deliberately not passed here.
            model, provider = _normalize(user.model)
            base_url = user.base_url
            source = SOURCE_USER
        else:
            model, provider = _normalize(org_model)
            base_url = ""
            source = SOURCE_ORG

    if not model:
        raise ModelSelectionError(
            "No model could be resolved: the org default model is empty and no "
            "per-task label or per-user default was supplied."
        )

    # The model string is interpolated into the EC2 user-data bash
    # script (`OPENCODE_MODEL="..."`) and into the container's
    # opencode.json, so EVERY source is validated — not just the raw
    # label. A per-user model comes from the buyer's own Terraform and
    # an org model from the operator's: a typo in either must fail
    # loudly rather than land in a shell script unvalidated.
    if not is_valid_model(model):
        raise ModelSelectionError(
            f"The selected model {model!r} is not a valid '<provider>/<model>' "
            f"value and was rejected. Models must look like "
            f"'provider/model' (e.g. 'anthropic/claude-sonnet-4')."
        )

    if needs_user_credential and not user.has_api_key:
        who = f" for {user_login}" if user_login else ""
        raise ModelSelectionError(
            f"model label {raw_label_value!r} selects a raw '<provider>/<model>' "
            f"value, which requires a per-user credential — the org credential is "
            f"never reachable from a label{who}. Configure 'opencode_api_key' (and "
            f"'opencode_api_key_provider') in your user-pool, or use an "
            f"operator-registered alias instead."
        )

    # Credential: the per-user credential supersedes the org one.
    if user.has_api_key:
        credential_path = user_credential_path
        credential_provider = (user.provider or "").strip()
    else:
        credential_path = org_credential_path
        credential_provider = (org_credential_provider or "").strip() or provider_of(
            org_model
        )

    if not credential_provider:
        who = f" for {user_login}" if user_login else ""
        raise ModelSelectionError(
            f"The selected credential{who} has no declared provider, so it cannot "
            f"be bound to the selected model {model!r}. Set "
            f"'opencode_api_key_provider' in your user-pool (or "
            f"'opencode_api_key_provider' in the org Terraform)."
        )

    if credential_provider != provider:
        raise ModelSelectionError(
            f"Credential/provider mismatch: the selected model {model!r} is served "
            f"by provider {provider!r}, but the available credential belongs to "
            f"provider {credential_provider!r}. A credential is only ever used "
            f"with a model whose provider matches. Either select a model on "
            f"provider {credential_provider!r}, or configure a credential for "
            f"provider {provider!r} (a per-user 'opencode_api_key' + "
            f"'opencode_api_key_provider', or a per-profile org key)."
        )

    if base_url and not is_valid_base_url(base_url):
        # Defensive: the base URL comes from Terraform (trusted), but
        # it is interpolated into a bash script and a JSON document.
        raise ModelSelectionError(
            f"The selected base URL for model {model!r} is not a valid "
            f"http(s) URL and was rejected."
        )

    return Selection(
        model=model,
        provider=provider,
        base_url=base_url,
        credential_path=credential_path,
        credential_provider=credential_provider,
        source=source,
        warnings=tuple(warnings),
    )


__all__ = [
    "MODEL_LABEL_PREFIX",
    "SOURCE_LABEL_ALIAS",
    "SOURCE_LABEL_RAW",
    "SOURCE_ORG",
    "SOURCE_USER",
    "ModelProfile",
    "ModelSelectionError",
    "Selection",
    "UserOpencodeProfile",
    "is_valid_base_url",
    "is_valid_model",
    "load_allowed_providers",
    "load_model_profiles",
    "parse_model_labels",
    "provider_of",
    "resolve_selection",
]
