output "agent_logs_bucket_arn" {
  description = "ARN of the shared agent-logs bucket. Pass this into infra/alicloud/{dev,prod}/alicloud.tfvars as agent_logs_bucket_name. The Alicloud OSS bucket ARN format is acs:oss:<region>:<account>:<bucket-name>."
  value       = "acs:oss:${var.alicloud_region}:${data.alicloud_account.current.id}:${alicloud_oss_bucket.agent_logs.bucket}"
}

output "agent_logs_bucket_name" {
  description = "Name of the shared agent-logs bucket"
  value       = alicloud_oss_bucket.agent_logs.bucket
}

output "stt_models_bucket_arn" {
  description = "ARN of the shared STT models bucket. Pass this into infra/alicloud/{dev,prod}/alicloud.tfvars as stt_models_bucket_name (or override the default)."
  value       = "acs:oss:${var.alicloud_region}:${data.alicloud_account.current.id}:${alicloud_oss_bucket.stt_models.bucket}"
}

output "stt_models_bucket_name" {
  description = "Name of the shared STT models bucket"
  value       = alicloud_oss_bucket.stt_models.bucket
}

# Add the data source the outputs reference.
data "alicloud_account" "current" {}
