# Analogue of infra/aws/modules/core/iam.tf.
#
# Alicloud RAM (Resource Access Management) is the AWS IAM analogue.
# The policy shape is similar to IAM but the trust-policy `Service`
# principals and the action names differ:
#
#   * AWS `lambda.amazonaws.com` -> Alicloud `fc.aliyuncs.com`
#   * AWS `ec2.amazonaws.com`    -> Alicloud `ecs.aliyuncs.com`
#   * AWS `ssm:GetParameter`     -> Alicloud `kms:GetSecretValue`
#   * AWS `s3:GetObject`         -> Alicloud `oss:GetObject`
#   * AWS `s3:ListBucket`        -> Alicloud `oss:ListBuckets` +
#                                   `oss:GetBucketAcl`
#   * AWS `ec2:RunInstances`     -> Alicloud `ecs:RunInstances`
#   * AWS `iam:PassRole`         -> Alicloud `ram:PassRole`
#   * AWS `logs:CreateLogGroup`  -> Alicloud `log:CreateLogGroup`
#                                   (singular — Aliyun SLS uses `log:`)
#   * AWS `sqs:SendMessage`      -> Alicloud `mns:SendMessage`
#   * AWS `scheduler:*`          -> N/A in this PR (no EventBridge
#                                   Scheduler equivalent). The
#                                   `BLITZLOG_PROVIDER=alicloud` path
#                                   holds a 1 h token only. Follow-up
#                                   issue for the Aliyun scheduler
#                                   equivalent of #145.
#
# Three roles mirror the AWS side:
#   * alicloud_ram_role.lambda_role   - FC execution role
#   * alicloud_ram_role.ecs_agent_role - ECS instance role
# Per-user bot pool grants use literal `/blitzlog/users/*` ARNs
# (mirroring the AWS-side literal in iam.tf:118-122).

# Function Compute execution role.
resource "alicloud_ram_role" "lambda_role" {
  role_name   = "blitzlog-${var.environment}-function-role"
  description = "FC execution role for blitzlog (env: ${var.environment})"
  assume_role_policy_document = jsonencode({
    Version = "1"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = ["fc.aliyuncs.com"]
      }
      Action = "sts:AssumeRole"
    }]
  })
  force = true
}

# Function Compute policy. Mirrors the inline policy in
# infra/aws/modules/core/iam.tf:16-214 with the action renames above.
# The `scheduler:CreateSchedule` and `iam:PassRole` Sids are dropped
# (no Alicloud equivalent in this PR). The `iam:GetInstanceProfile`
# Sid becomes `ram:GetInstanceProfile`.
resource "alicloud_ram_policy" "lambda_policy" {
  policy_name = "blitzlog-${var.environment}-function-policy"
  policy_document = jsonencode({
    Version = "1"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecs:RunInstances",
          "ecs:DescribeImages",
          "ecs:DescribeInstances",
          "ecs:CreateTags",
          "ecs:DescribeVSwitches",
          "ecs:DescribeSecurityGroups",
          "ecs:DescribeSpotPriceHistory",
        ]
        Resource = ["*"]
      },
      {
        Effect = "Allow"
        Action = [
          "ecs:TerminateInstances",
        ]
        Resource = ["*"]
        Condition = {
          StringEquals = {
            "ecs:ResourceTag/Purpose"     = "autonomous-agent"
            "ecs:ResourceTag/Environment" = var.environment
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "kms:GetSecretValue",
        ]
        # Env-scoped: the FC reads the per-env GitHub App creds, the
        # STT config, the OpenCode API key, and the per-env ephemeral
        # root. The literal ARNs mirror the AWS-side
        # `aws_ssm_parameter.<name>.arn` references in iam.tf:55-67.
        Resource = [
          alicloud_kms_secret.github_app_id.arn,
          alicloud_kms_secret.github_app_private_key.arn,
          alicloud_kms_secret.github_app_installation_id.arn,
          alicloud_kms_secret.github_webhook_secret.arn,
          # Per-user bot pools and per-user local LLM config are
          # env-independent under /blitzlog/users/ — same posture as
          # the AWS-side iam.tf:118-122.
          "acs:kms:${var.alicloud_region}:${data.alicloud_account.current.id}:secret/blitzlog/users",
          "acs:kms:${var.alicloud_region}:${data.alicloud_account.current.id}:secret/blitzlog/users/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "log:CreateLogGroup",
          "log:CreateLogStream",
          "log:PutLogEvents",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ram:PassRole",
        ]
        Resource = alicloud_ram_role.ecs_agent_role.arn
      },
      {
        Effect = "Allow"
        Action = [
          "ram:GetInstanceProfile",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "mns:SendMessage",
        ]
        Resource = local.mns_dlq_arn
      },
      {
        Effect = "Allow"
        Action = [
          "kms:GetSecretValue",
        ]
        Resource = [
          alicloud_kms_secret.opencode_api_key.arn,
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "oss:GetObject",
          "oss:PutObject",
          "oss:DeleteObject",
        ]
        Resource = [
          "${local.agent_logs_bucket_arn}/bot-pool-locks/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "oss:GetBucketAcl",
        ]
        Resource = local.agent_logs_bucket_arn
        Condition = {
          StringLike = {
            "oss:prefix" = "bot-pool-locks/*"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "oss:PutObject",
        ]
        Resource = "${local.agent_logs_bucket_arn}/user-data/*"
      },
      {
        Effect = "Allow"
        Action = [
          "kms:CreateSecret",
        ]
        Resource = "acs:kms:${var.alicloud_region}:${data.alicloud_account.current.id}:secret/${local.ssm_ephemeral_root}/*"
      },
      {
        Effect = "Allow"
        Action = [
          "oss:GetObject",
        ]
        # The FC function fetches its own code zip from OSS at cold
        # start (see alicloud_oss_bucket_object.fc_code in
        # function_compute.tf and the
        # code{oss_bucket_name=…,oss_object_name=…} block on
        # alicloud_fcv3_function.handler). The grant is restricted to
        # the per-env key so the role cannot read other paths under
        # the agent-logs bucket. The deploy-time AK that uploads the
        # zip is the bucket owner; it does not need a RAM grant.
        Resource = "${local.agent_logs_bucket_arn}/${local.fc_code_object_key}"
      },
    ]
  })
}

