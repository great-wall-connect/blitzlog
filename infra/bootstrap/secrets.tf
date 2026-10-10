# SSM Parameter Store entries for terraform deploy-time config +
# secrets. The actual VALUES are operator-managed (set via
# `aws ssm put-parameter` after first apply); Terraform only
# provisions the parameter *names*, *types*, and *tags* with a
# placeholder so the deploy role's IAM scope is locked in at
# bootstrap time.
#
# `lifecycle.ignore_changes = [value]` ensures subsequent
# `terraform apply` runs do NOT clobber the operator's real values.
# Without it, the placeholder would re-appear after every bootstrap
# apply and break the next deploy.
#
# The deploy workflows fetch these parameters via
# `aws ssm get-parameters-by-path --path /blitzlog/<env>/` at
# apply time, so the values never appear in the GitHub Actions
# context (no exposure to PRs from forks on this public repo).
#
# Rotation: the operator updates the parameter in place with
# `aws ssm put-parameter --name <name> --value <new> --type <String|SecureString>
# --overwrite`. The next deploy picks up the new value. See issue
# #120 for the future automated-rotation work (Lambda on a
# 90-day EventBridge schedule per secret type).
#
# Type rationale: `SecureString` for actual credentials (PEM keys,
# HMAC secrets, API keys) and `String` for non-secrets (region, VPC
# ID, bucket name, etc.). The type is set at provision time; the
# operator doesn't need to change it. The deploy role's
# `ssm:GetParametersByPath --with-decryption` call decrypts
# SecureString entries and returns String entries in the clear, so
# the workflow treats both types uniformly.

locals {
  # Parameter leaf name -> SSM type. Mirrors infra/<env>/variables.tf
  # 1:1. Adding a new TF var requires adding the corresponding
  # entry here (with the same leaf name, hyphens-not-underscores)
  # so the deploy workflow can resolve it.
  deploy_parameter_types = {
    "aws-region"                 = "String"
    "vpc-id"                     = "String"
    "ec2-subnet-id"              = "String"
    "ssh-allowed-cidrs"          = "String"
    "github-app-id"              = "String"
    "github-app-private-key"     = "SecureString"
    "github-app-installation-id" = "String"
    "github-webhook-secret"      = "SecureString"
    "alert-email"                = "String"
    "opencode-model"             = "String"
    "opencode-agent-max-steps"   = "String"
    "opencode-api-key"           = "SecureString"
    "agent-logs-bucket-name"     = "String"
    "stt-api-url"                = "String"
    "stt-api-key"                = "SecureString"
    "stt-model"                  = "String"
    "stt-language"               = "String"
    "upload-stt-model"           = "String"
    "stt-model-source-url"       = "String"
    "stt-models-bucket-name"     = "String"
    "aws-profile"                = "String"
    "spot-instance-types"        = "String"
  }

  deploy_string_leaves = [
    for leaf, type in local.deploy_parameter_types : leaf
    if type == "String"
  ]

  deploy_secure_leaves = [
    for leaf, type in local.deploy_parameter_types : leaf
    if type == "SecureString"
  ]

  deploy_envs = ["dev", "prod"]
}

# String-typed parameters. Split from the SecureString resource
# because the AWS provider's `aws_ssm_parameter` does not allow
# `key_id` for `String` type, and the cleanest way to express the
# type-specific attribute is a dedicated resource per type.
resource "aws_ssm_parameter" "deploy_string" {
  for_each = {
    for combo in setproduct(local.deploy_string_leaves, local.deploy_envs) :
    "${combo[0]}/${combo[1]}" => {
      leaf = combo[0]
      env  = combo[1]
    }
  }

  name = "/blitzlog/${each.value.env}/${each.value.leaf}"
  type = "String"
  # Placeholder; operator replaces via `aws ssm put-parameter`
  # after first apply. lifecycle.ignore_changes below ensures
  # subsequent applies don't clobber the real value.
  value = "PLACEHOLDER_SET_VIA_AWS_CLI"

  tags = {
    Purpose     = "blitzlog-deploy-${each.value.env}"
    ManagedBy   = "blitzlog-bootstrap"
    ParameterId = each.value.leaf
  }

  lifecycle {
    ignore_changes = [value]
  }
}

# SecureString-typed parameters (PEM keys, HMAC secrets, API
# tokens). Decrypted at read time by the deploy role's
# `ssm:GetParametersByPath --with-decryption` call. Uses the
# AWS-managed KMS key (alias/aws/ssm) for free encryption with
# no key-policy management.
resource "aws_ssm_parameter" "deploy_secure" {
  for_each = {
    for combo in setproduct(local.deploy_secure_leaves, local.deploy_envs) :
    "${combo[0]}/${combo[1]}" => {
      leaf = combo[0]
      env  = combo[1]
    }
  }

  name = "/blitzlog/${each.value.env}/${each.value.leaf}"
  type = "SecureString"
  # Placeholder; operator replaces via `aws ssm put-parameter`
  # after first apply. lifecycle.ignore_changes below ensures
  # subsequent applies don't clobber the real value.
  value = "PLACEHOLDER_SET_VIA_AWS_CLI"
  # AWS-managed key (free). Operators can rotate to a CMK later
  # by recreating the parameter; out of scope for this bootstrap.
  key_id = "alias/aws/ssm"

  tags = {
    Purpose     = "blitzlog-deploy-${each.value.env}"
    ManagedBy   = "blitzlog-bootstrap"
    ParameterId = each.value.leaf
  }

  lifecycle {
    ignore_changes = [value]
  }
}
