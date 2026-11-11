packer {
  required_plugins {
    alicloud = {
      version = ">= 1.0.0"
      source  = "github.com/hashicorp/alicloud"
    }
  }
}

locals {
  timestamp = formatdate("YYYYMMDD-hhmmss", timestamp())
}

variable "environment" {
  type        = string
  description = "Environment name (prod / dev). Tags the image and writes to KMS."
}

variable "agent_image_tag" {
  type        = string
  description = "Tag of the blitzlog-agent container image to bake into the image. Same value as the docker-images.pkrvars.hcl agent_tag."
  default     = "2.0.0"
}

variable "stt_model" {
  type        = string
  description = "Whisper model name (e.g. base.en, small.en). Used to download the model into the image at build time."
  default     = "base.en"
}

variable "stt_models_bucket" {
  type        = string
  description = "OSS bucket hosting whisper.cpp model files."
}

variable "alicloud_region" {
  type        = string
  description = "Aliyun region to build in. The image is published to this region's KMS Secrets Manager."
  default     = "cn-hangzhou"
}

variable "github_token" {
  type        = string
  description = "GitHub Personal Access Token with read:packages scope. Used by scripts-ecs-ubuntu/03-bake-images.sh to authenticate `docker pull` from ghcr.io. Pass via -var 'github_token=ghp_...' on the packer build command line (or via the GH_TOKEN env var read by Packer)."
  default     = ""
  sensitive   = true
}

variable "source_image_id" {
  type        = string
  description = "The Canonical-published Ubuntu image to build from. Resolved by the caller (the release workflow) via `aliyun ecs DescribeImages`; empty means 'let the builder pick the default Ubuntu image'."
  default     = ""
}

variable "instance_type" {
  type        = string
  description = "Aliyun ECS instance type used for the build instance. The Alicloud analogue of the AWS build instance type."
  default     = "ecs.g7.large"
}

source "alicloud-ecs" "ubuntu" {
  region            = var.alicloud_region
  image_id          = var.source_image_id
  instance_type     = var.instance_type
  instance_name     = "blitzlog-packer-${var.environment}-${local.timestamp}"
  ssh_username      = "root"
  security_group_id = var.packer_security_group_id

  image_name        = "blitzlog-${var.environment}-agent-image-ecs-ubuntu-${local.timestamp}"
  image_description = "Blitzlog agent ECS image (Ubuntu) with dockerd + baked blitzlog-agent:${var.agent_image_tag}"

  tags = {
    Name        = "blitzlog-${var.environment}-agent-image-ecs-ubuntu-${local.timestamp}"
    Purpose     = "blitzlog-agent-image"
    Environment = var.environment
    OsFamily    = "ubuntu"
    Runtime     = "docker"
    AgentTag    = var.agent_image_tag
    # Combined image reference — what the runtime watchdog sources from
    # /root/blitzlog.env to launch the baked-in container. Pairs with
    # AgentTag (build identifier) so operators have both human and
    # machine views.
    AgentImage = "ghcr.io/great-wall-connect/blitzlog-agent:${var.agent_image_tag}"
  }

  system_disk {
    name = "blitzlog-packer-${var.environment}"
    size = 40
  }
}

variable "packer_security_group_id" {
  type        = string
  description = "Security group for the Packer build instance. Must allow SSH (22) inbound from the CI runner and all egress (docker pull + OSS + KMS)."
  default     = ""
}

build {
  name = "blitzlog-agent-ecs-ubuntu"

  sources = ["source.alicloud-ecs.ubuntu"]

  # 1. Install dockerd + host extras (aliyun CLI + ossutil + docker-ce)
  provisioner "shell" {
    script = "${path.root}/scripts-ecs-ubuntu/01-system.sh"
  }

  # 1a. Upload the host-side OSS abstraction (issue #125) BEFORE
  # 03-bake-images.sh runs, because that bake script `source`s
  # /tmp/blitzlog-oss-uploader.sh to download the Whisper model. The
  # final copy to /usr/local/bin/ is done by 02-systemd.sh (step 3).
  provisioner "file" {
    source      = "${path.root}/scripts-ecs-ubuntu/blitzlog_oss_uploader.sh"
    destination = "/tmp/blitzlog-oss-uploader.sh"
  }

  # 1b. Upload the canonical container-hardening contract (issue #164).
  # Sourced by watchdog.sh at boot so the container it launches carries
  # --security-opt=no-new-privileges:true, --cap-drop=ALL, --pids-limit
  # and --init. The contract lives next to the image it describes (the
  # agent package) so the dev tasks in mise.toml can source the very
  # same file from the repo. The final copy to /usr/local/bin/ is done
  # by 02-systemd.sh.
  provisioner "file" {
    source      = "${path.root}/../../../packages/images/agent/container-hardening.env"
    destination = "/tmp/blitzlog-container-hardening.env"
  }

  # 2. Bake the blitzlog-agent container image
  provisioner "shell" {
    environment_vars = [
      "AGENT_IMAGE_TAG=${var.agent_image_tag}",
      "STT_MODEL=${var.stt_model}",
      "STT_MODELS_BUCKET=${var.stt_models_bucket}",
      "ALICLOUD_REGION=${var.alicloud_region}",
      # GITHUB_TOKEN is consumed by 03-bake-images.sh to `docker login
      # ghcr.io` before pulling the blitzlog-agent image. Empty string
      # when not set; the script warns and the pull fails with
      # `unauthorized`.
      "GITHUB_TOKEN=${var.github_token}",
    ]
    script = "${path.root}/scripts-ecs-ubuntu/03-bake-images.sh"
  }

  # 3. systemd setup
  provisioner "file" {
    # Packer's `shell` provisioner uploads only the `script` file, not
    # its sibling files. 02-systemd.sh installs watchdog.sh, so we
    # upload it separately first. cp + chmod on the receiving end
    # avoids the `install` binary (not reliably present on the Ubuntu
    # minimal arm64 base).
    source      = "${path.root}/scripts-ecs-ubuntu/watchdog.sh"
    destination = "/tmp/blitzlog-watchdog.sh"
  }

  provisioner "shell" {
    script = "${path.root}/scripts-ecs-ubuntu/02-systemd.sh"
  }

  # 4. Publish the image id to KMS. Two-step post-processor:
  #    a. The `manifest` post-processor writes the build manifest to
  #       manifest.json (in the working dir) AFTER the image is created.
  #    b. The `shell-local` post-processor reads manifest.json and
  #       publishes the actual new image id to KMS.
  # Replaces the old 04-publish.sh provisioner (which ran on the
  # source ECS BEFORE the image existed and accidentally published the
  # source instance's base image id).
  post-processor "manifest" {
    output     = "manifest.json"
    strip_path = true
  }

  post-processor "shell-local" {
    name = "publish-image-id"
    environment_vars = [
      "BLITZLOG_ENV=${var.environment}",
      "ALICLOUD_REGION=${var.alicloud_region}",
    ]
    script = "${path.root}/scripts-ecs-ubuntu/05-publish-ami-id.sh"
  }
}
