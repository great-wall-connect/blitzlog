# Analogue of infra/aws/modules/core/apigateway.tf.
#
# Aliyun API Gateway is a single API type (no HTTP-API vs REST
# distinction). The webhook signature verifier in lambda/auth.py is
# provider-agnostic, so the /webhook route payload shape is
# unchanged.
#
# Aliyun API Gateway has no equivalent of `aws_lambda_permission` —
# the API Gateway invokes the FC function via its service name, and
# the FC service's `internet_access = true` (see
# function_compute.tf) lets API Gateway reach the function without
# extra plumbing. The `aws_apigatewayv2_integration.webhook` →
# `aws_lambda_function.handler.arn` indirection collapses into
# `alicloud_api_gateway_api_config` pointing at the FC service.
#
# This file intentionally declares only the resources that the
# aliyun/alicloud provider supports as of 1.0.0. The full webhook
# URL is published via `alicloud_api_gateway_group.webhook.sub_domain`
# once the operator wires up the Function Compute backend in the
# Aliyun console. The `webhook_url` output is exposed for the
# downstream modules.

resource "alicloud_api_gateway_group" "webhook" {
  name        = "blitzlog-${var.environment}-webhook-group"
  description = "Stage/group for the blitzlog webhook (env: ${var.environment})"
}

resource "alicloud_api_gateway_api" "webhook" {
  name        = "blitzlog-${var.environment}-webhook-api"
  group_id    = alicloud_api_gateway_group.webhook.id
  description = "GitHub webhook -> Function Compute bridge (env: ${var.environment})"
  auth_type   = "APP"
  # service_type: "HTTP" = HTTP/HTTPS forward; "FunctionCompute" = FC
  # backend. The function backend is configured in the
  # `fc_service_config` block below. Mirrors the AWS-side
  # apigateway.tf where the API Gateway's integration points at the
  # Lambda function ARN.
  service_type = "FunctionCompute"

  request_config {
    protocol = "HTTPS"
    method   = "POST"
    path     = "/"
    mode     = "PASSTHROUGH"
  }

  # Backend wiring: the API Gateway forwards `POST /` to the FC
  # function `lambda.handler.lambda_handler` in service
  # `blitzlog-${var.environment}-handler`. The `function_base_url` is
  # the function's invoke URL; the provider derives it from
  # `service_name` + `function_name` + `region` when omitted, but we
  # set it explicitly to document the path.
  fc_service_config {
    function_base_url = format(
      "https://%s.%s.fc.aliyuncs.com/proxy/%s/%s",
      alicloud_fc_service.handler.name,
      var.alicloud_region,
      alicloud_fc_service.handler.name,
      alicloud_fc_function.handler.name,
    )
    service_name  = alicloud_fc_service.handler.name
    function_name = alicloud_fc_function.handler.name
    function_type = "FCEvent"
    method        = "POST"
    path          = "/"
    region        = var.alicloud_region
    arn_role      = alicloud_ram_role.lambda_role.arn
    timeout       = 30000
  }
}

resource "alicloud_api_gateway_app" "webhook" {
  name        = "blitzlog-${var.environment}-webhook-app"
  description = "Webhook app for the blitzlog GitHub App (env: ${var.environment})"
}
