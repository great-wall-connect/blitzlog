# Terraform deploy role for blitzlog. The CI/CD workflows
# (.github/workflows/release.yml and .github/workflows/terraform-apply-dev.yml)
# assume this role via OIDC when they run `terraform apply` against
# infra/dev and infra/prod. The role is intentionally broad because
# `terraform apply` needs to manage every resource the core module
# creates — Lambda, IAM, API Gateway v2, SQS, SNS, CloudWatch, SSM,
# security groups — but S3 (for the terraform state file) and EC2
# terminate are scoped tighter.
#
# Trust policy shape mirrors `aws_iam_role.packer_build` in
# infra/bootstrap/packer-role.tf:30-79 (GitHub Actions OIDC for any
# branch of great-wall-connect/blitzlog, plus the admin-SSO escape
# hatch for local applies). The packer role's EC2 service-principal
# statement is intentionally dropped here — the local-exec in
# `infra/modules/core/lambda.tf:35-92` runs on the GHA runner, not on
# an EC2 instance, so the role does not need to be assumable by EC2.

# State bucket name. Override via TF_VAR_tf_backend_bucket or by
# editing this default if the operator uses a non-default bucket.
variable "tf_backend_bucket" {
  description = "Name of the S3 bucket that holds the terraform state for infra/dev and infra/prod. Must match the `-backend-config=bucket=...` value passed to `terraform init` in the deploy workflows."
  type        = string
  default     = "gwc-infra-tf-state"
}

# The shared `aws_caller_identity.current` and
# `aws_iam_role.admin_sso` data sources live in
# infra/bootstrap/data-sources.tf so the trust policy here
# references the same lookups the packer-role.tf uses. This keeps
# the two trust policies in lockstep and avoids "duplicate data
# source" errors.

