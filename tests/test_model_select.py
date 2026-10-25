"""Per-run model + credential resolution (issue #165).

Covers the three-level precedence (task label > per-user default >
org default), the two-step label resolution (alias, then raw
`provider/model` on the approved-provider list), the binding rule
(credential provider must equal model provider), and the strict
validators that stop a label value from carrying shell/JSON
metacharacters into the EC2 user-data.
"""

import unittest
from unittest.mock import patch

from model_select import (
    SOURCE_LABEL_ALIAS,
    SOURCE_LABEL_RAW,
    SOURCE_ORG,
    SOURCE_USER,
    ModelProfile,
    ModelSelectionError,
    UserOpencodeProfile,
    is_valid_base_url,
    is_valid_model,
    load_allowed_providers,
    load_model_profiles,
    parse_model_labels,
    provider_of,
    resolve_selection,
)

ORG_MODEL = "minimax-coding-plan/MiniMax-M3"
ORG_KEY_PATH = "/blitzlog/prod/opencode/api-key"
USER_KEY_PATH = "/blitzlog/users/octocat/opencode/api-key"


def _resolve(**overrides):
    """Call `resolve_selection` with sane defaults for the knobs a
    test does not care about."""
    kwargs = {
        "labels": set(),
        "org_model": ORG_MODEL,
        "org_credential_provider": "minimax-coding-plan",
        "org_credential_path": ORG_KEY_PATH,
        "user_profile": None,
        "user_credential_path": USER_KEY_PATH,
        "profiles": {},
        "allowed_providers": [],
    }
    kwargs.update(overrides)
    return resolve_selection(**kwargs)


class TestParseModelLabels(unittest.TestCase):
    def test_extracts_and_sorts(self):
        labels = {"model:zeta", "model:alpha", "autonomous", "enhancement"}
        self.assertEqual(parse_model_labels(labels), ["alpha", "zeta"])

    def test_ignores_non_model_labels(self):
        self.assertEqual(parse_model_labels({"autonomous", "assisted"}), [])

    def test_ignores_empty_value(self):
        self.assertEqual(parse_model_labels({"model:"}), [])
        self.assertEqual(parse_model_labels({"model:   "}), [])

    def test_dedupes(self):
        self.assertEqual(parse_model_labels(["model:a", "model:a"]), ["a"])

    def test_handles_none(self):
        self.assertEqual(parse_model_labels(None), [])


class TestProviderOf(unittest.TestCase):
    def test_splits_on_first_slash(self):
        self.assertEqual(provider_of("qwen/qwen3.8-27b"), "qwen")
        self.assertEqual(provider_of("openai/gpt-4o"), "openai")

    def test_no_slash_returns_empty(self):
        self.assertEqual(provider_of("gpt-4o"), "")


class TestValidators(unittest.TestCase):
    """The validators are a security boundary: the label value ends
    up inside a bash script and a JSON document."""

    def test_accepts_valid_models(self):
        for model in (
            "openai/gpt-4o",
            "minimax-coding-plan/MiniMax-M3",
            "qwen/qwen3.8-27b",
            "ollama/qwen2.5-coder:32b",
        ):
            self.assertTrue(is_valid_model(model), model)

    def test_rejects_shell_metacharacters(self):
        for model in (
            'evil/x"; rm -rf / #',
            "evil/$(id)",
            "evil/`id`",
            "evil/x\\n",
            "evil/x y",
            "evil/x'y",
            "evil/",
            "/x",
            "noslash",
            "",
        ):
            self.assertFalse(is_valid_model(model), model)

    def test_accepts_valid_base_urls(self):
        for url in (
            "https://api.openai.com/v1",
            "http://100.x.y.z:11434/v1",
            "https://llm.internal:8443",
            "https://gw.example.com/v1?tenant=acme",
        ):
            self.assertTrue(is_valid_base_url(url), url)

    def test_rejects_shell_metacharacters_in_base_urls(self):
        for url in (
            'https://x/v1"; rm -rf / #',
            "https://x/$(id)",
            "https://x/`id`",
            "https://x/a b",
            "https://x/a'b",
            "https://x/a\\b",
            "ftp://x/v1",
            "https://",
            "",
        ):
            self.assertFalse(is_valid_base_url(url), url)


