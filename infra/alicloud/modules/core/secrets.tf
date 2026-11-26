# Analogue of infra/aws/modules/core/iam.tf (the SSM parameter
# resources at infra/aws/modules/core/iam.tf:356-423).
#
# The Alicloud secret store is the OOS Parameter Store (Operation
# Orchestration Service), NOT KMS Secrets Manager. KMS Secrets
# Manager was the original choice, but on KMS 3.0 a secret can only
# be created inside a dedicated KMS instance — `CreateSecret`
# without a `DKMSInstanceId` is rejected with
# `UnsupportedOperation` ("This action is not supported"), and a
# KMS instance is a billable resource (USD 500/month subscription,
# USD 4.50/day pay-as-you-go, plus a secret-quota pack). The OOS
# Parameter Store is free, is available in cn-hongkong, and its
# API maps 1:1 onto the `SecretStore` interface
# (lambda/provider/alicloud.py::AlicloudSecretStore).
#
# Every parameter is an encrypted one (`type = "Secret"`), encrypted
# with the account's CMK — the one the bootstrap stack creates
# (infra/alicloud/bootstrap/kms.tf) and passes down as
# `var.kms_key_id`. Alicloud allows one CMK per region per account,
# so there is exactly one, shared by every env and every parameter.
#
# The parameter names mirror the AWS-side SSM paths byte-for-byte
# so `lambda/_env.py` reads them via the same `BLITZLOG_PROVIDER`-
# agnostic path strings.
#
# The OOS resource exposes no ARN attribute, so the RAM policies
# that grant access to these parameters spell the ARNs out
# (`acs:oos:<region>:<account>:secretparameter/<name>`) — see
# `ram.tf`.

data "alicloud_account" "current" {}

# The deploy-time parameters, read WITH decryption.
#
# The provider reads an OOS secret parameter back WITHOUT decryption,
# so terraform never sees a value the operator set out-of-band: its
# copy of a value is whatever was last APPLIED. Two things follow,
# and both are why this data source exists.
#
#   * A rotation. The deploy-time set (the bootstrap stack's
#     parameters, which the deploy legs read and the operator owns) is
#     the source of truth; the runtime's copy below follows it, so a
#     rotation reaches the agents on the next apply instead of
#     needing a second copy to keep in step.
#   * A re-key. A parameter's encryption key is fixed at creation, so
#     moving one onto another key means recreating it — and a
#     replacement writes the LAST APPLIED value, which is empty for
#     every parameter the operator filled in after its creation.
#     Reading the live value from OOS carries it across instead.
#
# `try(...)` falls back to the variable for an account where the
# deploy-time parameter does not exist yet — a fresh account, before
# the bootstrap has created it.
data "alicloud_oos_secret_parameters" "deploy" {
  for_each = local.deploy_secret_parameters

  secret_parameter_name = each.value
  enable_details        = true
  with_decryption       = true
}

resource "alicloud_oos_secret_parameter" "github_app_id" {
  secret_parameter_name = local.oos_github_app_id_name
  value                 = try(data.alicloud_oos_secret_parameters.deploy["github-app-id"].parameters[0].value, var.github_app_id)
  type                  = "Secret"
  key_id                = var.kms_key_id
  description           = "GitHub App ID (env: ${var.environment})"
}

resource "alicloud_oos_secret_parameter" "github_app_private_key" {
  secret_parameter_name = local.oos_github_app_private_key_name
  value                 = try(data.alicloud_oos_secret_parameters.deploy["github-app-private-key"].parameters[0].value, var.github_app_private_key)
  type                  = "Secret"
  key_id                = var.kms_key_id
  description           = "GitHub App private key (base64 encoded, env: ${var.environment})"
}

resource "alicloud_oos_secret_parameter" "github_app_installation_id" {
  secret_parameter_name = local.oos_github_app_installation_name
  value                 = try(data.alicloud_oos_secret_parameters.deploy["github-app-installation-id"].parameters[0].value, var.github_app_installation_id)
  type                  = "Secret"
  key_id                = var.kms_key_id
  description           = "GitHub App installation ID (install on a throwaway sandbox repo only, env: ${var.environment})"
}

resource "alicloud_oos_secret_parameter" "github_webhook_secret" {
  secret_parameter_name = local.oos_github_webhook_secret_name
  value                 = try(data.alicloud_oos_secret_parameters.deploy["github-webhook-secret"].parameters[0].value, var.github_webhook_secret)
  type                  = "Secret"
  key_id                = var.kms_key_id
  description           = "GitHub webhook HMAC secret (env: ${var.environment})"
}

resource "alicloud_oos_secret_parameter" "opencode_api_key" {
  secret_parameter_name = local.oos_opencode_api_key_name
  value                 = try(data.alicloud_oos_secret_parameters.deploy["opencode-api-key"].parameters[0].value, var.opencode_api_key)
  type                  = "Secret"
  key_id                = var.kms_key_id
  description           = "OpenCode inference provider API key (env: ${var.environment})"
}

resource "alicloud_oos_secret_parameter" "stt_api_url" {
  secret_parameter_name = local.oos_stt_api_url_name
  value                 = try(data.alicloud_oos_secret_parameters.deploy["stt-api-url"].parameters[0].value, var.stt_api_url)
  type                  = "Secret"
  key_id                = var.kms_key_id
  description           = "Whisper-compatible STT endpoint exposed by whisper-stt-shim on the EC2 instance (env: ${var.environment})"
}

resource "alicloud_oos_secret_parameter" "stt_api_key" {
  secret_parameter_name = local.oos_stt_api_key_name
  value                 = try(data.alicloud_oos_secret_parameters.deploy["stt-api-key"].parameters[0].value, var.stt_api_key)
  type                  = "Secret"
  key_id                = var.kms_key_id
  description           = "API key passed through to the STT provider (env: ${var.environment})"
}

resource "alicloud_oos_secret_parameter" "stt_model" {
  secret_parameter_name = local.oos_stt_model_name
  value                 = try(data.alicloud_oos_secret_parameters.deploy["stt-model"].parameters[0].value, var.stt_model)
  type                  = "Secret"
  key_id                = var.kms_key_id
  description           = "whisper.cpp model name (e.g. base.en, tiny.en, small.en) (env: ${var.environment})"
}

resource "alicloud_oos_secret_parameter" "stt_language" {
  secret_parameter_name = local.oos_stt_language_name
  value                 = try(data.alicloud_oos_secret_parameters.deploy["stt-language"].parameters[0].value, var.stt_language)
  type                  = "Secret"
  key_id                = var.kms_key_id
  description           = "Whisper language hint passed to whisper-cli (empty = auto-detect) (env: ${var.environment})"
}

resource "alicloud_oos_secret_parameter" "stt_models_bucket" {
  secret_parameter_name = local.oos_stt_models_bucket_name
  value                 = try(data.alicloud_oos_secret_parameters.deploy["stt-models-bucket-name"].parameters[0].value, data.alicloud_oss_buckets.stt_models.buckets[0].name)
  type                  = "Secret"
  key_id                = var.kms_key_id
  description           = "OSS bucket hosting whisper.cpp model files for ECS boot-time download (env: ${var.environment})"
}
