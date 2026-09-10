output "telegram_bot_pool_parameter_arns" {
  description = "ARNs of the per-user Telegram bot pool SSM parameters"
  value       = { for k, p in aws_ssm_parameter.telegram_bot_pool : k => p.arn }
}

output "telegram_allowed_user_id_parameter_arn" {
  description = "ARN of the per-user allowed Telegram user ID SSM parameter"
  value       = aws_ssm_parameter.telegram_allowed_user_id.arn
}

output "owner_login" {
  description = "Owner login this pool belongs to"
  value       = var.owner_login
}

output "tailscale_auth_key_parameter_arn" {
  description = "ARN of the per-user Tailscale auth key SSM parameter (empty string when not configured)"
  value       = try(aws_ssm_parameter.local_llm["tailscale-auth-key"].arn, "")
}
