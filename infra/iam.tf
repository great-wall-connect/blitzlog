resource "aws_iam_role" "lambda_role" {
  name = "blitzlog-lambda-role"

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
  name = "blitzlog-lambda-policy"
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
            "ec2:ResourceTag/Purpose" = "autonomous-agent"
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
          "arn:aws:ssm:*:*:parameter/blitzlog/users",
          "arn:aws:ssm:*:*:parameter/blitzlog/users/*",
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
          "${aws_s3_bucket.agent_logs.arn}/bot-pool-locks/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
        ]
        Resource = aws_s3_bucket.agent_logs.arn
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
        Resource = "${aws_s3_bucket.agent_logs.arn}/user-data/*"
      },
      {
        Effect = "Allow"
        Action = [
          "ssm:PutParameter",
        ]
        Resource = "arn:aws:ssm:*:*:parameter/blitzlog/ephemeral/*"
      },
    ]
  })
}

resource "aws_iam_role" "ec2_agent_role" {
  name = "blitzlog-ec2-agent-role"

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
  name = "blitzlog-ec2-agent-policy"
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
            "ec2:ResourceTag/Purpose" = "autonomous-agent"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
        ]
        Resource = "${aws_s3_bucket.agent_logs.arn}/*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
        ]
        Resource = aws_s3_bucket.agent_logs.arn
      },
      {
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
        ]
        Resource = [
          aws_ssm_parameter.opencode_api_key.arn,
          "arn:aws:ssm:*:*:parameter/blitzlog/ephemeral/*",
        ]
      },
    ]
  })
}

resource "aws_iam_instance_profile" "ec2_agent_profile" {
  name = "blitzlog-ec2-agent-profile"
  role = aws_iam_role.ec2_agent_role.name
}

resource "aws_iam_role_policy_attachment" "ec2_agent_ssm" {
  role       = aws_iam_role.ec2_agent_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_ssm_parameter" "github_app_id" {
  name        = "/blitzlog/github-app/id"
  type        = "String"
  value       = var.github_app_id
  description = "GitHub App ID"
}

resource "aws_ssm_parameter" "github_app_private_key" {
  name        = "/blitzlog/github-app/private-key"
  type        = "SecureString"
  value       = var.github_app_private_key
  description = "GitHub App private key (base64 encoded)"
}

resource "aws_ssm_parameter" "github_app_installation_id" {
  name        = "/blitzlog/github-app/installation-id"
  type        = "String"
  value       = var.github_app_installation_id
  description = "GitHub App installation ID"
}

resource "aws_ssm_parameter" "github_webhook_secret" {
  name        = "/blitzlog/github-webhook/secret"
  type        = "SecureString"
  value       = var.github_webhook_secret
  description = "GitHub webhook HMAC secret"
}

resource "aws_ssm_parameter" "opencode_api_key" {
  name        = "/blitzlog/opencode/api-key"
  type        = "SecureString"
  value       = var.opencode_api_key
  description = "OpenCode inference provider API key"
}