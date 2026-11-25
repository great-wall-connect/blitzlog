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
  description = "Environment name (prod / dev). Tags the image and writes to the OOS Parameter Store."
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
  description = "Aliyun region to build in. The image is published to this region's OOS Parameter Store. Defaults to cn-hongkong — the region the blitzlog Alicloud account's OSS buckets live in."
  default     = "cn-hongkong"
}

variable "github_token" {
  type        = string
  description = "GitHub Personal Access Token with read:packages scope. Used by scripts-ecs-ubuntu/03-bake-images.sh to authenticate `docker pull` from ghcr.io. Pass via -var 'github_token=ghp_...' on the packer build command line (or via the GH_TOKEN env var read by Packer)."
  default     = ""
  sensitive   = true
}

variable "source_image_id" {
  type        = string
  description = "The Alicloud-published Ubuntu image to build from, as an image ID (e.g. `ubuntu_26_04_arm64_20G_alibase_20261016.vhd`). The plugin's key is `source_image`; there is no `image_id`."
  default     = "ubuntu_26_04_arm64_20G_alibase_20261016.vhd"
}

variable "instance_type" {
  type        = string
  description = "Aliyun ECS instance type used for the build instance. Must be ARM64 (the agent container image is arm64-only) — `ecs.g8y.*` is the general-purpose ARM64 family in cn-hongkong."
  default     = "ecs.g8y.large"
}

variable "vpc_id" {
  type        = string
  description = "VPC for the build instance. Empty means 'let the plugin create one' — which it cannot do without vpc:CreateVpc, so pass the bootstrap stack's VPC."
  default     = ""
}

variable "vswitch_id" {
  type        = string
  description = "VSwitch for the build instance, inside `vpc_id`. Must be in a zone where the ARM64 instance type is offered."
  default     = ""
}

variable "zone_id" {
  type        = string
  description = "Zone for the build instance. Must be a zone where `instance_type` is offered."
  default     = ""
}

variable "eip_id" {
  type        = string
  description = "ID of the elastic IP the build instance gets its public address from (e.g. `eip-j6c...`). Required: the builder only reaches a VPC instance over an EIP, and it cannot allocate one itself — see the `eip_id` note on the source block. The bootstrap stack owns it: `cd infra/alicloud/bootstrap && terraform output packer_eip_id`."
}

variable "ecs_ram_role_name" {
  type        = string
  description = "RAM role attached to the build instance. ossutil fetches the whisper model from OSS in `EcsRamRole` mode, which reads the instance's role credentials from the metadata service — without a role there are no credentials. The packer build role carries `oss:GetObject` on the STT bucket and `ram:PassRole` on itself."
  default     = "blitzlog-packer-build-role"
}

source "alicloud-ecs" "ubuntu" {
  region            = var.alicloud_region
  source_image      = var.source_image_id
  instance_type     = var.instance_type
  instance_name     = "blitzlog-packer-${var.environment}-${local.timestamp}"
  ssh_username      = "root"
  security_group_id = var.packer_security_group_id

  vpc_id    = var.vpc_id
  vswitch_id = var.vswitch_id
  zone_id   = var.zone_id

  # The build instance lives in a VPC, and the builder's SSH
  # communicator reaches it over its public address — so it needs an
  # EIP. The builder can only get one two ways: it allocates one
  # itself, or it is handed an existing one through `eip_id`. It
  # cannot do the first here: it sends the same
  # `internet_charge_type` / `internet_max_bandwidth_out` to both the
  # RunInstances call and the AllocateEipAddress call, and the EIP
  # call rejects a bandwidth below 1 (`IllegalParam.Bandwidth`)
  # while any non-zero bandwidth makes ECS hand the instance an
  # ECS-managed public IP ("natpublicip") — which Alicloud refuses to
  # replace with an EIP
  # ("EIP_CAN_NOT_ASSOCIATE_WITH_PUBLIC_IP: instance already bind
  # natpublicip, cannot bind eip").
  #
  # So the EIP is created by the bootstrap stack and passed in here.
  # The builder associates it with the build instance and drops the
  # association when the build ends; the EIP itself survives for the
  # next bake.
  eip_id = var.eip_id

  # The build instance downloads the whisper model from OSS with
  # ossutil in `EcsRamRole` mode, which takes the credentials from
  # the RAM role attached to the INSTANCE (not from the Packer
  # process) — so the instance needs one. The packer build role has
  # `oss:GetObject` on the STT bucket and `ram:PassRole` on itself,
  # which is what lets the caller attach it.
  ecs_ram_role_name = var.ecs_ram_role_name

  # `associate_public_ip_address` is what makes the builder associate
  # the EIP with the instance. It does NOT ask ECS for a public IP of
  # its own — the instance's public network is configured by the two
  # `internet_*` parameters below, which must stay unset.
  associate_public_ip_address = true

  # The instance must NOT get an ECS-managed public IP: that public IP
  # and the EIP are mutually exclusive (see the `eip_id` note above).
  # Leaving `internet_charge_type` and `internet_max_bandwidth_out`
  # unset is what keeps ECS from assigning one — the EIP is the
  # instance's only public address.
  #
  # cn-hongkong is outside mainland China, so `PayByTraffic` is the
  # only billing method the API accepts there — but that only applies
  # to the instance's own public bandwidth, which we deliberately do
  # not ask for.

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

  system_disk_mapping {
    disk_name = "blitzlog-packer-${var.environment}"
    disk_size = 40
  }
}

variable "packer_security_group_id" {
  type        = string
  description = "Security group for the Packer build instance. Must allow SSH (22) inbound from the CI runner and all egress (docker pull + OSS + OOS)."
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

  # 4. Publish the image id to the OOS Parameter Store. Two-step
  #    post-processor:
  #    a. The `manifest` post-processor writes the build manifest to
  #       manifest.json (in the working dir) AFTER the image is created.
  #    b. The `shell-local` post-processor reads manifest.json and
  #       publishes the actual new image id to OOS.
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
