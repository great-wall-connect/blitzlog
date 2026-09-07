variable "aws_region" {
  description = "AWS region for SSM parameters"
  type        = string
  default     = "ap-east-1"
}

variable "environment" {
  description = "Environment name (prod, dev). SSM parameters are namespaced under /blitzlog/<environment>/users/<owner_login>/..."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,30}[a-z0-9]$", var.environment))
    error_message = "environment must be lowercase alphanumeric with optional hyphens, 2-32 chars, and cannot start or end with a hyphen."
  }
}

variable "owner_login" {
  description = "GitHub login of the user who owns this bot pool"
  type        = string
}

variable "telegram_bot_tokens" {
  description = "Map of Telegram bot names to tokens for this user's pool"
  type        = map(string)
  sensitive   = true
}

variable "telegram_allowed_user_id" {
  description = "Telegram user ID allowed to interact with this user's bots"
  type        = string
}
