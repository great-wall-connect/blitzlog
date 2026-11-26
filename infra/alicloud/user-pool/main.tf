# The Alicloud user pool (issue #125).
#
# The Alicloud mirror of infra/aws/user-pool/ — the same per-user
# data, in the Alicloud secret store (OOS Parameter Store) instead
# of SSM Parameter Store:
#
#   * the Telegram bot pool (the tokens),
#   * the Telegram user ID allowed to talk to those bots,
#   * the per-user local-LLM config,
#   * the per-user git author identity (issue #119).
#
# WHY A SEPARATE STACK. The pool namespace is env-INDEPENDENT
# (`blitzlog/users/<login>/…`, no env prefix): dev and prod read the
# same parameters, exactly as they read the same SSM parameters on
# AWS. It is also per-operator (one pool per GitHub login), so it is
# applied locally by the operator — there is no shared state and no
# CI involvement, the same posture as the AWS sibling.
#
# THE PARAMETERS ARE ENCRYPTED WITH THE ACCOUNT'S CMK. There is one
# CMK per region per account (Alicloud allows no more), so it is
# account-wide rather than per-env — and an env-independent
# namespace can use it: dev and prod hold the same CMK, so a
# CMK-encrypted pool parameter is readable by both.
#
# WHO READS WHAT. The Function Compute function reads the pool
# (`oos:GetSecretParametersByPath` on `blitzlog/users/*`) and hands
# the chosen bot's name + token to the agent instance through the
# user-data. The instance's RAM role deliberately CANNOT read the
# bot tokens — it may read only `users/*/git-author/*` and
# `users/*/local-llm/*`, because the instance runs user code.

terraform {
  required_version = ">= 1.0"

  required_providers {
    alicloud = {
      source  = "aliyun/alicloud"
      version = "~> 2.0"
    }
  }
}

provider "alicloud" {
  region = var.alicloud_region
}

locals {
  bot_names = nonsensitive(keys(var.telegram_bot_tokens))

  local_llm_endpoint      = trimspace(var.local_llm_endpoint)
  local_llm_model         = trimspace(var.local_llm_model)
  local_llm_api_key       = var.local_llm_api_key
  local_llm_allow_private = var.local_llm_endpoint_allow_private_cidrs
  local_llm_fallback      = var.local_llm_fallback
  tailscale_auth_key      = trimspace(var.tailscale_auth_key)

  local_llm_endpoint_set = local.local_llm_endpoint != ""
  local_llm_model_set    = local.local_llm_model != ""
  tailscale_key_set      = nonsensitive(local.tailscale_auth_key != "")

  # The parameter names. OOS names its parameters WITHOUT a leading
  # slash (`blitzlog/users/<login>/…`) — the store strips the SSM
  # path's slash before the name reaches the wire.
  user_root = "blitzlog/users/${var.owner_login}"

  # Build the list of parameter keys to create. Keys are derived from
  # hardcoded string literals + boolean conditions, so the list itself
  # is non-sensitive — required because for_each cannot accept maps
  # whose values include sensitive data.
  local_llm_keys_to_create = concat(
    # Always-present metadata parameters.
    ["allow-private-cidrs", "fallback"],
    # Endpoint-grouped parameters; only create when the endpoint is set so
    # the user doesn't end up with orphaned api-key/model entries.
    local.local_llm_endpoint_set ? ["endpoint", "model", "api-key"] : [],
    # Tailscale auth key is only meaningful when configured (per-run
    # enrollment) and is independent of the endpoint being set.
    local.tailscale_key_set ? ["tailscale-auth-key"] : [],
  )

  local_llm_param_values = {
    "endpoint"            = local.local_llm_endpoint
    "model"               = local.local_llm_model
    "api-key"             = local.local_llm_api_key
    "allow-private-cidrs" = local.local_llm_allow_private ? "true" : "false"
    "fallback"            = local.local_llm_fallback
    "tailscale-auth-key"  = local.tailscale_auth_key
  }

  # Git author identity for every agent commit on this pool's behalf
  # (the ECS runtime + the GHA worker — see issue #119). The defaults
  # derive from var.owner_login so an operator who only sets
  # owner_login gets the correct noreply form; explicit overrides are
  # rare and should keep the two values in sync (setting name but
  # leaving email empty is rejected by the precondition below).
  git_author_name_resolved  = trimspace(var.git_author_name) != "" ? trimspace(var.git_author_name) : var.owner_login
  git_author_email_resolved = trimspace(var.git_author_email) != "" ? trimspace(var.git_author_email) : "${var.owner_login}@users.noreply.github.com"

  # The pool's parameters, keyed for the data source's for_each.
  pool_secret_parameters = merge(
    { for name in local.bot_names : "telegram-pool-${name}" => "${local.user_root}/telegram/pool/${name}" },
    { for key in local.local_llm_keys_to_create : "local-llm-${key}" => "${local.user_root}/local-llm/${key}" },
    {
      "telegram-allowed-user-id" = "${local.user_root}/telegram/allowed-user-id"
      "git-author-name"          = "${local.user_root}/git-author/name"
      "git-author-email"         = "${local.user_root}/git-author/email"
    },
  )
}

