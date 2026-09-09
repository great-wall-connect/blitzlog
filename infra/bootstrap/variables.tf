variable "aws_region" {
  description = "AWS region for the bootstrap resources"
  type        = string
  default     = "ap-east-1"
}

variable "agent_logs_bucket_name" {
  description = "Name of the shared S3 bucket that stores agent logs and session archives. Must be globally unique across AWS. Both prod and dev envs reference this bucket via a data source."
  type        = string
}

variable "stt_models_bucket_name" {
  description = "Name of the shared S3 bucket hosting whisper.cpp model files. Must be globally unique across AWS. Both prod and dev envs reference this bucket via a data source."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.stt_models_bucket_name))
    error_message = "S3 bucket names must be 3-63 characters, lowercase, and contain only letters, numbers, hyphens, and dots. Cannot start or end with a hyphen or dot."
  }
}