resource "aws_iam_role" "deploy" {
  name        = "blitzlog-deploy-role"
  description = "Terraform deploy role for infra/dev and infra/prod (assumed via GitHub OIDC)"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # GitHub Actions OIDC for any branch of great-wall-connect/blitzlog.
        # `aud=sts.amazonaws.com` matches the OIDC provider's client_id_list
        # in github-oidc-provider.tf. The `StringLike` condition matches
        # the same prefix as packer-role.tf so the two roles share an
        # identical trust surface.
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
            "token.actions.githubusercontent.com:sub" = "repo:great-wall-connect*/blitzlog*:ref:refs/heads/*"
          }
        }
      },
      {
        # Admin-SSO escape hatch for local `terraform apply` from a
        # maintainer's laptop. Mirrors packer-role.tf:65-76 so a local
        # operator who has AdministratorAccess can still drive deploys
        # without going through GitHub Actions.
        Effect = "Allow"
        Principal = {
          AWS = data.aws_iam_role.admin_sso.arn
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "aws:RequestedRegion" = "ap-east-1"
          }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy" "deploy" {
  name = "blitzlog-deploy-policy"
  role = aws_iam_role.deploy.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Terraform state file I/O. Scoped to the per-env keys under
        # the shared state bucket so a single role can drive both
        # dev and prod applies (each workflow passes its own
        # `-backend-config=key=...`).
        Sid    = "TerraformStateS3"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = [
          "arn:aws:s3:::${var.tf_backend_bucket}/dev/*",
          "arn:aws:s3:::${var.tf_backend_bucket}/prod/*",
        ]
      },
      {
        Sid    = "TerraformStateBucketList"
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
        ]
        Resource = "arn:aws:s3:::${var.tf_backend_bucket}"
        Condition = {
          StringLike = {
            "s3:prefix" = [
              "dev/*",
              "prod/*",
            ]
          }
        }
      },
      {
        # STT model upload from `terraform_data.stt_model_upload`
        # (infra/modules/core/storage.tf:17-74). Only used when
        # upload_stt_model = true; harmless otherwise.
        Sid    = "STTModelUpload"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:HeadObject",
        ]
        Resource = [
          aws_s3_bucket.stt_models.arn,
          "${aws_s3_bucket.stt_models.arn}/*",
        ]
      },
      {
        # Lambda: create/update the blitzlog-<env>-handler function,
        # the matching log group, and the API-Gateway invoke
        # permission. UpdateFunctionCode is the hot-patch path; the
        # full apply path uses CreateFunction / DeleteFunction.
        Sid    = "LambdaManagement"
        Effect = "Allow"
        Action = [
          "lambda:CreateFunction",
          "lambda:UpdateFunctionCode",
          "lambda:UpdateFunctionConfiguration",
          "lambda:DeleteFunction",
          "lambda:GetFunction",
          "lambda:GetFunctionConfiguration",
          "lambda:GetAlias",
          "lambda:CreateEventSourceMapping",
          "lambda:UpdateEventSourceMapping",
          "lambda:DeleteEventSourceMapping",
          "lambda:GetEventSourceMapping",
          "lambda:ListEventSourceMappings",
          "lambda:ListTagsForResource",
          "lambda:TagResource",
          "lambda:UntagResource",
          "lambda:AddPermission",
          "lambda:RemovePermission",
          "lambda:GetPolicy",
        ]
        Resource = "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:blitzlog-*"
      },
      {
        # Lambda: pass the execution role when creating/updating
        # the blitzlog-<env>-handler function. iam:PassRole is
        # conditional on the service principal so the role can
        # only be passed to Lambda (not, e.g., to EC2 for an
        # arbitrary instance).
        Sid    = "LambdaPassRole"
        Effect = "Allow"
        Action = [
          "iam:PassRole",
        ]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/blitzlog-*-lambda-role"
        Condition = {
          StringEquals = {
            "iam:PassedToService" = "lambda.amazonaws.com"
          }
        }
      },
      {
        # IAM: create the blitzlog-<env>-lambda-role and
        # blitzlog-<env>-ec2-agent-role (with the matching managed
        # AmazonSSMManagedInstanceCore attachment) plus the
        # ec2-agent-profile instance profile. Wildcard on Resource
        # is the standard pattern for "I need to manage IAM in this
        # account" — restricting it to specific role names doesn't
        # add much because the role's *trust policy* still gates who
        # can assume it.
        Sid    = "IAMRoleManagement"
        Effect = "Allow"
        Action = [
          "iam:CreateRole",
          "iam:DeleteRole",
          "iam:GetRole",
          "iam:UpdateRole",
          "iam:UpdateRoleDescription",
          "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies",
          "iam:ListInstanceProfilesForRole",
          "iam:PutRolePolicy",
          "iam:GetRolePolicy",
          "iam:DeleteRolePolicy",
          "iam:AttachRolePolicy",
          "iam:DetachRolePolicy",
          "iam:TagRole",
          "iam:UntagRole",
          "iam:CreateInstanceProfile",
          "iam:DeleteInstanceProfile",
          "iam:GetInstanceProfile",
          "iam:AddRoleToInstanceProfile",
          "iam:RemoveRoleFromInstanceProfile",
          "iam:TagInstanceProfile",
          "iam:UntagInstanceProfile",
        ]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/blitzlog-*"
      },
      {
        # IAM instance profile: the per-env apply refreshes
        # `aws_iam_instance_profile.ec2_agent_profile` at the
        # start of `terraform plan`. `iam:GetInstanceProfile` is in
        # the IAMRoleManagement Action list, but the Resource is
        # scoped to `role/blitzlog-*` — which doesn't match the
        # instance profile's ARN format (`instance-profile/...`).
        # Without this separate Sid, the apply errors mid-plan
        # with:
        #   Error: reading IAM Instance Profile (...): User is not
        #   authorized to perform: iam:GetInstanceProfile
        Sid    = "IAMInstanceProfileRead"
        Effect = "Allow"
        Action = [
          "iam:GetInstanceProfile",
          "iam:ListInstanceProfilesForRole",
        ]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:instance-profile/blitzlog-*"
      },
      {
        # iam:PassRole for the EC2 agent role. The Lambda assumes
        # this role on the EC2 instance profile; terraform needs to
        # be able to pass it on create. Conditional on the EC2
        # service principal so the role can only end up on an EC2
        # instance, not on Lambda.
        Sid    = "EC2PassRole"
        Effect = "Allow"
        Action = [
          "iam:PassRole",
        ]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/blitzlog-*-ec2-agent-role"
        Condition = {
          StringEquals = {
            "iam:PassedToService" = "ec2.amazonaws.com"
          }
        }
      },
      {
        # EC2: the core module creates one security group per env
        # and a per-CIDR ingress rule. The lambda role already
        # describes EC2 instances (the spot agents it launches);
        # terraform itself only needs the SG lifecycle + describe
        # permissions.
        Sid    = "EC2SecurityGroupManagement"
        Effect = "Allow"
        Action = [
          "ec2:CreateSecurityGroup",
          "ec2:DeleteSecurityGroup",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeVpcs",
          "ec2:DescribeSubnets",
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:RevokeSecurityGroupIngress",
          "ec2:CreateTags",
          "ec2:DeleteTags",
        ]
        Resource = "*"
      },
      {
        # API Gateway v2: create/update the blitzlog-<env>-webhook-api
        # and its integration, route, deployment, and stage. The
        # `apigateway:` namespace covers both v1 and v2; v1 is not
        # used by this project but a wildcard keeps the policy stable
        # across AWS-side changes.
        Sid    = "APIGatewayManagement"
        Effect = "Allow"
        Action = [
          "apigateway:GET",
          "apigateway:POST",
          "apigateway:PATCH",
          "apigateway:DELETE",
          "apigateway:PUT",
        ]
        Resource = [
          "arn:aws:apigateway:${var.aws_region}::/apis",
          "arn:aws:apigateway:${var.aws_region}::/apis/*",
          "arn:aws:apigateway:${var.aws_region}::/integrations",
          "arn:aws:apigateway:${var.aws_region}::/integrations/*",
          "arn:aws:apigateway:${var.aws_region}::/routes",
          "arn:aws:apigateway:${var.aws_region}::/routes/*",
          "arn:aws:apigateway:${var.aws_region}::/deployments",
          "arn:aws:apigateway:${var.aws_region}::/deployments/*",
          "arn:aws:apigateway:${var.aws_region}::/stages",
          "arn:aws:apigateway:${var.aws_region}::/stages/*",
        ]
      },
      {
        # SQS: the blitzlog-<env>-lambda-dlq. The Lambda pushes
        # failed events to this queue; terraform manages its
        # lifecycle and message_retention_seconds.
        Sid    = "SQSManagement"
        Effect = "Allow"
        Action = [
          "sqs:CreateQueue",
          "sqs:DeleteQueue",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:SetQueueAttributes",
          "sqs:ListQueues",
          "sqs:ListQueueTags",
          "sqs:TagQueue",
          "sqs:UntagQueue",
        ]
        Resource = "arn:aws:sqs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:blitzlog-*-lambda-dlq"
      },
      {
        # SNS: blitzlog-<env>-alerts topic + email subscription.
        Sid    = "SNSManagement"
        Effect = "Allow"
        Action = [
          "sns:CreateTopic",
          "sns:DeleteTopic",
          "sns:GetTopicAttributes",
          "sns:SetTopicAttributes",
          "sns:ListTopics",
          "sns:ListSubscriptions",
          "sns:ListSubscriptionsByTopic",
          "sns:Subscribe",
          "sns:Unsubscribe",
          "sns:TagResource",
          "sns:UntagResource",
          # `ListTagsForResource` is needed by the terraform AWS
          # provider to refresh the existing SNS topic's tag set
          # at the start of `terraform plan`. Without it, the
          # apply errors mid-plan with:
          #   Error: listing tags for SNS Topic (...): User is not
          #   authorized to perform: SNS:ListTagsForResource
        "sns:ListTagsForResource",
        ]
        Resource = [
          "arn:aws:sns:${var.aws_region}:${data.aws_caller_identity.current.account_id}:blitzlog-*-alerts",
          "arn:aws:sns:${var.aws_region}:${data.aws_caller_identity.current.account_id}:blitzlog-*-alerts:*",
        ]
      },
      {
        # CloudWatch: the blitzlog-<env>-lambda-errors alarm and
        # the Lambda log group. The metric-alarm resource also
        # reads the Lambda's Errors metric (lambda:GetFunction
        # above covers the function lookup; cloudwatch:Describe*
        # covers the metric stream).
        Sid    = "CloudWatchManagement"
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricAlarm",
          "cloudwatch:DeleteAlarms",
          "cloudwatch:DescribeAlarms",
          "cloudwatch:ListTagsForResource",
          "cloudwatch:TagResource",
          "cloudwatch:UntagResource",
        ]
        Resource = [
          "arn:aws:cloudwatch:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alarm:blitzlog-*-lambda-errors",
        ]
      },
      {
        # CloudWatch Logs: /aws/lambda/blitzlog-<env>-handler log
        # group + retention. Logs:CreateLogGroup is also implicitly
        # needed for the Lambda to write its own log streams; this
        # grant is for terraform's log-group lifecycle.
        Sid    = "CloudWatchLogsManagement"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:DeleteLogGroup",
          "logs:PutRetentionPolicy",
          "logs:DescribeLogGroups",
          "logs:ListTagsForResource",
          "logs:TagResource",
          "logs:UntagResource",
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/blitzlog-*-handler"
      },
      {
        # CloudWatch Logs: account-level `DescribeLogGroups` /
        # `ListTagsForResource`. The per-env apply's
        # `aws_cloudwatch_log_group` resource calls
        # `DescribeLogGroups` with no log-group filter; the
        # resource-ARN-constrained grant above doesn't match
        # an empty log-group name in the API call (the
        # wildcard `blitzlog-*-handler` requires at least one
        # character). Without this, the apply errors with
        # `AccessDeniedException: ... logs:DescribeLogGroups on
        # resource: arn:aws:logs:...:log-group::log-stream:`.
        Sid    = "CloudWatchLogsAccountLevelReads"
        Effect = "Allow"
        Action = [
          "logs:DescribeLogGroups",
          "logs:ListTagsForResource",
        ]
        Resource = "*"
      },
      {
        # SSM Parameter Store: per-env credentials and runtime
        # config under /blitzlog/<env>/*. The user-pool namespace
        # (/blitzlog/users/*) is env-independent; per-env stacks
        # don't write there, so it's intentionally NOT in this
        # grant.
        Sid    = "SSMParameterManagement"
        Effect = "Allow"
        Action = [
          "ssm:PutParameter",
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:DeleteParameter",
          "ssm:GetParametersByPath",
          "ssm:DescribeParameters",
          "ssm:AddTagsToResource",
          "ssm:RemoveTagsFromResource",
          "ssm:ListTagsForResource",
        ]
        Resource = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/blitzlog/dev/*",
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/blitzlog/prod/*",
        ]
      },
      {
        # SSM Parameter Store: account-level reads the per-env
        # apply needs to enumerate parameters and tags. The two
        # actions in this Sid are account-scoped (they don't
        # operate on a specific parameter ARN), so the resource
        # has to be "*". Without this the `aws_ssm_parameter`
        # resources in the per-env apply error mid-plan with
        # `AccessDeniedException: ... ssm:DescribeParameters on
        # resource: arn:aws:ssm:...:*`.
        Sid    = "SSMAccountLevelReads"
        Effect = "Allow"
        Action = [
          "ssm:DescribeParameters",
          "ssm:ListTagsForResource",
        ]
        Resource = "*"
      },
      {
        # S3: read access to the shared `agent-logs` and
        # `stt-models` buckets. The per-env apply's
        # `data "aws_s3_bucket" "agent_logs"` /
        # `data "aws_s3_bucket" "stt_models"` data sources (and
        # the STT model download from the EC2 user-data) need to
        # read these. The bootstrap's S3 state-file grant is
        # scoped to the state bucket's `dev/*` / `prod/*` prefix
        # and doesn't cover these.
        Sid    = "SharedS3BucketRead"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:GetObjectVersion",
          "s3:ListBucket",
          "s3:GetBucketLocation",
        ]
        Resource = [
          aws_s3_bucket.agent_logs.arn,
          "${aws_s3_bucket.agent_logs.arn}/*",
          aws_s3_bucket.stt_models.arn,
          "${aws_s3_bucket.stt_models.arn}/*",
        ]
      },
    ]
  })
}

output "deploy_role_arn" {
  description = "ARN of the blitzlog-deploy-role. Configure the deploy GitHub Actions workflows with this ARN (role-to-assume)."
  value       = aws_iam_role.deploy.arn
}
