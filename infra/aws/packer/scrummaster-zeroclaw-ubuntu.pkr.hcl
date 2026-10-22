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

variable "zeroclaw_image_tag" {
  type        = string
  description = "Upstream tag of ghcr.io/zeroclaw-labs/zeroclaw to bake. Resolved to a sha256 digest at build time (01-system.sh); the AMI only ever runs that digest."
  default     = "default-features-v0.8.6"
}

variable "aws_region" {
  type    = string
  default = "ap-east-1"
}

source "amazon-ebs" "ubuntu" {
  region = var.aws_region

  # Same Canonical Ubuntu 26.04 LTS arm64 base as agent-docker-ubuntu.pkr.hcl.
  # arm64 because the scrummaster runs on a t4g.nano (Graviton).
  source_ami_filter {
    filters = {
      name                = "ubuntu/images/hvm-ssd-gp3/ubuntu-resolute-26.04-arm64-server-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    most_recent = true
    owners      = ["099720109477"]  # Canonical
  }

  instance_type        = "t4g.small"
  ssh_username         = "ubuntu"
  iam_instance_profile = "blitzlog-packer-build-instance-profile"

  ami_name        = "blitzlog-scrummaster-zeroclaw-ubuntu-${local.timestamp}"
  ami_description = "Blitzlog scrummaster AMI (Ubuntu 26.04 LTS arm64) with dockerd + baked zeroclaw:${var.zeroclaw_image_tag}"
  ami_regions     = [var.aws_region]

  tags = {
    Name        = "blitzlog-scrummaster-zeroclaw-ubuntu-${local.timestamp}"
    Purpose     = "blitzlog-scrummaster-image"
    OsFamily    = "ubuntu"
    Runtime     = "docker"
    ZeroclawTag = var.zeroclaw_image_tag
  }

  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_size           = 12
    volume_type           = "gp3"
    delete_on_termination = true
  }
}

build {
  name = "blitzlog-scrummaster-zeroclaw-ubuntu"

  sources = ["source.amazon-ebs.ubuntu"]

  # 1. Upload the systemd units BEFORE 01-system.sh runs — the script
  # substitutes the resolved image digest into zeroclaw@.service and
  # installs both units under /etc/systemd/system/. Packer's `shell`
  # provisioner uploads only its own script, not sibling files.
  provisioner "file" {
    source      = "${path.root}/../scrummaster/systemd/zeroclaw-image-pull.service"
    destination = "/tmp/zeroclaw-image-pull.service"
  }

  provisioner "file" {
    source      = "${path.root}/../scrummaster/systemd/zeroclaw@.service"
    destination = "/tmp/zeroclaw@.service"
  }

  # 2. dockerd + zeroclaw-host user + digest-pinned image bake + units
  provisioner "shell" {
    environment_vars = [
      "ZEROCLAW_IMAGE_TAG=${var.zeroclaw_image_tag}",
    ]
    script = "${path.root}/scripts-scrummaster-ubuntu/01-system.sh"
  }

  # 3. Publish AMI id to SSM (same manifest -> shell-local pattern as
  # agent-docker-ubuntu.pkr.hcl; the AMI id only exists after the build).
  post-processor "manifest" {
    output     = "manifest.json"
    strip_path = true
  }

  post-processor "shell-local" {
    name = "publish-ami-id"
    environment_vars = [
      "AWS_REGION=${var.aws_region}",
    ]
    script = "${path.root}/scripts-scrummaster-ubuntu/02-publish-ami-id.sh"
  }
}
