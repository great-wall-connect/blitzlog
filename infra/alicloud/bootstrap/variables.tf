variable "alicloud_region" {
  description = "Alicloud region for the bootstrap stack"
  type        = string
  default     = "cn-hangzhou"
}

variable "agent_logs_bucket_name" {
  description = "Name of the shared agent-logs OSS bucket. Must be globally unique across OSS. AWS analogue: infra/aws/bootstrap/variables.tf:agent_logs_bucket_name."
  type        = string
}

variable "stt_models_bucket_name" {
  description = "Name of the shared STT-models OSS bucket. Must be globally unique across OSS."
  type        = string
}
