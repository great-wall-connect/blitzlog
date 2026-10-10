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
