# SSM Parameter Store entries for the per-env deploy-time config +
# secrets. The bootstrap apply reads each value from `var.dev` (or
# `var.prod`) — the operator's source of truth is the per-env
# `terraform.tfvars` (regenerated into infra/bootstrap/terraform.dev.tfvars
# and infra/bootstrap/terraform.prod.tfvars via
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
#   a re-apply with the same tfvars produces no diff; rotating a
#   value means regenerating the bootstrap tfvars and re-applying.
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
  # entry here (with the same leaf name, hyphens-not-underscores).
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

  # Per-leaf defaults. Used when a key is absent from `var.dev` (or
  # `var.prod`). The five required keys (github-app-id,
  # github-app-private-key, github-app-installation-id,
  # github-webhook-secret, opencode-api-key) are intentionally
  # absent — if the operator omits them, the apply fails with
  # "value is required" and the one-time-setup error points at the
  # bootstrap tfvars.
  #
  # `map(string)` means non-string values (lists, bools) must be
  # stringified in the default. We use the same canonical HCL
  # spellings the operator's per-env tfvars would use so the
  # SSM value round-trips cleanly through the deploy workflow
  # (the per-env apply re-parses these strings back to their
  # declared types at parse time).
  leaf_defaults = {
    "aws-region"               = "ap-east-1"
    "vpc-id"                   = ""
    "ec2-subnet-id"            = ""
    "ssh-allowed-cidrs"        = "[]"
    "alert-email"              = ""
    "opencode-model"           = "minimax-coding-plan/MiniMax-M3"
    "opencode-agent-max-steps" = "500"
    "agent-logs-bucket-name"   = ""
    "stt-api-url"              = "http://127.0.0.1:7878/v1"
    "stt-api-key"              = "placeholder-not-used-by-localhost-shim"
    "stt-model"                = "base.en"
    "stt-language"             = "en"
    "upload-stt-model"         = "false"
    "stt-model-source-url"     = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
    "stt-models-bucket-name"   = ""
    "aws-profile"              = ""
    "spot-instance-types"      = "[\"t4g.medium\", \"t4g.large\", \"t4g.xlarge\"]"
  }

  # The operator-supplied per-env maps. The `for` walks every leaf
  # the bootstrap provisions, so a missing key in `var.dev` /
  # `var.prod` falls through to `leaf_defaults` (or, for required
  # leaves with no default, `lookup` returns `null` and the apply
  # fails with a clear "value is required" error).
  env_values = {
    for env in local.deploy_envs :
    env => {
      for leaf in keys(local.deploy_parameter_types) :
      leaf => lookup({ dev = var.dev, prod = var.prod }[env], leaf, lookup(local.leaf_defaults, leaf, null))
    }
  }
}

# String-typed parameters. Split from the SecureString resource
# because the AWS provider's `aws_ssm_parameter` does not allow
# `key_id` for `String` type, and the cleanest way to express the
# type-specific attribute is a dedicated resource per type.
#
# The for_each product of (leaf, env) is what lets us "cycle
# through `["dev", "prod"]`" in a single resource block. Each map
# key is "<env>/<leaf>" (e.g. "dev/github-app-private-key") and
# each value is the corresponding entry from `local.env_values`.
resource "aws_ssm_parameter" "deploy_string" {
  for_each = {
    for combo in setproduct(local.deploy_string_leaves, local.deploy_envs) :
    "${combo[1]}/${combo[0]}" => {
      leaf = combo[0]
      env  = combo[1]
    }
    # Skip non-null empty values (e.g. `aws-profile` when the
    # operator doesn't set it) — SSM rejects empty values with
    # the cryptic "Member must have length greater than or equal
    # to 1". Keep null entries so the precondition below fires
    # for the 5 required keys when the operator omits them.
    #
    # `nonsensitive()` strips the sensitivity that would otherwise
    # propagate from `local.env_values` (var.dev and var.prod
    # are sensitive). The condition itself is just a bool (include
    # or skip) — no real secret is exposed by marking it
    # non-sensitive.
    if nonsensitive(
      local.env_values[combo[1]][combo[0]] == null
      || local.env_values[combo[1]][combo[0]] != ""
    )
  }

  name  = "/blitzlog/${each.value.env}/${each.value.leaf}"
  type  = "String"
  value = local.env_values[each.value.env][each.value.leaf]
  # The previous bootstrap apply created these parameters in AWS
  # (with placeholder values, before the map-based design was
  # implemented). `overwrite = true` makes `PutParameter` pass
  # `Overwrite=true` so a re-apply with new values succeeds instead
  # of erroring with `ParameterAlreadyExists`.
  overwrite = true

  # Fail fast at plan time if a value resolved to null (the 5
  # required keys have no default in `leaf_defaults`; if the
  # operator's per-env tfvars doesn't supply them, the lookup
  # yields `null` and the AWS provider otherwise errors with the
  # cryptic "one of insecure_value, value, value_wo must be
  # specified" mid-apply). Empty values are handled by the
  # for_each filter above; the precondition only fires on null.
  lifecycle {
    precondition {
      condition     = local.env_values[each.value.env][each.value.leaf] != null
      error_message = "/blitzlog/${each.value.env}/${each.value.leaf} has no value. Add the key to terraform.${each.value.env}.tfvars as `${each.value.leaf} = \"...\"` inside the ${each.value.env} = { ... } block."
    }
  }

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
    "${combo[1]}/${combo[0]}" => {
      leaf = combo[0]
      env  = combo[1]
    }
    if nonsensitive(
      local.env_values[combo[1]][combo[0]] == null
      || local.env_values[combo[1]][combo[0]] != ""
    )
  }

  name      = "/blitzlog/${each.value.env}/${each.value.leaf}"
  type      = "SecureString"
  value     = local.env_values[each.value.env][each.value.leaf]
  key_id    = "alias/aws/ssm"
  overwrite = true

  # Same precondition as deploy_string (null check only — empty
  # values are filtered out by the for_each above).
  lifecycle {
    precondition {
      condition     = local.env_values[each.value.env][each.value.leaf] != null
      error_message = "/blitzlog/${each.value.env}/${each.value.leaf} has no value. Add the key to terraform.${each.value.env}.tfvars as `${each.value.leaf} = \"...\"` inside the ${each.value.env} = { ... } block."
    }
  }

  tags = {
    Purpose     = "blitzlog-deploy-${each.value.env}"
    ManagedBy   = "blitzlog-bootstrap"
    ParameterId = each.value.leaf
  }
}
