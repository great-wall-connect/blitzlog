output "webhook_url" {
  description = "Webhook URL for GitHub configuration"
  value       = module.core.webhook_url
}

output "lambda_function_name" {
  description = "Name of the prod Lambda function"
  value       = module.core.lambda_function_name
}

output "lambda_function_arn" {
  description = "ARN of the prod Lambda function"
  value       = module.core.lambda_function_arn
}

output "ec2_security_group_id" {
  description = "Security group ID for prod EC2 instances"
  value       = module.core.ec2_security_group_id
}

output "ec2_instance_profile_name" {
  description = "IAM instance profile name for prod EC2 agent role"
  value       = module.core.ec2_instance_profile_name
}

output "api_gateway_id" {
  description = "prod API Gateway HTTP API ID"
  value       = module.core.api_gateway_id
}

output "stt_models_bucket" {
  description = "prod STT models S3 bucket name"
  value       = module.core.stt_models_bucket
}

output "ssm_root" {
  description = "prod SSM root (everything prod lives under this path)"
  value       = module.core.ssm_root
}
