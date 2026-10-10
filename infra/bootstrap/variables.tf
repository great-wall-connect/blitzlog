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

# ----------------------------------------------------------------------------
# Per-env deploy-time config + secrets
#
# The bootstrap apply writes each of these directly to SSM Parameter Store
# under /blitzlog/<env>/<leaf>. The deploy workflows
# (release.yml's deploy job, terraform-apply-dev.yml) fetch them via
# `aws ssm get-parameters-by-path --path /blitzlog/<env>/` and feed them
# into terraform as TF_VAR_<name> env vars.
#
# Naming: <env>_<snake_case_leaf> matches the keys that
# scripts/tfvars-to-bootstrap.py emits from infra/<env>/terraform.tfvars.
# All are `sensitive = true` because the bootstrap state would otherwise
# contain plaintext values.
#
# Defaults mirror infra/<env>/variables.tf so the operator can omit any
# optional value from the bootstrap tfvars and pick up the same default
# the per-env apply would.
# ----------------------------------------------------------------------------

variable "dev_aws_region" {
  description = "AWS region for all resources (dev)"
  type        = string
  default     = "ap-east-1"
  sensitive   = true
}

variable "dev_vpc_id" {
  description = "VPC ID for EC2 instances and security group (dev)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "dev_ec2_subnet_id" {
  description = "Subnet ID for EC2 spot instances (dev)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "dev_ssh_allowed_cidrs" {
  description = "CIDR blocks allowed SSH access to dev EC2 instances. Empty = no SSH ingress (dev)"
  type        = list(string)
  default     = []
  sensitive   = true
}

variable "dev_github_app_id" {
  description = "GitHub App ID (use a dedicated 'Blitzlog Dev' App — distinct from prod)"
  type        = string
  sensitive   = true
}

variable "dev_github_app_private_key" {
  description = "GitHub App private key, base64-encoded PEM (dev)"
  type        = string
  sensitive   = true
}

variable "dev_github_app_installation_id" {
  description = "GitHub App installation ID (install on a throwaway sandbox repo only; dev)"
  type        = string
  sensitive   = true
}

variable "dev_github_webhook_secret" {
  description = "GitHub webhook HMAC secret (dev)"
  type        = string
  sensitive   = true
}

variable "dev_alert_email" {
  description = "Email address for CloudWatch alarm notifications. Empty = no email subscription (dev)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "dev_opencode_model" {
  description = "OpenCode model ID for dev agents. Often set to the same provider/model as prod."
  type        = string
  default     = "minimax-coding-plan/MiniMax-M3"
  sensitive   = true
}

variable "dev_opencode_agent_max_steps" {
  description = "Max agentic iterations per opencode session for dev agents. Same semantics as the module-level variable — when hit, opencode forces summarization."
  type        = number
  default     = 500
  sensitive   = true
}

variable "dev_opencode_api_key" {
  description = "API key for the OpenCode inference provider used by dev agents. Use a separate API key if your provider supports multiple keys."
  type        = string
  sensitive   = true
}

variable "dev_agent_logs_bucket_name" {
  description = "Name of the S3 bucket that stores dev agent logs. Reuse the prod agent-logs bucket — dev keys land under the <env>/<repo>/... prefix."
  type        = string
  default     = ""
  sensitive   = true
}

variable "dev_stt_api_url" {
  description = "Whisper-compatible STT endpoint URL (dev)"
  type        = string
  default     = "http://127.0.0.1:7878/v1"
  sensitive   = true
}

variable "dev_stt_api_key" {
  description = "API key forwarded by the Telegram bot to the STT provider (dev)"
  type        = string
  default     = "placeholder-not-used-by-localhost-shim"
  sensitive   = true
}

variable "dev_stt_model" {
  description = "Whisper model name. Model file must be uploaded to the STT models bucket under models/<name>.bin (dev)"
  type        = string
  default     = "base.en"
  sensitive   = true
}

variable "dev_stt_language" {
  description = "Whisper language hint passed to whisper-cli (empty = auto-detect; dev)"
  type        = string
  default     = "en"
  sensitive   = true
}

variable "dev_upload_stt_model" {
  description = "If true, terraform apply downloads the whisper model and uploads it to the STT models bucket (dev)"
  type        = bool
  default     = false
  sensitive   = true
}

variable "dev_stt_model_source_url" {
  description = "HTTPS base URL for whisper model files (dev)"
  type        = string
  default     = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
  sensitive   = true
}

variable "dev_stt_models_bucket_name" {
  description = "Name of the S3 bucket hosting whisper.cpp model files for dev. Defaults to blitzlog-dev-stt-models."
  type        = string
  default     = ""
  sensitive   = true
}

variable "dev_aws_profile" {
  description = "AWS profile name to use when the local-exec provisioner uploads the whisper model (dev). Empty (default) inherits AWS_PROFILE from the parent terraform process."
  type        = string
  default     = ""
  sensitive   = true
}

