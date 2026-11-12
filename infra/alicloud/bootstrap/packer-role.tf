# Packer build role for the blitzlog Alicloud ECS image.
#
# The Alicloud analogue of infra/aws/bootstrap/packer-role.tf. The
# Alicloud Packer pipeline (infra/alicloud/packer/agent-ecs-ubuntu.pkr.hcl)
# runs in GitHub Actions via OIDC and assumes this role to:
#   1. Read the source Ubuntu image (Canonical-published, via
#      DescribeImages)
#   2. Create the ECS image (ecs:CreateImage + the snapshot/disk
#      actions the builder needs)
#   3. Write the resulting image id to
#      /blitzlog/<env>/agent-ami-id-ecs-ubuntu (KMS Secrets Manager)
#   4. Read STT model files from the shared OSS bucket during image
#      baking
#
# GATED OFF BY DEFAULT. The release workflow's `alicloud-packer` job
# only runs when the repo variable ENABLE_ALICLOUD_BAKE is set to
# "true" (see .github/workflows/release.yml), so this role can exist
# without the bake ever firing.
#
# TRUST POLICY. The `sub` claim is matched with StringLike rather
# than workflow_file_ref + ref constraints, so no workflow path is
# pinned here — the same shape as the AWS-side packer role.
#
# WHY THE BUILD INSTANCE GETS ITS OWN ROLE. The Packer build runs on
# an ECS instance that needs OSS + KMS credentials to download the
# whisper model (03-bake-images.sh) and to publish the image id
# (05-publish-ami-id.sh). The build instance therefore assumes this
# role via `ecs:AssumeRole`, exactly as the AWS build VM assumes the
# AWS packer role via its instance profile.

# `data.alicloud_account.current` is declared in outputs.tf — it is
# shared by this file and the bucket ARNs there.

locals {
  github_repo = "great-wall-connect/blitzlog"
  github_org  = "great-wall-connect"

  # Alicloud OSS bucket ARN format:
  #   acs:oss:<region>:<account-id>:<bucket-name>
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
          Federated = ["acs:ram::${data.alicloud_account.current.id}:oidc-provider/${local.github_org}/${local.github_repo}"]
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "oidc:token.actions.githubusercontent.com:aud" = "sts.aliyuncs.com"
          }
          StringLike = {
            "oidc:token.actions.githubusercontent.com:sub" = "repo:${local.github_org}*/${local.github_repo}*:ref:refs/heads/*"
          }
        }
      },
      {
        # Allow the Packer-launched ECS build instance to assume this
        # role via its RAM role (the builder's `instance_role_name`).
        # Without this, the build instance has no Alicloud credentials
        # and the `ossutil cp` of the whisper model fails.
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
        # The KMS secret that holds the baked image id
        # (/blitzlog/<env>/agent-ami-id-ecs-ubuntu) is written by the
        # 05-publish-ami-id.sh post-processor. The Alicloud
        # `SecretStore` implementation is KMS Secrets Manager, so the
        # publish path needs kms:CreateSecret + kms:PutSecretValue.
        #
        # The grant is env-scoped in the same shape as the AWS-side
        # packer role's SSM grant — one entry per env, because the
        # Alicloud RAM resource ARN for a KMS secret does not support
        # the `*` wildcard in the env segment the way the AWS
        # parameter ARN does.
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
