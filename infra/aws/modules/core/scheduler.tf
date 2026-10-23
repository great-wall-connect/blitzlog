# Issue #145 — EventBridge Scheduler role for the token-refresh chain.
#
# GitHub App installation tokens are capped to 1 h regardless of the
# requested `expires_at` (verified empirically — the 8 h default the
# Lambda historically requested is silently clamped). To make the 1 h
# cap operationally invisible, the Lambda schedules a chain of one-time
# EventBridge Scheduler events that re-mint the token every 50 min.
#
# The chain self-perpetuates from the Lambda side, so the only
# scheduler-side infrastructure we need is:
#
#   1. A role that EventBridge Scheduler can assume to invoke the
#      Lambda. The role's trust policy restricts the principal to
#      `scheduler.amazonaws.com`; the inline policy grants
#      `lambda:InvokeFunction` on the blitzlog handler function only.
#      (The blast-radius posture: a compromised scheduler role can only
#      invoke the ONE function it points at, with no other AWS API
#      access.)
#
# (Per-issue schedule groups are created dynamically by the Lambda on
# first `CreateSchedule` call, NOT provisioned here. See
# `lambda/handler.py::_refresh_schedule_group_name` for the naming
# convention; matching the per-issue SSM namespace shape. This means
# `terraform destroy` of the bootstrap stack no longer takes out
# running chain events — each instance's chain lives in its own
# group and is reaped by the watchdog at instance teardown.)
#
# The Lambda's IAM role gets `scheduler:CreateSchedule` /
# `scheduler:DeleteSchedule` (see iam.tf), so it can create the
# chain and clean up after itself. The EC2 host's role intentionally
# has NO scheduler permissions — the bootstrap is not involved in
# scheduling; the Lambda owns the entire chain.

resource "aws_iam_role" "scheduler_role" {
  name = "blitzlog-${var.environment}-scheduler-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        # EventBridge Scheduler. The trust policy restricts the role
        # to that service only — no other AWS principal can assume
        # it. Same-account invocations don't need an `aws:SourceAccount`
        # condition (EventBridge is a fully-managed service), but
        # adding it makes the trust policy explicit and resistant to
        # future cross-account paths EventBridge might add.
        Service = "scheduler.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_policy" {
  name = "blitzlog-${var.environment}-scheduler-policy"
  role = aws_iam_role.scheduler_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "lambda:InvokeFunction"
      # Narrowly scoped: the scheduler can only invoke the blitzlog
      # handler. If the function ARN ever leaks, the scheduler role
      # still can't reach SSM, S3, or any other service.
      Resource = aws_lambda_function.handler.arn
    }]
  })
}