# The pool's parameters, read back WITH decryption.
#
# A parameter's encryption key is fixed at creation, so moving one
# onto the account's CMK means recreating it — and the provider reads
# an OOS secret parameter back WITHOUT decryption, so terraform's
# copy of a value is whatever was last APPLIED. A replacement writes
# that value, which is empty for every parameter the operator filled
# in after its creation. Reading the live value from OOS carries it
# across instead — and it is also what makes the git-author identity
# keep its value when the derived default changes.
#
# `try(...)` falls back to the variable for a parameter that does not
# exist yet — the first apply, before the pool has any parameters.
data "alicloud_oos_secret_parameters" "current" {
  for_each = local.pool_secret_parameters

  secret_parameter_name = each.value
  enable_details        = true
  with_decryption       = true
}

resource "alicloud_oos_secret_parameter" "telegram_bot_pool" {
  for_each = toset(local.bot_names)

  secret_parameter_name = "${local.user_root}/telegram/pool/${each.key}"
  value                 = try(data.alicloud_oos_secret_parameters.current["telegram-pool-${each.key}"].parameters[0].value, var.telegram_bot_tokens[each.key])
  type                  = "Secret"
  key_id                = var.kms_key_id
  description           = "Telegram bot token for ${var.owner_login} pool bot: ${each.key}"
}

resource "alicloud_oos_secret_parameter" "telegram_allowed_user_id" {
  secret_parameter_name = "${local.user_root}/telegram/allowed-user-id"
  value                 = try(data.alicloud_oos_secret_parameters.current["telegram-allowed-user-id"].parameters[0].value, var.telegram_allowed_user_id)
  type                  = "Secret"
  key_id                = var.kms_key_id
  description           = "Telegram user ID allowed to interact with ${var.owner_login}'s bots"
}

resource "alicloud_oos_secret_parameter" "local_llm" {
  for_each = toset(local.local_llm_keys_to_create)

  secret_parameter_name = "${local.user_root}/local-llm/${each.value}"
  value                 = try(data.alicloud_oos_secret_parameters.current["local-llm-${each.value}"].parameters[0].value, local.local_llm_param_values[each.value])
  type                  = "Secret"
  key_id                = var.kms_key_id
  description           = "Local LLM config for ${var.owner_login}: ${each.value}"
}

# Per-pool git author identity (issue #119). Always emit when the
# pool is provisioned — the defaults derived from var.owner_login
# guarantee a usable identity for every pool. The two parameters land
# under blitzlog/users/<owner>/git-author/{name,email} so both the
# ECS agent runtime (the `blitzlog/users/*/git-author/*` grant on the
# agent role) and the GHA worker (the narrow grant in
# infra/aws/bootstrap/deploy-role.tf) can fetch them with a single
# read.
resource "alicloud_oos_secret_parameter" "git_author_name" {
  secret_parameter_name = "${local.user_root}/git-author/name"
  value                 = try(data.alicloud_oos_secret_parameters.current["git-author-name"].parameters[0].value, local.git_author_name_resolved)
  type                  = "Secret"
  key_id                = var.kms_key_id
  description           = "Git author name for ${var.owner_login}'s agent commits (issue #119)"
}

resource "alicloud_oos_secret_parameter" "git_author_email" {
  secret_parameter_name = "${local.user_root}/git-author/email"
  value                 = try(data.alicloud_oos_secret_parameters.current["git-author-email"].parameters[0].value, local.git_author_email_resolved)
  type                  = "Secret"
  key_id                = var.kms_key_id
  description           = "Git author email for ${var.owner_login}'s agent commits (issue #119)"
}
