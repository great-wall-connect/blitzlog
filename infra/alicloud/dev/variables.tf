variable "alicloud_region" {
  description = "Alicloud region for all resources. Defaults to cn-hongkong — the region the blitzlog Alicloud account's OSS buckets live in (the state bucket `gwc-infra-tf-state` and the shared agent-logs / STT-models buckets). AWS analogue: aws_region."
  type        = string
  default     = "cn-hongkong"
}

variable "vpc_id" {
  description = "ID of the shared agent VPC. REQUIRED — Alicloud has no default VPC per region. Copy it from the bootstrap stack's output: 'cd infra/alicloud/bootstrap && terraform output vpc_id'. dev and prod share this VPC; their security groups keep them separate."
  type        = string
}

variable "vswitch_ids" {
  description = "IDs of the agent VSwitches. REQUIRED — an ECS launch requires a VSwitch. Copy them from the bootstrap stack's output: 'terraform output vswitch_ids'. One per AZ; the spot launcher picks one by AZ."
  type        = list(string)

  validation {
    condition     = length(var.vswitch_ids) > 0
    error_message = "vswitch_ids must contain at least one VSwitch id — an ECS launch requires a VSwitch. Copy the list from the bootstrap stack: 'terraform output vswitch_ids'."
  }
}

variable "ssh_allowed_cidrs" {
  description = "CIDR blocks allowed SSH access to dev ECS instances. Empty = no SSH ingress."
  type        = list(string)
  default     = []
}

variable "github_app_id" {
  description = "GitHub App ID (use a dedicated 'Blitzlog Dev' App — distinct from prod)"
  type        = string
}

variable "github_app_private_key" {
  description = "GitHub App private key (base64 encoded)"
  type        = string
  sensitive   = true
}

variable "github_app_installation_id" {
  description = "GitHub App installation ID (install on a throwaway sandbox repo only)"
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
  description = "OpenCode model ID for dev agents. Often set to the same provider/model as prod."
  type        = string
  default     = "minimax-coding-plan/MiniMax-M3"
}

variable "opencode_agent_max_steps" {
  description = "Max agentic iterations per opencode session for dev agents."
  type        = number
  default     = 500
}

variable "opencode_api_key" {
  description = "API key for the OpenCode inference provider used by dev agents."
  type        = string
  sensitive   = true
}

variable "agent_logs_bucket_name" {
  description = "Name of the OSS bucket that stores dev agent logs. Reuse the prod agent-logs bucket — dev keys land under the <env>/<repo>/... prefix."
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
  description = "Whisper model name. Model file must be uploaded to blitzlog-dev-stt-models under models/<name>.bin."
  type        = string
  default     = "base.en"
}

variable "stt_language" {
  description = "Whisper language hint passed to whisper-cli (empty = auto-detect)."
  type        = string
  default     = "en"
}

variable "stt_models_bucket_name" {
  description = "Name of the OSS bucket hosting whisper.cpp model files. Defaults to blitzlog-dev-stt-models. Must be globally unique across OSS."
  type        = string
  default     = ""
}

variable "spot_instance_types" {
  description = "ECS preemptible spot instance types the dev function will try, in preference order. Override in terraform.tfvars for regions without the default inventory."
  type        = list(string)
  default     = ["ecs.t6-c1m2.large", "ecs.t6-c1m4.large", "ecs.t6-c1m4.xlarge"]
}
