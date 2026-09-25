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

variable "github_token" {
  type        = string
  description = "GitHub Personal Access Token with read:packages scope. Used by scripts-docker-ubuntu/03-bake-images.sh to authenticate `docker pull` from ghcr.io. Pass via -var 'github_token=ghp_...' on the packer build command line (or via the GH_TOKEN env var read by Packer)."
  default     = ""
  sensitive   = true
}

source "amazon-ebs" "ubuntu" {
  region = var.aws_region

  # Canonical-published Ubuntu 26.04 LTS arm64 minimal image
  source_ami_filter {
    filters = {
      name                = "ubuntu/images/hvm-ssd-gp3/ubuntu-resolute-26.04-arm64-server-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    most_recent = true
    owners      = ["099720109477"]  # Canonical
  }

  instance_type        = "t4g.medium"
  ssh_username         = "ubuntu"
  iam_instance_profile = "blitzlog-packer-build-instance-profile"

  ami_name        = "blitzlog-${var.environment}-agent-ami-docker-ubuntu-${local.timestamp}"
  ami_description = "Blitzlog agent AMI (Ubuntu 26.04 LTS arm64) with dockerd + baked blitzlog-agent:${var.agent_image_tag}"
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
      # GITHUB_TOKEN is consumed by 03-bake-images.sh to `docker login
      # ghcr.io` before pulling the blitzlog-agent image. Empty string
      # when not set; the script warns and the pull fails with
      # `unauthorized`.
      "GITHUB_TOKEN=${var.github_token}",
    ]
    script = "${path.root}/scripts-docker-ubuntu/03-bake-images.sh"
  }

  # 3. systemd setup
  provisioner "shell" {
    script = "${path.root}/scripts-docker-ubuntu/02-systemd.sh"
  }

  # 4. Publish AMI id to SSM. Two-step post-processor:
  #    a. The `manifest` post-processor writes the build manifest to
  #       manifest.json (in the working dir) AFTER the AMI is registered.
  #    b. The `shell-local` post-processor reads manifest.json and
  #       publishes the actual new AMI id to SSM.
  # Replaces the old 04-publish.sh provisioner (which ran on the
  # source EC2 BEFORE the AMI existed and accidentally published the
  # source EC2's base AMI id).
  post-processor "manifest" {
    output     = "manifest.json"
    strip_path = true
  }

  post-processor "shell-local" {
    name = "publish-ami-id"
    environment_vars = [
      "BLITZLOG_ENV=${var.environment}",
      "AWS_REGION=${var.aws_region}",
    ]
    script = "${path.root}/scripts-docker-ubuntu/05-publish-ami-id.sh"
  }
}