class TestProfileLoading(unittest.TestCase):
    def test_parses_valid_json(self):
        raw = (
            '{"fast": {"model": "anthropic/claude-haiku"}, '
            '"proxy": {"model": "m", "base_url": "https://llm.internal/v1", '
            '"provider": "anthropic"}}'
        )
        profiles = load_model_profiles(raw)
        self.assertEqual(set(profiles), {"fast", "proxy"})
        self.assertEqual(profiles["fast"].model, "anthropic/claude-haiku")
        self.assertEqual(profiles["proxy"].base_url, "https://llm.internal/v1")
        self.assertEqual(profiles["proxy"].provider, "anthropic")

    def test_malformed_json_degrades_to_empty(self):
        with patch("model_select.logger") as log:
            self.assertEqual(load_model_profiles("{not json"), {})
            log.error.assert_called()

    def test_non_object_degrades_to_empty(self):
        with patch("model_select.logger") as log:
            self.assertEqual(load_model_profiles('["a"]'), {})
            log.error.assert_called()

    def test_profile_without_model_is_skipped(self):
        with patch("model_select.logger") as log:
            profiles = load_model_profiles('{"a": {"base_url": "https://x/v1"}}')
            self.assertEqual(profiles, {})
            log.error.assert_called()

    def test_profile_with_invalid_base_url_is_skipped(self):
        with patch("model_select.logger") as log:
            profiles = load_model_profiles('{"a": {"model": "p/m", "base_url": "x y"}}')
            self.assertEqual(profiles, {})
            log.error.assert_called()

    def test_empty_env_var(self):
        self.assertEqual(load_model_profiles(""), {})

    def test_allowed_providers_parsing(self):
        self.assertEqual(
            load_allowed_providers('["openai", " anthropic "]'),
            [
                "openai",
                "anthropic",
            ],
        )

    def test_allowed_providers_malformed_fails_closed(self):
        with patch("model_select.logger") as log:
            self.assertEqual(load_allowed_providers("{oops"), [])
            log.error.assert_called()

    def test_allowed_providers_non_list_fails_closed(self):
        with patch("model_select.logger") as log:
            self.assertEqual(load_allowed_providers('{"a": 1}'), [])
            log.error.assert_called()


class TestPrecedence(unittest.TestCase):
    def test_org_default_when_nothing_configured(self):
        sel = _resolve()
        self.assertEqual(sel.model, ORG_MODEL)
        self.assertEqual(sel.provider, "minimax-coding-plan")
        self.assertEqual(sel.source, SOURCE_ORG)
        self.assertEqual(sel.credential_path, ORG_KEY_PATH)

    def test_user_default_beats_org_default(self):
        user = UserOpencodeProfile(
            model="anthropic/claude-sonnet-4",
            has_api_key=True,
            provider="anthropic",
        )
        sel = _resolve(user_profile=user)
        self.assertEqual(sel.model, "anthropic/claude-sonnet-4")
        self.assertEqual(sel.source, SOURCE_USER)
        self.assertEqual(sel.credential_path, USER_KEY_PATH)

    def test_label_alias_beats_user_default(self):
        user = UserOpencodeProfile(
            model="anthropic/claude-sonnet-4",
            has_api_key=True,
            provider="anthropic",
        )
        profiles = {
            "cheap": ModelProfile(alias="cheap", model="anthropic/claude-haiku")
        }
        sel = _resolve(labels={"model:cheap"}, user_profile=user, profiles=profiles)
        self.assertEqual(sel.model, "anthropic/claude-haiku")
        self.assertEqual(sel.source, SOURCE_LABEL_ALIAS)

    def test_user_base_url_travels_with_the_user_level(self):
        user = UserOpencodeProfile(
            model="acme/llama",
            has_api_key=True,
            provider="acme",
            base_url="https://llm.internal/v1",
        )
        sel = _resolve(user_profile=user)
        self.assertEqual(sel.base_url, "https://llm.internal/v1")

    def test_org_level_has_no_base_url(self):
        self.assertEqual(_resolve().base_url, "")


