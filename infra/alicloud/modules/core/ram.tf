# Analogue of infra/aws/modules/core/iam.tf.
#
# Alicloud RAM (Resource Access Management) is the AWS IAM analogue.
# The policy shape is similar to IAM but the trust-policy `Service`
# principals and the action names differ:
#
#   * AWS `lambda.amazonaws.com` -> Alicloud `fc.aliyuncs.com`
#   * AWS `ec2.amazonaws.com`    -> Alicloud `ecs.aliyuncs.com`
#   * AWS `ssm:GetParameter`     -> Alicloud `oos:GetSecretParameter`
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
# The Alicloud secret store is the OOS Parameter Store, so the
# secret grants are `oos:*` on
# `acs:oos:<region>:<account>:secretparameter/<name>`. The OOS
# resource exposes no ARN attribute, so the grants spell the ARNs
# out (the parameter names live in locals.tf).
#
# Three roles mirror the AWS side:
#   * alicloud_ram_role.lambda_role   - FC execution role
#   * alicloud_ram_role.ecs_agent_role - ECS instance role
# Per-user bot pool grants use literal `blitzlog/users/*` ARNs
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
        # The FC service itself (the function's execution role) and
        # the API Gateway, which assumes this role to invoke the
        # function — it is the `arn_role` on the API's FC backend.
        # Without the gateway in the trust policy its AssumeRole is
        # rejected with "Function Compute AssumeRole failed: status=403
        # NoPermission" and every invocation 403s.
        #
        # Only the "Service" principal type is accepted here: an
        # account-root principal is rejected with
        # "MalformedPolicyDocument — Unsupported principal type".
        Service = ["fc.aliyuncs.com", "apigateway.aliyuncs.com"]
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
  # Alicloud caps a custom policy at 5 versions, and the provider
  # creates a new version on every change — without this the fifth
  # apply fails with "LimitExceeded.Policy.Version: The count of
  # policy version is limited to 5".
  rotate_strategy = "DeleteOldestNonDefaultVersionWhenLimitExceeded"
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
          "oos:GetSecretParameter",
        ]
        # Env-scoped: the FC reads the per-env GitHub App creds, the
        # STT config, the OpenCode API key, and the per-env ephemeral
        # root. The literal ARNs mirror the AWS-side
        # `aws_ssm_parameter.<name>.arn` references in iam.tf:55-67.
        Resource = [
          "${local.oos_secretparameter_arn}${local.oos_github_app_id_name}",
          "${local.oos_secretparameter_arn}${local.oos_github_app_private_key_name}",
          "${local.oos_secretparameter_arn}${local.oos_github_app_installation_name}",
          "${local.oos_secretparameter_arn}${local.oos_github_webhook_secret_name}",
          # Per-user bot pools and per-user local LLM config are
          # env-independent under blitzlog/users/ — same posture as
          # the AWS-side iam.tf:118-122.
          "${local.oos_secretparameter_arn}blitzlog/users",
          "${local.oos_secretparameter_arn}blitzlog/users/*",
          # The Packer-baked agent image id, published by
          # infra/alicloud/packer/scripts-ecs-ubuntu/05-publish-ami-id.sh.
          # The FC reads it at launch time to pick the image the agent
          # instance boots from (lambda/ec2.py::get_agent_ami). The
          # AWS-side analogue is the ssm:GetParameter grant on
          # `blitzlog/<env>/agent-ami-id-docker-ubuntu` in iam.tf:66.
          "${local.oos_secretparameter_arn}${local.oos_root}/agent-ami-id-ecs-ubuntu",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "oos:GetSecretParametersByPath",
        ]
        # The FC lists the per-user bot-pool + local-LLM config under
        # blitzlog/users/<login>/... (lambda/bot_pool.py::list_bot_pool)
        # — the OOS analogue of the AWS-side `ssm:GetParametersByPath`
        # grant. The KMS implementation had no equivalent: it listed
        # the whole store with `ListSecrets` and filtered client-side,
        # and that verb was never granted.
        Resource = [
          "${local.oos_secretparameter_arn}blitzlog/users/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "fc:InvokeFunction",
        ]
        # The API Gateway invokes the function with this role's
        # credentials (the `arn_role` on the API's FC backend), so
        # the role needs the invoke verb on the function itself.
        # Without it the gateway's call is rejected with
        # "the caller is not authorized to perform 'fc:InvokeFunction'".
        Resource = "acs:fc:${var.alicloud_region}:${data.alicloud_account.current.id}:functions/${local.fc_function_name}"
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
          "oos:GetSecretParameter",
        ]
        Resource = [
          "${local.oos_secretparameter_arn}${local.oos_opencode_api_key_name}",
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
          "oos:CreateSecretParameter",
          "oos:UpdateSecretParameter",
          "oos:DeleteSecretParameter",
        ]
        # The FC writes the per-issue ephemeral GitHub token here at
        # launch time (lambda/ec2.py) and refreshes it in place
        # (lambda/handler.py).
        Resource = "${local.oos_secretparameter_arn}${local.oos_ephemeral_root}/*"
      },
      {
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
        ]
        # The parameters are encrypted with the per-env CMK. OOS
        # decrypts them server-side, but Alicloud's own docs are
        # inconsistent about whether the caller also needs the KMS
        # key grant (the RAM dependent-action table lists only
        # `oos:TLSVersion`, the API prose mentions a KMS action), so
        # the grant is here: narrowly scoped to the one CMK, and
        # harmless if OOS decrypts with its own credentials.
        Resource = "acs:kms:${var.alicloud_region}:${data.alicloud_account.current.id}:key/${var.kms_key_id}"
      },
      {
        Effect = "Allow"
        Action = [
          "kms:GetSecretValue",
        ]
        # Each OOS encrypted parameter is a KMS SECRET named
        # `secret/oos/<parameter path>` — OOS implements its
        # encrypted parameter kind on top of KMS Secrets Manager, so
        # reading one needs `kms:GetSecretValue` on that secret.
        # `kms:Decrypt` is the action for CMKs (`key/...`) and does
        # NOT apply to a secret resource.
        #
        # Without this grant every read fails with
        # `Kms.SecretFailed` / `Forbidden.NoPermission` ("This
        # operation for acs:kms:<region>:<account>:secret/oos/... is
        # not allowed"), which the deploy legs read as "the
        # parameter is missing or empty" — it is neither.
        Resource = [
          "acs:kms:${var.alicloud_region}:${data.alicloud_account.current.id}:secret/oos/blitzlog/${var.environment}/*",
          "acs:kms:${var.alicloud_region}:${data.alicloud_account.current.id}:secret/oos/blitzlog/users/*",
        ]
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
  # Alicloud caps a custom policy at 5 versions, and the provider
  # creates a new version on every change — without this the fifth
  # apply fails with "LimitExceeded.Policy.Version: The count of
  # policy version is limited to 5".
  rotate_strategy = "DeleteOldestNonDefaultVersionWhenLimitExceeded"
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
          "oss:PutObject",
          # Issue #125: the watchdog releases the bot-pool lock with
          # `blitzlog_oss_rm` when the agent container exits
          # (infra/alicloud/packer/scripts-ecs-ubuntu/watchdog.sh).
          # Without oss:DeleteObject the lock would never be released
          # and the same bot could not be picked up by the next run.
          # The AWS-side analogue is the s3:DeleteObject grant on
          # bot-pool-locks/* in infra/aws/modules/core/iam.tf:128.
          "oss:DeleteObject",
        ]
        Resource = [
          "${local.agent_logs_bucket_arn}/bot-pool-locks/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          # Issue #125: the assisted-mode bootstrap probes for a
          # previous session's metadata.json with `blitzlog_oss_ls`,
          # and the watchdog's lock release checks whether the lock
          # object still exists. The AWS-side analogue is
          # s3:ListBucket with a bot-pool-locks/* prefix condition.
          "oss:ListObjects",
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
          "oos:GetSecretParameter",
        ]
        Resource = [
          "${local.oos_secretparameter_arn}${local.oos_opencode_api_key_name}",
          "${local.oos_secretparameter_arn}${local.oos_stt_api_url_name}",
          "${local.oos_secretparameter_arn}${local.oos_stt_api_key_name}",
          "${local.oos_secretparameter_arn}${local.oos_stt_model_name}",
          "${local.oos_secretparameter_arn}${local.oos_stt_language_name}",
          "${local.oos_secretparameter_arn}${local.oos_stt_models_bucket_name}",
          "${local.oos_secretparameter_arn}${local.oos_ephemeral_root}/*",
          # Per-user local LLM config is env-independent — same
          # posture as the AWS-side iam.tf:323.
          "${local.oos_secretparameter_arn}blitzlog/users/*/local-llm/*",
          # Per-user git author identity (issue #119). Same broad
          # `users/*/git-author/*` shape as the local-llm grant
          # above — narrow enough to exclude the telegram bot tokens
          # and allowed-user-id params, which the ECS instance role
          # must NOT be able to read. The two git-author params are
          # non-secret String types whose values appear on every
          # agent commit.
          "${local.oos_secretparameter_arn}blitzlog/users/*/git-author/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
        ]
        # Same reasoning as the FC role's grant: the parameters are
        # encrypted with the per-env CMK.
        Resource = "acs:kms:${var.alicloud_region}:${data.alicloud_account.current.id}:key/${var.kms_key_id}"
      },
      {
        Effect = "Allow"
        Action = [
          "kms:GetSecretValue",
        ]
        # Each OOS encrypted parameter is a KMS SECRET named
        # `secret/oos/<parameter path>` — OOS implements its
        # encrypted parameter kind on top of KMS Secrets Manager, so
        # reading one needs `kms:GetSecretValue` on that secret.
        # `kms:Decrypt` is the action for CMKs (`key/...`) and does
        # NOT apply to a secret resource.
        #
        # Without this grant every read fails with
        # `Kms.SecretFailed` / `Forbidden.NoPermission` ("This
        # operation for acs:kms:<region>:<account>:secret/oos/... is
        # not allowed"), which the deploy legs read as "the
        # parameter is missing or empty" — it is neither.
        Resource = [
          "acs:kms:${var.alicloud_region}:${data.alicloud_account.current.id}:secret/oos/blitzlog/${var.environment}/*",
          "acs:kms:${var.alicloud_region}:${data.alicloud_account.current.id}:secret/oos/blitzlog/users/*",
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
