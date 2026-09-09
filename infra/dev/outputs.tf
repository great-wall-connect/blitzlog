output "webhook_url" {
  description = "Webhook URL for the dev GitHub App configuration (point your dev sandbox repo's webhook here)."
  value       = module.core.webhook_url
}

output "lambda_function_name" {
  description = "Name of the dev Lambda function"
  value       = module.core.lambda_function_name
}

output "lambda_function_arn" {
  description = "ARN of the dev Lambda function"
  value       = module.core.lambda_function_arn
}

output "ec2_security_group_id" {
  description = "Security group ID for dev EC2 instances"
  value       = module.core.ec2_security_group_id
}

output "ec2_instance_profile_name" {
  description = "IAM instance profile name for dev EC2 agent role"
  value       = module.core.ec2_instance_profile_name
}

output "api_gateway_id" {
  description = "dev API Gateway HTTP API ID"
  value       = module.core.api_gateway_id
}

output "stt_models_bucket" {
  description = "dev STT models S3 bucket name"
  value       = module.core.stt_models_bucket
}

output "ssm_root" {
  description = "dev SSM root (everything dev lives under this path)"
  value       = module.core.ssm_root
}
