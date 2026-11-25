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
#
# The webhook URL the GitHub App POSTs to is the API Gateway
# group's domain — `https://<group sub_domain><api path>` — and it
# is published as the `webhook_url` output. The stage is selected by
# the `X-Ca-Stage` request header, which GitHub cannot send; the
# default stage is RELEASE, the one `stage_names` deploys to.

resource "alicloud_api_gateway_group" "webhook" {
  # API Gateway names accept letters, digits and underscores only —
  # no hyphens ("GroupName ... 支持汉字、英文字母、数字、英文格式的
  # 下划线"). A hyphen is rejected with `InvalidGroupName`.
  name        = "blitzlog_${var.environment}_webhook_group"
  description = "Stage/group for the blitzlog webhook (env: ${var.environment})"
}

# The Function Compute HTTP trigger the API Gateway forwards to.
#
# It exists because an API Gateway backend of type `FCEvent`
# delivers the request body to the function WITHOUT the headers —
# the GitHub signature (`X-Hub-Signature-256`) never arrives, so the
# signature check could not pass. An HTTP trigger delivers the whole
# request (body + headers) in FC's own event envelope, which
# `lambda/handler.py::_normalize_event` converts to the API Gateway
# proxy shape the handler already speaks.
#
# `authType: anonymous` because the caller is the API Gateway, not
# an end user — the GitHub signature is the authentication.
resource "alicloud_fcv3_trigger" "webhook" {
  trigger_name  = "blitzlog_${var.environment}_webhook"
  trigger_type  = "http"
  function_name = alicloud_fcv3_function.handler.function_name
  qualifier     = "LATEST"
  description   = "HTTP trigger the API Gateway forwards the GitHub webhook to (env: ${var.environment})"
  trigger_config = jsonencode({
    authType = "anonymous"
    methods  = ["POST"]
  })
}

resource "alicloud_api_gateway_api" "webhook" {
  # Letters, digits and underscores only — same rule as the group
  # name ("支持汉字、英文字母、数字、英文格式的下划线").
  name        = "blitzlog_${var.environment}_webhook_api"
  group_id    = alicloud_api_gateway_group.webhook.id
  description = "GitHub webhook -> Function Compute bridge (env: ${var.environment})"
  # ANONYMOUS, not APP: the caller is GitHub, which cannot present
  # API Gateway app credentials. The authentication that matters is
  # the GitHub signature the function verifies itself
  # (lambda/auth.py::verify_github_signature) — the same posture as
  # the AWS side, where the API Gateway has no authorizer either.
  # With `auth_type = "APP"` the gateway rejects every request with
  # "Header `X-Ca-Key` is Required".
  auth_type = "ANONYMOUS"
  # Deploy the API to the RELEASE stage — an API that is not
  # deployed to a stage has no route and the group's domain answers
  # 404 for it.
  stage_names = ["RELEASE"]
  # service_type: "HTTP" = HTTP/HTTPS forward; "FunctionCompute" = FC
  # backend. The function backend is configured in the
  # `fc_service_config` block below. Mirrors the AWS-side
  # apigateway.tf where the API Gateway's integration points at the
  # Lambda function ARN.
  service_type = "FunctionCompute"

  request_config {
    protocol = "HTTPS"
    method   = "POST"
    path     = local.webhook_api_path
    mode     = "PASSTHROUGH"
  }

  # Backend wiring: API Gateway forwards `POST /` to the FCV3 function
  # `lambda.handler.lambda_handler` through the function's HTTP
  # trigger. `function_base_url` is the trigger's public URL (the
  # provider exposes it as `http_trigger[0].url_internet`), so the
  # gateway reaches the trigger the same way an end user would.
  #
  # `function_type = "HttpTrigger"` (not `FCEvent`): the FCEvent
  # backend hands the function the bare body and drops the headers,
  # and it rejects `path`/`method` outright
  # ("InvalidFunctionComputeConfig.path"). The HttpTrigger backend
  # forwards to the trigger's URL with the given path and method —
  # which is what carries the `X-Hub-Signature-256` header
  # through to the function.
  fc_service_config {
    function_name     = alicloud_fcv3_function.handler.function_name
    function_type     = "HttpTrigger"
    function_base_url = alicloud_fcv3_trigger.webhook.http_trigger[0].url_internet
    path              = local.webhook_api_path
    method            = "POST"
    region            = var.alicloud_region
    # `function_version = "3.0"` selects FCV3; `service_name` is
    # intentionally unset because the FCV3 model has no separate
    # service resource (alicloud_fcv3_function is flat).
    function_version = "3.0"
    arn_role         = alicloud_ram_role.lambda_role.arn
    timeout          = 30000
  }
}
