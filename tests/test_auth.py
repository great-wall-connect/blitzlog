"""GitHub App authentication + webhook signature verification."""

import base64
import unittest
from unittest.mock import MagicMock, patch

from auth import get_github_app_token


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

        result = get_github_app_token("owner/repo")

        self.assertEqual(result, "ghp_abc123")
        mock_jwt_encode.assert_called_once()
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(call_kwargs["json"], {"repositories": ["repo"]})
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

        get_github_app_token("org/very-specific-repo")

        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(len(body["repositories"]), 1)
        self.assertEqual(body["repositories"][0], "very-specific-repo")

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
        self.assertEqual(call_kwargs["json"], {"repositories": ["repo"]})


if __name__ == "__main__":
    unittest.main()
