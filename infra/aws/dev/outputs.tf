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

# Scrummaster outputs are re-exported verbatim (no `dev ` prefix) because
# the resources are env-independent — see infra/aws/modules/core/scrummaster.tf.
# infra/aws/modules/core/scrummaster/tests/smoketest.sh reads public_ip and
# zeroclaw_image_tag from `terraform output` run in this directory.
output "scrummaster_instance_id" {
  description = "EC2 instance id of the dev scrummaster, or null when enable_scrummaster is false."
  value       = module.core.scrummaster_instance_id
}

output "scrummaster_public_ip" {
  description = "Elastic IP of the dev scrummaster when enable_scrummaster_eip is true, otherwise null (ssh ubuntu@<public_ip>)"
  value       = module.core.scrummaster_public_ip
}

output "scrummaster_ami_id" {
  description = "AMI the dev scrummaster was launched from, or null when enable_scrummaster is false."
  value       = module.core.scrummaster_ami_id
}

output "scrummaster_zeroclaw_image_tag" {
  description = "Upstream zeroclaw tag the dev scrummaster AMI was baked with"
  value       = module.core.scrummaster_zeroclaw_image_tag
}
