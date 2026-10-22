# infra/aws/modules/core/scrummaster.tf — the always-on Scrummaster advisor
# host (epic #134, skeleton #135).
#
# One long-lived t4g.nano running dockerd + one ZeroClaw container per
# user-pool user (zeroclaw@.service, #136). It reads GitHub issues, PRs,
# and S3 session artifacts read-only, answers the operator over Telegram
# (#138), and is the only agent role permitted to apply the
# `autonomous` / `assisted` trigger labels under the four-rule carve-out
# in AGENTS.md (#140).
#
# ## Why this lives in `core` and not a separate stack
#
# It used to be its own stack at `infra/scrummaster/` (issue #135).
# That meant a second provider download, a second lock file, a second
# set of vars to thread through, and a second hand-run `terraform apply`
# that CI never touched. Folding it in here makes "apply the whole
# system" a single plan/apply, owned by CI, same as everything else.
# The destroy/blast-radius isolation the separate stack bought is not
# worth the setup friction — `dev` exists to be broken and rebuilt.
#
# ## Naming
#
# Every resource is `blitzlog-${var.environment}-scrummaster*`, matching the
# rest of this module (lambda.tf, ec2.tf, iam.tf, storage.tf). An earlier
# revision kept the scrummaster env-independent, reasoning that only one
# host should exist; that was wrong. Env-scoped naming is the module's
# uniform convention, it lets dev and prod run their own scrummaster from
# their own AMI, and it means the AMI id published by Packer lands in the
# same env-scoped SSM namespace as the agent AMI:
# /blitzlog/<env>/agent-ami-id-zeroclaw-docker-ubuntu.
#
# Renaming changes `iam_instance_profile`, which forces replacement of the
# running instance. Because it carries `disable_api_termination = true`,
# that replacement is a deliberate operator procedure, not something
# `terraform apply` can do unattended. See the operator runbook at the end
# of this file.
#
# ## Gating
#
# Every resource below is wrapped in `count = var.enable_scrummaster ? 1 : 0`,
# the same optional-feature idiom used by alerting.tf, ec2.tf and
# storage.tf. Off by default because enabling it requires a Packer bake
# to have published `/blitzlog/scrummaster/agent-ami-id-zeroclaw-ubuntu`
# first — nothing in CI bakes that AMI today, so a default-on module
# would gate the core webhook deploy behind a manual step. The AMI is
# published by infra/aws/packer/scrummaster-zeroclaw-ubuntu.pkr.hcl via
# scripts-scrummaster-ubuntu/02-publish-ami-id.sh.
#
# ## Instance lifecycle
#
# The instance is a pet: `disable_api_termination` blocks console/API
# terminates and `prevent_destroy` makes `terraform destroy` (and any
# plan that would replace the instance, e.g. after an AMI rebuild)
# fail. This now also blocks destroying the whole dev/prod stack —
# accepted deliberately, see the header. To retire or replace it,
# deliberately flip both guards in a commit.

# ---------------------------------------------------------------------------
# AMI lookup — gated so prod (enable_scrummaster = false) does no reads at
# plan time and cannot be broken by a missing SSM parameter.
# ---------------------------------------------------------------------------

data "aws_ssm_parameter" "scrummaster_ami_id" {
  count = var.enable_scrummaster ? 1 : 0

  name = "/blitzlog/${var.environment}/agent-ami-id-zeroclaw-docker-ubuntu"
}

data "aws_ami" "scrummaster" {
  count = var.enable_scrummaster ? 1 : 0

  owners = ["self"]

  filter {
    name   = "image-id"
    values = [nonsensitive(data.aws_ssm_parameter.scrummaster_ami_id[0].value)]
  }
}

# ---------------------------------------------------------------------------
# IAM — one role for all users. SSM read on /blitzlog/users/* is the only
# grant (refined per user in #136). No AmazonSSMManagedInstanceCore: that
# managed policy grants ssm:GetParameter on "*", which would widen the
# blast radius beyond the per-user namespace.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "scrummaster" {
  count = var.enable_scrummaster ? 1 : 0

  name = "blitzlog-${var.environment}-scrummaster-role"

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
  count = var.enable_scrummaster ? 1 : 0

  name = "blitzlog-${var.environment}-scrummaster-ssm-read"
  role = aws_iam_role.scrummaster[0].id

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
  count = var.enable_scrummaster ? 1 : 0

  name = "blitzlog-${var.environment}-scrummaster-profile"
  role = aws_iam_role.scrummaster[0].name
}

