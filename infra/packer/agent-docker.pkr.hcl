packer {
  required_plugins {
    amazon = {
      version = ">= 1.0.0"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

# Amazon Linux 2023 arm64 agent AMI: ECS-optimized base + awscli/git/jq
# host extras + a pre-baked blitzlog-agent container image. Spot EC2
# instances launched by the Lambda run this AMI; the host user-data
# does all AWS work (SSM, S3) and the container runs with zero AWS
# credentials.
locals {
  timestamp = formatdate("YYYYMMDD-hhmmss", timestamp())
}

variable "environment" {
  type        = string
  description = "Environment name (prod / dev). Tags the AMI and writes to SSM."
}

variable "agent_image_tag" {
  type        = string
  description = "Tag of the blitzlog-agent container image to bake into the AMI. Same value as the docker-images.pkrvars.hcl agent_tag."
  default     = "2.0.0"
}

variable "stt_model" {
  type        = string
  description = "Whisper model name (e.g. base.en, small.en). Used to download the model into the AMI at build time so the first launch doesn't pay the download cost."
  default     = "base.en"
}

variable "stt_models_bucket" {
  type        = string
  description = "S3 bucket hosting whisper.cpp model files. The Packer build downloads ggml-<stt_model>.bin from s3://<bucket>/models/."
}

variable "aws_region" {
  type        = string
  default     = "ap-east-1"
}

source "amazon-ebs" "al2023" {
  region = var.aws_region

  # ECS-optimized AL2023 arm64 AMI — ships with dockerd + a dormant ECS agent.
  source_ami_filter {
    filters = {
      name                = "amzn2-ami-ecs-hvm-*-arm64-ebs"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    most_recent = true
    owners      = ["591542846629"]  # amazon-ecs
  }

  instance_type = "t4g.medium"
  ssh_username  = "ec2-user"

  ami_name        = "blitzlog-${var.environment}-agent-ami-docker-al2023-${local.timestamp}"
  ami_description = "Blitzlog agent AMI (AL2023 arm64) with dockerd + baked blitzlog-agent:${var.agent_image_tag}"
  ami_regions     = [var.aws_region]

  tags = {
    Name        = "blitzlog-${var.environment}-agent-ami-docker-al2023-${local.timestamp}"
    Purpose     = "blitzlog-agent-image"
    Environment = var.environment
    OsFamily    = "al2023"
    Runtime     = "docker"
    AgentTag    = var.agent_image_tag
  }

  launch_block_device_mappings {
    device_name           = "/dev/xvda"
    volume_size           = 20
    volume_type           = "gp3"
    delete_on_termination = true
  }
}

build {
  name = "blitzlog-agent-docker-al2023"

  sources = ["source.amazon-ebs.al2023"]

  # 1. Install host-side extras (the dockerd is already present on the
  #    ECS-optimized AMI).
  provisioner "shell" {
    script = "${path.root}/scripts-docker/01-system.sh"
  }

  # 2. Bake the blitzlog-agent container image into /opt/blitzlog/images/.
  #    Starts dockerd in the build VM, pulls the image, saves as a gzipped
  #    tarball. Also downloads the whisper model so first launch is fast.
  provisioner "shell" {
    environment_vars = [
      "AGENT_IMAGE_TAG=${var.agent_image_tag}",
      "STT_MODEL=${var.stt_model}",
      "STT_MODELS_BUCKET=${var.stt_models_bucket}",
      "AWS_REGION=${var.aws_region}",
    ]
    script = "${path.root}/scripts-docker/03-bake-images.sh"
  }

  # 3. systemd setup: enable docker, write watchdog + load-image scripts.
  provisioner "shell" {
    script = "${path.root}/scripts-docker/02-systemd.sh"
  }

  # 4. Publish the AMI id to SSM.
  provisioner "shell" {
    environment_vars = [
      "BLITZLOG_ENV=${var.environment}",
      "AWS_REGION=${var.aws_region}",
    ]
    script = "${path.root}/scripts-docker/04-publish.sh"
  }
}
