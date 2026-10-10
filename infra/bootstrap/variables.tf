variable "aws_region" {
  description = "AWS region for the bootstrap resources"
  type        = string
  default     = "ap-east-1"
}

variable "agent_logs_bucket_name" {
  description = "Name of the shared S3 bucket that stores agent logs and session archives. Must be globally unique across AWS. Both prod and dev envs reference this bucket via a data source."
  type        = string
}

variable "stt_models_bucket_name" {
  description = "Name of the shared S3 bucket hosting whisper.cpp model files. Must be globally unique across AWS. Both prod and dev envs reference this bucket via a data source."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.stt_models_bucket_name))
    error_message = "S3 bucket names must be 3-63 characters, lowercase, and contain only letters, numbers, hyphens, and dots. Cannot start or end with a hyphen or dot."
  }
}

# ----------------------------------------------------------------------------
# Per-env deploy-time config + secrets
#
# The bootstrap apply reads each entry from `var.dev` (or `var.prod`)
# and writes it to SSM Parameter Store under
# `/blitzlog/<env>/<leaf>`. The deploy workflows
# (release.yml's deploy job, terraform-apply-dev.yml) fetch them via
# `aws ssm get-parameters-by-path --path /blitzlog/<env>/` at apply
# time.
#
# The map is keyed by SSM leaf name (kebab-case, e.g.
# "github-app-private-key"). Missing keys fall back to the per-leaf
# defaults declared in `infra/bootstrap/secrets.tf`'s
# `local.leaf_defaults`. The operator's bootstrap tfvars is generated
# by `scripts/tfvars-to-bootstrap.py` from the per-env
# `terraform.tfvars` files.
#
# Type is `map(string)` rather than `map(any)` because every value
# ends up as a string in SSM regardless (the AWS API's
# `Parameter.Value` is always a string), and the per-env
# `terraform apply` re-coerces to the variable's declared type from
# the `TF_VAR_<name>` env vars at parse time. A string-typed map
# keeps the variable declaration honest (no implicit `any`) and
# surfaces type mismatches in the per-env apply, where the type
# contracts live.
#
# Both maps are `sensitive = true` because the bootstrap state would
# otherwise contain plaintext values.
# ----------------------------------------------------------------------------

variable "dev" {
  description = "Dev deploy-time config + secrets, keyed by SSM leaf name (e.g. \"github-app-private-key\"). Missing keys fall back to the per-leaf defaults in infra/bootstrap/secrets.tf."
  type        = map(string)
  sensitive   = true
  default     = {}
}

variable "prod" {
  description = "Prod deploy-time config + secrets, keyed by SSM leaf name (e.g. \"github-app-private-key\"). Same shape as `var.dev`."
  type        = map(string)
  sensitive   = true
  default     = {}
}
