variable "alicloud_region" {
  description = "Alicloud region for the bootstrap stack. Defaults to cn-hongkong — the region the blitzlog Alicloud account's OSS buckets live in (the state bucket `gwc-infra-tf-state` and the shared agent-logs / STT-models buckets). AWS analogue: aws_region."
  type        = string
  default     = "cn-hongkong"
}

variable "agent_logs_bucket_name" {
  description = "Name of the shared agent-logs OSS bucket. Must be globally unique across OSS. AWS analogue: infra/aws/bootstrap/variables.tf:agent_logs_bucket_name."
  type        = string
}

variable "stt_models_bucket_name" {
  description = "Name of the shared STT-models OSS bucket. Must be globally unique across OSS."
  type        = string
}
