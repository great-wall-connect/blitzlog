"""The container's opencode.json rendering (issue #165).

Issue #165 adds a BYO / self-hosted endpoint: when the resolved
profile carries a `base_url`, the provider is a CUSTOM one and
opencode needs an explicit `models` map — without it opencode emits
`Model not found: <provider>/<id>` even when the endpoint is
reachable and the model exists.

These tests EXECUTE the entrypoint's `write_cloud_config` function
under `/bin/sh` (the container's shell — dash on python:3.12-slim)
and parse the JSON it writes. That is stronger than the string-level
checks in test_entrypoint_readiness.py: a heredoc that renders
malformed JSON fails here.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "packages" / "images" / "agent" / "entrypoint.sh"


def _extract_write_cloud_config(text: str) -> str:
    """Return the `write_cloud_config` shell function.

    The function is bounded by its definition and the `if
    [ -n "${OPENCODE_CONFIG_JSON:-}" ]` block that follows it. The
    function's own heredocs contain a `}` at column 0 (the JSON
    document's closing brace), so a naive brace matcher would stop
    inside the heredoc — the anchors are used instead.
    """
    start = text.index("write_cloud_config() {")
    end = text.index('if [ -n "${OPENCODE_CONFIG_JSON:-}" ]; then')
    return text[start:end]


class TestCloudConfigRendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = ENTRYPOINT.read_text()
        cls.function = _extract_write_cloud_config(cls.text)

    def _render(self, **kwargs):
        """Run write_cloud_config under /bin/sh with the given
        variables and return the parsed opencode.json."""
        defaults = {
            "opencode_model": "minimax-coding-plan/MiniMax-M3",
            "opencode_api_key": "sk-test-key",
            "opencode_base_url": "",
            "opencode_provider": "minimax-coding-plan",
            "opencode_model_id": "MiniMax-M3",
            "opencode_max_steps": "500",
            "agent_prompt": "",
        }
        defaults.update(kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            # Redirect the hard-coded config path into the temp dir so
            # the test never touches /root.
            function = self.function.replace(
                "/root/.config/opencode", f"{tmp}/opencode"
            )
            script = f"""#!/bin/sh
set -eu
mkdir -p {tmp}/opencode
{function}
"""
            for name, value in defaults.items():
                script += f'{name}="{value}"\n'
            script += "write_cloud_config\n"
            script += f"cat {tmp}/opencode/opencode.json\n"
            result = subprocess.run(
                ["sh", "-c", script],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                result.returncode,
                0,
                f"write_cloud_config failed: {result.stderr}",
            )
            return json.loads(result.stdout)

    def test_builtin_provider_has_no_base_url_or_models_map(self):
        """Without a base URL the provider is a built-in one: opencode
        resolves the model id from its own catalogue, so the rendered
        config stays exactly as it was before issue #165."""
        cfg = self._render()
        provider = cfg["provider"]["minimax-coding-plan"]
        self.assertEqual(provider["options"], {"apiKey": "sk-test-key"})
        self.assertNotIn("baseURL", provider["options"])
        self.assertNotIn("models", provider)
        self.assertEqual(cfg["model"], "minimax-coding-plan/MiniMax-M3")

    def test_byo_endpoint_renders_base_url_and_models_map(self):
        cfg = self._render(
            opencode_model="acme/llama",
            opencode_base_url="https://llm.internal/v1",
            opencode_provider="acme",
            opencode_model_id="llama",
        )
        provider = cfg["provider"]["acme"]
        self.assertEqual(
            provider["options"],
            {"baseURL": "https://llm.internal/v1", "apiKey": "sk-test-key"},
        )
        # The models map is REQUIRED for a custom provider — without
        # it opencode emits "Model not found: acme/llama".
        self.assertEqual(provider["models"], {"llama": {}})
        self.assertEqual(cfg["model"], "acme/llama")

    def test_models_map_key_is_everything_after_the_first_slash(self):
        """opencode parses the FIRST slash as the provider separator,
        so a model id that itself contains a slash is registered under
        the full remainder."""
        cfg = self._render(
            opencode_model="acme/qwen/qwen3.8-27b",
            opencode_base_url="https://llm.internal/v1",
            opencode_provider="acme",
            opencode_model_id="qwen/qwen3.8-27b",
        )
        self.assertEqual(cfg["provider"]["acme"]["models"], {"qwen/qwen3.8-27b": {}})

    def test_base_url_without_a_model_id_falls_back_to_the_plain_block(self):
        """A base URL with no resolvable provider/model must not emit a
        broken provider block."""
        cfg = self._render(
            opencode_model="llama",
            opencode_base_url="https://llm.internal/v1",
            opencode_provider="",
            opencode_model_id="",
        )
        self.assertNotIn("baseURL", cfg["provider"][""]["options"])

    def test_the_assisted_cloud_fallback_uses_the_same_writer(self):
        """The fallback must not carry its own inline provider block:
        it has to render the resolved credential and the BYO
        baseURL + models map exactly like the initial config."""
        i = self.text.index("User chose cloud fallback")
        block = self.text[i : i + 1200]
        self.assertIn("write_cloud_config", block)
        # The old inline heredoc is gone.
        self.assertNotIn("cat > /root/.config/opencode/opencode.jsonc", block)
        # ...and so is the old direct use of the raw env var.
        self.assertNotIn('"apiKey": "${OPENCODE_API_KEY}"', self.text)

    def test_the_writer_is_posix_shell(self):
        """The entrypoint runs under dash on python:3.12-slim."""
        result = subprocess.run(
            ["sh", "-n", "-c", self.function],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
