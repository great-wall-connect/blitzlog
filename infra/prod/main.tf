terraform {
  required_version = ">= 1.0"

  backend "s3" {
    bucket = ""
    key    = ""
    region = "ap-east-1"
  }
}

provider "aws" {
  region = var.aws_region
}

module "core" {
  source = "../modules/core"

  environment = "prod"

  aws_region        = var.aws_region
  vpc_id            = var.vpc_id
  ec2_subnet_id     = var.ec2_subnet_id
  ssh_allowed_cidrs = var.ssh_allowed_cidrs

  github_app_id              = var.github_app_id
  github_app_private_key     = var.github_app_private_key
  github_app_installation_id = var.github_app_installation_id
  github_webhook_secret      = var.github_webhook_secret

  alert_email      = var.alert_email
  opencode_model   = var.opencode_model
  opencode_api_key = var.opencode_api_key
  agent_os_family  = var.agent_os_family

  agent_logs_bucket_name = var.agent_logs_bucket_name

  stt_api_url            = var.stt_api_url
  stt_api_key            = var.stt_api_key
  stt_model              = var.stt_model
  stt_language           = var.stt_language
  upload_stt_model       = var.upload_stt_model
  stt_model_source_url   = var.stt_model_source_url
  stt_models_bucket_name = var.stt_models_bucket_name

  aws_profile = var.aws_profile
}
