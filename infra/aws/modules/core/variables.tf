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

variable "github_token_lifetime_hours" {
  description = "Requested lifetime in hours for the GitHub App installation token minted per webhook. Passed to the Lambda as GITHUB_TOKEN_LIFETIME_HOURS; the Lambda includes the matching `expires_at` field when POSTing to /app/installations/<id>/access_tokens. **GitHub caps installation tokens to 1 h regardless of the requested value** (verified empirically on 2026-10-13 — see issue #145); the value here is passed as `expires_at` to the API and the actual lifetime is `min(requested, 1h)`. The 1 h cap is operationally invisible to the agent because the Lambda schedules a 50-min refresh chain (see `lambda/handler.py` `action == refresh` and `infra/aws/packer/scripts-docker-ubuntu/blitzlog_token_poll.sh`) that re-mints the token and the watchdog on the EC2 instance picks up the new value within 5 min. Default 8 is preserved for historical reasons — the actual token is always 1 h, but a longer requested value documents the desired lifetime should GitHub ever raise the cap. Values < 1 are rejected."
  type        = number
  default     = 8

  validation {
    condition     = var.github_token_lifetime_hours >= 1
    error_message = "github_token_lifetime_hours must be >= 1."
  }
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

variable "spot_instance_types" {
  description = "EC2 spot instance types to try, in preference order (cheapest first when prices differ). Passed to the Lambda as the SPOT_INSTANCE_TYPES_JSON env var so operators can switch families per region without rebuilding the Lambda zip. Defaults to Arm (t4g.*) types; override in terraform.tfvars for regions without Arm spot inventory."
  type        = list(string)
  default     = ["t4g.medium", "t4g.large", "t4g.xlarge"]

  validation {
    condition     = length(var.spot_instance_types) > 0
    error_message = "spot_instance_types must contain at least one instance type."
  }

  validation {
    condition     = alltrue([for t in var.spot_instance_types : can(regex("^[a-z][0-9][a-z]?\\.[a-z0-9]+$", t))])
    error_message = "Each entry in spot_instance_types must be a valid EC2 instance type name (e.g. t4g.medium, m6i.large)."
  }
}

variable "lambda_requirements" {
  description = "Filename (relative to lambda/) of the per-cloud pip-compiled requirements file installed into the AWS Lambda zip. Defaults to requirements-aws.txt so the AWS deploy stays slim: boto3 + PyJWT + cryptography + requests only. The alibaba SDKs (oss2 / alibabacloud_kms20160120 / alibabacloud_ecs20140526) belong to lambda/requirements-alicloud.txt and are NOT shipped to the AWS deploy — alicloud is a Phase 5 follow-up (#124), and when that lands Function Compute ≠ Lambda so a parallel infra/alicloud/modules/core/lambda.tf analog (with `lambda_requirements = \"requirements-alicloud.txt\"`) will be added rather than reusing this module."
  type        = string
  default     = "requirements-aws.txt"

  validation {
    condition     = can(regex("^requirements-[a-z]+\\.txt$", var.lambda_requirements))
    error_message = "lambda_requirements must be a per-cloud requirements file (e.g. requirements-aws.txt, requirements-alicloud.txt)."
  }
}

# ---------------------------------------------------------------------------
# Scrummaster (epic #134, skeleton #135). See scrummaster.tf for why this
# is folded into `core` rather than kept as a separate stack.
# ---------------------------------------------------------------------------

variable "enable_scrummaster" {
  description = "Create the long-lived Scrummaster advisor host (t4g.nano + one ZeroClaw container per user-pool user). Env-scoped like the rest of this module: each env gets its own host, IAM objects and AMI, published to /blitzlog/<env>/agent-ami-id-zeroclaw-docker-ubuntu by infra/aws/packer/scrummaster-zeroclaw-docker-ubuntu.pkr.hcl. Off by default so a fresh install stays a single command — turning it on before the AMI has been baked would fail the plan at the data-source read."
  type        = bool
  default     = false
}

variable "zeroclaw_image_tag" {
  description = "Upstream ghcr.io/zeroclaw-labs/zeroclaw tag the AMI must have been baked with. Packer resolves it to a sha256 digest at build time; this stack refuses to launch an AMI whose ZeroclawTag differs."
  type        = string
  default     = "default-features-v0.8.6"
}

variable "ssh_key_name" {
  description = "Name of an existing EC2 key pair for `ssh ubuntu@<eip>` on the scrummaster. Empty launches without a key pair. Only used when enable_scrummaster is true."
  type        = string
  default     = ""
}

variable "enable_scrummaster_eip" {
  description = "Allocate and attach an Elastic IP to the scrummaster instance. Default false keeps the host without a public IP unless explicitly opted in. Only used when enable_scrummaster is true."
  type        = bool
  default     = false
}
