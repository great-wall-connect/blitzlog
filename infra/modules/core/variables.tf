variable "environment" {
  description = "Environment name (e.g. prod, dev). Used to prefix all resource names and the SSM namespace so multiple Blitzlog environments can coexist in the same AWS account without collision."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,30}[a-z0-9]$", var.environment))
    error_message = "environment must be lowercase alphanumeric with optional hyphens, 2-32 chars, and cannot start or end with a hyphen."
  }
}

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

variable "opencode_agent_max_steps" {
  description = "Maximum number of agentic iterations the opencode CLI is allowed per session. When the per-session counter reaches this cap, opencode injects MAX_STEPS_PROMPT and forces a summarization — the agent stops mid-task. Default 500 is sized well above a typical multi-file change (variable + script + wrapper + tests + docs + verify + commit + PR) but still acts as a safety net against runaway loops. Set higher for particularly large tasks; lower for cost-sensitive deployments."
  type        = number
  default     = 500
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
  description = "Whisper model name (e.g. base.en, tiny.en, small.en). Model file must be uploaded to the blitzlog-<env>-stt-models S3 bucket under models/<name>.bin."
  type        = string
  default     = "base.en"
}

variable "stt_language" {
  description = "Whisper language hint passed to whisper-cli (empty = auto-detect)."
  type        = string
  default     = "en"
}

variable "upload_stt_model" {
  description = "If true, terraform apply downloads the whisper model from stt_model_source_url and uploads it to s3://blitzlog-<env>-stt-models/. Requires outbound HTTPS from the Terraform host to the source URL, and s3:PutObject on the bucket from the Terraform host's credentials."
  type        = bool
  default     = false
}

variable "stt_model_source_url" {
  description = "HTTPS base URL for whisper model files. The shim downloads ggml-<stt_model>.bin by appending the model name to this URL (e.g. https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin when stt_model=base.en)."
  type        = string
  default     = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
}

variable "stt_models_bucket_name" {
  description = "Name of the S3 bucket hosting whisper.cpp model files. Defaults to blitzlog-<env>-stt-models. S3 bucket names must be globally unique across AWS, so open-source users need to override this."
  type        = string
  default     = ""

  validation {
    condition     = var.stt_models_bucket_name == "" || can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.stt_models_bucket_name))
    error_message = "S3 bucket names must be 3-63 characters, lowercase, and contain only letters, numbers, hyphens, and dots. Cannot start or end with a hyphen or dot."
  }
}

variable "aws_profile" {
  description = "AWS profile name to use when the local-exec provisioner uploads the whisper model. Empty (default) inherits AWS_PROFILE from the parent terraform process. Set explicitly (e.g. \"terraform\") when running from CI or wrappers that don't propagate env vars to subprocesses."
  type        = string
  default     = ""
}

variable "github_webhook_check_token" {
  description = "GitHub token used by the post-apply drift check to verify the configured webhook secret matches what GitHub has stored on the repo's webhook. Accepts a fine-grained PAT with repository webhooks: read, or a classic PAT with repo scope. Empty (default) disables the check entirely — the drift check is opt-in to avoid leaving a token footprint for operators who don't need it."
  type        = string
  default     = null
  sensitive   = true
}

variable "github_webhook_check_repos" {
  description = "List of <owner>/<repo> pairs whose webhooks the drift check should verify. The check lists each repo's active webhooks via the GitHub API and POSTs a signed probe to each one; a non-2xx response means the configured SSM secret no longer matches what GitHub has. Empty list (default) disables the check — set github_webhook_check_token AND this variable together to enable."
  type        = list(string)
  default     = []
}
