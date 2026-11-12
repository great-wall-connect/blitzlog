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
          Federated = ["acs:ram::${data.alicloud_account.current.id}:oidc-provider/${local.github_repo}"]
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "oidc:token.actions.githubusercontent.com:aud" = "sts.aliyuncs.com"
          }
          StringLike = {
            "oidc:token.actions.githubusercontent.com:sub" = "repo:${local.github_repo}*:ref:refs/heads/*"
          }
        }
      },
    ]
  })
}

resource "alicloud_ram_policy" "deploy" {
  policy_name = "blitzlog-deploy-policy"
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
        # The KMS secrets + CMK the core module creates. The grant
        # needs the write verbs, not just GetSecretValue — the apply
        # creates the secrets that carry the GitHub App private key
        # and the webhook secret.
        Sid    = "AllowKMSManagement"
        Effect = "Allow"
        Action = [
          "kms:CreateSecret",
          "kms:PutSecretValue",
          "kms:GetSecretValue",
          "kms:DeleteSecret",
          "kms:ListSecrets",
          "kms:DescribeSecret",
          "kms:CreateKey",
          "kms:DeleteKey",
          "kms:DescribeKey",
          "kms:CreateGrant",
          "kms:ListGrants",
        ]
        Resource = [
          "acs:kms:${var.alicloud_region}:${data.alicloud_account.current.id}:secret/blitzlog/*",
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
