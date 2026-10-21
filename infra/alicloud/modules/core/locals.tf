locals {
  # AWS analogue: infra/aws/modules/core/locals.tf.
  # The KMS secret names mirror the AWS SSM parameter names so the
  # Lambda-side _env.py reads them via the same path strings
  # (`/blitzlog/<env>/<service>/<name>`). Per-user bot pools and
  # per-user local LLM config live under /blitzlog/users/ with NO
  # env infix (mirrored in ram.tf's literal ARN grants).
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

  # No leading slash — interpolated directly into the RAM policy ARN
  # template ("acs:kms:${var.alicloud_region}:${data.alicloud_account.current.id}:secret/${local.ssm_ephemeral_root}/*").
  # A leading slash would produce a double slash in the rendered policy
  # (e.g. "secret//blitzlog/dev/ephemeral/*") which silently fails to
  # match the actual KMS secret path the Lambda writes to
  # ("/blitzlog/dev/ephemeral/github-token-N").
  ssm_ephemeral_root = "blitzlog/${var.environment}/ephemeral"

  # Per-user bot pools and per-user local LLM config live under
  # /blitzlog/users/ with NO env infix — both are user-owned data
  # shared across envs (a user has one set of Telegram bots and one
  # local LLM endpoint, not one per env). Referenced as literal
  # resource ARNs in ram.tf.

  stt_models_bucket = var.stt_models_bucket_name != "" ? var.stt_models_bucket_name : "blitzlog-${var.environment}-stt-models"

  agent_logs_prefix = var.environment

  # OSS key under the shared agent-logs bucket where the terraform
  # deployer uploads the per-env function-code zip. The function
  # cold-starts from here (see alicloud_oss_bucket_object.fc_code
  # and alicloud_fcv3_function.handler in function_compute.tf). The
  # fc execution role has oss:GetObject on this exact key only —
  # see ram.tf's `kms:CreateSecret` Sid's neighbour for the grant.
  fc_code_object_key = "fc-code/${var.environment}/function.zip"

  # MNS queue ARN format: acs:mns:<region>:<account-id>:/queues/<queue-name>
  # The data source returns queue names but not ARNs, so we build them.
  mns_dlq_arn = "acs:mns:${var.alicloud_region}:${data.alicloud_account.current.id}:/queues/blitzlog-${var.environment}-function-dlq"
}
