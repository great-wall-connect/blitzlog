"""Regression: a failed agent run must still run the watchdog cleanup.

The watchdog runs under `set -euo pipefail`. `sudo docker run ...` used
to be a plain command, so a non-zero exit — i.e. ANY failed agent run —
aborted the script at that line, before the cleanup block could:

  * upload the container + host logs to S3,
  * release the bot-pool lock (the next run for that bot was blocked),
  * delete the ephemeral SSM token parameter and its refresh schedule
    group,
  * terminate the EC2 instance (it kept burning spot capacity).

Observed on 2026-10-26 with agent exit code 100: the watchdog exited
100 with `blitzlog-agent.service` in `failed` state, the instance was
still running 45 minutes later, and the logs were never uploaded.

The fix captures the container's exit code explicitly
(`|| CONTAINER_EXIT=$?`) so errexit cannot skip the cleanup, and the
script still exits with the container's code at the very end.

This test parses watchdog.sh with simple regex — no AST magic, no
subprocess. It pins the guard, not the exact formatting.
"""

import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCHDOG = os.path.join(REPO_ROOT, "infra/aws/packer/scripts-docker-ubuntu/watchdog.sh")

# The command that starts the agent container. Since issue #175 it
# lives in a `run_agent_container` function, so the `docker run` line
# itself is indented — the regex matches on the command, not the
# column it starts in.
DOCKER_RUN_RE = re.compile(r"^\s*sudo docker run --name blitzlog-agent")

# The line that INVOKES the container and captures its exit code.
CONTAINER_START_RE = re.compile(r"^\s*run_agent_container \|\| CONTAINER_EXIT=\$\?")


def _read():
    with open(WATCHDOG, encoding="utf-8") as handle:
        return handle.read()


def _logical_lines(source):
    """Join shell line continuations so a multi-line command is one string."""
    lines = []
    buffer = ""
    for raw in source.splitlines():
        stripped = raw.strip()
        if buffer:
            buffer += " " + stripped
        else:
            buffer = stripped
        if buffer.endswith("\\"):
            buffer = buffer[:-1].rstrip()
            continue
        lines.append(buffer)
        buffer = ""
    if buffer:
        lines.append(buffer)
    return lines


class TestWatchdogRunsCleanupAfterAFailedRun(unittest.TestCase):
    def setUp(self):
        self.source = _read()
        self.lines = _logical_lines(self.source)

    def _docker_run_line(self):
        for line in self.lines:
            if DOCKER_RUN_RE.match(line):
                return line
        self.fail(
            "the watchdog no longer starts the agent container with "
            "`sudo docker run --name blitzlog-agent` — update this test"
        )

    def _container_start_line(self):
        """The line that starts the container and captures its exit code.

        Since issue #175 the `docker run` lives in a
        `run_agent_container` function, so the exit code is captured at
        the call site.
        """
        for line in self.lines:
            if CONTAINER_START_RE.match(line):
                return line
        self.fail(
            "the watchdog no longer starts the agent container with "
            "`run_agent_container || CONTAINER_EXIT=$?` — update this test"
        )

    def test_docker_run_cannot_abort_the_watchdog(self):
        """`set -e` must not kill the watchdog when the container fails."""
        line = self._container_start_line()
        self.assertIn(
            "|| CONTAINER_EXIT=$?",
            line,
            "the container start must capture the container's exit code "
            "(`|| CONTAINER_EXIT=$?`). Under `set -euo pipefail` a plain "
            "failing `docker run` aborts the script before the "
            "cleanup block, so a failed agent run leaves the instance "
            "running, the bot-pool lock held and the logs un-uploaded.",
        )

    def test_container_exit_is_initialised_before_the_run(self):
        """`CONTAINER_EXIT` must be 0 when the container exits cleanly."""
        index = self.lines.index(self._docker_run_line())
        preceding = self.lines[:index]
        self.assertIn(
            "CONTAINER_EXIT=0",
            preceding,
            "CONTAINER_EXIT must be initialised to 0 before `docker run` — "
            "otherwise a successful run leaves it unset and `set -u` kills "
            "the watchdog in the cleanup block.",
        )

    def test_cleanup_runs_after_the_container_exits(self):
        """The cleanup anchors must come after the container's exit."""
        index = self.lines.index(self._docker_run_line())
        after = "\n".join(self.lines[index:])
        for anchor in (
            "blitzlog_s3_put",  # log + session-artifact upload
            "bot-pool-locks/",  # bot-pool lock release
            "delete-parameter",  # ephemeral SSM token teardown
            "delete-schedule-group",  # token-refresh chain teardown
            "terminate-instances",  # instance teardown
        ):
            self.assertIn(
                anchor,
                after,
                f"the cleanup step {anchor!r} must run after the container "
                "exits — it is the only thing that stops a failed run from "
                "leaking the instance, the lock and the token",
            )

    def test_watchdog_exits_with_the_container_exit_code(self):
        self.assertIn(
            'exit "$CONTAINER_EXIT"',
            self.source,
            "the watchdog must exit with the container's exit code after "
            "the cleanup has run",
        )


if __name__ == "__main__":
    unittest.main()
