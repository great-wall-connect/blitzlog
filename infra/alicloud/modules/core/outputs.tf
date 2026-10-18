# Analogue of infra/aws/modules/core/outputs.tf.
#
# Output names are byte-for-byte identical to the AWS-side outputs
# (the `webhook_url` / `lambda_function_name` / etc. consumers in
# release.yml and the GHA workflows don't care which cloud
# produced the value — they read by output name).

output "webhook_url" {
  description = "Webhook URL for GitHub configuration. The Aliyun-side URL is published via API Gateway; for now this returns the API id, since the actual invocation URL is provisioned by the operator (see apigateway.tf)."
  value       = alicloud_api_gateway_api.webhook.id
}

output "lambda_function_name" {
  description = "Name of the Function Compute function"
  value       = alicloud_fc_function.handler.name
}

output "lambda_function_arn" {
  description = "ARN of the Function Compute function. Aliyun uses `acs:fc:<region>:<account>:functions/<service>/<name>` — this is the value the API Gateway backend config will reference."
  value       = alicloud_fc_function.handler.function_arn
}

output "ec2_security_group_id" {
  description = "Security group ID for ECS instances. AWS analogue: ec2_security_group_id."
  value       = alicloud_security_group.agent_sg.id
}

output "ec2_instance_profile_name" {
  description = "RAM role name for ECS agent role. AWS analogue: ec2_instance_profile_name (IAM instance profile). On Alicloud the RAM role is attached directly to the ECS instance via the RunInstances `RamRoleName` parameter, so this is just the role name."
  value       = alicloud_ram_role.ecs_agent_role.name
}

output "api_gateway_id" {
  description = "API Gateway API ID. AWS analogue: api_gateway_id."
  value       = alicloud_api_gateway_api.webhook.id
}

output "stt_models_bucket" {
  description = "OSS bucket hosting whisper.cpp model files for the local STT shim. Upload ggml-<model>.bin to models/ before first boot. AWS analogue: stt_models_bucket."
  value       = data.alicloud_oss_buckets.stt_models.buckets[0].name
}

output "environment" {
  description = "Environment name this stack was deployed with."
  value       = var.environment
}

output "ssm_root" {
  description = "Root KMS namespace for this environment (everything per-env lives under this path). AWS analogue: ssm_root."
  value       = local.ssm_root
}
