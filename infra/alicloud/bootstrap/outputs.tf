output "agent_logs_bucket_arn" {
  description = "ARN of the shared agent-logs bucket. Pass this into infra/alicloud/{dev,prod}/terraform.tfvars as agent_logs_bucket_name. The Alicloud OSS bucket ARN format is acs:oss:<location>:<account>:<bucket-name> — the location is the bucket's OSS location (oss-cn-hongkong), not the region id."
  value       = "acs:oss:${alicloud_oss_bucket.agent_logs.location}:${data.alicloud_account.current.id}:${alicloud_oss_bucket.agent_logs.bucket}"
}

output "agent_logs_bucket_name" {
  description = "Name of the shared agent-logs bucket"
  value       = alicloud_oss_bucket.agent_logs.bucket
}

output "stt_models_bucket_arn" {
  description = "ARN of the shared STT models bucket. Pass this into infra/alicloud/{dev,prod}/terraform.tfvars as stt_models_bucket_name (or override the default). The Alicloud OSS bucket ARN format is acs:oss:<location>:<account>:<bucket-name> — the location is the bucket's OSS location (oss-cn-hongkong), not the region id."
  value       = "acs:oss:${alicloud_oss_bucket.stt_models.location}:${data.alicloud_account.current.id}:${alicloud_oss_bucket.stt_models.bucket}"
}

output "stt_models_bucket_name" {
  description = "Name of the shared STT models bucket"
  value       = alicloud_oss_bucket.stt_models.bucket
}

output "vpc_id" {
  description = "ID of the shared agent VPC. Pass this into infra/alicloud/{dev,prod}/terraform.tfvars as vpc_id — the ECS instances and the security group attach to it."
  value       = alicloud_vpc.agent.id
}

output "vswitch_ids" {
  description = "IDs of the shared agent VSwitches. Pass this into infra/alicloud/{dev,prod}/terraform.tfvars as vswitch_ids — an ECS launch requires a VSwitch."
  value       = alicloud_vswitch.agent[*].id
}

output "kms_key_id" {
  description = "ID of the CMK that encrypts the OOS parameters. The CI reads it from the `blitzlog/shared/kms-key-id` OOS parameter; a local apply of a per-env stack copies it into terraform.tfvars."
  value       = alicloud_kms_key.secrets.id
}

output "packer_eip_id" {
  description = "ID of the elastic IP the Packer build instance uses. Pass it to the bake as `-var eip_id=...` (the CI bake job reads it from vars.ALICLOUD_EIP_ID)."
  value       = alicloud_eip_address.packer.id
}

# Add the data source the outputs reference.
data "alicloud_account" "current" {}
