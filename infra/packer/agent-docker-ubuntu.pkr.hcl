packer {
  required_plugins {
    amazon = {
      version = ">= 1.0.0"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

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
  description = "Whisper model name (e.g. base.en, small.en). Used to download the model into the AMI at build time."
  default     = "base.en"
}

variable "stt_models_bucket" {
  type        = string
  description = "S3 bucket hosting whisper.cpp model files."
}

variable "aws_region" {
  type        = string
  default     = "ap-east-1"
}

source "amazon-ebs" "ubuntu" {
  region = var.aws_region

  # Canonical-published Ubuntu 24.04 LTS arm64 minimal image
  source_ami_filter {
    filters = {
      name                = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    most_recent = true
    owners      = ["099720109477"]  # Canonical
  }

  instance_type = "t4g.medium"
  ssh_username  = "ubuntu"

  ami_name        = "blitzlog-${var.environment}-agent-ami-docker-ubuntu-${local.timestamp}"
  ami_description = "Blitzlog agent AMI (Ubuntu 24.04 LTS arm64) with dockerd + baked blitzlog-agent:${var.agent_image_tag}"
  ami_regions     = [var.aws_region]

  tags = {
    Name        = "blitzlog-${var.environment}-agent-ami-docker-ubuntu-${local.timestamp}"
    Purpose     = "blitzlog-agent-image"
    Environment = var.environment
    OsFamily    = "ubuntu"
    Runtime     = "docker"
    AgentTag    = var.agent_image_tag
  }

  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_size           = 20
    volume_type           = "gp3"
    delete_on_termination = true
  }
}

build {
  name = "blitzlog-agent-docker-ubuntu"

  sources = ["source.amazon-ebs.ubuntu"]

  # 1. Install dockerd + host extras
  provisioner "shell" {
    script = "${path.root}/scripts-docker-ubuntu/01-system.sh"
  }

  # 2. Bake the blitzlog-agent container image
  provisioner "shell" {
    environment_vars = [
      "AGENT_IMAGE_TAG=${var.agent_image_tag}",
      "STT_MODEL=${var.stt_model}",
      "STT_MODELS_BUCKET=${var.stt_models_bucket}",
      "AWS_REGION=${var.aws_region}",
    ]
    script = "${path.root}/scripts-docker-ubuntu/03-bake-images.sh"
  }

  # 3. systemd setup
  provisioner "shell" {
    script = "${path.root}/scripts-docker-ubuntu/02-systemd.sh"
  }

  # 4. Publish AMI id to SSM
  provisioner "shell" {
    environment_vars = [
      "BLITZLOG_ENV=${var.environment}",
      "AWS_REGION=${var.aws_region}",
    ]
    script = "${path.root}/scripts-docker-ubuntu/04-publish.sh"
  }
}
