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

  local_llm_endpoint_set = local.local_llm_endpoint != ""
  local_llm_model_set    = local.local_llm_model != ""

  local_llm_params = {
    for k, v in {
      "endpoint"            = local.local_llm_endpoint
      "model"               = local.local_llm_model
      "api-key"             = local.local_llm_api_key
      "allow-private-cidrs" = local.local_llm_allow_private ? "true" : "false"
      "fallback"            = local.local_llm_fallback
    } :
    k => v
    if contains(["allow-private-cidrs", "fallback"], k) || (k == "api-key") || local.local_llm_endpoint_set
  }

  local_llm_secure_keys = ["endpoint", "model", "api-key"]
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
  for_each = local.local_llm_params

  name        = "/blitzlog/users/${var.owner_login}/local-llm/${each.key}"
  type        = contains(local.local_llm_secure_keys, each.key) ? "SecureString" : "String"
  value       = each.value
  description = "Local LLM config for ${var.owner_login}: ${each.key}"
}
