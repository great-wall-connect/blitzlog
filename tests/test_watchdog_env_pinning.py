"""Regression: pin the COMMON_ARGS / mode-specific split invariant in
`infra/aws/packer/scripts-docker-ubuntu/watchdog.sh`.

The watchdog runs under `set -euo pipefail` and `source`s
`/etc/blitzlog.env` before building the `COMMON_ARGS=( ... )` array
that drives `docker run`. Every var referenced in `COMMON_ARGS` MUST be
set in the shell by the time the array is built — otherwise `set -u`
aborts the watchdog before the container even starts.

The watchdog has a documented invariant (see the comment block at
`infra/aws/packer/scripts-docker-ubuntu/watchdog.sh:139-143`):

> Mode-specific env vars. We don't reference these outside the
> matching branch — `set -u` would otherwise kill the script when a
> var that isn't declared in `/etc/blitzlog.env` for the current mode
> is read. This is intentional: a future contributor adding a var the
> watchdog should pass gets a loud failure, not a silent empty value.

In other words:
  * Vars in the unconditional `COMMON_ARGS=( ... )` block must be
    declared in BOTH env files (`autonomous.py` AND `assisted.py`) —
    OR be set in-script by the watchdog itself before the array is
    built (e.g. `GITHUB_TOKEN` from SSM, `OPENCODE_SERVER_PASSWORD`
    from `openssl rand`).
  * Vars in the `if [ "$MODE" = "assisted" ]` branch must be declared
    in `assisted.py`. They are allowed to be ABSENT from
    `autonomous.py`, because that branch is opt-in.

Issue #97 was the canonical trigger: `TELEGRAM_BOT_NAME` was placed in
the unconditional block, but only `assisted.py`'s env file declared
it. Autonomous runs died on `set -u` at line 138 (`TELEGRAM_BOT_NAME:
unbound variable`) before `docker run` ever executed. This test would
have caught that bug at PR-review time; it pins the invariant against
the same mistake being repeated (e.g. an STT_* var promoted to
COMMON_ARGS in error).

The test parses the relevant files with simple regex — no AST magic,
no subprocess. Drift on whitespace, indentation, or comment placement
does not break the test, but adding a var to either COMMON_ARGS block
or to either env-file heredoc WITHOUT updating the other will.
"""

import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCHDOG = os.path.join(REPO_ROOT, "infra/aws/packer/scripts-docker-ubuntu/watchdog.sh")
AUTONOMOUS_PY = os.path.join(REPO_ROOT, "lambda/scripts/aws/autonomous.py")
ASSISTED_PY = os.path.join(REPO_ROOT, "lambda/scripts/aws/assisted.py")

# Vars the watchdog sets in-script (from SSM, `openssl rand`, etc.)
# BEFORE the `COMMON_ARGS=(...)` array is built. These do NOT need
# to appear in either env file. Add to this set ONLY when a new
# in-script declaration is added above the `COMMON_ARGS=( ... )`
# block in watchdog.sh.
WATCHDOG_SELF_SET = frozenset(
    {
        "GITHUB_TOKEN",  # from /blitzlog/$BLITZLOG_ENV/ephemeral/gh/$REPO/$ISSUE_NUMBER
        "OPENCODE_SERVER_PASSWORD",  # generated at watchdog.sh:106 by `openssl rand -hex 16`
    }
)


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _extract_common_args(watchdog_src):
    """Return (unconditional_set, assisted_only_set) — both are
    frozensets of var names referenced via `-e "VAR=$VAR"` inside
    the two `COMMON_ARGS` arrays of `watchdog.sh`.

    The unconditional block is the top-level `COMMON_ARGS=( ... )`
    declaration. The assisted-only block is the body of `if [
    "$MODE" = "assisted" ]; then COMMON_ARGS+=( ... ); fi`.
    """
    # Strip comments and bash-only `\\` line continuations so we
    # don't match a commented-out `-e` line.
    cleaned = re.sub(r"(?m)^\s*#[^\n]*\n", "", watchdog_src)
    cleaned = cleaned.replace("\\\n", "")

    top_match = re.search(
        r"COMMON_ARGS=\(\s*\n(.*?)\n\s*\)",
        cleaned,
        flags=re.DOTALL,
    )
    if not top_match:
        raise AssertionError(
            "could not find top-level COMMON_ARGS=( ...) in watchdog.sh"
        )
    unconditional = _varnames_in_args(top_match.group(1))

    assisted_match = re.search(
        r'if \[ "\$MODE" = "assisted" \]; then\s*\n\s*COMMON_ARGS\+=\(\s*\n'
        r"(.*?)"
        r"\n\s*\)",
        cleaned,
        flags=re.DOTALL,
    )
    assisted_only = (
        _varnames_in_args(assisted_match.group(1)) if assisted_match else set()
    )
    return frozenset(unconditional), frozenset(assisted_only)


