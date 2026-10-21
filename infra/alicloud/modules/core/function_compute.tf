# Analogue of infra/aws/modules/core/lambda.tf.
#
# Aliyun Function Compute v3 (`alicloud_fcv3_function`) is the AWS
# Lambda analogue and replaces the deprecated v1 `alicloud_fc_service`
# + `alicloud_fc_function` pair (deprecated in provider v1.272.0).
# The v3 API is flat: there is no separate "service" resource; the
# `function_name` is a top-level string. The runtime id for Python
# 3.12 is identical (`python3.12`).
#
# The `null_resource.lambda_build` provisioner is byte-identical to
# the AWS one (same Python 3.12 venv, same `lambda/` package layout)
# because the source tree is provider-agnostic. The only TF-side
# difference between AWS and Alicloud is the requirements-file name
# (`var.lambda_requirements`, defaulting to `requirements-alicloud.txt`
# here vs. `requirements-aws.txt` on the AWS side — see #155's commit
# `chore(lambda): slim AWS Lambda bundle by per-cloud requirements
# split`).
#
# Token refresh chain (issue #145) is intentionally NOT modelled here:
# Aliyun has no EventBridge Scheduler equivalent in this PR, so
# `BLITZLOG_PROVIDER=alicloud` will hold a 1 h token only (the GitHub
# hard cap). Follow-up issue for the Aliyun scheduler equivalent.
#
# Code-delivery shape (unique to fcv3 vs the AWS Lambda analogue):
# alicloud_fcv3_function does NOT accept a `filename` like
# aws_lambda_function does — it wants either a base64-encoded
# `code.zip_file` (whose ~50 MB would land in state on every apply)
# or an OSS object reference (`code.oss_bucket_name` +
# `code.oss_object_name`). We use the OSS path: a corresponding
# `alicloud_oss_bucket_object.fc_code` below uploads the zip built
# by `null_resource.lambda_build` and `data.archive_file.function_zip`
# into the shared agent-logs bucket under
# `${local.fc_code_object_key}` (defined in locals.tf:42). The FC
# execution role has an `oss:GetObject` grant scoped to that
# exact key — see ram.tf.

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

    # Per-cloud runtime deps. `var.lambda_requirements` defaults to
    # `requirements-alicloud.txt` so the alibaba SDKs (oss2,
    # alibabacloud_kms20160120, alibabacloud_ecs20140526) ship in
    # this zip without contaminating the AWS Lambda bundle (see
    # infra/aws/modules/core/variables.tf:lambda_requirements).
    requirements = filemd5("${path.module}/../../../../lambda/${var.lambda_requirements}")
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
      ${path.module}/.build-venv/bin/pip install --no-cache-dir -r ${path.module}/../../../../lambda/${var.lambda_requirements} -t ${path.module}/build/
      rm -rf ${path.module}/.build-venv
    EOT
  }
}

# Upload the function zip to OSS so alicloud_fcv3_function can pull
# it from `code{oss_bucket_name,oss_object_name}` below. The bucket
# is shared across envs (the bootstrap stack creates it once) so the
# key is env-namespaced — `local.fc_code_object_key` =
# `fc-code/<env>/function.zip` — and the FC execution role's
# oss:GetObject grant in ram.tf is scoped to that exact key.
resource "alicloud_oss_bucket_object" "fc_code" {
  bucket      = local.agent_logs_bucket_name_resolved
  key         = local.fc_code_object_key
  source      = data.archive_file.function_zip.output_path
  content_md5 = data.archive_file.function_zip.output_md5
  # Default AES256 is fine; no KMS needed because the zip is a
  # derivative build artifact (no embedded GitHub / OpenCode / STT
  # secrets — those flow through KMS Secrets at function cold start).
  acl = "private"
}

# Function Compute v3 function. v3 is the modern API
# (`fc:2023-03-30`); the v1 fc_service + fc_function pair is
# deprecated in provider v1.272.0. v3 functions are flat: there is
# no separate `alicloud_fcv3_service` resource, the `function_name`
# is a top-level argument, and the role is attached to the function
# rather than to a wrapping service.
#
# `internet_access = true` mirrors the v1 posture (FC functions
# reach the public internet for OSS, KMS, ECS, and the GitHub /
# OpenCode APIs). The ECS instance-side networking is owned by
# ecs.tf.
resource "alicloud_fcv3_function" "handler" {
  function_name   = "blitzlog-${var.environment}-handler"
  description     = "Blitzlog webhook handler (env: ${var.environment})"
  runtime         = "python3.12"
  handler         = "lambda.handler.lambda_handler"
  memory_size     = 512
  timeout         = 180
  internet_access = true
  role            = alicloud_ram_role.lambda_role.arn

  # Code comes from OSS (uploaded by alicloud_oss_bucket_object.fc_code
  # above). The explicit object reference avoids the ~50 MB base64
  # blob that `code.zip_file = file(...)` would put into plan state.
  code {
    oss_bucket_name = local.agent_logs_bucket_name_resolved
    oss_object_name = local.fc_code_object_key
  }

  environment_variables = {
    BLITZLOG_ENV = var.environment
    # Cloud-provider key. Aliyun FC dispatches via
    # lambda/provider/__init__.py. The factory reads this on cold
    # start and picks AlicloudCompute / AlicloudSecretStore /
    # AlicloudObjectStore. The OSS2 / KMS / ECS SDKs are bundled
    # in the zip from lambda/requirements-alicloud.txt
    # (alibabacloud_*).
    BLITZLOG_PROVIDER     = "alicloud"
    VPC_ID                = var.vpc_id
    VSWITCH_IDS           = join(",", var.vswitch_ids)
    ECS_SECURITY_GROUP_ID = alicloud_security_group.agent_sg.id
    # Aliyun has no instance-profile concept; the RAM role name is
    # passed directly to RunInstances via the `RamRoleName` parameter.
    RAM_INSTANCE_PROFILE_NAME = alicloud_ram_role.ecs_agent_role.role_name
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
    alicloud_oss_bucket_object.fc_code,
    null_resource.lambda_build,
  ]
}
