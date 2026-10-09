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
#     lambda/scripts/_common.py     # shared prologue + install/config builders
#     lambda/scripts/autonomous.py  # build_autonomous_user_data
#     lambda/scripts/assisted.py    # build_assisted_user_data
#
# opencode plugins + tools (idle_watchdog, periodic_autosave,
# session_archive, spot_watchdog, shutdown) live in
# packages/images/agent/opencode/{plugins,tools}/ and are baked into
# the container image by packages/images/agent/Dockerfile. They are
# NOT loaded by the Lambda bootstrap — that path was removed when the
# container took over plugin installation.
#
# The `filemd5` trigger below enumerates every source file so any edit to
# the package forces a rebuild. Adding a new module means adding a new
# `filemd5(...)` line here.
resource "null_resource" "lambda_build" {
  triggers = {
    # Package entrypoint + modules
    handler_py        = filemd5("${path.module}/../../../lambda/handler.py")
    init_py           = filemd5("${path.module}/../../../lambda/__init__.py")
    root_version_py   = filemd5("${path.module}/../../../version.py")
    lambda_version_py = filemd5("${path.module}/../../../lambda/_version.py")
    env_py            = filemd5("${path.module}/../../../lambda/_env.py")
    auth_py           = filemd5("${path.module}/../../../lambda/auth.py")
    bot_pool_py       = filemd5("${path.module}/../../../lambda/bot_pool.py")
    llm_guard_py      = filemd5("${path.module}/../../../lambda/llm_guard.py")
    ec2_py            = filemd5("${path.module}/../../../lambda/ec2.py")

    # scripts/ subpackage
    scripts_common_py     = filemd5("${path.module}/../../../lambda/scripts/_common.py")
    scripts_autonomous_py = filemd5("${path.module}/../../../lambda/scripts/autonomous.py")
    scripts_assisted_py   = filemd5("${path.module}/../../../lambda/scripts/assisted.py")

    # Runtime deps + the embedded whisper-stt shim
    requirements = filemd5("${path.module}/../../../lambda/requirements.txt")
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      # The lambda runtime is python3.12 (see `runtime = "python3.12"` above).
      # The build MUST use the same version — otherwise C extensions
      # (e.g., cryptography's `_cffi_backend.cpython-312-*.so`) link against
      # the wrong Python ABI and fail to import at lambda runtime. Resolve
      # Python 3.12 from the first source we find:
      #   1. system python3.12 (apt install python3.12)
      #   2. mise's Python 3.12 install (the project pins python = "3.12" in
      #      mise.toml, so this is the common case)
      #   3. fail loudly
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
      # Copy the whole lambda/ source dir as a package; the AWS Lambda
      # runtime imports the handler as `lambda.handler.lambda_handler`.
      cp -r ${path.module}/../../../lambda ${path.module}/build/
      # The bootstrap heredoc reads packages/whisper-stt-shim/server.py
      # via `lambda/_env.py::_load_shim_source()` which looks for the file
      # at `lambda/packages/whisper-stt-shim/server.py` inside the zip.
      mkdir -p ${path.module}/build/lambda/packages/whisper-stt-shim
      cp ${path.module}/../../../packages/whisper-stt-shim/server.py \
         ${path.module}/build/lambda/packages/whisper-stt-shim/
      "$PYTHON_BIN" -m venv ${path.module}/.build-venv
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
      OPENCODE_AGENT_MAX_STEPS  = tostring(var.opencode_agent_max_steps)
      S3_LOGS_BUCKET            = data.aws_s3_bucket.agent_logs.bucket
      # JSON-encoded so a single env var can carry the ordered list; the Lambda
      # parses it once at cold start (see lambda/ec2.py::_load_spot_instance_types).
      SPOT_INSTANCE_TYPES_JSON = jsonencode(var.spot_instance_types)
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