resource "alicloud_ram_role_policy_attachment" "lambda_policy" {
  policy_name = alicloud_ram_policy.lambda_policy.policy_name
  policy_type = "Custom"
  role_name   = alicloud_ram_role.lambda_role.role_name
}

# ECS instance role.
resource "alicloud_ram_role" "ecs_agent_role" {
  role_name   = "blitzlog-${var.environment}-ecs-agent-role"
  description = "ECS execution role for the blitzlog agent (env: ${var.environment})"
  assume_role_policy_document = jsonencode({
    Version = "1"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = ["ecs.aliyuncs.com"]
      }
      Action = "sts:AssumeRole"
    }]
  })
  force = true
}

# ECS agent policy. Mirrors the inline policy in
# infra/aws/modules/core/iam.tf:231-344 with the action renames. The
# `scheduler:DeleteScheduleGroup` Sid (issue #145 watchdog cleanup)
# is dropped — no Alicloud scheduler equivalent in this PR.
resource "alicloud_ram_policy" "ecs_agent_policy" {
  policy_name = "blitzlog-${var.environment}-ecs-agent-policy"
  policy_document = jsonencode({
    Version = "1"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecs:TerminateInstances",
        ]
        Resource = ["*"]
        Condition = {
          StringEquals = {
            "ecs:ResourceTag/Purpose"     = "autonomous-agent"
            "ecs:ResourceTag/Environment" = var.environment
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "oss:GetObject",
          "oss:PutObject",
        ]
        Resource = [
          "${local.agent_logs_bucket_arn}/${var.environment}/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "oss:GetObject",
        ]
        # user-data/* is the function→ECS handoff for the bootstrap
        # script. The function writes one ephemeral script per ECS
        # launch to oss://<bucket>/user-data/... and the matching ECS
        # reads it via the user-data downloader
        # (_build_oss_downloader_script does `ossutil cp`). The
        # <env>/* restriction above is for *run logs*, not user-data.
        # The script contains no secrets (those come from KMS in the
        # next step), so widening read access across all ECS instances
        # is intentional.
        Resource = [
          "${local.agent_logs_bucket_arn}/user-data/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "oss:GetBucketAcl",
        ]
        Resource = local.agent_logs_bucket_arn
        Condition = {
          StringLike = {
            "oss:prefix" = "${var.environment}/*"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "kms:GetSecretValue",
        ]
        Resource = [
          alicloud_kms_secret.opencode_api_key.arn,
          alicloud_kms_secret.stt_api_url.arn,
          alicloud_kms_secret.stt_api_key.arn,
          alicloud_kms_secret.stt_model.arn,
          alicloud_kms_secret.stt_language.arn,
          alicloud_kms_secret.stt_models_bucket.arn,
          "acs:kms:${var.alicloud_region}:${data.alicloud_account.current.id}:secret/${local.ssm_ephemeral_root}/*",
          # Per-user local LLM config is env-independent — same
          # posture as the AWS-side iam.tf:323.
          "acs:kms:${var.alicloud_region}:${data.alicloud_account.current.id}:secret/blitzlog/users/*/local-llm/*",
          # Per-user git author identity (issue #119). Same broad
          # `users/*/git-author/*` shape as the local-llm grant
          # above — narrow enough to exclude the telegram bot tokens
          # and allowed-user-id params, which the ECS instance role
          # must NOT be able to read. The two git-author params are
          # non-secret String types whose values appear on every
          # agent commit.
          "acs:kms:${var.alicloud_region}:${data.alicloud_account.current.id}:secret/blitzlog/users/*/git-author/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "oss:GetObject",
        ]
        Resource = "${local.stt_models_bucket_arn}/*"
      },
    ]
  })
}

resource "alicloud_ram_role_policy_attachment" "ecs_agent_policy" {
  policy_name = alicloud_ram_policy.ecs_agent_policy.policy_name
  policy_type = "Custom"
  role_name   = alicloud_ram_role.ecs_agent_role.role_name
}

# Instance profile is the Alicloud analogue of
# aws_iam_instance_profile in iam.tf:346-349.
#
# Aliyun does not have a separate instance profile resource — the
# RAM role is attached directly to the ECS instance via the
# `RamRoleName` parameter on RunInstances. We just expose the role
# name as the equivalent output so the function env block can pass
# it through.
# (See `alicloud_ram_role.ecs_agent_role.role_name` usage in
# `AlicloudCompute.run_instances`.)
