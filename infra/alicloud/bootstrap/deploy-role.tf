# Terraform deploy role for the Alicloud stacks (issue #125).
#
# The Alicloud analogue of infra/aws/bootstrap/deploy-role.tf. The
# deploy workflow assumes this role via GitHub Actions OIDC.
#
# The role is intentionally broad: `terraform apply` has to manage every
# resource the core module creates, and narrowing any of them means the
# apply fails halfway with an AccessDenied.
#
# No `ecs.aliyuncs.com` service-principal statement, unlike the Packer
# role: the deploy runs on the GitHub Actions runner, not on an ECS
# instance.
#
# `local.github_repo` lives in packer-role.tf — Terraform allows ONE
# `locals` block per module, so the two roles share the single one.

resource "alicloud_ram_role" "deploy" {
  role_name   = "blitzlog-deploy-role"
  description = "Terraform deploy role for infra/alicloud/{dev,prod} (assumed via GitHub OIDC)"
  force       = true

  assume_role_policy_document = jsonencode({
    Version = "1"
    Statement = [
      {
        # GitHub Actions OIDC for any branch of
        # great-wall-connect/blitzlog. The `aud` claim must match the
        # OIDC provider's client id (sts.aliyuncs.com).
        Effect = "Allow"
        Principal = {
          Federated = ["acs:ram::${data.alicloud_account.current.id}:oidc-provider/${local.oidc_provider_name}"]
        }
        Action = "sts:AssumeRole"
        # Alicloud's RAM does NOT use the AWS condition keys for an
        # OIDC token — the claims live under `oidc:aud` / `oidc:iss` /
        # `oidc:sub`, not under `oidc:token.actions.githubusercontent.com:*`.
        # With the AWS key names the conditions can never be satisfied
        # (the service populates nothing under them) and every assume
        # is denied with `NoPermissionType: ImplicitDeny`.
        Condition = {
          StringEquals = {
            "oidc:aud" = "sts.aliyuncs.com"
            "oidc:iss" = "https://token.actions.githubusercontent.com"
          }
          StringLike = {
            "oidc:sub" = "repo:${local.github_repo}:*"
          }
        }
      },
    ]
  })
}

