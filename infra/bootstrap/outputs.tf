output "agent_logs_bucket_arn" {
  description = "ARN of the shared agent-logs bucket. Pass this into infra/prod/terraform.tfvars and infra/dev/terraform.tfvars as agent_logs_bucket_name."
  value       = aws_s3_bucket.agent_logs.arn
}

output "agent_logs_bucket_name" {
  description = "Name of the shared agent-logs bucket"
  value       = aws_s3_bucket.agent_logs.bucket
}

output "stt_models_bucket_arn" {
  description = "ARN of the shared STT models bucket. Pass this into infra/prod/terraform.tfvars and infra/dev/terraform.tfvars as stt_models_bucket_name (or override the default)."
  value       = aws_s3_bucket.stt_models.arn
}

output "stt_models_bucket_name" {
  description = "Name of the shared STT models bucket"
  value       = aws_s3_bucket.stt_models.bucket
}

# Reference outputs for the SSM parameters provisioned by
# secrets.tf. The operator uses these during one-time setup
# (README §"One-time operator setup for the deploy workflows")
# to drive `aws ssm put-parameter` invocations without having to
# remember the parameter name -> type mapping. The deploy
# workflows themselves don't read these outputs (they use
# `aws ssm get-parameters-by-path` directly).
output "deploy_parameter_names" {
  description = "Map of env -> list of SSM parameter names provisioned by secrets.tf. Reference for the one-time `aws ssm put-parameter` invocations that replace the bootstrap placeholders with real values."
  value = {
    for env in local.deploy_envs : env => sort(concat(
      [for leaf in local.deploy_string_leaves : "/blitzlog/${env}/${leaf}"],
      [for leaf in local.deploy_secure_leaves : "/blitzlog/${env}/${leaf}"],
    ))
  }
}

output "deploy_parameter_types" {
  description = "Map of SSM parameter name -> type (String or SecureString). Drives the `--type` flag of the one-time `aws ssm put-parameter` invocations."
  value = {
    for env in local.deploy_envs : env => {
      for leaf, type in local.deploy_parameter_types :
      "/blitzlog/${env}/${leaf}" => type
    }
  }
}
