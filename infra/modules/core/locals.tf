locals {
  ssm_root = "/blitzlog/${var.environment}"

  ssm_github_app_id_name           = "${local.ssm_root}/github-app/id"
  ssm_github_app_private_key_name  = "${local.ssm_root}/github-app/private-key"
  ssm_github_app_installation_name = "${local.ssm_root}/github-app/installation-id"
  ssm_github_webhook_secret_name   = "${local.ssm_root}/github-webhook/secret"
  ssm_opencode_api_key_name        = "${local.ssm_root}/opencode/api-key"
  ssm_stt_api_url_name             = "${local.ssm_root}/stt/api-url"
  ssm_stt_api_key_name             = "${local.ssm_root}/stt/api-key"
  ssm_stt_model_name               = "${local.ssm_root}/stt/model"
  ssm_stt_language_name            = "${local.ssm_root}/stt/language"
  ssm_stt_models_bucket_name       = "${local.ssm_root}/stt/models-bucket"

  ssm_user_pool_root   = "${local.ssm_root}/users"
  ssm_ephemeral_root   = "${local.ssm_root}/ephemeral"
  ssm_user_llm_pattern = "${local.ssm_user_pool_root}/*/local-llm/*"

  stt_models_bucket = var.stt_models_bucket_name != "" ? var.stt_models_bucket_name : "blitzlog-${var.environment}-stt-models"

  agent_logs_prefix = var.environment
}
