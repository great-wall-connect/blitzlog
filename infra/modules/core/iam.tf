resource "aws_iam_role" "lambda_role" {
  name = "blitzlog-${var.environment}-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda_policy" {
  name = "blitzlog-${var.environment}-lambda-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:RunInstances",
          "ec2:DescribeImages",
          "ec2:DescribeInstances",
          "ec2:CreateTags",
          "ec2:DescribeSubnets",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeSpotPriceHistory",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:TerminateInstances",
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/Purpose"     = "autonomous-agent"
            "ec2:ResourceTag/Environment" = var.environment
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
        ]
        Resource = [
          aws_ssm_parameter.github_app_id.arn,
          aws_ssm_parameter.github_app_private_key.arn,
          aws_ssm_parameter.github_app_installation_id.arn,
          aws_ssm_parameter.github_webhook_secret.arn,
          "arn:aws:ssm:*::parameter/aws/service/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect = "Allow"
        Action = [
          "iam:PassRole",
        ]
        Resource = aws_iam_role.ec2_agent_role.arn
      },
      {
        Effect = "Allow"
        Action = [
          "iam:GetInstanceProfile",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
        ]
        Resource = aws_sqs_queue.dlq.arn
      },
      {
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
        ]
        Resource = [
          aws_ssm_parameter.opencode_api_key.arn,
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "ssm:GetParametersByPath",
          "ssm:GetParameter",
        ]
        Resource = [
          "arn:aws:ssm:*:*:parameter/${local.ssm_user_pool_root}",
          "arn:aws:ssm:*:*:parameter/${local.ssm_user_pool_root}/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = [
          "${data.aws_s3_bucket.agent_logs.arn}/bot-pool-locks/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
        ]
        Resource = data.aws_s3_bucket.agent_logs.arn
        Condition = {
          StringLike = {
            "s3:prefix" = "bot-pool-locks/*"
          }
        }
        }, {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
        ]
        Resource = "${data.aws_s3_bucket.agent_logs.arn}/user-data/*"
      },
      {
        Effect = "Allow"
        Action = [
          "ssm:PutParameter",
        ]
        Resource = "arn:aws:ssm:*:*:parameter/${local.ssm_ephemeral_root}/*"
      },
    ]
  })
}

resource "aws_iam_role" "ec2_agent_role" {
  name = "blitzlog-${var.environment}-ec2-agent-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "ec2_agent_policy" {
  name = "blitzlog-${var.environment}-ec2-agent-policy"
  role = aws_iam_role.ec2_agent_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:TerminateInstances",
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/Purpose"     = "autonomous-agent"
            "ec2:ResourceTag/Environment" = var.environment
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
        ]
        Resource = [
          "${data.aws_s3_bucket.agent_logs.arn}/${var.environment}/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
        ]
        Resource = data.aws_s3_bucket.agent_logs.arn
        Condition = {
          StringLike = {
            "s3:prefix" = "${var.environment}/*"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
        ]
        Resource = [
          aws_ssm_parameter.opencode_api_key.arn,
          aws_ssm_parameter.stt_api_url.arn,
          aws_ssm_parameter.stt_api_key.arn,
          aws_ssm_parameter.stt_model.arn,
          aws_ssm_parameter.stt_language.arn,
          aws_ssm_parameter.stt_models_bucket.arn,
          "arn:aws:ssm:*:*:parameter/${local.ssm_ephemeral_root}/*",
          "arn:aws:ssm:*:*:parameter/${local.ssm_user_llm_pattern}",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
        ]
        Resource = "${data.aws_s3_bucket.stt_models.arn}/*"
      },
    ]
  })
}

resource "aws_iam_instance_profile" "ec2_agent_profile" {
  name = "blitzlog-${var.environment}-ec2-agent-profile"
  role = aws_iam_role.ec2_agent_role.name
}

resource "aws_iam_role_policy_attachment" "ec2_agent_ssm" {
  role       = aws_iam_role.ec2_agent_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_ssm_parameter" "github_app_id" {
  name        = local.ssm_github_app_id_name
  type        = "String"
  value       = var.github_app_id
  description = "GitHub App ID (env: ${var.environment})"
}

resource "aws_ssm_parameter" "github_app_private_key" {
  name        = local.ssm_github_app_private_key_name
  type        = "SecureString"
  value       = var.github_app_private_key
  description = "GitHub App private key (base64 encoded, env: ${var.environment})"
}

resource "aws_ssm_parameter" "github_app_installation_id" {
  name        = local.ssm_github_app_installation_name
  type        = "String"
  value       = var.github_app_installation_id
  description = "GitHub App installation ID (env: ${var.environment})"
}

resource "aws_ssm_parameter" "github_webhook_secret" {
  name        = local.ssm_github_webhook_secret_name
  type        = "SecureString"
  value       = var.github_webhook_secret
  description = "GitHub webhook HMAC secret (env: ${var.environment})"
}

resource "aws_ssm_parameter" "opencode_api_key" {
  name        = local.ssm_opencode_api_key_name
  type        = "SecureString"
  value       = var.opencode_api_key
  description = "OpenCode inference provider API key (env: ${var.environment})"
}

resource "aws_ssm_parameter" "stt_api_url" {
  name        = local.ssm_stt_api_url_name
  type        = "String"
  value       = var.stt_api_url
  description = "Whisper-compatible STT endpoint exposed by whisper-stt-shim on the EC2 instance (env: ${var.environment})"
}

resource "aws_ssm_parameter" "stt_api_key" {
  name        = local.ssm_stt_api_key_name
  type        = "SecureString"
  value       = var.stt_api_key
  description = "API key passed through to the STT provider (env: ${var.environment})"
}

resource "aws_ssm_parameter" "stt_model" {
  name        = local.ssm_stt_model_name
  type        = "String"
  value       = var.stt_model
  description = "whisper.cpp model name (e.g. base.en, tiny.en, small.en) (env: ${var.environment})"
}

resource "aws_ssm_parameter" "stt_language" {
  name        = local.ssm_stt_language_name
  type        = "String"
  value       = var.stt_language
  description = "Whisper language hint passed to whisper-cli (empty = auto-detect) (env: ${var.environment})"
}

resource "aws_ssm_parameter" "stt_models_bucket" {
  name        = local.ssm_stt_models_bucket_name
  type        = "String"
  value       = data.aws_s3_bucket.stt_models.bucket
  description = "S3 bucket hosting whisper.cpp model files for EC2 boot-time download (env: ${var.environment})"
}
