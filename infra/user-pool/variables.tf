variable "aws_region" {
  description = "AWS region for SSM parameters"
  type        = string
  default     = "ap-east-1"
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
