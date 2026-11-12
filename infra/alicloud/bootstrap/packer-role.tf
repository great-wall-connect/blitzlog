# Packer build role for the blitzlog Alicloud ECS image.
#
# The Alicloud analogue of infra/aws/bootstrap/packer-role.tf. The
# Alicloud Packer pipeline (infra/alicloud/packer/agent-ecs-ubuntu.pkr.hcl)
# runs in GitHub Actions via OIDC and assumes this role to read the
# source Ubuntu image, create the ECS image, publish the image id to
# /blitzlog/<env>/agent-ami-id-ecs-ubuntu, and read the STT models.
#
# GATED OFF BY DEFAULT — the release workflow's `alicloud-packer` job
# only runs when the repo variable ENABLE_ALICLOUD_BAKE is "true", so
# this role can exist without the bake ever firing.
#
# The `sub` claim is matched with StringLike rather than
# workflow_file_ref + ref, so no workflow path is pinned here — the
# same shape as the AWS-side packer role.
#
# The `ecs.aliyuncs.com` service-principal statement is there
# because the Packer build runs on an ECS instance that needs OSS +
# KMS credentials to download the whisper model and publish the image
# id — the AWS build VM gets the same via its instance profile.

# `data.alicloud_account.current` is declared in outputs.tf — it is
# shared by this file and the bucket ARNs there.

locals {
  # The GitHub repo, as "owner/repo". The OIDC provider is registered
  # under that path and the `sub` claim matches on it, so it must NOT
  # be prefixed with the org again — that would produce a subject
  # matching nothing.
  github_repo = "great-wall-connect/blitzlog"

  # The `alicloud_oss_bucket` resource has no `arn` attribute (the
  # data source in the core module does, but the bootstrap stack owns
  # the buckets directly), so the ARN is constructed the same way
  # infra/alicloud/modules/core/oss.tf:36-37 does.
  stt_models_bucket_arn = "acs:oss:${var.alicloud_region}:${data.alicloud_account.current.id}:${alicloud_oss_bucket.stt_models.bucket}"
}

resource "alicloud_ram_role" "packer_build" {
  role_name   = "blitzlog-packer-build-role"
  description = "Packer build role for the blitzlog Alicloud ECS image (assumed via GitHub OIDC)"
  force       = true

  assume_role_policy_document = jsonencode({
    Version = "1"
    Statement = [
      {
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
      {
        # The Packer-launched ECS build instance assumes this role via
        # its RAM role. Without it the build instance has no
        # credentials and the `ossutil cp` of the whisper model fails.
        Effect = "Allow"
        Principal = {
          Service = ["ecs.aliyuncs.com"]
        }
        Action = "sts:AssumeRole"
      },
    ]
  })
}

resource "alicloud_ram_policy" "packer_build" {
  policy_name = "blitzlog-packer-build-policy"
  policy_document = jsonencode({
    Version = "1"
    Statement = [
      {
        # ECS image creation + the instance lifecycle the builder
        # drives (launch, stop, terminate the build instance).
        Sid    = "AllowECSImageCreation"
        Effect = "Allow"
        Action = [
          "ecs:CreateImage",
          "ecs:CopyImage",
          "ecs:DescribeImages",
          "ecs:DescribeInstances",
          "ecs:DescribeSnapshots",
          "ecs:DescribeRegions",
          "ecs:DescribeSecurityGroups",
          "ecs:DescribeVSwitches",
          "ecs:DescribeVpcs",
          "ecs:DescribeKeyPairs",
          "ecs:CreateKeyPair",
          "ecs:DeleteKeyPair",
          "ecs:CreateTags",
          "ecs:DeleteTags",
          "ecs:RegisterImage",
          "ecs:DeregisterImage",
          "ecs:ModifyImageAttribute",
          "ecs:ModifyImageAttribute",
          "ecs:CreateSnapshot",
          "ecs:DeleteSnapshot",
          "ecs:RunInstances",
          "ecs:StartInstance",
          "ecs:StopInstances",
          "ecs:TerminateInstances",
        ]
        Resource = ["*"]
      },
      {
        # The image id is published to KMS by the 05-publish-ami-id.sh
        # post-processor. One entry per env: the Alicloud RAM ARN for a
        # KMS secret does not support a `*` wildcard in the env
        # segment the way the AWS parameter ARN does.
        Sid    = "AllowKMSPublishAgentImageId"
        Effect = "Allow"
        Action = [
          "kms:CreateSecret",
          "kms:PutSecretValue",
          "kms:GetSecretValue",
          "kms:DeleteSecret",
        ]
        Resource = [
          "acs:kms:${var.alicloud_region}:${data.alicloud_account.current.id}:secret/blitzlog/dev/agent-ami-id-ecs-ubuntu",
          "acs:kms:${var.alicloud_region}:${data.alicloud_account.current.id}:secret/blitzlog/prod/agent-ami-id-ecs-ubuntu",
        ]
      },
      {
        # Reading the whisper model files from the shared STT bucket
        # during the bake.
        Sid    = "AllowReadingSTTModels"
        Effect = "Allow"
        Action = [
          "oss:GetObject",
        ]
        Resource = [
          "${local.stt_models_bucket_arn}/models/*",
        ]
      },
      {
        # The build instance needs to attach its own RAM role.
        Sid    = "AllowPassRoleForPackerInstance"
        Effect = "Allow"
        Action = [
          "ram:PassRole",
        ]
        Resource = [
          alicloud_ram_role.packer_build.arn,
        ]
      },
    ]
  })
}

resource "alicloud_ram_role_policy_attachment" "packer_build" {
  policy_name = alicloud_ram_policy.packer_build.policy_name
  policy_type = "Custom"
  role_name   = alicloud_ram_role.packer_build.role_name
}

output "packer_build_role_arn" {
  description = "ARN of the blitzlog-packer-build-role. Configure the GitHub OIDC trust in the release workflow with this ARN, and set vars.ALICLOUD_ACCOUNT_ID to the account id."
  value       = alicloud_ram_role.packer_build.arn
}