# ---------------------------------------------------------------------------
# Security group. Security groups cannot filter by hostname, so the
# GitHub API / Tailscale control plane / minimax.io egress is expressed
# as 443/TCP to anywhere; Tailscale's direct WireGuard path and STUN need
# the two UDP ports. DNS (VPC resolver) and NTP (169.254.169.123) are not
# subject to security-group rules.
# ---------------------------------------------------------------------------

resource "aws_security_group" "scrummaster" {
  count = var.enable_scrummaster ? 1 : 0

  name        = "blitzlog-${var.environment}-scrummaster-sg"
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
    Name        = "blitzlog-${var.environment}-scrummaster-sg"
    Environment = var.environment
  }
}

resource "aws_security_group_rule" "scrummaster_ssh" {
  count = var.enable_scrummaster && length(var.ssh_allowed_cidrs) > 0 ? 1 : 0

  type              = "ingress"
  description       = "SSH from operator CIDRs"
  from_port         = 22
  to_port           = 22
  protocol          = "tcp"
  cidr_blocks       = var.ssh_allowed_cidrs
  security_group_id = aws_security_group.scrummaster[0].id
}

# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------

resource "aws_instance" "scrummaster" {
  count = var.enable_scrummaster ? 1 : 0

  ami                         = data.aws_ami.scrummaster[0].id
  instance_type               = "t4g.nano"
  subnet_id                   = var.ec2_subnet_id
  associate_public_ip_address = false
  key_name                    = var.ssh_key_name != "" ? var.ssh_key_name : null
  vpc_security_group_ids      = [aws_security_group.scrummaster[0].id]
  iam_instance_profile        = aws_iam_instance_profile.scrummaster[0].name
  disable_api_termination     = true
  user_data                   = file("${path.module}/scrummaster/user-data.sh")

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 12
    encrypted   = true
  }

  tags = {
    Name        = "blitzlog-${var.environment}-scrummaster"
    Purpose     = "blitzlog-scrummaster"
    Environment = var.environment
    ZeroclawTag = var.zeroclaw_image_tag
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = lookup(data.aws_ami.scrummaster[0].tags, "ZeroclawTag", "") == var.zeroclaw_image_tag
      error_message = "The scrummaster AMI in /blitzlog/${var.environment}/agent-ami-id-zeroclaw-docker-ubuntu was not baked with zeroclaw_image_tag. Rebuild infra/aws/packer/scrummaster-zeroclaw-docker-ubuntu.pkr.hcl with -var environment=${var.environment} -var zeroclaw_image_tag=<tag>, or set var.zeroclaw_image_tag to the AMI's ZeroclawTag."
    }
  }
}

resource "aws_eip" "scrummaster" {
  count = var.enable_scrummaster && var.enable_scrummaster_eip ? 1 : 0

  domain   = "vpc"
  instance = aws_instance.scrummaster[0].id

  tags = {
    Name        = "blitzlog-${var.environment}-scrummaster-eip"
    Environment = var.environment
  }
}
# ---------------------------------------------------------------------------
# Operator runbook: migrating a scrummaster that was created with the
# previous env-independent names (blitzlog-scrummaster-role / -sg / -profile)
#
# This applies only to instances created before the rename to
# blitzlog-${var.environment}-scrummaster*. A fresh apply is unaffected.
# Terraform cannot perform the replacement unattended: changing
# `iam_instance_profile` requires destroying the instance, and
# `disable_api_termination = true` blocks that.
#
#   # 0. Confirm nothing is running before you start.
#    terraform -chdir=infra/aws/dev plan -out=pre.tfplan
#
#   # 1. Temporarily drop the termination guard. Delete the empty
#    #    prevent_destroy lifecycle block too, or the plan is refused
#    #    before it can even offer a replacement.
#    #    (edit this file: disable_api_termination = false)
#
#   # 2. Force replacement.
#    terraform -chdir=infra/aws/dev apply -replace=module.core.aws_instance.scrummaster[0]
#
#   # 3. Restore both guards and commit them.
#    #    (edit this file: disable_api_termination = true)
#
#   # 4. The old IAM role / SG / instance profile have no prefix overlap
#    #    with the new ones, so they are orphaned. The old instance is
#    #    terminated by step 2 and its EBS volume goes with it.
#
# Expect a host with no ZeroClaw per-user container running for the
# duration of the swap (one launch, typically 60-120s). The per-user
# state lives on the host under /var/lib/zeroclaw, so plan any migration
# of that data before step 2 — it does not survive the replacement.
# ---------------------------------------------------------------------------
