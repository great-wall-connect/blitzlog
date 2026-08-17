output "webhook_url" {
  description = "Webhook URL for GitHub configuration"
  value       = "${aws_apigatewayv2_api.webhook.api_endpoint}/"
}

output "lambda_function_name" {
  description = "Name of the Lambda function"
  value       = aws_lambda_function.handler.function_name
}

output "lambda_function_arn" {
  description = "ARN of the Lambda function"
  value       = aws_lambda_function.handler.arn
}

output "ec2_security_group_id" {
  description = "Security group ID for EC2 instances"
  value       = aws_security_group.agent_sg.id
}

output "ec2_instance_profile_name" {
  description = "IAM instance profile name for EC2 agent role"
  value       = aws_iam_instance_profile.ec2_agent_profile.name
}

output "api_gateway_id" {
  description = "API Gateway HTTP API ID"
  value       = aws_apigatewayv2_api.webhook.id
}