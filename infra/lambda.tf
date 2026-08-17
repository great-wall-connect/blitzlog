data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/build"
  output_path = "${path.module}/lambda_function.zip"

  depends_on = [null_resource.lambda_build]
}

resource "null_resource" "lambda_build" {
  triggers = {
    handler      = filemd5("${path.module}/../lambda/handler.py")
    requirements = filemd5("${path.module}/../lambda/requirements.txt")
    timestamp    = timestamp()
  }

  provisioner "local-exec" {
    command = <<-EOT
      rm -rf ${path.module}/build ${path.module}/.build-venv
      mkdir -p ${path.module}/build
      cp ${path.module}/../lambda/handler.py ${path.module}/build/
      python3 -m venv ${path.module}/.build-venv
      curl -sS https://bootstrap.pypa.io/get-pip.py | ${path.module}/.build-venv/bin/python3
      ${path.module}/.build-venv/bin/pip install --no-cache-dir -r ${path.module}/../lambda/requirements.txt -t ${path.module}/build/
      rm -rf ${path.module}/.build-venv
    EOT
  }
}

resource "aws_lambda_function" "handler" {
  function_name    = "blitzlog-handler"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  role             = aws_iam_role.lambda_role.arn
  timeout          = 180
  dead_letter_config {
    target_arn = aws_sqs_queue.dlq.arn
  }

  environment {
    variables = {
      VPC_ID                    = var.vpc_id
      EC2_SUBNET_ID             = var.ec2_subnet_id
      EC2_SECURITY_GROUP_ID     = aws_security_group.agent_sg.id
      EC2_INSTANCE_PROFILE_NAME = aws_iam_instance_profile.ec2_agent_profile.name
      OPENCODE_MODEL            = var.opencode_model
      S3_LOGS_BUCKET            = aws_s3_bucket.agent_logs.bucket
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
  name              = "/aws/lambda/blitzlog-handler"
  retention_in_days = 14
}
