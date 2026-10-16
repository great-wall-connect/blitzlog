# Integration-test role for the watchdog token-refresh mechanism
# (issue #145, level 2 of the 3-level testing convention). The
# `.github/workflows/integration-test.yml` workflow assumes this
# role via OIDC to drive the real-AWS round-trip test
# (`tests/test_watchdog_credential_poll_e2e.py::test_watchdog_poll_loop_rewrites_against_real_ssm`).
#
# The trust policy is INTENTIONALLY narrow: only the
# `integration-test.yml` workflow can assume it (the
# `token.actions.githubusercontent.com:sub` condition matches
# `repo:great-wall-connect/blitzlog:workflow:integration-test.yml:ref:refs/heads/*`).
# The deploy and packer roles cannot assume this role even by
# accident, and a future PR cannot add a workflow that silently
# gains the integration-test SSM permissions.
#
# Permissions are scoped to a UUID-suffixed SSM prefix
# (`/blitzlog/<env>/ephemeral/github-token-integration-test-*`) so
# the test's writes cannot collide with any real prod params, and
# a future "I wonder what would happen if I changed X in prod"
# refactor cannot use this role to touch real params.
#
# The admin-SSO escape hatch matches the deploy and packer
# roles' shape so a local operator who has AdministratorAccess can
# still assume this role for ad-hoc debugging without going
# through GitHub Actions.

resource "aws_iam_role" "integration_test" {
  name        = "blitzlog-integration-test-role"
  description = "Integration-test role for the watchdog token-refresh mechanism (assumed via GitHub OIDC, scoped to the test SSM prefix)"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # GitHub Actions OIDC, narrowed to the integration-test
        # workflow file only. The `job_workflow_ref` claim is the
        # GitHub OIDC token's workflow-specific identifier; for a
        # token issued by `integration-test.yml` it is
        # `great-wall-connect/blitzlog/.github/workflows/integration-test.yml@refs/heads/<branch>`.
        # The deploy / packer / release / terraform-apply-dev /
        # retention workflows all have a DIFFERENT
        # `job_workflow_ref` (e.g.
        # `great-wall-connect/blitzlog/.github/workflows/release.yml@refs/heads/main`)
        # and their tokens are denied by this condition.
        #
        # Why `job_workflow_ref` and not `sub`? GitHub's `sub`
        # claim for `workflow_dispatch` and `schedule` events is
        # the same push-event format
        # (`repo:OWNER@INTID/REPO@INTID:ref:refs/heads/BRANCH`) —
        # it does NOT include the workflow filename. So a `sub`
        # condition cannot narrow to a specific workflow. The
        # `job_workflow_ref` claim was added by GitHub specifically
        # for this kind of workflow-narrow OIDC trust policy. See
        # https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/about-security-hardening-with-openid-connect
        # for the full claim catalogue.
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.github.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          }
          StringLike = {
            "token.actions.githubusercontent.com:job_workflow_ref" = "great-wall-connect/blitzlog/.github/workflows/integration-test.yml@refs/heads/*"
          }
        }
      },
      {
        # Admin-SSO escape hatch for local operators. Mirrors the
        # deploy-role and packer-role patterns so a local operator
        # who has AdministratorAccess can still assume this role
        # for ad-hoc integration debugging without going through
        # GitHub Actions.
        Effect = "Allow"
        Principal = {
          AWS = data.aws_iam_role.admin_sso.arn
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "aws:RequestedRegion" = var.aws_region
          }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy" "integration_test" {
  name = "blitzlog-integration-test-policy"
  role = aws_iam_role.integration_test.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # SSM Parameter Store: Get/Put/Delete on the test prefix
        # only. The test mints a UUID-suffixed param under
        # /blitzlog/<env>/ephemeral/github-token-integration-test-*
        # and cleans it up in `finally`; the test prefix
        # deliberately does NOT overlap with any real prod
        # param namespace. A future contributor cannot use this
        # role to touch real prod params.
        #
        # The `dev` and `prod` envs are both listed because the
        # integration test reads `BLITZLOG_ENV` from the env (or
        # defaults to `dev`); the workflow sets it explicitly to
        # `dev` to keep the prod env out of the test's blast
        # radius, but the role grants both in case a future
        # operator wants to run Level 2 against prod (Level 3
        # is the only thing that actually needs prod-real
        # credentials; Level 2 is parameter-content-only).
        Sid    = "IntegrationTestSSM"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:PutParameter",
          "ssm:DeleteParameter",
        ]
        Resource = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/blitzlog/dev/ephemeral/github-token-integration-test-*",
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/blitzlog/prod/ephemeral/github-token-integration-test-*",
        ]
      },
    ]
  })
}

output "integration_test_role_arn" {
  description = "ARN of the blitzlog-integration-test-role. The integration-test workflow assumes this role via OIDC."
  value       = aws_iam_role.integration_test.arn
}