def _varnames_in_args(args_block):
    """Pull var names from `-e "VAR=$VAR"` lines. Skips lines without
    an `$VAR` expansion (which would not trip `set -u` anyway)."""
    names = set()
    for m in re.finditer(r'-e\s+"([A-Z_][A-Z0-9_]*)=\$([A-Z_][A-Z0-9_]*)"', args_block):
        lhs, rhs = m.group(1), m.group(2)
        if lhs != rhs:
            # Defensive: this file only writes `-e "VAR=$VAR"` (same
            # name on both sides). If somebody refactors to
            # `-e "FOO=$BAR"`, we'd miss it — but that pattern wouldn't
            # trip `set -u` for BAR either, so this test's scope is
            # the same-name case.
            continue
        names.add(lhs)
    return names


def _extract_blitzlog_env_vars(builder_src):
    """Return the frozenset of var NAMES written into the
    `cat > /etc/blitzlog.env <<ENVEOF ... ENVEOF` heredoc of an
    `assisted.py` / `autonomous.py` bootstrap builder.

    Matches lines of the form `VAR=...` (initial-only — i.e. the
    leftmost `=` of the line is the assignment). Heredoc body
    backtick references (none today, but cheap defense) and `${{...}}`
    shell-param expansions on the RHS are allowed.
    """
    match = re.search(
        r"cat > /etc/blitzlog\.env\s*<<ENVEOF\s*\n(.*?)\nENVEOF\b",
        builder_src,
        flags=re.DOTALL,
    )
    if not match:
        raise AssertionError("could not find /etc/blitzlog.env heredoc in builder")
    names = set()
    for line in match.group(1).splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=", line)
        if m:
            names.add(m.group(1))
    return frozenset(names)


