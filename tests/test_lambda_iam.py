"""Regression guards for the blitzlog-<env>-lambda-role IAM policy.

Mirrors the pattern in tests/test_deploy_workflow.py: read the HCL
file as a string, scan the relevant `policy = jsonencode({...})` body
with regex, and assert the shapes that prevent regressions from
landing unnoticed.

Covers the Lambda's runtime IAM needs — the role the Lambda's
execution environment assumes. Most Lambda permissions (SSM read,
EC2 launch, S3 upload) are covered by the deployment-side
integration tests (they're exercised end-to-end on every release
apply). The token-refresh chain (issue #145) is a Lambda-internal
loop that does NOT run during a normal deploy, so a missing IAM
grant only surfaces 50 minutes after a fresh instance launches
— and by then the agent has already done most of its work. The
tests below pin the exact Sid + actions + condition that the
chain's `iam:PassRole` requires so a future refactor of
`infra/aws/modules/core/iam.tf` cannot drop it without failing here.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAMBDA_IAM_TF = REPO_ROOT / "infra" / "aws" / "modules" / "core" / "iam.tf"


def _policy_body_for_inline_lambda_role(iam_tf: str) -> str:
    """Return the HCL body of the Lambda role's inline policy block.

    The Lambda role is `aws_iam_role.lambda_role` in
    `infra/aws/modules/core/iam.tf`. Its inline policy is the
    `jsonencode({...})` call inside the
    `aws_iam_role_policy.lambda_policy` resource, which lives in
    the same `iam.tf` file (separate from the EC2 agent role and
    scheduler role). Mirrors `_policy_body_for_role` in
    `test_deploy_workflow.py`.
    """
    start_marker = 'resource "aws_iam_role_policy" "lambda_policy" {'
    start_idx = iam_tf.find(start_marker)
    assert start_idx >= 0, f"could not find {start_marker} in {LAMBDA_IAM_TF}"
    body_open = iam_tf.find("{", start_idx)
    depth = 0
    i = body_open
    while i < len(iam_tf):
        c = iam_tf[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = iam_tf[body_open + 1 : i]
    jm = re.search(r"jsonencode\s*\(", body)
    assert jm, "lambda_policy has no jsonencode(...) call"
    start = jm.end()
    depth = 0
    for j in range(start, len(body)):
        c = body[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return body[start : j + 1]
    raise AssertionError("unterminated jsonencode in lambda_policy")


def _policy_body_for_inline_ec2_agent_role(iam_tf: str) -> str:
    """Return the HCL body of the EC2 agent role's inline policy block.

    The EC2 agent role is `aws_iam_role.ec2_agent_role` in
    `infra/aws/modules/core/iam.tf`. Its inline policy is the
    `jsonencode({...})` call inside the `aws_iam_role_policy.ec2_agent_policy`
    resource — the same file, a different resource block from the
    Lambda's.
    """
    start_marker = 'resource "aws_iam_role_policy" "ec2_agent_policy" {'
    start_idx = iam_tf.find(start_marker)
    assert start_idx >= 0, f"could not find {start_marker} in {LAMBDA_IAM_TF}"
    body_open = iam_tf.find("{", start_idx)
    depth = 0
    i = body_open
    while i < len(iam_tf):
        c = iam_tf[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = iam_tf[body_open + 1 : i]
    jm = re.search(r"jsonencode\s*\(", body)
    assert jm, "ec2_agent_policy has no jsonencode(...) call"
    start = jm.end()
    depth = 0
    for j in range(start, len(body)):
        c = body[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return body[start : j + 1]
    raise AssertionError("unterminated jsonencode in ec2_agent_policy")


def _sid_body(policy_body: str, sid_name: str) -> str:
    """Scope a Sid's body out of the rendered policy. Returns the
    contents of the `{ Sid = "<sid_name>" ... }` Statement.
    """
    idx = re.search(rf'Sid\s*=\s*"{re.escape(sid_name)}"', policy_body)
    assert idx is not None, f"Sid {sid_name!r} not present in policy body"
    idx = idx.start()
    open_idx = policy_body.rfind("{", 0, idx)
    assert open_idx > 0, f"could not find opening brace for Sid {sid_name!r}"
    m = re.search(r"^      \}", policy_body[open_idx:], re.MULTILINE)
    assert m is not None, f"could not find closing brace for Sid {sid_name!r}"
    return policy_body[open_idx : open_idx + m.end()]


class TestLambdaIamRole(unittest.TestCase):
    """Static checks on the Lambda role's inline policy.

    Note: there's a separate `aws_iam_role.ec2_agent_role` and
    `aws_iam_role.scheduler_role` in the same file. We only assert
    on the Lambda role here; the EC2 agent role's policy is
    end-to-end exercised by the deploy workflow; the scheduler
    role has no inline policy (trust only). Add new test methods
    here as the chain's needs grow.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = LAMBDA_IAM_TF.read_text()
        cls.body = _policy_body_for_inline_lambda_role(cls.text)

    def test_scheduler_role_passrole_sid_present(self):
        """The Lambda MUST be allowed iam:PassRole on the scheduler
        role, conditional on the EventBridge Scheduler service
        principal.

        Without this Sid, the first `schedule_next_refresh` call
        at instance launch errors with AccessDeniedException:

          User: arn:aws:sts::...:assumed-role/blitzlog-<env>-lambda-role/...
                is not authorized to perform: iam:PassRole on resource:
                arn:aws:iam::...:role/blitzlog-<env>-scheduler-role

        The chain dies at iteration 0 and the bind mount's token
        never refreshes after the launch-time mint. This is the
        regression observed on the dev instance
        (Ver=48, no further mints in 65+ min — see PR #154
        commit "fix(lambda): schedule_next_refresh — diagnose
        and fix chain self-perpetuation").
        """
        body = self.body
        self.assertRegex(
            body,
            r'Sid\s*=\s*"SchedulerRolePassRole"',
            "lambda_policy must include a SchedulerRolePassRole Sid "
            "(EventBridge Scheduler requires iam:PassRole on the "
            "RoleArn passed to every CreateSchedule; missing it "
            "kills the token-refresh chain at iteration 0).",
        )
        sid_body = _sid_body(body, "SchedulerRolePassRole")
        # Action: must include iam:PassRole (and only that — no
        # other Lambda PassRole sid should grant PassRole on the
        # scheduler role; least-privilege).
        self.assertIn(
            '"iam:PassRole"',
            sid_body,
            "SchedulerRolePassRole Sid must grant iam:PassRole",
        )
        # Resource: must be the scheduler role's ARN. We can't
        # interpolate the ARN value at test time, so we just
        # require the line `Resource = aws_iam_role.scheduler_role.arn`
        # to be present (the test for the deploy role uses a
        # similar pattern).
        self.assertIn(
            "Resource = aws_iam_role.scheduler_role.arn",
            sid_body,
            "SchedulerRolePassRole Sid Resource must be "
            "aws_iam_role.scheduler_role.arn (scoped to the "
            "scheduler role; widening to '*' is a privilege creep).",
        )
        # Condition: must be conditional on the scheduler service
        # principal (mirrors the Lambda + EC2 PassRole Sids in
        # `deploy-role.tf`). StringEquals on
        # iam:PassedToService = scheduler.amazonaws.com.
        self.assertIn(
            '"scheduler.amazonaws.com"',
            sid_body,
            "SchedulerRolePassRole Sid must condition on "
            "iam:PassedToService=scheduler.amazonaws.com (so a "
            "compromised Lambda can't pass the scheduler role to "
            "some other service).",
        )

    def test_scheduler_create_delete_schedule_sid_present(self):
        """The Lambda MUST have scheduler:CreateSchedule /
        scheduler:DeleteSchedule on the per-env refresh group.

        Pre-existing Sid from issue #145; pinned here so a future
        refactor doesn't drop the schedule-lifecycle actions
        without test coverage. The chain's `schedule_next_refresh`
        creates a new schedule every 50 min; without this grant,
        every iteration errors.

        The original statement has no explicit `Sid =` field, so
        we assert on the action+resource combo rather than a named
        Sid. (Adding an explicit Sid is a separate cleanup; not
        in scope here.)
        """
        body = self.body
        # Both actions must appear in the policy. We don't assert
        # they're in the same Action list (the policy body is
        # pre-formatted and regex over multi-line HCL can be brittle);
        # we just need them to be present.
        self.assertIn(
            '"scheduler:CreateSchedule"',
            body,
            "lambda_policy must grant scheduler:CreateSchedule "
            "(chain creates a new EventBridge schedule every 50 min)",
        )
        self.assertIn(
            '"scheduler:DeleteSchedule"',
            body,
            "lambda_policy must grant scheduler:DeleteSchedule "
            "(chain cleans up old schedules)",
        )
        # The corresponding Resource must scope to the per-issue
        # refresh groups' schedule pattern (defence in depth: the
        # Lambda cannot create schedules in any other group).
        self.assertIn(
            "schedule/blitzlog-*-token-refresh-*/*",
            body,
            "scheduler:CreateSchedule/DeleteSchedule Resource must "
            "be scoped to the per-issue refresh groups' schedule pattern.",
        )
        # And must NOT be wildcarded.
        self.assertNotRegex(
            body,
            r"scheduler:CreateSchedule[\s\S]{0,200}Resource\s*=\s*\"\*\"",
            'scheduler:CreateSchedule must NOT have Resource = "*" '
            "(scoped to the per-env refresh group; widening is a "
            "privilege creep that lets a compromised Lambda schedule "
            "workloads anywhere).",
        )

    def test_scheduler_delete_schedule_group_sid_present(self):
        """The Lambda MUST have `scheduler:DeleteScheduleGroup`.

        The Lambda's fallback cleanup (`_sweep_stale_schedule_group`)
        deletes a leftover per-issue schedule group when the chain
        starts, so a run whose predecessor died without a teardown can
        still start. Without this grant the sweep silently fails and
        the chain's first `CreateSchedule` collides with the
        previous run's leftover `iter-0` (ConflictException) — the
        chain never starts and the agent's token dies 1 h after the
        launch-time mint.

        Note: `scheduler:DeleteSchedule` on the child schedules must
        accompany it; the group delete cascades over them.
        """
        body = self.body
        self.assertIn(
            '"scheduler:DeleteScheduleGroup"',
            body,
            "lambda_policy must grant scheduler:DeleteScheduleGroup "
            "(the fallback cleanup deletes a leftover per-issue group "
            "when a chain starts).",
        )
        self.assertIn(
            "schedule-group/blitzlog-*-token-refresh-*",
            body,
            "scheduler:DeleteScheduleGroup Resource must be scoped to "
            "the per-issue refresh groups.",
        )
        self.assertNotRegex(
            body,
            r"scheduler:DeleteScheduleGroup[\s\S]{0,200}Resource\s*=\s*\"\*\"",
            'scheduler:DeleteScheduleGroup must NOT have Resource = "*" '
            "(scoped to the per-issue refresh groups; widening is a "
            "privilege creep).",
        )


