output "webhook_url" {
  description = "Webhook URL for the prod GitHub App configuration. On Alicloud the actual URL is published by API Gateway post-apply; the value here is the API id, used as a placeholder until the operator wires up the Function Compute backend in the Aliyun console."
  value       = module.core.webhook_url
}

output "lambda_function_name" {
  description = "Name of the prod Function Compute function"
  value       = module.core.lambda_function_name
}

output "lambda_function_arn" {
  description = "ARN of the prod Function Compute function"
  value       = module.core.lambda_function_arn
}

output "ec2_security_group_id" {
  description = "Security group ID for prod ECS instances"
  value       = module.core.ec2_security_group_id
}

output "ec2_instance_profile_name" {
  description = "RAM role name for prod ECS agent role (Alicloud has no instance-profile concept; the role is attached directly to the ECS instance via RunInstances)"
  value       = module.core.ec2_instance_profile_name
}

output "api_gateway_id" {
  description = "prod API Gateway API ID"
  value       = module.core.api_gateway_id
}

output "stt_models_bucket" {
  description = "prod STT models OSS bucket name"
  value       = module.core.stt_models_bucket
}

output "ssm_root" {
  description = "prod KMS root (everything prod lives under this path)"
  value       = module.core.ssm_root
}
