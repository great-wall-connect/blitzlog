terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
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

  # Build the list of SSM parameter keys to create. Keys are derived from
  # hardcoded string literals + boolean conditions, so the list itself is
  # non-sensitive — required because for_each cannot accept maps whose
  # values include sensitive data.
  local_llm_keys_to_create = concat(
    # Always-present metadata parameters (string-typed).
    ["allow-private-cidrs", "fallback"],
    # Endpoint-grouped parameters; only create when the endpoint is set so
    # the user doesn't end up with orphaned api-key/model SSM entries.
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

  local_llm_secure_keys = ["endpoint", "model", "api-key", "tailscale-auth-key"]

  # Git author identity for every agent commit on this pool's behalf
  # (EC2 runtime + GHA worker — see issue #119). The defaults derive
  # from var.owner_login so an operator who only sets owner_login gets
  # the correct noreply form; explicit overrides are rare and should
  # keep the two values in sync (setting name but leaving email empty
  # is rejected by the precondition below).
  git_author_name_resolved  = trimspace(var.git_author_name) != "" ? trimspace(var.git_author_name) : var.owner_login
  git_author_email_resolved = trimspace(var.git_author_email) != "" ? trimspace(var.git_author_email) : "${var.owner_login}@users.noreply.github.com"

  # Per-user model + credential (issue #165). Mirrors the org layout
  # (/blitzlog/<env>/opencode/api-key) but without the env prefix —
  # a buyer has one model default and one credential, not one per env.
  #
  # The credential's PROVIDER is what the binding rule checks the
  # selected model's provider against, so it is declared explicitly
  # (defaulting to the provider prefix of the user's own model). A
  # buyer with a key but no model of their own sets it explicitly:
  # their key then runs the org's default model, which is the purest
  # BYOK story — but only when the two providers match.
  opencode_model                  = trimspace(var.opencode_model)
  opencode_api_key                = var.opencode_api_key
  opencode_base_url               = trimspace(var.opencode_base_url)
  opencode_provider_explicit      = trimspace(var.opencode_api_key_provider)
  opencode_model_provider_segment = length(split("/", local.opencode_model)) > 1 ? split("/", local.opencode_model)[0] : ""
  opencode_provider_resolved      = local.opencode_provider_explicit != "" ? local.opencode_provider_explicit : local.opencode_model_provider_segment

  opencode_model_set    = local.opencode_model != ""
  opencode_key_set      = nonsensitive(local.opencode_api_key != "")
  opencode_base_url_set = local.opencode_base_url != ""
  opencode_provider_set = local.opencode_provider_resolved != ""
  opencode_any_set      = local.opencode_model_set || local.opencode_key_set || local.opencode_base_url_set || local.opencode_provider_set

  # Only create the namespace when the buyer configured something, so
  # an unconfigured pool leaves no orphaned parameters behind (the
  # same discipline the local-llm block uses).
  opencode_keys_to_create = concat(
    local.opencode_any_set ? ["allow-private-cidrs"] : [],
    local.opencode_model_set ? ["model"] : [],
    local.opencode_key_set ? ["api-key"] : [],
    local.opencode_provider_set ? ["provider"] : [],
    local.opencode_base_url_set ? ["base-url"] : [],
  )

  opencode_param_values = {
    "model"               = local.opencode_model
    "api-key"             = local.opencode_api_key
    "provider"            = local.opencode_provider_resolved
    "base-url"            = local.opencode_base_url
    "allow-private-cidrs" = var.opencode_base_url_allow_private_cidrs ? "true" : "false"
  }

  # Only the credential is secret. The model id, the provider name
  # and the base URL are configuration — they appear in the run's
  # rendered opencode.json and in the boot log.
  opencode_secure_keys = ["api-key"]
}

resource "aws_ssm_parameter" "telegram_bot_pool" {
  for_each = toset(nonsensitive(keys(var.telegram_bot_tokens)))

  name        = "/blitzlog/users/${var.owner_login}/telegram/pool/${each.key}"
  type        = "SecureString"
  value       = var.telegram_bot_tokens[each.key]
  description = "Telegram bot token for ${var.owner_login} pool bot: ${each.key}"
}

resource "aws_ssm_parameter" "telegram_allowed_user_id" {
  name        = "/blitzlog/users/${var.owner_login}/telegram/allowed-user-id"
  type        = "String"
  value       = var.telegram_allowed_user_id
  description = "Telegram user ID allowed to interact with ${var.owner_login}'s bots"
}

resource "aws_ssm_parameter" "local_llm" {
  for_each = toset(local.local_llm_keys_to_create)

  name        = "/blitzlog/users/${var.owner_login}/local-llm/${each.value}"
  type        = contains(local.local_llm_secure_keys, each.value) ? "SecureString" : "String"
  value       = local.local_llm_param_values[each.value]
  description = "Local LLM config for ${var.owner_login}: ${each.value}"
}

# Per-pool git author identity (issue #119). Always emit when the
# pool is provisioned — the defaults derived from var.owner_login
# guarantee a usable identity for every pool. The two params land
# under /blitzlog/users/<owner>/git-author/{name,email} so both the
# EC2 agent runtime (existing /blitzlog/users/* grant on the EC2
# role) and the GHA worker (new narrow grant in
# infra/aws/bootstrap/deploy-role.tf) can fetch them with a single
# GetParameter call.
#
# Parameters are typed String (not SecureString) because the
# git author name and noreply email are public by design — they
# appear on every commit. The local-llm namespace is the
# precedent: 'allow-private-cidrs' and 'fallback' are also
# non-secret String params under /blitzlog/users/*.
resource "aws_ssm_parameter" "git_author_name" {
  name        = "/blitzlog/users/${var.owner_login}/git-author/name"
  type        = "String"
  value       = local.git_author_name_resolved
  description = "Git author name for ${var.owner_login}'s agent commits (issue #119)"

  lifecycle {
    # The resolved value derives from var.owner_login (or its
    # override) — ignore upstream changes so a re-apply after
    # owner_login edits doesn't churn the param when only the
    # resolved default changed. Operators who actually want to
    # change the value can update it via `aws ssm put-parameter
    # --overwrite` (the operator owns the value, not this stack).
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "git_author_email" {
  name        = "/blitzlog/users/${var.owner_login}/git-author/email"
  type        = "String"
  value       = local.git_author_email_resolved
  description = "Git author email for ${var.owner_login}'s agent commits (issue #119)"

  lifecycle {
    ignore_changes = [value]
  }
}

# Per-user OpenCode model + credential (issue #165). Level 2 of the
# three-level model selection: the task label wins, then this, then
# the org default.
#
# The namespace mirrors the org layout (/blitzlog/<env>/opencode/...)
# without the env prefix — a buyer configures their model and their
# credential once and both prod and dev Lambdas read the same
# values.
#
# The credential is a SecureString and the Lambda NEVER reads it: the
# Lambda resolves the PATH (/blitzlog/users/<login>/opencode/api-key)
# and the EC2 host fetches the key from there. The `provider` param
# is what the binding rule checks the selected model's provider
# against — a credential is only ever used with a model whose
# provider matches.
resource "aws_ssm_parameter" "opencode" {
  for_each = toset(local.opencode_keys_to_create)

  name        = "/blitzlog/users/${var.owner_login}/opencode/${each.value}"
  type        = contains(local.opencode_secure_keys, each.value) ? "SecureString" : "String"
  value       = local.opencode_param_values[each.value]
  description = "OpenCode config for ${var.owner_login}: ${each.value}"
}
