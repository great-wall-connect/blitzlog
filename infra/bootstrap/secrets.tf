# SSM Parameter Store entries for the per-env deploy-time config +
# secrets. The bootstrap apply writes each value directly from the
# matching `var.<env>_<leaf>` (declared in variables.tf) — the
# operator's source of truth is the per-env terraform.tfvars
# (regenerated into infra/bootstrap/terraform.tfvars via
# scripts/tfvars-to-bootstrap.py).
#
# Why a single bootstrap apply (no per-param `aws ssm put-parameter`
# round-trip):
#
# - One `cd infra/bootstrap && terraform apply` provisions the S3
#   buckets AND writes all 44 SSM parameters (22 leaves × 2 envs)
#   with the values from the operator's tfvars in a single pass.
# - The deploy workflows
#   (release.yml's deploy job, terraform-apply-dev.yml) read these
#   values at apply time via
#   `aws ssm get-parameters-by-path --path /blitzlog/<env>/`, so
#   per-env GitHub Actions secrets are not required.
# - The value comes from a tfvars var, not a hard-coded placeholder;
#   the `lifecycle.ignore_changes = [value]` safety net from the
#   earlier placeholder design is no longer needed (a re-apply with
#   the same tfvars produces no diff; rotating a value means
#   regenerating the bootstrap tfvars and re-applying).
#
# The deploy role's existing `ssm:GetParametersByPath` grant on
# `/blitzlog/<env>/*` (infra/bootstrap/deploy-role.tf) covers the
# read path; the OIDC role-to-assume is unchanged.
#
# Type rationale: `SecureString` for actual credentials (PEM keys,
# HMAC secrets, API keys) and `String` for non-secrets (region, VPC
# ID, bucket name, etc.). The type is set at provision time and
# matches the per-env SSM-parameter type expectations in
# infra/modules/core/iam.tf.

locals {
  # Parameter leaf name -> SSM type. Mirrors infra/<env>/variables.tf
  # 1:1. Adding a new TF var requires adding the corresponding
  # entry here (with the same leaf name, hyphens-not-underscores) AND
  # the matching `var.<env>_<snake_case_leaf>` declaration in
  # variables.tf.
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

  # Map of <env> -> { <leaf> = <value> }. The value is sourced from
  # the matching `var.<env>_<snake_case_leaf>` so the operator's
  # tfvars is the single source of truth. We use a map of maps (not
  # a dynamic `var[...]` index) because Terraform only allows
  # attribute access on the `var` object, not index access.
  deploy_value_maps = {
    dev = {
      "aws-region"                 = var.dev_aws_region
      "vpc-id"                     = var.dev_vpc_id
      "ec2-subnet-id"              = var.dev_ec2_subnet_id
      "ssh-allowed-cidrs"          = var.dev_ssh_allowed_cidrs
      "github-app-id"              = var.dev_github_app_id
      "github-app-private-key"     = var.dev_github_app_private_key
      "github-app-installation-id" = var.dev_github_app_installation_id
      "github-webhook-secret"      = var.dev_github_webhook_secret
      "alert-email"                = var.dev_alert_email
      "opencode-model"             = var.dev_opencode_model
      "opencode-agent-max-steps"   = var.dev_opencode_agent_max_steps
      "opencode-api-key"           = var.dev_opencode_api_key
      "agent-logs-bucket-name"     = var.dev_agent_logs_bucket_name
      "stt-api-url"                = var.dev_stt_api_url
      "stt-api-key"                = var.dev_stt_api_key
      "stt-model"                  = var.dev_stt_model
      "stt-language"               = var.dev_stt_language
      "upload-stt-model"           = var.dev_upload_stt_model
      "stt-model-source-url"       = var.dev_stt_model_source_url
      "stt-models-bucket-name"     = var.dev_stt_models_bucket_name
      "aws-profile"                = var.dev_aws_profile
      "spot-instance-types"        = var.dev_spot_instance_types
    }
    prod = {
      "aws-region"                 = var.prod_aws_region
      "vpc-id"                     = var.prod_vpc_id
      "ec2-subnet-id"              = var.prod_ec2_subnet_id
      "ssh-allowed-cidrs"          = var.prod_ssh_allowed_cidrs
      "github-app-id"              = var.prod_github_app_id
      "github-app-private-key"     = var.prod_github_app_private_key
      "github-app-installation-id" = var.prod_github_app_installation_id
      "github-webhook-secret"      = var.prod_github_webhook_secret
      "alert-email"                = var.prod_alert_email
      "opencode-model"             = var.prod_opencode_model
      "opencode-agent-max-steps"   = var.prod_opencode_agent_max_steps
      "opencode-api-key"           = var.prod_opencode_api_key
      "agent-logs-bucket-name"     = var.prod_agent_logs_bucket_name
      "stt-api-url"                = var.prod_stt_api_url
      "stt-api-key"                = var.prod_stt_api_key
      "stt-model"                  = var.prod_stt_model
      "stt-language"               = var.prod_stt_language
      "upload-stt-model"           = var.prod_upload_stt_model
      "stt-model-source-url"       = var.prod_stt_model_source_url
      "stt-models-bucket-name"     = var.prod_stt_models_bucket_name
      "aws-profile"                = var.prod_aws_profile
      "spot-instance-types"        = var.prod_spot_instance_types
    }
  }
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
  # Value comes from the per-env value map built in locals
  # (sourced from the matching `var.<env>_<leaf>`). The map
  # lookup keeps this resource a one-liner without dynamic var
  # indexing (which Terraform forbids).
  value = local.deploy_value_maps[each.value.env][each.value.leaf]

  tags = {
    Purpose     = "blitzlog-deploy-${each.value.env}"
    ManagedBy   = "blitzlog-bootstrap"
    ParameterId = each.value.leaf
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
  # See note on the deploy_string resource above re: the value map.
  value = local.deploy_value_maps[each.value.env][each.value.leaf]
  # AWS-managed key (free). Operators can rotate to a CMK later
  # by recreating the parameter; out of scope for this bootstrap.
  key_id = "alias/aws/ssm"

  tags = {
    Purpose     = "blitzlog-deploy-${each.value.env}"
    ManagedBy   = "blitzlog-bootstrap"
    ParameterId = each.value.leaf
  }
}
