variable "alicloud_region" {
  description = "Alicloud region for all resources"
  type        = string
  default     = "cn-hangzhou"
}

variable "vpc_id" {
  description = "VPC ID for ECS instances and security group. Reuse the prod VPC or pick a separate one."
  type        = string
  default     = ""
}

variable "vswitch_ids" {
  description = "VSwitch IDs for ECS spot instances. One per AZ."
  type        = list(string)
  default     = []
}

variable "ssh_allowed_cidrs" {
  description = "CIDR blocks allowed SSH access to prod ECS instances. Empty = no SSH ingress."
  type        = list(string)
  default     = []
}

variable "github_app_id" {
  description = "GitHub App ID (use the prod 'Blitzlog' App — distinct from dev)"
  type        = string
}

variable "github_app_private_key" {
  description = "GitHub App private key (base64 encoded)"
  type        = string
  sensitive   = true
}

variable "github_app_installation_id" {
  description = "GitHub App installation ID (install on real user repos only)"
  type        = string
}

variable "github_webhook_secret" {
  description = "GitHub webhook HMAC secret"
  type        = string
  sensitive   = true
}

variable "alert_email" {
  description = "Email address for Cloud Monitor alarm notifications. Empty = no email subscription."
  type        = string
  default     = ""
}

variable "opencode_model" {
  description = "OpenCode model ID for prod agents."
  type        = string
  default     = "minimax-coding-plan/MiniMax-M3"
}

variable "opencode_agent_max_steps" {
  description = "Max agentic iterations per opencode session for prod agents."
  type        = number
  default     = 500
}

variable "opencode_api_key" {
  description = "API key for the OpenCode inference provider used by prod agents."
  type        = string
  sensitive   = true
}

variable "agent_logs_bucket_name" {
  description = "Name of the OSS bucket that stores prod agent logs."
  type        = string
  default     = ""
}

variable "stt_api_url" {
  description = "Whisper-compatible STT endpoint URL."
  type        = string
  default     = "http://127.0.0.1:7878/v1"
}

variable "stt_api_key" {
  description = "API key forwarded by the Telegram bot to the STT provider."
  type        = string
  sensitive   = true
  default     = "placeholder-not-used-by-localhost-shim"
}

variable "stt_model" {
  description = "Whisper model name. Model file must be uploaded to blitzlog-prod-stt-models under models/<name>.bin."
  type        = string
  default     = "base.en"
}

variable "stt_language" {
  description = "Whisper language hint passed to whisper-cli (empty = auto-detect)."
  type        = string
  default     = "en"
}

variable "stt_models_bucket_name" {
  description = "Name of the OSS bucket hosting whisper.cpp model files. Defaults to blitzlog-prod-stt-models. Must be globally unique across OSS."
  type        = string
  default     = ""
}

variable "spot_instance_types" {
  description = "ECS preemptible spot instance types the prod function will try, in preference order."
  type        = list(string)
  default     = ["ecs.t6-c1m2.large", "ecs.t6-c1m4.large", "ecs.t6-c1m4.xlarge"]
}
