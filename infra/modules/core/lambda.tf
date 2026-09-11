data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/build"
  output_path = "${path.module}/lambda_function.zip"

  depends_on = [null_resource.lambda_build]
}

# The Lambda zip bundles the `lambda/` source directory as a package
# at the zip task root. The runtime invokes `lambda.handler.lambda_handler`
# (see `aws_lambda_function.handler` below); `lambda/` is a package whose
# top-level layout is:
#
#     lambda/__init__.py            # not present; PEP 420 namespace package
#     lambda/_env.py                # env-scoped SSM constants + shim source
#     lambda/handler.py             # lambda_handler, extract_event_data
#     lambda/auth.py                # GitHub App JWT + webhook signature
#     lambda/bot_pool.py            # per-user bot pool + locks
#     lambda/llm_guard.py           # IP safety guard for local LLM endpoints
#     lambda/ec2.py                 # EC2 spot launch + helpers
#     lambda/plugins.py             # JS plugin loader + heredoc writers
#     lambda/plugins/*.js           # 5 JS sources (extracted from string literals)
#     lambda/scripts/_common.py     # shared prologue + install/config builders
#     lambda/scripts/autonomous.py  # build_autonomous_user_data
#     lambda/scripts/assisted.py    # build_assisted_user_data
#
# The `filemd5` trigger below enumerates every source file so any edit to
# the package forces a rebuild. Adding a new module means adding a new
# `filemd5(...)` line here.
resource "null_resource" "lambda_build" {
  triggers = {
    # Package entrypoint + modules
    handler_py   = filemd5("${path.module}/../../../lambda/handler.py")
    env_py       = filemd5("${path.module}/../../../lambda/_env.py")
    auth_py      = filemd5("${path.module}/../../../lambda/auth.py")
    bot_pool_py  = filemd5("${path.module}/../../../lambda/bot_pool.py")
    llm_guard_py = filemd5("${path.module}/../../../lambda/llm_guard.py")
    ec2_py       = filemd5("${path.module}/../../../lambda/ec2.py")
    plugins_py   = filemd5("${path.module}/../../../lambda/plugins.py")

    # scripts/ subpackage
    scripts_common_py     = filemd5("${path.module}/../../../lambda/scripts/_common.py")
    scripts_autonomous_py = filemd5("${path.module}/../../../lambda/scripts/autonomous.py")
    scripts_assisted_py   = filemd5("${path.module}/../../../lambda/scripts/assisted.py")

    # JS plugin sources (loaded at cold-start, embedded into bootstrap heredoc)
    plugin_session_archive_js   = filemd5("${path.module}/../../../lambda/plugins/session_archive.js")
    plugin_spot_watchdog_js     = filemd5("${path.module}/../../../lambda/plugins/spot_watchdog.js")
    plugin_periodic_autosave_js = filemd5("${path.module}/../../../lambda/plugins/periodic_autosave.js")
    plugin_idle_watchdog_js     = filemd5("${path.module}/../../../lambda/plugins/idle_watchdog.js")
    plugin_shutdown_tool_js     = filemd5("${path.module}/../../../lambda/plugins/shutdown_tool.js")

    # Runtime deps + the embedded whisper-stt shim
    requirements = filemd5("${path.module}/../../../lambda/requirements.txt")
    shim_source  = filemd5("${path.module}/../../../packages/whisper-stt-shim/server.py")
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      rm -rf ${path.module}/build ${path.module}/.build-venv
      mkdir -p ${path.module}/build
      # Copy the whole lambda/ source dir as a package; the AWS Lambda
      # runtime imports the handler as `lambda.handler.lambda_handler`.
      cp -r ${path.module}/../../../lambda ${path.module}/build/
      # The bootstrap heredoc reads packages/whisper-stt-shim/server.py
      # via `lambda/_env.py::_load_shim_source()` which looks for the file
      # at `lambda/packages/whisper-stt-shim/server.py` inside the zip.
      mkdir -p ${path.module}/build/lambda/packages/whisper-stt-shim
      cp ${path.module}/../../../packages/whisper-stt-shim/server.py \
         ${path.module}/build/lambda/packages/whisper-stt-shim/
      python3 -m venv ${path.module}/.build-venv
      curl -sS https://bootstrap.pypa.io/get-pip.py | ${path.module}/.build-venv/bin/python3
      ${path.module}/.build-venv/bin/pip install --no-cache-dir -r ${path.module}/../../../lambda/requirements.txt -t ${path.module}/build/
      rm -rf ${path.module}/.build-venv
    EOT
  }
}

resource "aws_lambda_function" "handler" {
  function_name    = "blitzlog-${var.environment}-handler"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  handler          = "lambda.handler.lambda_handler"
  runtime          = "python3.12"
  role             = aws_iam_role.lambda_role.arn
  timeout          = 180
  dead_letter_config {
    target_arn = aws_sqs_queue.dlq.arn
  }

  environment {
    variables = {
      BLITZLOG_ENV              = var.environment
      VPC_ID                    = var.vpc_id
      EC2_SUBNET_ID             = var.ec2_subnet_id
      EC2_SECURITY_GROUP_ID     = aws_security_group.agent_sg.id
      EC2_INSTANCE_PROFILE_NAME = aws_iam_instance_profile.ec2_agent_profile.name
      OPENCODE_MODEL            = var.opencode_model
      S3_LOGS_BUCKET            = data.aws_s3_bucket.agent_logs.bucket
    }
  }

  depends_on = [
    aws_iam_role_policy.lambda_policy,
    aws_cloudwatch_log_group.lambda_log_group,
    null_resource.lambda_build,
  ]

  lifecycle {
    ignore_changes = [last_modified]
  }
}

resource "aws_cloudwatch_log_group" "lambda_log_group" {
  name              = "/aws/lambda/blitzlog-${var.environment}-handler"
  retention_in_days = 14
}
