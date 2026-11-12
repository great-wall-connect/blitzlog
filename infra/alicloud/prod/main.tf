terraform {
  required_version = ">= 1.10"

  required_providers {
    # See infra/alicloud/dev/main.tf for the why; this stack has the
    # same implicit-source drift risk.
    alicloud = {
      source  = "aliyun/alicloud"
      version = "~> 2.0"
    }
  }

  # The OSS backend. The bucket / key / region are supplied via
  # `-backend-config` (see prod-backend.hcl.example); the credential
  # comes from the ALICLOUD_ACCESS_KEY / ALICLOUD_SECRET_KEY env
  # vars, or from the `profile` below, which reads
  # `~/.aliyun/config.json`.
  #
  # The default region here matches the account's bucket region
  # (cn-hongkong). The backend's region MUST match the bucket's
  # region — an OSS bucket name is globally unique, but its region
  # is not, so a mismatch fails with "Authorization header is
  # invalid" rather than a clear "wrong region" error.
  backend "oss" {
    bucket = ""
    key    = ""
    region = "cn-hongkong"
    # The profile name in ~/.aliyun/config.json (written by
    # `aliyun configure --profile <name>`). Empty by default so the
    # env-var path is used unless the operator opts in.
    profile = ""
  }
}

provider "alicloud" {
  region = var.alicloud_region
}

module "core" {
  source = "../modules/core"

  environment = "prod"

  alicloud_region   = var.alicloud_region
  vpc_id            = var.vpc_id
  vswitch_ids       = var.vswitch_ids
  ssh_allowed_cidrs = var.ssh_allowed_cidrs

  github_app_id              = var.github_app_id
  github_app_private_key     = var.github_app_private_key
  github_app_installation_id = var.github_app_installation_id
  github_webhook_secret      = var.github_webhook_secret

  alert_email              = var.alert_email
  opencode_model           = var.opencode_model
  opencode_agent_max_steps = var.opencode_agent_max_steps
  opencode_api_key         = var.opencode_api_key

  agent_logs_bucket_name = var.agent_logs_bucket_name

  stt_api_url            = var.stt_api_url
  stt_api_key            = var.stt_api_key
  stt_model              = var.stt_model
  stt_language           = var.stt_language
  stt_models_bucket_name = var.stt_models_bucket_name

  spot_instance_types = var.spot_instance_types
}
