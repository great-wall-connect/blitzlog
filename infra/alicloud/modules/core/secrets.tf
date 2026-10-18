# Analogue of infra/aws/modules/core/iam.tf (the SSM parameter
# resources at infra/aws/modules/core/iam.tf:356-423).
#
# Alicloud KMS Secrets are the AWS SSM Parameter Store analogue for
# non-String types. KMS Secrets give us per-secret encryption with a
# dedicated CMK, versioned rotation, and ACL-based access — the
# three things the AWS-side SSM SecureString provides. The
# secret-name paths mirror the AWS-side paths byte-for-byte so
# `lambda/_env.py` reads them via the same `BLITZLOG_PROVIDER`-
# agnostic path strings.
#
# The CMK itself is in `kms.tf` (separate file). All secrets below
# reference `alicloud_kms_key.secrets.id` for encryption.

data "alicloud_account" "current" {}

resource "alicloud_kms_secret" "github_app_id" {
  description                   = "GitHub App ID (env: ${var.environment})"
  secret_name                   = local.ssm_github_app_id_name
  secret_data                   = var.github_app_id
  encryption_key_id             = alicloud_kms_key.secrets.id
  version_id                    = "v1"
  force_delete_without_recovery = true
}

resource "alicloud_kms_secret" "github_app_private_key" {
  description                   = "GitHub App private key (base64 encoded, env: ${var.environment})"
  secret_name                   = local.ssm_github_app_private_key_name
  secret_data                   = var.github_app_private_key
  encryption_key_id             = alicloud_kms_key.secrets.id
  version_id                    = "v1"
  force_delete_without_recovery = true
}

resource "alicloud_kms_secret" "github_app_installation_id" {
  description                   = "GitHub App installation ID (env: ${var.environment})"
  secret_name                   = local.ssm_github_app_installation_name
  secret_data                   = var.github_app_installation_id
  encryption_key_id             = alicloud_kms_key.secrets.id
  version_id                    = "v1"
  force_delete_without_recovery = true
}

resource "alicloud_kms_secret" "github_webhook_secret" {
  description                   = "GitHub webhook HMAC secret (env: ${var.environment})"
  secret_name                   = local.ssm_github_webhook_secret_name
  secret_data                   = var.github_webhook_secret
  encryption_key_id             = alicloud_kms_key.secrets.id
  version_id                    = "v1"
  force_delete_without_recovery = true
}

resource "alicloud_kms_secret" "opencode_api_key" {
  description                   = "OpenCode inference provider API key (env: ${var.environment})"
  secret_name                   = local.ssm_opencode_api_key_name
  secret_data                   = var.opencode_api_key
  encryption_key_id             = alicloud_kms_key.secrets.id
  version_id                    = "v1"
  force_delete_without_recovery = true
}

resource "alicloud_kms_secret" "stt_api_url" {
  description                   = "Whisper-compatible STT endpoint exposed by whisper-stt-shim on the EC2 instance (env: ${var.environment})"
  secret_name                   = local.ssm_stt_api_url_name
  secret_data                   = var.stt_api_url
  encryption_key_id             = alicloud_kms_key.secrets.id
  version_id                    = "v1"
  force_delete_without_recovery = true
}

resource "alicloud_kms_secret" "stt_api_key" {
  description                   = "API key passed through to the STT provider (env: ${var.environment})"
  secret_name                   = local.ssm_stt_api_key_name
  secret_data                   = var.stt_api_key
  encryption_key_id             = alicloud_kms_key.secrets.id
  version_id                    = "v1"
  force_delete_without_recovery = true
}

resource "alicloud_kms_secret" "stt_model" {
  description                   = "whisper.cpp model name (e.g. base.en, tiny.en, small.en) (env: ${var.environment})"
  secret_name                   = local.ssm_stt_model_name
  secret_data                   = var.stt_model
  encryption_key_id             = alicloud_kms_key.secrets.id
  version_id                    = "v1"
  force_delete_without_recovery = true
}

resource "alicloud_kms_secret" "stt_language" {
  description                   = "Whisper language hint passed to whisper-cli (empty = auto-detect) (env: ${var.environment})"
  secret_name                   = local.ssm_stt_language_name
  secret_data                   = var.stt_language
  encryption_key_id             = alicloud_kms_key.secrets.id
  version_id                    = "v1"
  force_delete_without_recovery = true
}

resource "alicloud_kms_secret" "stt_models_bucket" {
  description                   = "OSS bucket hosting whisper.cpp model files for ECS boot-time download (env: ${var.environment})"
  secret_name                   = local.ssm_stt_models_bucket_name
  secret_data                   = data.alicloud_oss_buckets.stt_models.buckets[0].name
  encryption_key_id             = alicloud_kms_key.secrets.id
  version_id                    = "v1"
  force_delete_without_recovery = true
}
