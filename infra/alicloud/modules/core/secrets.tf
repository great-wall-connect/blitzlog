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
# with the CMK in `kms.tf`. The CMK is a KMS *default master key*,
# which KMS bills for at no charge — the billable KMS object is the
# instance, and we do not buy one.
#
# The parameter names mirror the AWS-side SSM paths byte-for-byte
# so `lambda/_env.py` reads them via the same `BLITZLOG_PROVIDER`-
# agnostic path strings.
#
# `ignore_changes = [value]`: the provider's read calls
# `GetSecretParameter` WITHOUT `WithDecryption`, so the value reads
# back empty and Terraform would plan an update on every run —
# rewriting the value from the tfvars and clobbering whatever the
# operator set out-of-band (the documented workflow: the apply
# creates the parameters, then `aliyun oos UpdateSecretParameter`
# sets the real value). The tfvars value is used at creation; later
# changes go through the OOS API.
#
# The OOS resource exposes no ARN attribute, so the RAM policies
# that grant access to these parameters spell the ARNs out
# (`acs:oos:<region>:<account>:secretparameter/<name>`) — see
# `ram.tf`.

data "alicloud_account" "current" {}

resource "alicloud_oos_secret_parameter" "github_app_id" {
  secret_parameter_name = local.oos_github_app_id_name
  value                 = var.github_app_id
  type                  = "Secret"
  key_id                = alicloud_kms_key.secrets.id
  description           = "GitHub App ID (env: ${var.environment})"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "alicloud_oos_secret_parameter" "github_app_private_key" {
  secret_parameter_name = local.oos_github_app_private_key_name
  value                 = var.github_app_private_key
  type                  = "Secret"
  key_id                = alicloud_kms_key.secrets.id
  description           = "GitHub App private key (base64 encoded, env: ${var.environment})"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "alicloud_oos_secret_parameter" "github_app_installation_id" {
  secret_parameter_name = local.oos_github_app_installation_name
  value                 = var.github_app_installation_id
  type                  = "Secret"
  key_id                = alicloud_kms_key.secrets.id
  description           = "GitHub App installation ID (env: ${var.environment})"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "alicloud_oos_secret_parameter" "github_webhook_secret" {
  secret_parameter_name = local.oos_github_webhook_secret_name
  value                 = var.github_webhook_secret
  type                  = "Secret"
  key_id                = alicloud_kms_key.secrets.id
  description           = "GitHub webhook HMAC secret (env: ${var.environment})"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "alicloud_oos_secret_parameter" "opencode_api_key" {
  secret_parameter_name = local.oos_opencode_api_key_name
  value                 = var.opencode_api_key
  type                  = "Secret"
  key_id                = alicloud_kms_key.secrets.id
  description           = "OpenCode inference provider API key (env: ${var.environment})"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "alicloud_oos_secret_parameter" "stt_api_url" {
  secret_parameter_name = local.oos_stt_api_url_name
  value                 = var.stt_api_url
  type                  = "Secret"
  key_id                = alicloud_kms_key.secrets.id
  description           = "Whisper-compatible STT endpoint exposed by whisper-stt-shim on the EC2 instance (env: ${var.environment})"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "alicloud_oos_secret_parameter" "stt_api_key" {
  secret_parameter_name = local.oos_stt_api_key_name
  value                 = var.stt_api_key
  type                  = "Secret"
  key_id                = alicloud_kms_key.secrets.id
  description           = "API key passed through to the STT provider (env: ${var.environment})"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "alicloud_oos_secret_parameter" "stt_model" {
  secret_parameter_name = local.oos_stt_model_name
  value                 = var.stt_model
  type                  = "Secret"
  key_id                = alicloud_kms_key.secrets.id
  description           = "whisper.cpp model name (e.g. base.en, tiny.en, small.en) (env: ${var.environment})"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "alicloud_oos_secret_parameter" "stt_language" {
  secret_parameter_name = local.oos_stt_language_name
  value                 = var.stt_language
  type                  = "Secret"
  key_id                = alicloud_kms_key.secrets.id
  description           = "Whisper language hint passed to whisper-cli (empty = auto-detect) (env: ${var.environment})"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "alicloud_oos_secret_parameter" "stt_models_bucket" {
  secret_parameter_name = local.oos_stt_models_bucket_name
  value                 = data.alicloud_oss_buckets.stt_models.buckets[0].name
  type                  = "Secret"
  key_id                = alicloud_kms_key.secrets.id
  description           = "OSS bucket hosting whisper.cpp model files for ECS boot-time download (env: ${var.environment})"

  lifecycle {
    ignore_changes = [value]
  }
}
