# Analogue of infra/aws/modules/core/apigateway.tf.
#
# Aliyun API Gateway is a single API type (no HTTP-API vs REST
# distinction). The webhook signature verifier in lambda/auth.py is
# provider-agnostic, so the /webhook route payload shape is
# unchanged.
#
# Aliyun API Gateway has no equivalent of `aws_lambda_permission` —
# the API Gateway invokes the FC function via its name + region, and
# the FC function's `internet_access = true` (see
# function_compute.tf) lets API Gateway reach the function without
# extra plumbing. The `aws_apigatewayv2_integration.webhook` →
# `aws_lambda_function.handler.arn` indirection collapses into
# `alicloud_api_gateway_api_config` pointing at the FC function.
#
# function_version = "3.0" below points API Gateway at FCV3 (the
# `fc:2023-03-30` API). The `service_name` argument is only
# required when `function_version = "2.0"` (the v1
# `alicloud_fc_service` model), so we omit it; FCV3 functions live
# flat against the account-level default service.

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

  # Backend wiring: API Gateway forwards `POST /` to the FCV3 function
  # `lambda.handler.lambda_handler`. The FCV3 invoke URL is derived
  # by API Gateway from `region` + `function_name` + the account id;
  # we do NOT pass `function_base_url` (that argument is only for
  # `function_type = "HttpTrigger"`).
  fc_service_config {
    function_name = alicloud_fcv3_function.handler.function_name
    function_type = "FCEvent"
    method        = "POST"
    path          = "/"
    region        = var.alicloud_region
    # `function_version = "3.0"` selects FCV3; `service_name` is
    # intentionally unset because the FCV3 model has no separate
    # service resource (alicloud_fcv3_function is flat).
    function_version = "3.0"
    arn_role         = alicloud_ram_role.lambda_role.arn
    timeout          = 30000
  }
}

resource "alicloud_api_gateway_app" "webhook" {
  name        = "blitzlog-${var.environment}-webhook-app"
  description = "Webhook app for the blitzlog GitHub App (env: ${var.environment})"
}
