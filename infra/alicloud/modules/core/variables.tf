variable "environment" {
  description = "Environment name (e.g. prod, dev). Used to prefix all resource names and the KMS namespace so multiple Blitzlog environments can coexist in the same Alicloud account without collision. AWS analogue: infra/aws/modules/core/variables.tf:environment."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,30}[a-z0-9]$", var.environment))
    error_message = "environment must be lowercase alphanumeric with optional hyphens, 2-32 chars, and cannot start or end with a hyphen."
  }
}

variable "alicloud_region" {
  description = "Alicloud region for all resources. AWS analogue: aws_region."
  type        = string
  default     = "cn-hangzhou"
}

variable "vpc_id" {
  description = "VPC ID for ECS instances and security group. AWS analogue: same name (vpc_id)."
  type        = string
  default     = ""
}

variable "vswitch_ids" {
  description = "VSwitch IDs for ECS preemptible instances. ECS supports multiple VSwitches per VPC, so we accept a list. AWS analogue: ec2_subnet_id (single Subnet)."
  type        = list(string)
  default     = []
}

variable "ssh_allowed_cidrs" {
  description = "CIDR blocks allowed SSH access to ECS instances. Empty = no SSH ingress. AWS analogue: same name."
  type        = list(string)
  default     = []
}

variable "github_app_id" {
  description = "GitHub App ID. AWS analogue: same name."
  type        = string
}

variable "github_app_private_key" {
  description = "GitHub App private key (base64 encoded). AWS analogue: same name."
  type        = string
  sensitive   = true
}

variable "github_app_installation_id" {
  description = "GitHub App installation ID. AWS analogue: same name."
  type        = string
}

variable "github_token_lifetime_hours" {
  description = "Requested lifetime in hours for the GitHub App installation token. The actual lifetime is capped to 1 h by GitHub (issue #145). The Alicloud path has NO token-refresh chain equivalent in this PR — the function will hold a 1 h token only. AWS analogue: same name (with the EventBridge Scheduler chain)."
  type        = number
  default     = 8

  validation {
    condition     = var.github_token_lifetime_hours >= 1
    error_message = "github_token_lifetime_hours must be >= 1."
  }
}

variable "github_webhook_secret" {
  description = "GitHub webhook HMAC secret. AWS analogue: same name."
  type        = string
  sensitive   = true
}

variable "alert_email" {
  description = "Email address for Cloud Monitor alarm notifications. Empty = no email subscription. AWS analogue: same name (CloudWatch SNS)."
  type        = string
  default     = ""
}

variable "opencode_model" {
  description = "OpenCode model ID. AWS analogue: same name."
  type        = string
  default     = "minimax-coding-plan/MiniMax-M3"
}

variable "opencode_agent_max_steps" {
  description = "Maximum agentic iterations per opencode session. AWS analogue: same name."
  type        = number
  default     = 500
}

variable "opencode_api_key" {
  description = "OpenCode inference provider API key. AWS analogue: same name."
  type        = string
  sensitive   = true
}

variable "agent_logs_bucket_name" {
  description = "Name of the OSS bucket that stores agent logs and session archives. Must be globally unique across all OSS (same constraint as S3). AWS analogue: same name (S3 bucket)."
  type        = string
  default     = ""
}

variable "stt_api_url" {
  description = "Whisper-compatible STT endpoint URL. AWS analogue: same name."
  type        = string
  default     = "http://127.0.0.1:7878/v1"
}

variable "stt_api_key" {
  description = "API key forwarded by the Telegram bot to the STT provider. AWS analogue: same name."
  type        = string
  sensitive   = true
  default     = "placeholder-not-used-by-localhost-shim"
}

variable "stt_model" {
  description = "Whisper model name (e.g. base.en, tiny.en, small.en). AWS analogue: same name."
  type        = string
  default     = "base.en"
}

variable "stt_language" {
  description = "Whisper language hint passed to whisper-cli (empty = auto-detect). AWS analogue: same name."
  type        = string
  default     = "en"
}

variable "stt_models_bucket_name" {
  description = "Name of the OSS bucket hosting whisper.cpp model files. Defaults to blitzlog-<env>-stt-models. Must be globally unique across OSS. AWS analogue: same name."
  type        = string
  default     = ""

  validation {
    condition     = var.stt_models_bucket_name == "" || can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.stt_models_bucket_name))
    error_message = "OSS bucket names must be 3-63 characters, lowercase, and contain only letters, numbers, hyphens, and dots. Cannot start or end with a hyphen or dot."
  }
}

variable "spot_instance_types" {
  description = "ECS preemptible spot instance types to try, in preference order. Aliyun ECS instance type names follow ecs.<family>.<size>. AWS analogue: same name (EC2 instance types). Override in terraform.tfvars for regions without the default inventory."
  type        = list(string)
  default     = ["ecs.t6-c1m2.large", "ecs.t6-c1m4.large", "ecs.t6-c1m4.xlarge"]

  validation {
    condition     = length(var.spot_instance_types) > 0
    error_message = "spot_instance_types must contain at least one instance type."
  }

  validation {
    condition     = alltrue([for t in var.spot_instance_types : can(regex("^ecs\\.[a-z][0-9][a-z0-9.-]*\\.[a-z0-9]+$", t))])
    error_message = "Each entry in spot_instance_types must be a valid Aliyun ECS instance type name (e.g. ecs.t6-c1m2.large)."
  }
}

variable "lambda_requirements" {
  description = "Filename (relative to lambda/) of the per-cloud pip-compiled requirements file installed into the Function Compute zip. Defaults to requirements-alicloud.txt so the alicloud path ships the alibaba SDKs (oss2, alibabacloud_kms20160120, alibabacloud_ecs20140526) without dragging them into the AWS Lambda zip. AWS analogue: infra/aws/modules/core/variables.tf:lambda_requirements (defaults to requirements-aws.txt there)."
  type        = string
  default     = "requirements-alicloud.txt"

  validation {
    condition     = can(regex("^requirements-[a-z]+\\.txt$", var.lambda_requirements))
    error_message = "lambda_requirements must be a per-cloud requirements file (e.g. requirements-alicloud.txt)."
  }
}
