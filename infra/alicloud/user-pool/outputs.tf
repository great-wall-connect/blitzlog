# The OOS Parameter Store names of the per-user pool. The Alicloud
# analogue of the AWS sibling's ARN outputs — OOS parameters have no
# ARN attribute to hand out, so the name is the handle.

output "telegram_bot_pool_parameter_names" {
  description = "Names of the per-user Telegram bot pool OOS parameters"
  value       = { for k, p in alicloud_oos_secret_parameter.telegram_bot_pool : k => p.secret_parameter_name }
}

output "telegram_allowed_user_id_parameter_name" {
  description = "Name of the per-user allowed Telegram user ID OOS parameter"
  value       = alicloud_oos_secret_parameter.telegram_allowed_user_id.secret_parameter_name
}

output "owner_login" {
  description = "Owner login this pool belongs to"
  value       = var.owner_login
}

output "tailscale_auth_key_parameter_name" {
  description = "Name of the per-user Tailscale auth key OOS parameter (empty string when not configured)"
  value       = try(alicloud_oos_secret_parameter.local_llm["tailscale-auth-key"].secret_parameter_name, "")
}

output "git_author_name_parameter_name" {
  description = "Name of the per-user git author name OOS parameter (issue #119). Lives under blitzlog/users/<owner>/git-author/name."
  value       = alicloud_oos_secret_parameter.git_author_name.secret_parameter_name
}

output "git_author_email_parameter_name" {
  description = "Name of the per-user git author email OOS parameter (issue #119). Lives under blitzlog/users/<owner>/git-author/email."
  value       = alicloud_oos_secret_parameter.git_author_email.secret_parameter_name
}
