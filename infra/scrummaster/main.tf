# infra/scrummaster/ — operator-managed read-only advisor host (issue #135).
#
# One long-lived on-demand t4g.nano running dockerd + one ZeroClaw
# container per user (zeroclaw@<login>.service, activated in #136). This
# stack is operator hosting — blitzlog-agent runs must not modify it
# (see AGENTS.md). Like infra/user-pool/, it is env-independent: there is
# no var.environment and nothing here is prod- or dev-scoped.
#
# The instance is a pet: `disable_api_termination` blocks console/API
# terminates and `prevent_destroy` makes `terraform destroy` (and any
# plan that would replace the instance, e.g. after an AMI rebuild) fail.
# To retire or replace it, deliberately flip both guards in a commit.

terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # bucket comes from scrummaster-backend.hcl (gitignored, like the
  # prod/dev/bootstrap *-backend.hcl files):
  #   terraform init -backend-config=scrummaster-backend.hcl
  backend "s3" {
    bucket = ""
    key    = "scrummaster/blitzlog.tfstate"
    region = "ap-east-1"
  }
}

provider "aws" {
  region = var.aws_region
}

# Published by infra/packer/scripts-scrummaster-ubuntu/02-publish-ami-id.sh.
data "aws_ssm_parameter" "scrummaster_ami_id" {
  name = "/blitzlog/scrummaster/agent-ami-id-zeroclaw-ubuntu"
}

data "aws_ami" "scrummaster" {
  owners = ["self"]

  filter {
    name   = "image-id"
    values = [nonsensitive(data.aws_ssm_parameter.scrummaster_ami_id.value)]
  }
}

# ---------------------------------------------------------------------------
# IAM — one role for all users. SSM read on /blitzlog/users/* is the only
# grant (refined per user in #136). No AmazonSSMManagedInstanceCore: that
# managed policy grants ssm:GetParameter on "*", which would widen the
# blast radius beyond the per-user namespace.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "scrummaster" {
  name = "blitzlog-scrummaster-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "ec2.amazonaws.com" }
        Action    = "sts:AssumeRole"
      },
    ]
  })
}

resource "aws_iam_role_policy" "scrummaster_ssm_read" {
  name = "blitzlog-scrummaster-ssm-read"
  role = aws_iam_role.scrummaster.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:GetParametersByPath",
        ]
        Resource = [
          "arn:aws:ssm:*:*:parameter/blitzlog/users/*",
        ]
      },
    ]
  })
}

resource "aws_iam_instance_profile" "scrummaster" {
  name = "blitzlog-scrummaster-profile"
  role = aws_iam_role.scrummaster.name
}

# ---------------------------------------------------------------------------
# Security group. Security groups cannot filter by hostname, so the
# GitHub API / Tailscale control plane / minimax.io egress is expressed
# as 443/TCP to anywhere; Tailscale's direct WireGuard path and STUN need
# the two UDP ports. DNS (VPC resolver) and NTP (169.254.169.123) are not
# subject to security-group rules.
# ---------------------------------------------------------------------------

resource "aws_security_group" "scrummaster" {
  name        = "blitzlog-scrummaster-sg"
  description = "blitzlog scrummaster (ZeroClaw host)"
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS: GitHub API, Tailscale control plane, minimax.io"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Tailscale WireGuard (direct connections)"
    from_port   = 41641
    to_port     = 41641
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Tailscale STUN (NAT traversal)"
    from_port   = 3478
    to_port     = 3478
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "blitzlog-scrummaster-sg"
  }
}

resource "aws_security_group_rule" "scrummaster_ssh" {
  count             = length(var.ssh_allowed_cidrs) > 0 ? 1 : 0
  type              = "ingress"
  description       = "SSH from operator CIDRs"
  from_port         = 22
  to_port           = 22
  protocol          = "tcp"
  cidr_blocks       = var.ssh_allowed_cidrs
  security_group_id = aws_security_group.scrummaster.id
}

# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------

resource "aws_instance" "scrummaster" {
  ami                     = data.aws_ami.scrummaster.id
  instance_type           = "t4g.nano"
  subnet_id               = var.subnet_id
  key_name                = var.ssh_key_name != "" ? var.ssh_key_name : null
  vpc_security_group_ids  = [aws_security_group.scrummaster.id]
  iam_instance_profile    = aws_iam_instance_profile.scrummaster.name
  disable_api_termination = true
  user_data               = file("${path.module}/user-data.sh")

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 12
    encrypted   = true
  }

  tags = {
    Name        = "blitzlog-scrummaster"
    Purpose     = "blitzlog-scrummaster"
    ZeroclawTag = var.zeroclaw_image_tag
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = lookup(data.aws_ami.scrummaster.tags, "ZeroclawTag", "") == var.zeroclaw_image_tag
      error_message = "The scrummaster AMI in /blitzlog/scrummaster/agent-ami-id-zeroclaw-ubuntu was not baked with zeroclaw_image_tag. Rebuild infra/packer/scrummaster-zeroclaw-ubuntu.pkr.hcl with -var zeroclaw_image_tag=<tag>, or set var.zeroclaw_image_tag to the AMI's ZeroclawTag."
    }
  }
}

resource "aws_eip" "scrummaster" {
  domain   = "vpc"
  instance = aws_instance.scrummaster.id

  tags = {
    Name = "blitzlog-scrummaster-eip"
  }
}