class TestLabelResolution(unittest.TestCase):
    def test_alias_resolves_to_the_profile(self):
        profiles = {
            "cheap": ModelProfile(
                alias="cheap",
                model="minimax-coding-plan/MiniMax-M2",
                base_url="https://llm.internal/v1",
                provider="minimax-coding-plan",
            )
        }
        sel = _resolve(labels={"model:cheap"}, profiles=profiles)
        self.assertEqual(sel.model, "minimax-coding-plan/MiniMax-M2")
        self.assertEqual(sel.base_url, "https://llm.internal/v1")
        self.assertEqual(sel.source, SOURCE_LABEL_ALIAS)

    def test_profile_provider_overrides_the_model_prefix(self):
        profiles = {
            "proxied": ModelProfile(
                alias="proxied",
                model="MiniMax-M2",
                provider="minimax-coding-plan",
            )
        }
        sel = _resolve(labels={"model:proxied"}, profiles=profiles)
        self.assertEqual(sel.model, "minimax-coding-plan/MiniMax-M2")
        self.assertEqual(sel.provider, "minimax-coding-plan")

    def test_raw_label_on_approved_provider(self):
        user = UserOpencodeProfile(
            model="minimax-coding-plan/MiniMax-M3",
            has_api_key=True,
            provider="minimax-coding-plan",
        )
        sel = _resolve(
            labels={"model:minimax-coding-plan/MiniMax-M2"},
            user_profile=user,
            allowed_providers=["minimax-coding-plan"],
        )
        self.assertEqual(sel.model, "minimax-coding-plan/MiniMax-M2")
        self.assertEqual(sel.source, SOURCE_LABEL_RAW)

    def test_raw_label_on_disapproved_provider_falls_through(self):
        user = UserOpencodeProfile(
            model="anthropic/claude-sonnet-4",
            has_api_key=True,
            provider="anthropic",
        )
        with patch("model_select.logger") as log:
            sel = _resolve(
                labels={"model:evil/model"},
                user_profile=user,
                allowed_providers=["anthropic"],
            )
            self.assertEqual(sel.model, "anthropic/claude-sonnet-4")
            self.assertEqual(sel.source, SOURCE_USER)
            # The rejected value is named in the warning.
            self.assertTrue(any("evil/model" in w for w in sel.warnings), sel.warnings)
            self.assertTrue(
                any("evil/model" in str(c) for c in log.warning.call_args_list)
            )

    def test_unknown_alias_falls_through_to_org(self):
        with patch("model_select.logger") as log:
            sel = _resolve(labels={"model:nosuchalias"})
            self.assertEqual(sel.model, ORG_MODEL)
            self.assertEqual(sel.source, SOURCE_ORG)
            self.assertTrue(any("nosuchalias" in w for w in sel.warnings), sel.warnings)
            self.assertTrue(
                any("nosuchalias" in str(c) for c in log.warning.call_args_list)
            )

    def test_label_with_shell_metacharacters_is_rejected(self):
        """Regression: a label value is untrusted input that lands in
        the EC2 user-data bash script."""
        with patch("model_select.logger") as log:
            sel = _resolve(labels={'model:evil/x"; rm -rf / #'})
            self.assertEqual(sel.model, ORG_MODEL)
            self.assertTrue(any("evil/x" in w for w in sel.warnings), sel.warnings)
            self.assertTrue(any("evil/x" in str(c) for c in log.warning.call_args_list))

    def test_multiple_labels_pick_the_first_that_resolves(self):
        profiles = {
            "beta": ModelProfile(alias="beta", model="minimax-coding-plan/MiniMax-M2")
        }
        user = UserOpencodeProfile(
            model="minimax-coding-plan/MiniMax-M3",
            has_api_key=True,
            provider="minimax-coding-plan",
        )
        sel = _resolve(
            labels={"model:alpha", "model:beta", "model:gamma"},
            user_profile=user,
            profiles=profiles,
            allowed_providers=["minimax-coding-plan"],
        )
        # "alpha" is alphabetically first but does not resolve; "beta"
        # is an alias and wins over the user default.
        self.assertEqual(sel.model, "minimax-coding-plan/MiniMax-M2")
        self.assertEqual(sel.source, SOURCE_LABEL_ALIAS)
        self.assertTrue(any("model: labels" in w for w in sel.warnings), sel.warnings)

    def test_multiple_labels_are_deterministic(self):
        profiles = {
            "alpha": ModelProfile(alias="alpha", model="minimax-coding-plan/M2"),
            "beta": ModelProfile(alias="beta", model="minimax-coding-plan/M3"),
        }
        sel = _resolve(labels={"model:beta", "model:alpha"}, profiles=profiles)
        self.assertEqual(sel.model, "minimax-coding-plan/M2")


