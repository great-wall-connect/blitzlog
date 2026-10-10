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
# by copying `terraform.{dev,prod}.tfvars.example` to
# `terraform.{dev,prod}.tfvars` (gitignored) and editing.
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
#
# All bootstrap-time inputs (including the AWS region and the
# agent-logs / stt-models bucket names) live in these maps. The
# bootstrap creates one bucket pair (agent-logs + stt-models) per
# env, so the operator can use distinct bucket names per env. The
# per-env stacks consume the same values from SSM via the deploy
# workflow's `get-parameters-by-path` fetch.
# ----------------------------------------------------------------------------

variable "dev" {
  description = "Dev deploy-time config + secrets, keyed by SSM leaf name (e.g. \"github-app-private-key\", \"aws-region\", \"agent-logs-bucket-name\", \"stt-models-bucket-name\"). Missing keys fall back to the per-leaf defaults in infra/bootstrap/secrets.tf."
  type        = map(string)
  sensitive   = true
  default     = {}
}

variable "prod" {
  description = "Prod deploy-time config + secrets, keyed by SSM leaf name. Same shape as `var.dev`. Each env can have its own bucket names (and region) so the bootstrap creates a per-env bucket pair."
  type        = map(string)
  sensitive   = true
  default     = {}
}
