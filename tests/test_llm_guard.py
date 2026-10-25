"""BYO-provider endpoint safety guard (issue #165).

`llm_guard.validate_byo_endpoint` guards the per-user
`opencode_base_url` parameter — the endpoint a buyer's own cloud
credential is sent to. The posture is the mirror image of the
local-LLM guard: a cloud provider's API normally resolves to a
PUBLIC IP (fine), while loopback and link-local stay hard-blocked
(that is the instance-metadata path), and private ranges need an
explicit opt-in.
"""

import unittest
from unittest.mock import patch

from llm_guard import validate_byo_endpoint


class TestValidateByoEndpoint(unittest.TestCase):
    def test_accepts_public_endpoint(self):
        with patch(
            "llm_guard._resolve_endpoint_ips", return_value=["93.184.216.34"]
        ):
            ok, reason = validate_byo_endpoint("https://api.openai.com/v1")
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")

    def test_rejects_loopback(self):
        with patch("llm_guard._resolve_endpoint_ips", return_value=["127.0.0.1"]):
            ok, reason = validate_byo_endpoint("http://127.0.0.1:11434/v1")
        self.assertFalse(ok)
        self.assertIn("always-blocked", reason)

    def test_rejects_link_local(self):
        """169.254.0.0/16 is the EC2 instance metadata service."""
        with patch("llm_guard._resolve_endpoint_ips", return_value=["169.254.169.254"]):
            ok, reason = validate_byo_endpoint("http://169.254.169.254/latest")
        self.assertFalse(ok)
        self.assertIn("always-blocked", reason)

    def test_rejects_private_without_opt_in(self):
        for ip in ("10.0.0.5", "172.16.0.1", "192.168.1.10", "100.64.0.1"):
            with patch("llm_guard._resolve_endpoint_ips", return_value=[ip]):
                ok, reason = validate_byo_endpoint("http://llm.internal/v1")
            self.assertFalse(ok, ip)
            self.assertIn("private IP", reason)

    def test_accepts_private_with_opt_in(self):
        with patch("llm_guard._resolve_endpoint_ips", return_value=["100.64.0.1"]):
            ok, reason = validate_byo_endpoint(
                "http://llm.internal/v1", allow_private_cidrs=True
            )
        self.assertTrue(ok, reason)

    def test_rejects_empty_url(self):
        ok, reason = validate_byo_endpoint("")
        self.assertFalse(ok)
        self.assertIn("empty", reason)

    def test_rejects_non_http_scheme(self):
        ok, reason = validate_byo_endpoint("ftp://example.com/v1")
        self.assertFalse(ok)
        self.assertIn("scheme", reason)

    def test_rejects_unresolvable_host(self):
        with patch(
            "llm_guard._resolve_endpoint_ips", side_effect=OSError("no such host")
        ):
            ok, reason = validate_byo_endpoint("https://nope.invalid/v1")
        self.assertFalse(ok)
        self.assertIn("DNS resolution failed", reason)

    def test_rejects_empty_resolution(self):
        with patch("llm_guard._resolve_endpoint_ips", return_value=[]):
            ok, reason = validate_byo_endpoint("https://example.com/v1")
        self.assertFalse(ok)
        self.assertIn("no resolved addresses", reason)

    def test_rejects_unparseable_ip(self):
        with patch("llm_guard._resolve_endpoint_ips", return_value=["not-an-ip"]):
            ok, reason = validate_byo_endpoint("https://example.com/v1")
        self.assertFalse(ok)
        self.assertIn("cannot parse", reason)


if __name__ == "__main__":
    unittest.main()
