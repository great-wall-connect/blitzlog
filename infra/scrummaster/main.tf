# infra/scrummaster — operator-managed ZeroClaw advisor host.
#
# Foundation stack for the scrummaster epic (#134). This file provisions:
#
#   - One on-demand t4g.nano in the existing dev VPC with
#     disable_api_termination = true (no accidental destroys).
#   - A security group with egress 443/TCP only (HTTPS to GitHub API,
#     Tailscale control plane, and https://minimax.io) and ingress
#     tcp/22 from var.ssh_allowed_cidrs.
#   - An IAM role + instance profile with read-only access to the
#     /blitzlog/scrummaster/* SSM namespace (params are written by
#     later issues in the epic; the grant ships in this issue).
#   - A user-data script that installs the ZeroClaw binary, lays down
#     the three directories (/etc/zeroclaw, /var/lib/zeroclaw,
#     /var/log/zeroclaw), and writes a placeholder config.toml that
#     fails closed (no providers, no channels).
#
# No systemd unit is created in this issue — by design, the daemon is
# wired up in #136. Until then, `systemctl list-units --type=service`
# returns nothing for zeroclaw.

terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# Read the latest Canonical-published Ubuntu 26.04 LTS arm64 server
# AMI. Same source_ami_filter the Packer agent bake uses
# (infra/packer/agent-docker-ubuntu.pkr.hcl:51-60), so the scrummaster
# host shares a base OS family with the per-issue agent AMIs and gets
# the same kernel / cloud-init semantics. Operators can override via
# var.ami_id when they want a hardened / patched image.
data "aws_ami" "ubuntu_26_04_arm64" {
  count = var.ami_id == "" ? 1 : 0

  owners      = ["099720109477"] # Canonical
  most_recent = true

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-resolute-26.04-arm64-server-*"]
  }

  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

locals {
  ami_id_resolved = var.ami_id != "" ? var.ami_id : data.aws_ami.ubuntu_26_04_arm64[0].image_id
}

# Security group: 443/TCP egress only (HTTPS to GitHub API, Tailscale
# control plane, and https://minimax.io — all of which speak 443).
# AWS security groups egress by CIDR, not hostname, so we allow
# 0.0.0.0/0 and rely on the ZeroClaw config.toml to gate what the
# daemon actually talks to. Ingress is tcp/22 from
# var.ssh_allowed_cidrs; the count = 0 branch leaves ingress empty
# when the operator doesn't list any CIDRs (no SSH at all).
resource "aws_security_group" "scrummaster" {
  name        = "blitzlog-scrummaster-sg"
  description = "Security group for the scrummaster ZeroClaw host (always-on)"
  vpc_id      = var.vpc_id

  egress = [
    {
      description      = "HTTPS to GitHub API / Tailscale control plane / https://minimax.io"
      from_port        = 443
      to_port          = 443
      protocol         = "tcp"
      cidr_blocks      = ["0.0.0.0/0"]
      ipv6_cidr_blocks = []
      prefix_list_ids  = []
      security_groups  = []
      self             = false
    },
  ]

  tags = {
    Name        = "blitzlog-scrummaster-sg"
    Purpose     = "scrummaster-host"
    ManagedBy   = "blitzlog-scrummaster"
  }
}

resource "aws_security_group_rule" "ssh_from_admin" {
  count             = length(var.ssh_allowed_cidrs) > 0 ? 1 : 0
  type              = "ingress"
  from_port         = 22
  to_port           = 22
  protocol          = "tcp"
  cidr_blocks       = var.ssh_allowed_cidrs
  security_group_id = aws_security_group.scrummaster.id

  description = "SSH from operator-locked CIDRs (count = 0 when var.ssh_allowed_cidrs is empty)"
}

# IAM role + instance profile. The trust policy permits only EC2
# (so a leaked access key cannot be swapped in for the instance).
resource "aws_iam_role" "scrummaster" {
  name        = "blitzlog-scrummaster-role"
  description = "Scrummaster host role (read-only SSM access under /blitzlog/scrummaster/*)"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

# SSM read-only on the scrummaster namespace. Parameters themselves
# are created by later epic issues (#137 wiring the provider, #138
# the Telegram channel tokens, #139 the GitHub App creds); the role
# ships now so the user-data installer can read them at boot without
# a follow-up IAM change.
#
# SSM Session Manager support is attached below so an operator who
# can't reach the host via SSH (e.g. the IAM lockout misconfig path)
# can still get a shell without exposing 22 to the public internet.
resource "aws_iam_role_policy" "scrummaster_ssm_read" {
  name = "blitzlog-scrummaster-ssm-read"
  role = aws_iam_role.scrummaster.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ScrummasterSSMRead"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:GetParametersByPath",
        ]
        Resource = "arn:aws:ssm:${var.aws_region}:*:parameter/blitzlog/scrummaster/*"
      },
      {
        Sid      = "ScrummasterSSMDescribe"
        Effect   = "Allow"
        Action   = ["ssm:DescribeParameters"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_instance_profile" "scrummaster" {
  name = "blitzlog-scrummaster-profile"
  role = aws_iam_role.scrummaster.name
}

resource "aws_iam_role_policy_attachment" "scrummaster_ssm_managed" {
  role       = aws_iam_role.scrummaster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# On-demand t4g.nano. disable_api_termination = true so a stray
# `terraform destroy` (or an over-eager console click) cannot take
# the scrummaster down. To intentionally destroy, the operator must
# `terraform destroy -force` after explicitly editing the attribute,
# per the AGENTS.md pre-push convention.
resource "aws_instance" "scrummaster" {
  ami           = local.ami_id_resolved
  instance_type = var.instance_type
  subnet_id     = var.subnet_id

  vpc_security_group_ids = [aws_security_group.scrummaster.id]

  iam_instance_profile = aws_iam_instance_profile.scrummaster.name

  # 20 GiB is plenty for a t4g.nano running only ZeroClaw + the
  # three config/log directories. Operators with heavy SQLite memory
  # usage can override via var.root_volume_size_gb.
  root_block_device {
    volume_size           = var.root_volume_size_gb
    volume_type           = "gp3"
    delete_on_termination = true
  }

  # Required by issue #135: prevents accidental destroy. `terraform
  # destroy` errors with "ApiTermination is disabled" until the
  # operator explicitly toggles the attribute on the instance.
  disable_api_termination = true

  # The user-data script is templated inline so the operator can
  # audit it without leaving the repo (no s3 upload + download
  # dance). templatefile() substitutes ${zeroclaw_release} /
  # ${zeroclaw_version} from the variables below before cloud-init
  # base64-encodes the result. The rendered script is small enough
  # to stay well below the 16 KB user-data ceiling for t4g.nano.
  user_data = base64encode(
    templatefile("${path.module}/user-data.sh", {
      zeroclaw_release = var.zeroclaw_release_tag
      zeroclaw_version = "v0.0.0-latest" # surfaced in logs; real tag resolves via /releases/latest
    }),
  )

  # An optional SSH keypair — operators who pin SSH ingress to a
  # CIDR still want key-based auth. Empty string (default) launches
  # without one; the SSM-managed policy above gives a Session
  # Manager fallback so the operator can still get a shell when no
  # keypair is attached.
  key_name = var.ssh_key_name

  tags = {
    Name      = "blitzlog-scrummaster"
    Purpose   = "scrummaster-host"
    ManagedBy = "blitzlog-scrummaster"
  }
}