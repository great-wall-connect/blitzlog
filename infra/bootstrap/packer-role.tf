# Packer build role for blitzlog-agent AMIs. The Packer pipeline
# (infra/packer/agent-docker*.pkr.hcl) runs in GitHub Actions via OIDC and
# assumes this role to:
#   1. Read the source AMI (ECS-optimized AL2023 or Canonical Ubuntu)
#   2. Create the blitzlog agent AMI (ec2:CreateImage)
#   3. Write the resulting AMI id to /blitzlog/<env>/agent-ami-id-docker-*
#   4. Read STT model files from the shared S3 bucket during image baking
#
# Trust policy: GitHub Actions OIDC for the great-wall-connect/blitzlog repo,
# workflow_file_ref pinned to the packer-build workflow + branch refs.

data "aws_caller_identity" "current" {}

locals {
  github_repo   = "great-wall-connect/blitzlog"
  github_org    = "great-wall-connect"
  workflow_path = ".github/workflows/packer-build.yml"

  allowed_refs = [
    "refs/heads/main",
    "refs/heads/feat/*",
    "refs/tags/v*",
  ]
}

resource "aws_iam_role" "packer_build" {
  name        = "blitzlog-packer-build-role"
  description = "Packer build role for blitzlog agent AMIs (assumed via GitHub OIDC)"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:${local.github_org}/${local.github_repo}:ref:refs/heads/*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "packer_build" {
  name = "blitzlog-packer-build-policy"
  role = aws_iam_role.packer_build.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowEC2ImageCreation"
        Effect = "Allow"
        Action = [
          "ec2:CreateImage",
          "ec2:CopyImage",
          "ec2:DescribeImages",
          "ec2:DescribeInstances",
          "ec2:DescribeSnapshots",
          "ec2:CreateTags",
          "ec2:DeleteTags",
          "ec2:RegisterImage",
          "ec2:DeregisterImage",
          "ec2:CreateSnapshot",
          "ec2:DeleteSnapshot",
        ]
        Resource = "*"
      },
      {
        Sid    = "AllowPassRoleForPackerInstance"
        Effect = "Allow"
        Action = [
          "iam:PassRole",
        ]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/blitzlog-packer-build-instance-role"
      },
      {
        Sid    = "AllowSSMPublishForBothFamilies"
        Effect = "Allow"
        Action = [
          "ssm:PutParameter",
          "ssm:GetParameter",
          "ssm:DeleteParameter",
        ]
        Resource = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/blitzlog/*/agent-ami-id-docker-al2023",
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/blitzlog/*/agent-ami-id-docker-ubuntu",
        ]
      },
      {
        Sid    = "AllowReadingSTTModels"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
        ]
        Resource = [
          "${aws_s3_bucket.stt_models.arn}/models/*",
        ]
      },
      {
        Sid    = "AllowEC2RunInstancesForPacker"
        Effect = "Allow"
        Action = [
          "ec2:RunInstances",
          "ec2:TerminateInstances",
          "ec2:StopInstances",
          "ec2:CreateSecurityGroup",
          "ec2:DeleteSecurityGroup",
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:RevokeSecurityGroupIngress",
        ]
        Resource = "*"
      },
    ]
  })
}

output "packer_build_role_arn" {
  description = "ARN of the blitzlog-packer-build-role. Configure the GitHub OIDC trust in the actions workflow with this ARN."
  value       = aws_iam_role.packer_build.arn
}
