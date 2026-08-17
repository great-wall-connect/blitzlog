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

variable "tf_state_bucket" {
  description = "S3 bucket for Terraform state backend. Required; supply via terraform.tfvars or a -backend.hcl file."
  type        = string
  default     = ""
}

variable "agent_logs_bucket_name" {
  description = "Name of the S3 bucket that stores agent logs and session archives. Required; supply via terraform.tfvars."
  type        = string
  default     = ""
}