output "webhook_url" {
  description = "Webhook URL for GitHub configuration"
  value       = "${aws_apigatewayv2_api.webhook.api_endpoint}/"
}

output "lambda_function_name" {
  description = "Name of the Lambda function"
  value       = aws_lambda_function.handler.function_name
}

output "lambda_function_arn" {
  description = "ARN of the Lambda function"
  value       = aws_lambda_function.handler.arn
}

output "ec2_security_group_id" {
  description = "Security group ID for EC2 instances"
  value       = aws_security_group.agent_sg.id
}

output "ec2_instance_profile_name" {
  description = "IAM instance profile name for EC2 agent role"
  value       = aws_iam_instance_profile.ec2_agent_profile.name
}

output "api_gateway_id" {
  description = "API Gateway HTTP API ID"
  value       = aws_apigatewayv2_api.webhook.id
}

output "stt_models_bucket" {
  description = "S3 bucket hosting whisper.cpp model files for the local STT shim. Upload ggml-<model>.bin to models/ before first boot."
  value       = data.aws_s3_bucket.stt_models.bucket
}

output "environment" {
  description = "Environment name this stack was deployed with."
  value       = var.environment
}

output "ssm_root" {
  description = "Root SSM namespace for this environment (everything per-env lives under this path)."
  value       = local.ssm_root
}

output "scrummaster_instance_id" {
  description = "EC2 instance id of the scrummaster, or null when enable_scrummaster is false."
  value       = try(aws_instance.scrummaster[0].id, null)
}

output "scrummaster_public_ip" {
  description = "Elastic IP of the scrummaster when enable_scrummaster_eip is true (ssh ubuntu@<public_ip>), otherwise null"
  value       = try(aws_eip.scrummaster[0].public_ip, null)
}

output "scrummaster_ami_id" {
  description = "AMI the scrummaster was launched from, or null when enable_scrummaster is false."
  value       = try(data.aws_ami.scrummaster[0].id, null)
}

output "scrummaster_zeroclaw_image_tag" {
  description = "Upstream zeroclaw tag the AMI was baked with (the running digest is in /opt/zeroclaw/images/zeroclaw.ref on the host)"
  value       = var.zeroclaw_image_tag
}
