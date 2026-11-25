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
  # The GitHub repo, as "owner/repo". The `sub` claim matches on it,
  # so it must NOT be prefixed with the org again — that would produce
  # a subject matching nothing.
  github_repo = "great-wall-connect/blitzlog"

  # The RAM OIDC provider's NAME — the last segment of the provider
  # ARN (`acs:ram::<account-id>:oidc-provider/<name>`). It is the
  # IdP name in the RAM console, and Alicloud's name field takes no
  # slash, so this is NOT `local.github_repo`: the provider is
  # registered as "blitzlog", not as "great-wall-connect/blitzlog".
  # The STS OIDC token request names the provider by ARN, and the
  # trust policies below name it the same way — the three must agree
  # or the request is rejected with "No such OIDC Provider
  # registered".
  oidc_provider_name = "blitzlog"

  # The `alicloud_oss_bucket` resource has no `arn` attribute (the
  # data source in the core module does, but the bootstrap stack owns
  # the buckets directly), so the ARN is constructed the same way
  # infra/alicloud/modules/core/oss.tf does.
  #
  # The region segment is the bucket's OSS LOCATION ("oss-cn-hongkong"),
  # NOT the region id ("cn-hongkong"): OSS matches a RAM grant against
  # the bucket's location string, so a grant written with the region id
  # silently never matches and the bake's model download comes back 403
  # AccessDenied ("The bucket you access does not belong to you") even
  # though the policy document looks right.
  stt_models_bucket_arn = "acs:oss:${alicloud_oss_bucket.stt_models.location}:${data.alicloud_account.current.id}:${alicloud_oss_bucket.stt_models.bucket}"
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
          Federated = ["acs:ram::${data.alicloud_account.current.id}:oidc-provider/${local.oidc_provider_name}"]
        }
        Action = "sts:AssumeRole"
        # Alicloud's RAM does NOT use the AWS condition keys for an
        # OIDC token — the claims live under `oidc:aud` / `oidc:iss` /
        # `oidc:sub`, not under `oidc:token.actions.githubusercontent.com:*`.
        # With the AWS key names the conditions can never be satisfied
        # (the service populates nothing under them) and every assume
        # is denied with `NoPermissionType: ImplicitDeny`.
        # The audience and issuer conditions are LISTS, not a style
        # choice: GitHub's `aud` claim is a JSON array
        # (`["sts.aliyuncs.com"]`), and a string condition never
        # matches an array-valued claim. Alicloud's own examples use
        # lists for both keys too.
        Condition = {
          StringEquals = {
            "oidc:aud" = ["sts.aliyuncs.com"]
            "oidc:iss" = ["https://token.actions.githubusercontent.com"]
          }
          StringLike = {
            "oidc:sub" = "repo:${local.github_repo}:*"
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
  # Alicloud caps a custom policy at 5 versions, and the provider
  # creates a new version on every change — without this the fifth
  # apply fails with "LimitExceeded.Policy.Version: The count of
  # policy version is limited to 5".
  rotate_strategy = "DeleteOldestNonDefaultVersionWhenLimitExceeded"
  policy_document = jsonencode({
    Version = "1"
    Statement = [
      {
        # ECS image creation + the instance lifecycle the builder
        # drives (launch, stop, terminate the build instance).
        #
        # The list is the plugin's own documented minimum
        # (packer-plugin-alicloud's builder/alicloud-ecs README) plus
        # the tag verbs this builder uses. The VPC / security-group /
        # EIP verbs are needed because the plugin creates the build
        # instance's own temporary security group and EIP rather than
        # reusing a pre-existing one.
        Sid    = "AllowECSImageCreation"
        Effect = "Allow"
        Action = [
          "ecs:AttachKeyPair",
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
          "ecs:DescribeDisks",
          "ecs:DescribeImageFromFamily",
          "ecs:CreateKeyPair",
          "ecs:DeleteKeyPairs",
          "ecs:ImportKeyPair",
          "ecs:DetachKeyPair",
          "ecs:CreateTags",
          "ecs:DeleteTags",
          "ecs:TagResources",
          "ecs:UntagResources",
          "ecs:AddTags",
          "ecs:RegisterImage",
          "ecs:DeregisterImage",
          "ecs:ModifyImageAttribute",
          "ecs:DescribeImageSharePermission",
          "ecs:ModifyImageSharePermission",
          "ecs:DeleteImage",
          "ecs:CancelCopyImage",
          "ecs:CreateSnapshot",
          "ecs:DeleteSnapshot",
          "ecs:RunInstances",
          "ecs:StartInstance",
          "ecs:StopInstances",
          "ecs:TerminateInstances",
          "ecs:CreateInstance",
          "ecs:DeleteInstance",
          "ecs:RebootInstance",
          "ecs:RenewInstance",
          "ecs:AllocatePublicIpAddress",
          "ecs:CreateSecurityGroup",
          "ecs:AuthorizeSecurityGroup",
          "ecs:AuthorizeSecurityGroupEgress",
          "ecs:DeleteSecurityGroup",
          "vpc:DescribeVpcs",
          "vpc:CreateVpc",
          "vpc:DeleteVpc",
          "vpc:DescribeVSwitches",
          "vpc:CreateVSwitch",
          "vpc:DeleteVSwitch",
          "vpc:AllocateEipAddress",
          "vpc:AssociateEipAddress",
          "vpc:UnassociateEipAddress",
          "vpc:ReleaseEipAddress",
        ]
        Resource = ["*"]
      },
      {
        # The image id is published to the OOS Parameter Store by the
        # 05-publish-ami-id.sh post-processor. One entry per env: the
        # Alicloud RAM ARN for an OOS parameter does not support a
        # `*` wildcard in the env segment the way the AWS parameter
        # ARN does.
        Sid    = "AllowOOSPublishAgentImageId"
        Effect = "Allow"
        Action = [
          "oos:CreateSecretParameter",
          "oos:UpdateSecretParameter",
          "oos:GetSecretParameter",
          "oos:DeleteSecretParameter",
        ]
        Resource = [
          "acs:oos:${var.alicloud_region}:${data.alicloud_account.current.id}:secretparameter/blitzlog/dev/agent-ami-id-ecs-ubuntu",
          "acs:oos:${var.alicloud_region}:${data.alicloud_account.current.id}:secretparameter/blitzlog/prod/agent-ami-id-ecs-ubuntu",
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
