"""Regression guards for the agent container's opencode-serve readiness loop.

The container's entrypoint (packages/images/agent/entrypoint.sh) polls
http://127.0.0.1:4096/ with curl until opencode serve is ready, then
auto-selects the project and starts the Telegram bot. If a curl in that
loop hangs forever, the shell blocks in waitpid and the rest of the
entrypoint never runs — the agent sits idle and Telegram never gets a
"ready" notification. Reproduced on prod for issue #30.

These tests are static (string-level checks on entrypoint.sh). They mirror
tests/test_mise_bootstrap.py's pattern: read a config file, assert the
shapes that prevent the bug from regressing. No shell, no docker, no AWS.
"""

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "packages" / "images" / "agent" / "entrypoint.sh"


class TestEntrypointReadiness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = ENTRYPOINT.read_text()

    def test_wait_loop_curl_has_max_time(self):
        """The wait-loop curl MUST cap each request with --max-time.

        Without it, an opencode serve that is up (port 4096 listening)
        but slow on / traps curl forever, the shell blocks in waitpid,
        and the bot never starts. See #73.
        """
        i = self.text.index("opencode serve ready (after")
        wait_loop = self.text[: i + 200]  # the wait loop is just above
        self.assertIn(
            "--max-time 3",
            wait_loop,
            "wait-loop curl must set --max-time 3 (regression guard for #73)",
        )

    def test_wait_loop_curl_has_connect_timeout(self):
        """The wait-loop curl MUST also cap the TCP handshake."""
        i = self.text.index("opencode serve ready (after")
        wait_loop = self.text[: i + 200]
        self.assertIn(
            "--connect-timeout 2",
            wait_loop,
            "wait-loop curl must set --connect-timeout 2 (regression guard for #73)",
        )

    def test_opencode_json_written_log_present(self):
        """After writing opencode.jsonc/.json, the entrypoint must log a
        confirmation so the cloud-success path has the same operator
        diagnostic the local-LLM path has had since the refactor.

        The cloud-fallback path at line ~411 also has a `cp
        opencode.jsonc opencode.json` (when the user accepts the cloud
        fallback after local LLM fails), so we can't simply rindex for
        `cp` — we need to find the cp that's in the *initial* config
        writing block. The diagnostic log we want to guard is the
        unconditional one (after the if/elif/elif/fi chain), not the
        fallback restart. Find the log line directly and verify it
        follows some cp + fi in the initial block.
        """
        i = self.text.index('log "Wrote /root/.config/opencode/opencode.json')
        # Sanity: the log must come after at least one `cp` of the
        # .jsonc to .json rename in the initial config-writing block.
        # We require the cp to be at most 1200 chars before the log —
        # the initial block is ~100 lines but the cp is at the very end,
        # and rationale blocks added by later PRs (e.g. #89) push the cp
        # further out.
        preceding = self.text[max(0, i - 1200) : i]
        self.assertIn(
            "cp /root/.config/opencode/opencode.jsonc /root/.config/opencode/opencode.json",
            preceding,
            "the 'Wrote opencode.json' log must follow a cp of .jsonc to .json",
        )
        # And the log itself must include the diagnostic fields.
        line = self.text[i : i + 200]
        self.assertIn("provider=$opencode_provider", line)
        self.assertIn("api_key_prefix=", line)

    def test_opencode_config_branches_still_present(self):
        """Sanity: the four opencode.json writing branches (lambda
        pre-render, local LLM, cloud+key, model-only) must still exist.
        Guards against a refactor that drops the cloud or model-only
        fallback while adding the new log line.
        """
        self.assertIn("OPENCODE_CONFIG_JSON:-}", self.text)
        self.assertIn("local_llm_endpoint", self.text)
        self.assertIn('opencode_provider" ] && [ -n "$opencode_api_key" ]', self.text)
        self.assertIn('elif [ -n "$opencode_model" ]; then', self.text)

    def test_no_bash_only_parameter_expansions(self):
        """The entrypoint's shebang is `#!/bin/sh` and the runtime image
        is `python:3.12-slim`, where /bin/sh -> dash. Dash rejects
        bash-only parameter-expansion forms with "bad substitution"
        (regression in #89). The smoke test runs `bash -n`, which
        accepts bash-only syntax, so we need a string-level guard that
        explicitly fails on these shapes:

        - ${var:N} or ${var:N:M}  substring expansion (bash-only)
        - ${var/pat/repl}, ${var//pat/repl}  pattern substitution (bash-only)
        - ${var^^}, ${var,,}, ${var^}, ${var,}  case modification (bash-only)
        - ${!var}  indirect reference (bash-only)
        - ${PIPESTATUS[…]}  array subscript on a bash-only array (bash-only)

        Patterns are matched anywhere on a line; the test reports every
        match with file:line so the failure message is actionable.
        """
        import re

        patterns = {
            r"\$\{[^}]+:[0-9]+(:[0-9]+)?\}": "substring expansion ${var:N[:M]}",
            r"\$\{[^}]+//[^}/]+/[^}]*\}": "pattern substitution ${var//pat/repl}",
            r"\$\{[^}]+/[^}/]+/[^}]*\}": "pattern substitution ${var/pat/repl}",
            r"\$\{[^}]+\^\^?}?\}": "case modification (${var^}, ${var^^})",
            r"\$\{[^}]+,,?}?\}": "case modification (${var,}, ${var,,})",
            r"\$\{![^}]+\}": "indirect reference ${!var}",
            r"\$\{PIPESTATUS\[[^}]*\}": "PIPESTATUS array subscript (bash-only)",
        }
        # Track which lines are full-line comments so we can skip matches
        # there (rationale blocks frequently mention the bash-only
        # constructs by name — they shouldn't false-positive).
        is_comment = [line.lstrip().startswith("#") for line in self.text.splitlines()]
        for pattern, label in patterns.items():
            for m in re.finditer(pattern, self.text):
                line_no = self.text[: m.start()].count("\n") + 1
                if is_comment[line_no - 1]:
                    continue
                self.fail(
                    f"bash-only parameter expansion in entrypoint.sh "
                    f"at line {line_no}: {label!r} matched {m.group(0)!r}. "
                    f"The entrypoint runs under dash on python:3.12-slim; "
                    f"rewrite to POSIX before merging."
                )

    def test_git_user_name_email_block_present(self):
        """Issue #119: the container's audit-friendly git identity. The
        Lambda bootstrap sets GIT_USER_NAME / GIT_USER_EMAIL via
        `docker run -e ...` (watchdog.sh forwards them). The
        entrypoint must read them and run `git config --global
        user.{name,email}` so any commit the agent makes inside the
        container is attributed to the human who triggered the run
        (the issue's example: daniel-sarosi-gwc), NOT the EC2 default
        `root@<hostname>`.
        """
        # The if-block is gated on the env var being non-empty —
        # without the guard, `git config` with an empty user.name
        # silently writes a malformed config and every subsequent
        # commit gets an empty author.
        i = self.text.index('if [ -n "${GIT_USER_NAME:-}" ]')
        snippet = self.text[i : i + 200]
        self.assertIn(
            'git config --global user.name "${GIT_USER_NAME}"',
            snippet,
            "entrypoint.sh must apply GIT_USER_NAME to git config --global "
            "user.name (issue #119 audit trail)",
        )
        self.assertIn(
            'git config --global user.email "${GIT_USER_EMAIL:-}"',
            snippet,
            "entrypoint.sh must apply GIT_USER_EMAIL to git config --global "
            "user.email (issue #119 audit trail)",
        )


if __name__ == "__main__":
    unittest.main()
