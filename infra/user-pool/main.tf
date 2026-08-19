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
}

resource "aws_ssm_parameter" "telegram_bot_pool" {
  for_each = toset(local.bot_names)

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