resource "alicloud_ram_policy" "deploy" {
  policy_name = "blitzlog-deploy-policy"
  # Alicloud caps a custom policy at 5 versions, and the provider
  # creates a new version on every change — without this the fifth
  # apply fails with "LimitExceeded.Policy.Version: The count of
  # policy version is limited to 5".
  rotate_strategy = "DeleteOldestNonDefaultVersionWhenLimitExceeded"
  policy_document = jsonencode({
    Version = "1"
    Statement = [
      {
        # The function-compute + api-gateway + mns + network surface
        # the core module manages.
        Sid    = "AllowComputeAndNetworkManagement"
        Effect = "Allow"
        Action = [
          "fc:CreateFunction",
          "fc:DeleteFunction",
          "fc:UpdateFunction",
          "fc:UpdateFunctionCode",
          "fc:GetFunction",
          "fc:ListFunctions",
          "fc:CreateFunctionTrigger",
          "fc:DeleteFunctionTrigger",
          "fc:UpdateFunctionTrigger",
          "fc:GetFunctionTrigger",
          "fc:ListFunctionTriggers",
          "apigateway:CreateApi",
          "apigateway:DeleteApi",
          "apigateway:ModifyApi",
          "apigateway:DescribeApis",
          "apigateway:CreateApiGroup",
          "apigateway:DeleteApiGroup",
          "apigateway:ModifyApiGroup",
          "apigateway:DescribeApiGroups",
          "apigateway:CreateApp",
          "apigateway:DeleteApp",
          "apigateway:ModifyApp",
          "apigateway:DescribeApps",
          "mns:CreateQueue",
          "mns:DeleteQueue",
          "mns:ModifyQueue",
          "mns:DescribeQueue",
          "mns:ListQueue",
          "vpc:CreateVpc",
          "vpc:DeleteVpc",
          "vpc:ModifyVpc",
          "vpc:DescribeVpcs",
          "vpc:CreateVSwitch",
          "vpc:DeleteVSwitch",
          "vpc:ModifyVSwitch",
          "vpc:DescribeVSwitches",
          "ecs:DescribeInstances",
          "ecs:DescribeImages",
          "ecs:DescribeSecurityGroups",
          "ecs:DescribeRegions",
          "ecs:CreateSecurityGroup",
          "ecs:DeleteSecurityGroup",
          "ecs:AuthorizeSecurityGroupIngress",
          "ecs:RevokeSecurityGroupIngress",
        ]
        Resource = ["*"]
      },
      {
        # The OOS parameters + the KMS CMK the core module creates.
        # The grant needs the write verbs, not just the read one —
        # the apply creates the parameters that carry the GitHub App
        # private key and the webhook secret.
        Sid    = "AllowOOSParameterManagement"
        Effect = "Allow"
        Action = [
          "oos:CreateSecretParameter",
          "oos:UpdateSecretParameter",
          "oos:DeleteSecretParameter",
          "oos:GetSecretParameter",
          "oos:GetSecretParametersByPath",
          "oos:ListSecretParameters",
          "oos:ListSecretParameterVersions",
        ]
        Resource = [
          "acs:oos:${var.alicloud_region}:${data.alicloud_account.current.id}:secretparameter/blitzlog/*",
        ]
      },
      {
        # The CMK that encrypts the OOS parameters. Default master
        # keys are free of charge — KMS bills for *instances*, and
        # this deployment does not buy one.
        Sid    = "AllowKMSKeyManagement"
        Effect = "Allow"
        Action = [
          "kms:CreateKey",
          "kms:DeleteKey",
          "kms:DescribeKey",
          "kms:CreateGrant",
          "kms:ListGrants",
        ]
        Resource = [
          "acs:kms:${var.alicloud_region}:${data.alicloud_account.current.id}:key/*",
        ]
      },
      {
        # The OSS buckets + the function-code object the core module
        # writes (`alicloud_oss_bucket_object.fc_code`).
        Sid    = "AllowOSSManagement"
        Effect = "Allow"
        Action = [
          "oss:PutObject",
          "oss:GetObject",
          "oss:DeleteObject",
          "oss:ListObjects",
          "oss:GetBucketAcl",
          "oss:PutBucketAcl",
          "oss:ListBuckets",
          "oss:GetBucketInfo",
          "oss:PutBucketLifecycle",
          "oss:GetBucketLifecycle",
          "oss:DeleteBucketLifecycle",
          "oss:PutBucketVersioning",
          "oss:GetBucketVersioning",
          "oss:PutBucketEncryption",
          "oss:GetBucketEncryption",
          "oss:DeleteBucketEncryption",
          "oss:CreateBucket",
          "oss:DeleteBucket",
          "oss:PutBucketPolicy",
          "oss:GetBucketPolicy",
          "oss:DeleteBucketPolicy",
        ]
        Resource = ["*"]
      },
      {
        # RAM management: the core module creates the FC execution
        # role and the ECS instance role, plus their policies and
        # attachments. `ram:PassRole` is required so the deploy can
        # attach the roles to the FC function and the ECS instances.
        Sid    = "AllowRAMManagement"
        Effect = "Allow"
        Action = [
          "ram:CreateRole",
          "ram:DeleteRole",
          "ram:UpdateRole",
          "ram:UpdateAssumeRolePolicy",
          "ram:GetRole",
          "ram:ListRoles",
          "ram:CreatePolicy",
          "ram:DeletePolicy",
          "ram:UpdatePolicy",
          "ram:GetPolicy",
          "ram:ListPolicies",
          "ram:AttachPolicy",
          "ram:DetachPolicy",
          "ram:ListAttachedRolePolicies",
          "ram:PassRole",
        ]
        Resource = ["*"]
      },
      {
        # No `ecs:RunInstances` here — the spot-launch is done by the
        # Lambda at runtime, not by terraform. That grant lives on the
        # FC execution role (infra/alicloud/modules/core/ram.tf).
        Sid    = "AllowReadAgentResources"
        Effect = "Allow"
        Action = [
          "ecs:DescribeInstances",
          "ecs:DescribeTags",
          "ecs:DescribeNetworkInterfaces",
          "ecs:DescribeDisks",
          "ecs:DescribeVpcs",
        ]
        Resource = ["*"]
      },
      {
        Sid    = "AllowAccountRead"
        Effect = "Allow"
        Action = [
          "ram:GetAccountSummary",
          "ram:GetSecurityPreference",
          "vpc:DescribeVSwitches",
        ]
        Resource = ["*"]
      },
    ]
  })
}

resource "alicloud_ram_role_policy_attachment" "deploy" {
  policy_name = alicloud_ram_policy.deploy.policy_name
  policy_type = "Custom"
  role_name   = alicloud_ram_role.deploy.role_name
}

output "deploy_role_arn" {
  description = "ARN of the blitzlog-deploy-role. The release workflow assumes it via OIDC; set vars.ALICLOUD_ACCOUNT_ID to the account id."
  value       = alicloud_ram_role.deploy.arn
}
