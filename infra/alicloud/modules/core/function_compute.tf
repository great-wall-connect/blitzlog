# Analogue of infra/aws/modules/core/lambda.tf.
#
# Aliyun Function Compute (FC) is the AWS Lambda analogue. The
# `null_resource.lambda_build` provisioner is byte-identical to the
# AWS one (same Python 3.12 venv, same `lambda/` package layout, same
# `filemd5` trigger list) because the source tree is provider-agnostic.
# The cloud-side wrapper uses alicloud_fc_service + alicloud_fc_function
# with `runtime = "python3.12"` (Aliyun's `python3.12` runtime id).
#
# Aliyun FC differs from AWS Lambda in three ways we have to model here:
#
#   1. FC services require an explicit `internet_access` + `vpc_config`
#      block; AWS Lambda handles this implicitly.
#   2. FC functions reference the service they're deployed in
#      (`alicloud_fc_service.handler.name`) instead of being a single
#      resource like `aws_lambda_function`.
#   3. Aliyun has no API Gateway HTTP API in the AWS sense (no
#      `protocol_type = "HTTP"`); the API Gateway is configured in
#      apigateway.tf and points at the FC function's invoke URL.
#
# Token refresh chain (issue #145) is intentionally NOT modelled here:
# Aliyun has no EventBridge Scheduler equivalent in this PR, so
# `BLITZLOG_PROVIDER=alicloud` will hold a 1 h token only (the GitHub
# hard cap). Follow-up issue for the Aliyun scheduler equivalent.

data "archive_file" "function_zip" {
  type        = "zip"
  source_dir  = "${path.module}/build"
  output_path = "${path.module}/function.zip"

  depends_on = [null_resource.lambda_build]
}

resource "null_resource" "lambda_build" {
  triggers = {
    # Package entrypoint + modules
    handler_py        = filemd5("${path.module}/../../../../lambda/handler.py")
    init_py           = filemd5("${path.module}/../../../../lambda/__init__.py")
    version_py        = filemd5("${path.module}/../../../../lambda/version.py")
    lambda_version_py = filemd5("${path.module}/../../../../lambda/_version.py")
    env_py            = filemd5("${path.module}/../../../../lambda/_env.py")
    auth_py           = filemd5("${path.module}/../../../../lambda/auth.py")
    bot_pool_py       = filemd5("${path.module}/../../../../lambda/bot_pool.py")
    llm_guard_py      = filemd5("${path.module}/../../../../lambda/llm_guard.py")
    ec2_py            = filemd5("${path.module}/../../../../lambda/ec2.py")

    # scripts/ subpackage
    scripts_common_py     = filemd5("${path.module}/../../../../lambda/scripts/alicloud/_common.py")
    scripts_autonomous_py = filemd5("${path.module}/../../../../lambda/scripts/alicloud/autonomous.py")
    scripts_assisted_py   = filemd5("${path.module}/../../../../lambda/scripts/alicloud/assisted.py")

    # provider/ subpackage — see #83
    provider_init_py     = filemd5("${path.module}/../../../../lambda/provider/__init__.py")
    provider_base_py     = filemd5("${path.module}/../../../../lambda/provider/base.py")
    provider_aws_py      = filemd5("${path.module}/../../../../lambda/provider/aws.py")
    provider_alicloud_py = filemd5("${path.module}/../../../../lambda/provider/alicloud.py")

    # Runtime deps + the embedded whisper-stt shim
    requirements = filemd5("${path.module}/../../../../lambda/requirements.txt")
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      # The function runtime is python3.12 (see `runtime = "python3.12"` below).
      # The build MUST use the same version — otherwise C extensions
      # (e.g., cryptography's `_cffi_backend.cpython-312-*.so`) link against
      # the wrong Python ABI and fail to import at function runtime. Resolve
      # Python 3.12 from the first source we find (mirrors the AWS-side
      # local-exec in infra/aws/modules/core/lambda.tf).
      PYTHON_BIN="$(command -v python3.12 || true)"
      if [ -z "$PYTHON_BIN" ] && [ -d "$HOME/.local/share/mise/installs/python" ]; then
        PYTHON_BIN="$(ls -d "$HOME/.local/share/mise/installs/python"/3.12.*/bin/python3 2>/dev/null | sort -V | tail -1)"
      fi
      if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
        echo "FATAL: Python 3.12 not found. Install via 'apt install python3.12' or 'mise install'." >&2
        exit 1
      fi
      rm -rf ${path.module}/build ${path.module}/.build-venv
      mkdir -p ${path.module}/build
      # Copy the whole lambda/ source dir as a package; the FC runtime
      # imports the handler as `lambda.handler.lambda_handler` (same
      # entrypoint as AWS Lambda).
      cp -r ${path.module}/../../../../lambda ${path.module}/build/
      # The bootstrap heredoc reads packages/whisper-stt-shim/server.py
      # via `lambda/_env.py::_load_shim_source()` which looks for the file
      # at `lambda/packages/whisper-stt-shim/server.py` inside the zip.
      mkdir -p ${path.module}/build/lambda/packages/whisper-stt-shim
      cp ${path.module}/../../../../packages/whisper-stt-shim/server.py \
         ${path.module}/build/lambda/packages/whisper-stt-shim/
      "$PYTHON_BIN" -m venv ${path.module}/.build-venv
      curl -sS https://bootstrap.pypa.io/get-pip.py | ${path.module}/.build-venv/bin/python3
      ${path.module}/.build-venv/bin/pip install --no-cache-dir -r ${path.module}/../../../../lambda/requirements.txt -t ${path.module}/build/
      rm -rf ${path.module}/.build-venv
    EOT
  }
}

