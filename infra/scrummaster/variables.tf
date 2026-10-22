variable "aws_region" {
  description = "AWS region for the scrummaster instance"
  type        = string
  default     = "ap-east-1"
}

variable "zeroclaw_image_tag" {
  description = "Upstream ghcr.io/zeroclaw-labs/zeroclaw tag the AMI must have been baked with. Packer resolves it to a sha256 digest at build time; this stack refuses to launch an AMI whose ZeroclawTag differs."
  type        = string
  default     = "default-features-v0.8.6"
}

variable "ssh_allowed_cidrs" {
  description = "CIDRs allowed to SSH (port 22) into the scrummaster. Empty disables SSH ingress."
  type        = list(string)
  default     = []
}

variable "ssh_key_name" {
  description = "Name of an existing EC2 key pair for `ssh ubuntu@<eip>`. Empty launches without a key pair."
  type        = string
  default     = ""
}

variable "enable_eip" {
  description = "Whether to allocate and attach an Elastic IP to the scrummaster instance. Default false keeps the host without a public IP unless explicitly opted in."
  type        = bool
  default     = false
}

variable "vpc_id" {
  description = "VPC to place the scrummaster security group in"
  type        = string
}

variable "subnet_id" {
  description = "Public subnet for the scrummaster instance (must route 0.0.0.0/0 to an internet gateway for the EIP to work)"
  type        = string
}
