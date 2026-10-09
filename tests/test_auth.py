"""GitHub App authentication + webhook signature verification."""

import base64
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from auth import (
    GITHUB_TOKEN_LIFETIME_HOURS_DEFAULT,
    _get_github_token_lifetime_hours,
    get_github_app_token,
)


def _expires_at_to_utc(value: str) -> datetime:
    """Parse a GitHub 'YYYY-MM-DDTHH:MM:SSZ' timestamp into a tz-aware
    datetime. Centralised so the lifetime tests don't repeat the
    format dance (and the assumption that GitHub echoes the format
    back untouched — see #114)."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class TestGetGithubAppToken(unittest.TestCase):
    @patch("auth.jwt.encode", return_value="fake.jwt.token")
    @patch("auth.requests.post")
    @patch("auth.get_ssm_param")
    def test_posts_repo_scoped_body_for_long_lived_token(
        self, mock_ssm, mock_post, mock_jwt_encode
    ):
        mock_ssm.side_effect = lambda name, with_decryption=True: {
            "github-app/id": "12345",
            "github-app/private-key": base64.b64encode(b"fake-key").decode(),
            "github-app/installation-id": "67890",
        }[name]
        mock_post.return_value.ok = True
        mock_post.return_value.json.return_value = {"token": "ghp_abc123"}
        mock_post.return_value.raise_for_status = MagicMock()

        before = datetime.now(timezone.utc)
        result = get_github_app_token("owner/repo")
        after = datetime.now(timezone.utc)

        self.assertEqual(result, "ghp_abc123")
        mock_jwt_encode.assert_called_once()
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        body = call_kwargs["json"]
        self.assertEqual(body["repositories"], ["repo"])
        # expires_at is set and reflects the 8h default lifetime. Allow
        # generous skew because the test process and the lambda's
        # `datetime.now()` call run in the same window but not
        # atomically.
        self.assertIn("expires_at", body)
        expires_at = _expires_at_to_utc(body["expires_at"])
        expected_min = before + timedelta(hours=GITHUB_TOKEN_LIFETIME_HOURS_DEFAULT)
        expected_max = after + timedelta(hours=GITHUB_TOKEN_LIFETIME_HOURS_DEFAULT)
        self.assertGreaterEqual(expires_at, expected_min - timedelta(seconds=1))
        self.assertLessEqual(expires_at, expected_max + timedelta(seconds=1))
        url = mock_post.call_args.args[0]
        self.assertIn("/installations/67890/access_tokens", url)

    @patch("auth.jwt.encode", return_value="fake.jwt.token")
    @patch("auth.requests.post")
    @patch("auth.get_ssm_param")
    def test_scopes_to_single_repo_for_eight_hour_lifetime(
        self, mock_ssm, mock_post, mock_jwt_encode
    ):
        mock_ssm.side_effect = lambda name, with_decryption=True: {
            "github-app/id": "1",
            "github-app/private-key": base64.b64encode(b"k").decode(),
            "github-app/installation-id": "2",
        }[name]
        mock_post.return_value.ok = True
        mock_post.return_value.json.return_value = {"token": "t"}
        mock_post.return_value.raise_for_status = MagicMock()

        before = datetime.now(timezone.utc)
        get_github_app_token("org/very-specific-repo")
        after = datetime.now(timezone.utc)

        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(len(body["repositories"]), 1)
        self.assertEqual(body["repositories"][0], "very-specific-repo")
        # Default lifetime is 8h — covers a single overnight assisted
        # session (idle_watchdog hard-shutdowns at 3h, but the token
        # must outlive the watchdog in case the user is actively
        # driving the agent).
        expires_at = _expires_at_to_utc(body["expires_at"])
        delta = expires_at - before
        # `before` is captured before the call, so delta should be
        # ~8h, not >8h. Allow 8h ± a few seconds for clock jitter.
        self.assertGreaterEqual(
            delta,
            timedelta(hours=GITHUB_TOKEN_LIFETIME_HOURS_DEFAULT) - timedelta(seconds=5),
        )
        # `after` is captured after, so delta vs after should be <= 8h.
        delta_after = expires_at - after
        self.assertLessEqual(
            delta_after,
            timedelta(hours=GITHUB_TOKEN_LIFETIME_HOURS_DEFAULT) + timedelta(seconds=5),
        )

    @patch("auth.jwt.encode", return_value="fake.jwt.token")
    @patch("auth.requests.post")
    @patch("auth.get_ssm_param")
    def test_expires_at_reflects_env_var(self, mock_ssm, mock_post, mock_jwt_encode):
        """Operator sets GITHUB_TOKEN_LIFETIME_HOURS=2 because their
        GitHub App's org policy caps installation tokens at 2h — the
        request body's expires_at should follow suit, not the 8h
        default."""
        mock_ssm.side_effect = lambda name, with_decryption=True: {
            "github-app/id": "1",
            "github-app/private-key": base64.b64encode(b"k").decode(),
            "github-app/installation-id": "2",
        }[name]
        mock_post.return_value.ok = True
        mock_post.return_value.json.return_value = {"token": "t"}
        mock_post.return_value.raise_for_status = MagicMock()

        with patch.dict(os.environ, {"GITHUB_TOKEN_LIFETIME_HOURS": "2"}):
            before = datetime.now(timezone.utc)
            get_github_app_token("owner/repo")
            after = datetime.now(timezone.utc)

        body = mock_post.call_args.kwargs["json"]
        expires_at = _expires_at_to_utc(body["expires_at"])
        # 2h ± 5s tolerance for the before/after clock drift.
        self.assertGreaterEqual(
            expires_at - before, timedelta(hours=2) - timedelta(seconds=5)
        )
        self.assertLessEqual(
            expires_at - after, timedelta(hours=2) + timedelta(seconds=5)
        )

    @patch("auth.jwt.encode", return_value="fake.jwt.token")
    @patch("auth.requests.post")
    @patch("auth.get_ssm_param")
    def test_logs_response_body_and_raises_on_422(
        self, mock_ssm, mock_post, mock_jwt_encode
    ):
        import requests as real_requests

        mock_ssm.side_effect = lambda name, with_decryption=True: {
            "github-app/id": "12345",
            "github-app/private-key": base64.b64encode(b"fake-key").decode(),
            "github-app/installation-id": "67890",
        }[name]

        error_response = real_requests.Response()
        error_response.status_code = 422
        error_response._content = (
            b'{"message":"Validation Failed","errors":["Bad repository name"]}'
        )
        mock_post.return_value = error_response

        with self.assertRaises(real_requests.HTTPError):
            get_github_app_token("owner/repo")

        call_kwargs = mock_post.call_args.kwargs
        body = call_kwargs["json"]
        # Even on the error path the body must include the scope
        # and the lifetime we requested — that's what GitHub will
        # validate. Don't regress to the pre-#114 "no expires_at"
        # shape.
        self.assertEqual(body["repositories"], ["repo"])
        self.assertIn("expires_at", body)


class TestGithubTokenLifetimeHoursEnv(unittest.TestCase):
    """Direct tests for the env-var resolver. Keeps the
    request-body tests above free of env-mutation boilerplate."""

    def setUp(self):
        # Snapshot so each test can mutate without leaking into the
        # next (unittest's default isolation is per-test, but
        # os.environ is module-global).
        self._env_snapshot = dict(os.environ)
        # Default path: nothing in env.
        os.environ.pop("GITHUB_TOKEN_LIFETIME_HOURS", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_snapshot)

    def test_returns_default_when_env_var_missing(self):
        self.assertEqual(
            _get_github_token_lifetime_hours(),
            GITHUB_TOKEN_LIFETIME_HOURS_DEFAULT,
        )

    def test_returns_parsed_value_when_env_var_set(self):
        os.environ["GITHUB_TOKEN_LIFETIME_HOURS"] = "3"
        self.assertEqual(_get_github_token_lifetime_hours(), 3)

    def test_falls_back_to_default_when_env_var_is_zero(self):
        os.environ["GITHUB_TOKEN_LIFETIME_HOURS"] = "0"
        self.assertEqual(
            _get_github_token_lifetime_hours(),
            GITHUB_TOKEN_LIFETIME_HOURS_DEFAULT,
        )

    def test_falls_back_to_default_when_env_var_is_negative(self):
        os.environ["GITHUB_TOKEN_LIFETIME_HOURS"] = "-1"
        self.assertEqual(
            _get_github_token_lifetime_hours(),
            GITHUB_TOKEN_LIFETIME_HOURS_DEFAULT,
        )

    def test_falls_back_to_default_when_env_var_is_not_an_int(self):
        os.environ["GITHUB_TOKEN_LIFETIME_HOURS"] = "soon"
        self.assertEqual(
            _get_github_token_lifetime_hours(),
            GITHUB_TOKEN_LIFETIME_HOURS_DEFAULT,
        )

    def test_treats_empty_string_as_unset(self):
        os.environ["GITHUB_TOKEN_LIFETIME_HOURS"] = ""
        self.assertEqual(
            _get_github_token_lifetime_hours(),
            GITHUB_TOKEN_LIFETIME_HOURS_DEFAULT,
        )


if __name__ == "__main__":
    unittest.main()