# Function Compute service. One service per env; the function is
# deployed INTO the service. Aliyun's `internet_access = true` is the
# default and matches AWS Lambda's default-egress posture.
#
# The execution RAM role is attached to the SERVICE (not the
# function) — this is the aliyun/alicloud provider's design. The
# `role` attribute here is the ARN of the FC execution role from
# ram.tf.
resource "alicloud_fc_service" "handler" {
  name            = "blitzlog-${var.environment}-handler"
  internet_access = true
  role            = alicloud_ram_role.lambda_role.arn

  # No VPC config: FC is invoked via API Gateway, which connects from
  # the public internet. The ECS agent (which DOES need VPC) is
  # modelled in ecs.tf. Keeping FC serverless-public mirrors the
  # AWS-Lambda "no VPC" posture in infra/aws/modules/core/lambda.tf.
}

resource "alicloud_fc_function" "handler" {
  service  = alicloud_fc_service.handler.name
  name     = "blitzlog-${var.environment}-handler"
  filename = data.archive_file.function_zip.output_path
  # `code_checksum` is the FC analogue of `source_code_hash`.
  code_checksum = data.archive_file.function_zip.output_base64sha256
  handler       = "lambda.handler.lambda_handler"
  runtime       = "python3.12"
  memory_size   = 512
  timeout       = 180

  environment_variables = {
    BLITZLOG_ENV = var.environment
    # Cloud-provider key. Aliyun FC dispatches via
    # lambda/provider/__init__.py. The factory reads this on cold
    # start and picks AlicloudCompute / AlicloudSecretStore /
    # AlicloudObjectStore. The OSS2 / KMS / ECS SDKs are bundled
    # in the zip from lambda/requirements.in (alibabacloud_*).
    BLITZLOG_PROVIDER     = "alicloud"
    VPC_ID                = var.vpc_id
    VSWITCH_IDS           = join(",", var.vswitch_ids)
    ECS_SECURITY_GROUP_ID = alicloud_security_group.agent_sg.id
    # Aliyun has no instance-profile concept; the RAM role name is
    # passed directly to RunInstances via the `RamRoleName` parameter.
    RAM_INSTANCE_PROFILE_NAME = alicloud_ram_role.ecs_agent_role.name
    OPENCODE_MODEL            = var.opencode_model
    OPENCODE_AGENT_MAX_STEPS  = tostring(var.opencode_agent_max_steps)
    OSS_LOGS_BUCKET           = local.agent_logs_bucket_name_resolved
    # JSON-encoded so a single env var can carry the ordered list; the
    # function parses it once at cold start (see lambda/ec2.py).
    SPOT_INSTANCE_TYPES_JSON = jsonencode(var.spot_instance_types)
    # Requested GitHub App installation-token lifetime in hours. The
    # function reads this on every invocation and includes the
    # matching `expires_at` when POSTing to
    # /app/installations/<id>/access_tokens, so `git` and `gh` stay
    # authed for the full agent run. The 1 h cap (issue #145) is
    # documented; the Alicloud path has no token-refresh chain in
    # this PR.
    GITHUB_TOKEN_LIFETIME_HOURS = tostring(var.github_token_lifetime_hours)
  }

  depends_on = [
    alicloud_ram_role_policy_attachment.lambda_policy,
    null_resource.lambda_build,
  ]

  lifecycle {
    # FC has no `last_modified` attribute (unlike AWS Lambda's
    # timestamp); no equivalent ignore_changes needed.
  }
}
