terraform {
  required_version = ">= 1.10"

  required_providers {
    # Declared explicitly here so the root's `provider "alicloud"`
    # block below resolves `alicloud` to `aliyun/alicloud 2.x` rather
    # than the implicit default `hashicorp/alicloud` (which would
    # pull in a dead provider binary). The module's
    # `required_providers` on its own does not change the root's
    # resolution.
    alicloud = {
      source  = "aliyun/alicloud"
      version = "~> 2.0"
    }
  }

  backend "oss" {
    bucket = ""
    key    = ""
    region = "cn-hangzhou"
  }
}

provider "alicloud" {
  region = var.alicloud_region
}

module "core" {
  source = "../modules/core"

  environment = "dev"

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
