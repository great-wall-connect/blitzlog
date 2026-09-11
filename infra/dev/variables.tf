variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "ap-east-1"
}

variable "vpc_id" {
  description = "VPC ID for EC2 instances and security group. Reuse the prod VPC or pick a separate one — same VPC is fine for dev since security groups are env-scoped."
  type        = string
  default     = ""
}

variable "ec2_subnet_id" {
  description = "Subnet ID for EC2 spot instances."
  type        = string
  default     = ""
}

variable "ssh_allowed_cidrs" {
  description = "CIDR blocks allowed SSH access to dev EC2 instances. Empty = no SSH ingress."
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
  description = "Email address for CloudWatch alarm notifications. Empty = no email subscription."
  type        = string
  default     = ""
}

variable "opencode_model" {
  description = "OpenCode model ID for dev agents. Often set to the same provider/model as prod."
  type        = string
  default     = "minimax-coding-plan/MiniMax-M3"
}

variable "agent_os_family" {
  description = "OS family for the dev agent AMI (al2023 or ubuntu). Defaults to al2023."
  type        = string
  default     = "al2023"

  validation {
    condition     = contains(["al2023", "ubuntu"], var.agent_os_family)
    error_message = "agent_os_family must be 'al2023' or 'ubuntu'."
  }
}

variable "opencode_api_key" {
  description = "API key for the OpenCode inference provider used by dev agents. Use a separate API key if your provider supports multiple keys."
  type        = string
  sensitive   = true
}

variable "agent_logs_bucket_name" {
  description = "Name of the S3 bucket that stores dev agent logs. Reuse the prod agent-logs bucket — dev keys land under the <env>/<repo>/... prefix."
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

variable "upload_stt_model" {
  description = "If true, terraform apply downloads the whisper model and uploads it to s3://blitzlog-dev-stt-models/."
  type        = bool
  default     = false
}

variable "stt_model_source_url" {
  description = "HTTPS base URL for whisper model files."
  type        = string
  default     = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
}

variable "stt_models_bucket_name" {
  description = "Name of the S3 bucket hosting whisper.cpp model files. Defaults to blitzlog-dev-stt-models. Must be globally unique across AWS."
  type        = string
  default     = ""
}

variable "aws_profile" {
  description = "AWS profile name to use when the local-exec provisioner uploads the whisper model. Empty (default) inherits AWS_PROFILE from the parent terraform process."
  type        = string
  default     = ""
}