class TestEc2AgentIamRole(unittest.TestCase):
    """Static checks on the EC2 agent role's inline policy.

    The EC2 agent role is what the watchdog's teardown runs as. Its
    schedule-group cleanup is the PRIMARY cleanup path for the
    token-refresh chain (the Lambda's startup sweep is only the
    fallback), so a missing grant here means every run leaves its
    schedule group behind.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = LAMBDA_IAM_TF.read_text()
        cls.body = _policy_body_for_inline_ec2_agent_role(cls.text)

    def test_delete_schedule_group_is_granted(self):
        """The watchdog's teardown deletes the per-issue schedule
        group, so the EC2 role must be able to call
        `scheduler:DeleteScheduleGroup` on it."""
        self.assertIn(
            '"scheduler:DeleteScheduleGroup"',
            self.body,
            "ec2_agent_policy must grant scheduler:DeleteScheduleGroup "
            "(the watchdog's teardown deletes the per-issue group — the "
            "PRIMARY cleanup path for the refresh chain).",
        )
        self.assertIn(
            "schedule-group/blitzlog-*-token-refresh-*",
            self.body,
            "scheduler:DeleteScheduleGroup Resource must be scoped to "
            "the per-issue refresh groups.",
        )

    def test_delete_schedule_on_child_schedules_is_granted(self):
        """`scheduler:DeleteSchedule` on the CHILD schedules is
        required in addition to `scheduler:DeleteScheduleGroup` on
        the group.

        EventBridge Scheduler authorises the group delete against
        every schedule it cascades over. With only the group grant,
        every teardown fails with:

          not authorized to perform: scheduler:DeleteSchedule on
          resource: arn:aws:scheduler:<region>:<acct>:
          schedule/<group>/* because no identity-based policy
          allows the scheduler:DeleteSchedule action

        The group then survives the run. Its fired one-time `at()`
        schedules linger (EventBridge Scheduler does not delete a
        one-time schedule after it fires), so the NEXT run for the
        same (env, repo, issue) fails its first `CreateSchedule`
        with `ConflictException` — the chain never starts and the
        agent's token dies 1 h after the launch-time mint.
        """
        self.assertIn(
            '"scheduler:DeleteSchedule"',
            self.body,
            "ec2_agent_policy must grant scheduler:DeleteSchedule on the "
            "child schedules — DeleteScheduleGroup authorises the cascade "
            "against them, so without it every teardown fails with "
            "AccessDenied and the group survives the run.",
        )
        self.assertIn(
            "schedule/blitzlog-*-token-refresh-*/*",
            self.body,
            "scheduler:DeleteSchedule Resource must be the per-issue "
            "refresh groups' schedule pattern "
            "(arn:aws:scheduler:*:*:schedule/<group>/*).",
        )
        self.assertNotRegex(
            self.body,
            r"scheduler:DeleteSchedule[\s\S]{0,200}Resource\s*=\s*\"\*\"",
            'scheduler:DeleteSchedule must NOT have Resource = "*" '
            "(scoped to the per-issue refresh groups; widening is a "
            "privilege creep).",
        )


if __name__ == "__main__":
    unittest.main()