class TestBindingRule(unittest.TestCase):
    def test_user_credential_must_match_the_model_provider(self):
        """The user's credential provider and the selected model's
        provider must agree."""
        user = UserOpencodeProfile(
            model="anthropic/claude-sonnet-4",
            has_api_key=True,
            provider="anthropic",
        )
        sel = _resolve(user_profile=user)
        self.assertEqual(sel.model, "anthropic/claude-sonnet-4")
        self.assertEqual(sel.credential_provider, "anthropic")

        with self.assertRaises(ModelSelectionError) as ctx:
            _resolve(
                user_profile=UserOpencodeProfile(
                    model="anthropic/claude-sonnet-4",
                    has_api_key=True,
                    provider="openai",
                ),
                user_login="octocat",
            )
        self.assertIn("mismatch", str(ctx.exception))

    def test_mismatch_between_user_key_and_org_model_fails_fast(self):
        """A user with an Anthropic key and no model of their own
        runs the ORG default model — which is on another provider.
        The run must fail fast rather than send the key to the
        wrong provider."""
        user = UserOpencodeProfile(has_api_key=True, provider="anthropic")
        with self.assertRaises(ModelSelectionError) as ctx:
            _resolve(user_profile=user, user_login="octocat")
        self.assertIn("mismatch", str(ctx.exception))
        self.assertIn("anthropic", str(ctx.exception))
        self.assertIn("minimax-coding-plan", str(ctx.exception))

    def test_user_key_with_org_default_model_when_providers_match(self):
        """The purest BYOK story: the user's own key, the org's
        default model, providers match."""
        user = UserOpencodeProfile(has_api_key=True, provider="minimax-coding-plan")
        sel = _resolve(user_profile=user)
        self.assertEqual(sel.model, ORG_MODEL)
        self.assertEqual(sel.credential_path, USER_KEY_PATH)
        self.assertEqual(sel.credential_provider, "minimax-coding-plan")

    def test_alias_may_use_the_org_credential(self):
        """An operator-registered alias is the operator's own
        choice, so it may run on the org credential."""
        profiles = {
            "cheap": ModelProfile(alias="cheap", model="minimax-coding-plan/MiniMax-M2")
        }
        sel = _resolve(labels={"model:cheap"}, profiles=profiles)
        self.assertEqual(sel.credential_path, ORG_KEY_PATH)
        self.assertEqual(sel.credential_provider, "minimax-coding-plan")

    def test_alias_on_another_provider_needs_a_matching_credential(self):
        """The binding rule still applies to an alias: an alias on a
        provider the org key does not belong to needs a per-user
        credential for that provider."""
        profiles = {
            "other": ModelProfile(alias="other", model="anthropic/claude-haiku")
        }
        with self.assertRaises(ModelSelectionError) as ctx:
            _resolve(labels={"model:other"}, profiles=profiles)
        self.assertIn("mismatch", str(ctx.exception))

        user = UserOpencodeProfile(
            model="anthropic/claude-sonnet-4",
            has_api_key=True,
            provider="anthropic",
        )
        sel = _resolve(labels={"model:other"}, profiles=profiles, user_profile=user)
        self.assertEqual(sel.model, "anthropic/claude-haiku")
        self.assertEqual(sel.credential_path, USER_KEY_PATH)

    def test_raw_label_requires_a_user_credential(self):
        """The org credential is never reachable from a raw label."""
        with self.assertRaises(ModelSelectionError) as ctx:
            _resolve(
                labels={"model:minimax-coding-plan/MiniMax-M2"},
                allowed_providers=["minimax-coding-plan"],
                user_login="octocat",
            )
        self.assertIn("per-user credential", str(ctx.exception))

    def test_raw_label_without_any_user_credential_fails_fast(self):
        with self.assertRaises(ModelSelectionError) as ctx:
            _resolve(
                labels={"model:anthropic/claude-haiku"},
                allowed_providers=["anthropic"],
                user_login="octocat",
            )
        self.assertIn("per-user credential", str(ctx.exception))

    def test_raw_label_with_matching_user_credential_is_allowed(self):
        user = UserOpencodeProfile(
            model="minimax-coding-plan/MiniMax-M3",
            has_api_key=True,
            provider="minimax-coding-plan",
        )
        sel = _resolve(
            labels={"model:minimax-coding-plan/MiniMax-M2"},
            user_profile=user,
            allowed_providers=["minimax-coding-plan"],
        )
        self.assertEqual(sel.model, "minimax-coding-plan/MiniMax-M2")
        self.assertEqual(sel.credential_path, USER_KEY_PATH)

    def test_credential_without_a_declared_provider_fails_fast(self):
        user = UserOpencodeProfile(model="anthropic/claude-sonnet-4", has_api_key=True)
        with self.assertRaises(ModelSelectionError) as ctx:
            _resolve(user_profile=user, user_login="octocat")
        self.assertIn("no declared provider", str(ctx.exception))

    def test_org_credential_provider_defaults_to_the_org_model(self):
        sel = _resolve(org_credential_provider="")
        self.assertEqual(sel.credential_provider, "minimax-coding-plan")

    def test_empty_org_model_fails_fast(self):
        with self.assertRaises(ModelSelectionError):
            _resolve(org_model="")


if __name__ == "__main__":
    unittest.main()
