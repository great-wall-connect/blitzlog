variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "ap-east-1"
}

variable "vpc_id" {
  description = "VPC ID for EC2 instances and security group. Leave empty to require setting in terraform.tfvars."
  type        = string
  default     = ""
}

variable "ec2_subnet_id" {
  description = "Subnet ID for EC2 spot instances. Leave empty to require setting in terraform.tfvars."
  type        = string
  default     = ""
}

variable "ssh_allowed_cidrs" {
  description = "CIDR blocks allowed SSH access to EC2 instances. Empty = no SSH ingress."
  type        = list(string)
  default     = []
}

variable "github_app_id" {
  description = "GitHub App ID"
  type        = string
}

variable "github_app_private_key" {
  description = "GitHub App private key (base64 encoded)"
  type        = string
  sensitive   = true
}

variable "github_app_installation_id" {
  description = "GitHub App installation ID"
  type        = string
}

variable "github_webhook_secret" {
  description = "GitHub webhook HMAC secret"
  type        = string
  sensitive   = true
}

variable "alert_email" {
  description = "Email address for CloudWatch alarm notifications. Empty = no email subscription."
  type        = string
  default     = ""
}

variable "opencode_model" {
  description = "OpenCode model ID (e.g. <provider>/<model>). Defaults to a MiniMax coding-plan model."
  type        = string
  default     = "minimax-coding-plan/MiniMax-M3"
}

variable "opencode_api_key" {
  description = "API key for the OpenCode inference provider"
  type        = string
  sensitive   = true
}

variable "agent_logs_bucket_name" {
  description = "Name of the S3 bucket that stores agent logs and session archives. Required; supply via terraform.tfvars."
  type        = string
  default     = ""
}

variable "stt_api_url" {
  description = "Whisper-compatible STT endpoint URL passed to the Telegram bot (default: localhost shim)."
  type        = string
  default     = "http://127.0.0.1:7878/v1"
}

variable "stt_api_key" {
  description = "API key forwarded by the Telegram bot to the STT provider. The localhost shim ignores it, but the value cannot be empty (SSM rejects empty parameter values). Override in tfvars if you want a custom placeholder."
  type        = string
  sensitive   = true
  default     = "placeholder-not-used-by-localhost-shim"
}

variable "stt_model" {
  description = "Whisper model name (e.g. base.en, tiny.en, small.en). Model file must be uploaded to the blitzlog-stt-models S3 bucket under models/<name>.bin."
  type        = string
  default     = "base.en"
}

variable "stt_language" {
  description = "Whisper language hint passed to whisper-cli (empty = auto-detect)."
  type        = string
  default     = "en"
}

variable "upload_stt_model" {
  description = "If true, terraform apply downloads the whisper model from stt_model_source_url and uploads it to s3://blitzlog-stt-models/. Requires outbound HTTPS from the Terraform host to the source URL, and s3:PutObject on the bucket from the Terraform host's credentials."
  type        = bool
  default     = false
}

variable "stt_model_source_url" {
  description = "HTTPS base URL for whisper model files. The shim downloads ggml-<stt_model>.bin by appending the model name to this URL (e.g. https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin when stt_model=base.en)."
  type        = string
  default     = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
}

variable "stt_models_bucket_name" {
  description = "Name of the S3 bucket hosting whisper.cpp model files. S3 bucket names must be globally unique across AWS, so open-source users need to override this (e.g. \"myorg-blitzlog-stt-models\"). Must be 3-63 chars, lowercase, may contain letters, numbers, hyphens, and dots."
  type        = string
  default     = "blitzlog-stt-models"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.stt_models_bucket_name))
    error_message = "S3 bucket names must be 3-63 characters, lowercase, and contain only letters, numbers, hyphens, and dots. Cannot start or end with a hyphen or dot."
  }
}

variable "aws_profile" {
  description = "AWS profile name to use when the local-exec provisioner uploads the whisper model. Empty (default) inherits AWS_PROFILE from the parent terraform process. Set explicitly (e.g. \"terraform\") when running from CI or wrappers that don't propagate env vars to subprocesses."
  type        = string
  default     = ""
}