class TestWatchdogEnvPinning(unittest.TestCase):
    """Pin the invariant that the watchdog's docker-run env-var layout
    is consistent with the env-file declarations in both bootstrap
    builders. Drops of `set -u` errors when agents launch in
    autonomous mode (the issue #97 bug class) are the failure mode."""

    @classmethod
    def setUpClass(cls):
        cls.watchdog_src = _read(WATCHDOG)
        cls.autonomous_src = _read(AUTONOMOUS_PY)
        cls.assisted_src = _read(ASSISTED_PY)
        cls.unconditional, cls.assisted_only = _extract_common_args(cls.watchdog_src)
        cls.autonomous_env = _extract_blitzlog_env_vars(cls.autonomous_src)
        cls.assisted_env = _extract_blitzlog_env_vars(cls.assisted_src)

    def test_unconditional_common_args_in_both_env_files(self):
        """Every var in unconditional COMMON_ARGS must be declared in
        BOTH `/etc/blitzlog.env` heredocs (or set in-script by watchdog
        itself above the COMMON_ARGS=(...) block)."""
        missing_autonomous = self.unconditional - (
            self.autonomous_env | WATCHDOG_SELF_SET
        )
        missing_assisted = self.unconditional - (self.assisted_env | WATCHDOG_SELF_SET)
        problems = []
        if missing_autonomous:
            problems.append(
                "  - autonomous mode missing from /etc/blitzlog.env "
                "(lambda/scripts/autonomous.py): "
                + ", ".join(sorted(missing_autonomous))
            )
        if missing_assisted:
            problems.append(
                "  - assisted mode missing from /etc/blitzlog.env "
                "(lambda/scripts/assisted.py): " + ", ".join(sorted(missing_assisted))
            )
        if problems:
            self.fail(
                "vars in watchdog.sh's unconditional COMMON_ARGS=( ...) "
                "block are not declared in the matching env file. Either "
                "declare them in the missing env file, or move the line "
                'into the `if [ "$MODE" = "assisted" ]` block '
                "(assisted-only). Fix:\n" + "\n".join(problems)
            )

    def test_assisted_branch_args_in_assisted_env_file(self):
        """Every var in the assisted-only COMMON_ARGS branch must be
        declared in `assisted.py`'s /etc/blitzlog.env (or set
        in-script by watchdog itself). It is allowed to be absent
        from `autonomous.py` — that's the whole point of the branch."""
        missing = self.assisted_only - (self.assisted_env | WATCHDOG_SELF_SET)
        if missing:
            self.fail(
                "vars in watchdog.sh's `if MODE=assisted` COMMON_ARGS+= "
                "branch are not declared in assisted.py's "
                "/etc/blitzlog.env: " + ", ".join(sorted(missing))
            )

    def test_known_unbound_var_does_not_appear_in_unconditional(self):
        """Defensive assertion against the specific bug we're fixing:
        `TELEGRAM_BOT_NAME` was added to unconditional COMMON_ARGS even
        though it is only declared by `assisted.py`. Pin that the
        regression cannot return without a loud test failure."""
        self.assertNotIn(
            "TELEGRAM_BOT_NAME",
            self.unconditional,
            "TELEGRAM_BOT_NAME must NOT be in watchdog.sh's "
            "unconditional COMMON_ARGS=( ...) — autonomous mode has no "
            "Telegram bot and `set -u` would abort. Keep it in the "
            "assisted-only branch instead.",
        )

    def test_container_cannot_reach_the_instance_metadata_service(self):
        """Issue #165: the EC2 agent role can now read ANY user's
        per-user inference credential (/blitzlog/users/*/opencode/*).
        The container runs as root on the docker bridge and could
        otherwise reach IMDS and assume that role.

        The watchdog drops IMDS traffic from the container's network
        (the DOCKER-USER chain only sees FORWARDED container traffic,
        so the host's own IMDS access is unaffected) and sets
        AWS_EC2_METADATA_DISABLED for the container as a second layer.
        """
        self.assertIn(
            "169.254.169.254",
            self.watchdog_src,
            "watchdog.sh must block the instance metadata service from "
            "the container's network (issue #165)",
        )
        self.assertIn(
            "DOCKER-USER",
            self.watchdog_src,
            "the IMDS block must be installed in the DOCKER-USER chain",
        )
        self.assertIn(
            "AWS_EC2_METADATA_DISABLED=true",
            self.watchdog_src,
            "the container must be told that the AWS SDK may not use IMDS",
        )

    def test_imds_block_is_fail_closed(self):
        """Issue #175: a boundary that fails silently is
        indistinguishable from one that works.

        The first version of the IMDS rule was best-effort — it logged
        a warning and started the agent anyway. The gap then went
        unnoticed for a release cycle: the container could assume the
        instance role and read SSM parameters it must never see (the
        GitHub App's private key, the webhook secret). If the rule
        cannot be installed, the agent must NOT start.
        """
        self.assertIn(
            "block_imds_from_container",
            self.watchdog_src,
            "watchdog.sh must gate the container start on the IMDS block "
            "being in force (issue #175)",
        )
        self.assertIn(
            "refusing to start the agent container",
            self.watchdog_src,
            "the watchdog must say loudly that it is refusing to start "
            "the container when the IMDS block cannot be installed",
        )
        # The refusal has to actually gate the start, not just log.
        self.assertRegex(
            self.watchdog_src,
            r"if block_imds_from_container; then\s+run_agent_container",
            "the container start must be inside the IMDS block's success "
            "branch (issue #175)",
        )
        # ...and the failure path must still run the cleanup (log
        # upload, bot-lock release, instance terminate), so a refused
        # start does not strand the instance.
        self.assertIn(
            "CONTAINER_EXIT=1",
            self.watchdog_src,
            "the refused start must set a non-zero exit code so the "
            "cleanup path runs and the instance terminates",
        )

    def test_ami_installs_iptables_explicitly(self):
        """The block must not depend on iptables arriving as a
        transitive dependency of docker-ce: if the binary is missing,
        the watchdog refuses to start the agent."""
        system_sh = _read(
            os.path.join(
                REPO_ROOT, "infra/aws/packer/scripts-docker-ubuntu/01-system.sh"
            )
        )
        self.assertRegex(
            system_sh,
            r"apt-get install[^\n]*\n(?:[^\n]*\\\n)*[^\n]*\biptables\b",
            "01-system.sh must install iptables explicitly (issue #175)",
        )

    def test_model_selection_vars_reach_the_container(self):
        """Skipped on this branch.

        The model-selection vars (OPENCODE_BASE_URL,
        OPENCODE_API_KEY_SSM_PARAM) belong to issue #165's three-level
        model selection, which is not on this branch — the assertion
        would fail for a feature that does not exist here yet. It is
        kept out rather than skipped so the file stays honest about
        what it covers.
        """
        self.assertNotIn(
            "OPENCODE_BASE_URL",
            self.unconditional,
            "OPENCODE_BASE_URL is issue #165's model-selection wiring; it "
            "does not belong on this branch. If you are landing #165, "
            "restore the assertion that it reaches the container.",
        )


if __name__ == "__main__":
    unittest.main()
