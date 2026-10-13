variable "aws_region" {
  description = "AWS region for the scrummaster host and its SSM grant. Defaults to ap-east-1 to match the rest of the blitzlog infra."
  type        = string
  default     = "ap-east-1"
}

variable "vpc_id" {
  description = "VPC ID hosting the scrummaster instance. Reuse the existing dev/prod VPC (same VPC is fine — the security group isolates it)."
  type        = string
  default     = ""
}

variable "subnet_id" {
  description = "Subnet ID for the scrummaster instance. Pick a public subnet so SSH + outbound HTTPS work without NAT gymnastics."
  type        = string
  default     = ""
}

variable "ssh_allowed_cidrs" {
  description = "CIDR blocks allowed SSH ingress to the scrummaster host. Empty list = no SSH ingress at all (rely on SSM Session Manager)."
  type        = list(string)
  default     = []
}

variable "ssh_key_name" {
  description = "Optional EC2 keypair name to attach for SSH. Empty = no keypair (SSM Session Manager remains the fallback)."
  type        = string
  default     = ""
}

variable "instance_type" {
  description = "EC2 instance type for the scrummaster host. Issue #135 pins t4g.nano for cost (~\\$3-5/mo); override only when testing capacity."
  type        = string
  default     = "t4g.nano"

  validation {
    condition     = can(regex("^[a-z][0-9][a-z]?\\.[a-z0-9]+$", var.instance_type))
    error_message = "instance_type must be a valid EC2 instance type name (e.g. t4g.nano)."
  }
}

variable "ami_id" {
  description = "AMI ID to launch. Empty (default) resolves to the latest Canonical-published Ubuntu 26.04 LTS arm64 minimal image via a data source — same family the Packer agent bake uses. Override to pin a hardened / patched image."
  type        = string
  default     = ""
}

variable "root_volume_size_gb" {
  description = "Root EBS volume size in GiB. 20 GiB is plenty for a ZeroClaw-only host; raise when operators expect heavy SQLite memory."
  type        = number
  default     = 20

  validation {
    condition     = var.root_volume_size_gb >= 8
    error_message = "root_volume_size_gb must be >= 8 GiB (Ubuntu 26.04 arm64 minimal image is ~3 GiB; leave headroom)."
  }
}

variable "zeroclaw_release_tag" {
  description = "GitHub release tag of the ZeroClaw binary to install from https://github.com/zeroclaw-labs/zeroclaw/releases. Defaults to the latest stable tag; pin a version for reproducibility."
  type        = string
  default     = "latest"
}