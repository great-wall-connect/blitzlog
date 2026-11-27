locals {
  # AWS analogue: infra/aws/modules/core/locals.tf.
  # The Alicloud secret store is the OOS Parameter Store (see
  # secrets.tf). Its parameter names mirror the AWS SSM parameter
  # names so the Lambda-side _env.py reads them via the same path
  # strings (`/blitzlog/<env>/<service>/<name>`). Per-user bot pools
  # and per-user local LLM config live under /blitzlog/users/ with
  # NO env infix (mirrored in ram.tf's literal ARN grants).
  #
  # No leading slash. The OOS resource ARN is
  # `acs:oos:<region>:<account>:secretparameter/<name>`, so a name
  # that already started with "/" would render an ARN with two
  # slashes after `secretparameter` and the RAM grants would stop
  # matching. `AlicloudSecretStore` strips the leading slash off the
  # SSM-style path it is handed, so the wire names are these.
  oos_root = "blitzlog/${var.environment}"

  oos_github_app_id_name           = "${local.oos_root}/github-app/id"
  oos_github_app_private_key_name  = "${local.oos_root}/github-app/private-key"
  oos_github_app_installation_name = "${local.oos_root}/github-app/installation-id"
  oos_github_webhook_secret_name   = "${local.oos_root}/github-webhook/secret"
  oos_opencode_api_key_name        = "${local.oos_root}/opencode/api-key"
  oos_stt_api_url_name             = "${local.oos_root}/stt/api-url"
  oos_stt_api_key_name             = "${local.oos_root}/stt/api-key"
  oos_stt_model_name               = "${local.oos_root}/stt/model"
  oos_stt_language_name            = "${local.oos_root}/stt/language"
  oos_stt_models_bucket_name       = "${local.oos_root}/stt/models-bucket"

  # The deploy-time parameters, keyed by the leaf the deploy legs
  # read, with the parameter's name as the value.
  #
  # These are the SOURCE OF TRUTH for the secrets: the bootstrap
  # stack owns them (infra/alicloud/bootstrap/secrets.tf), the deploy
  # legs read them to render the TF variables, and the runtime's copy
  # (the parameters in secrets.tf) follows them — so a rotation
  # reaches the agents on the next apply, and a parameter can be
  # moved onto another key without losing its value.
  deploy_secret_parameters = {
    "github-app-id"              = "${local.oos_root}/github-app-id"
    "github-app-private-key"     = "${local.oos_root}/github-app-private-key"
    "github-app-installation-id" = "${local.oos_root}/github-app-installation-id"
    "github-webhook-secret"      = "${local.oos_root}/github-webhook-secret"
    "opencode-api-key"           = "${local.oos_root}/opencode-api-key"
    "stt-api-url"                = "${local.oos_root}/stt-api-url"
    "stt-api-key"                = "${local.oos_root}/stt-api-key"
    "stt-model"                  = "${local.oos_root}/stt-model"
    "stt-language"               = "${local.oos_root}/stt-language"
    "stt-models-bucket-name"     = "${local.oos_root}/stt-models-bucket-name"
  }

  # The per-issue ephemeral GitHub token the Lambda writes at launch
  # time. Env-scoped, no leading slash for the same ARN reason as
  # `oos_root` above.
  oos_ephemeral_root = "blitzlog/${var.environment}/ephemeral"

  # Per-user bot pools and per-user local LLM config live under
  # /blitzlog/users/ with NO env infix — both are user-owned data
  # shared across envs (a user has one set of Telegram bots and one
  # local LLM endpoint, not one per env). Referenced as literal
  # resource ARNs in ram.tf.

  # The FC function's name. Shared so the RAM policy's
  # `fc:InvokeFunction` grant can name the function without
  # referencing the resource — a reference would be a dependency
  # cycle (the function depends on the policy attachment, and the
  # policy would depend on the function).
  fc_function_name = "blitzlog-${var.environment}-handler"

  # The API's request path — the path the API Gateway routes on, and
  # the tail of the webhook URL (the group's domain + this path is
  # the URL the GitHub App POSTs to). Kept in one place so the
  # `request_config.path` and the `webhook_url` output cannot drift.
  webhook_api_path = "/"

  # The OOS secret-parameter ARN prefix. The OOS resource exposes no
  # ARN attribute, so the RAM policies build the ARNs from this
  # prefix + the parameter name.
  #
  # The parameter is a KMS secret underneath, but RAM evaluates the
  # `oos:*` actions against the `acs:oos:` ARN form — the API's own
  # error names the internal `acs:kms:...:secret/oos/...` ARN, and
  # granting the action on that form does not satisfy the check.
  oos_secretparameter_arn = "acs:oos:${var.alicloud_region}:${data.alicloud_account.current.id}:secretparameter/"

  stt_models_bucket = var.stt_models_bucket_name != "" ? var.stt_models_bucket_name : "blitzlog-${var.environment}-stt-models"

  agent_logs_prefix = var.environment

  # OSS key under the shared agent-logs bucket where the terraform
  # deployer uploads the per-env function-code zip. The function
  # cold-starts from here (see alicloud_oss_bucket_object.fc_code
  # and alicloud_fcv3_function.handler in function_compute.tf). The
  # fc execution role has oss:GetObject on this exact key only —
  # see ram.tf's `oos:CreateSecretParameter` Sid's neighbour for the
  # grant.
  fc_code_object_key = "fc-code/${var.environment}/function.zip"

  # MNS queue ARN format: acs:mns:<region>:<account-id>:/queues/<queue-name>
  # The data source returns queue names but not ARNs, so we build them.
  mns_dlq_arn = "acs:mns:${var.alicloud_region}:${data.alicloud_account.current.id}:/queues/blitzlog-${var.environment}-function-dlq"
}