variable "dev_spot_instance_types" {
  description = "EC2 spot instance types the dev Lambda will try, in preference order. Forwarded to the Lambda as the SPOT_INSTANCE_TYPES_JSON env var so operators can switch families per region without rebuilding the Lambda zip. Override for regions without Arm (t4g.*) spot inventory."
  type        = list(string)
  default     = ["t4g.medium", "t4g.large", "t4g.xlarge"]
  sensitive   = true
}

variable "prod_aws_region" {
  description = "AWS region for all resources (prod)"
  type        = string
  default     = "ap-east-1"
  sensitive   = true
}

variable "prod_vpc_id" {
  description = "VPC ID for EC2 instances and security group (prod)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "prod_ec2_subnet_id" {
  description = "Subnet ID for EC2 spot instances (prod)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "prod_ssh_allowed_cidrs" {
  description = "CIDR blocks allowed SSH access to prod EC2 instances. Empty = no SSH ingress"
  type        = list(string)
  default     = []
  sensitive   = true
}

variable "prod_github_app_id" {
  description = "GitHub App ID (use a dedicated 'Blitzlog Prod' App)"
  type        = string
  sensitive   = true
}

variable "prod_github_app_private_key" {
  description = "GitHub App private key, base64-encoded PEM (prod)"
  type        = string
  sensitive   = true
}

variable "prod_github_app_installation_id" {
  description = "GitHub App installation ID (prod)"
  type        = string
  sensitive   = true
}

variable "prod_github_webhook_secret" {
  description = "GitHub webhook HMAC secret (prod)"
  type        = string
  sensitive   = true
}

variable "prod_alert_email" {
  description = "Email address for CloudWatch alarm notifications. Empty = no email subscription (prod)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "prod_opencode_model" {
  description = "OpenCode model ID for prod agents."
  type        = string
  default     = "minimax-coding-plan/MiniMax-M3"
  sensitive   = true
}

variable "prod_opencode_agent_max_steps" {
  description = "Max agentic iterations per opencode session for prod agents. Same semantics as the module-level variable — when hit, opencode forces summarization."
  type        = number
  default     = 500
  sensitive   = true
}

variable "prod_opencode_api_key" {
  description = "API key for the OpenCode inference provider (prod)"
  type        = string
  sensitive   = true
}

variable "prod_agent_logs_bucket_name" {
  description = "Name of the S3 bucket that stores agent logs and session archives (prod). Required; supply via terraform.tfvars."
  type        = string
  default     = ""
  sensitive   = true
}

variable "prod_stt_api_url" {
  description = "Whisper-compatible STT endpoint URL passed to the Telegram bot (default: localhost shim) (prod)"
  type        = string
  default     = "http://127.0.0.1:7878/v1"
  sensitive   = true
}

variable "prod_stt_api_key" {
  description = "API key forwarded by the Telegram bot to the STT provider (prod). The localhost shim ignores it, but the value cannot be empty (SSM rejects empty parameter values)."
  type        = string
  default     = "placeholder-not-used-by-localhost-shim"
  sensitive   = true
}

variable "prod_stt_model" {
  description = "Whisper model name. Model file must be uploaded to the STT models bucket under models/<name>.bin (prod)"
  type        = string
  default     = "base.en"
  sensitive   = true
}

variable "prod_stt_language" {
  description = "Whisper language hint passed to whisper-cli (empty = auto-detect; prod)"
  type        = string
  default     = "en"
  sensitive   = true
}

variable "prod_upload_stt_model" {
  description = "If true, terraform apply downloads the whisper model and uploads it to the STT models bucket (prod)"
  type        = bool
  default     = false
  sensitive   = true
}

variable "prod_stt_model_source_url" {
  description = "HTTPS base URL for whisper model files (prod)"
  type        = string
  default     = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
  sensitive   = true
}

variable "prod_stt_models_bucket_name" {
  description = "Name of the S3 bucket hosting whisper.cpp model files (prod). Defaults to blitzlog-prod-stt-models."
  type        = string
  default     = ""
  sensitive   = true
}

variable "prod_aws_profile" {
  description = "AWS profile name to use when the local-exec provisioner uploads the whisper model (prod). Empty (default) inherits AWS_PROFILE from the parent terraform process."
  type        = string
  default     = ""
  sensitive   = true
}

variable "prod_spot_instance_types" {
  description = "EC2 spot instance types the prod Lambda will try, in preference order. Forwarded to the Lambda as the SPOT_INSTANCE_TYPES_JSON env var so operators can switch families per region without rebuilding the Lambda zip. Override for regions without Arm (t4g.*) spot inventory."
  type        = list(string)
  default     = ["t4g.medium", "t4g.large", "t4g.xlarge"]
  sensitive   = true
}
