resource "aws_apigatewayv2_api" "webhook" {
  name          = "blitzlog-webhook-api"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "webhook" {
  api_id           = aws_apigatewayv2_api.webhook.id
  integration_type = "AWS_PROXY"
  integration_uri  = aws_lambda_function.handler.arn
}

resource "aws_apigatewayv2_route" "webhook" {
  api_id    = aws_apigatewayv2_api.webhook.id
  route_key = "POST /"
  target    = "integrations/${aws_apigatewayv2_integration.webhook.id}"
}

resource "aws_apigatewayv2_deployment" "webhook" {
  api_id = aws_apigatewayv2_api.webhook.id

  triggers = {
    route = md5(jsonencode(aws_apigatewayv2_route.webhook))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [aws_apigatewayv2_route.webhook]
}

resource "aws_apigatewayv2_stage" "webhook" {
  api_id        = aws_apigatewayv2_api.webhook.id
  name          = "$default"
  deployment_id = aws_apigatewayv2_deployment.webhook.id
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.webhook.execution_arn}/*/*"
}