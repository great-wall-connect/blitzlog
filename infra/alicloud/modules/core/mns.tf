# Analogue of the SQS DLQ at infra/aws/modules/core/alerting.tf:12-15
# and the Cloud Monitor alarm at infra/aws/modules/core/alerting.tf:17-34.
#
# Aliyun MNS (Message Notification Service) is the SQS analogue.
# `alicloud_mns_queue` was deprecated in provider v1.188.0 in favour of
# `alicloud_message_service_queue` (MNS itself was rebranded "Message
# Service"; the API surface and queue ARNs are unchanged). The
# dead-letter behaviour is configured at the API Gateway integration
# (apigateway.tf); the queue itself is just a target ARN.
#
# NOTE: the AWS-side alerting.tf also defines an SNS topic + email
# subscription + a CloudWatch metric alarm. The Alicloud equivalents
# are:
#   * SNS -> alicloud_sms_short_message? No — the AWS side is for
#     alarm notification (alarms call SNS, which emails). On Alicloud
#     the equivalent is a Cloud Monitor alarm + a contact / contact
#     group. Both are out of scope for this PR; document as a
#     follow-up. The MNS DLQ IS in scope because the function
#     dead-letter config is a runtime safety net, not a
#     human-notification path.
#
# Message Service Queue limits `message_retention_period` to 60..604800
# seconds (1 minute to 7 days). 604800 is the maximum; the AWS-side SQS
# uses 14 days (`message_retention_seconds = 1209600` at
# infra/aws/modules/core/alerting.tf:14), which is outside the
# envelope. 7 days is the closest analogue. Documented as a known
# divergence: messages older than 7 days are dropped (vs 14 days on
# AWS).
#
# Issue #126 acceptance criteria: "DLQ analogue present" — covered
# below. Alarm/email analogues are explicitly deferred.

resource "alicloud_message_service_queue" "dlq" {
  queue_name               = "blitzlog-${var.environment}-function-dlq"
  delay_seconds            = 0
  maximum_message_size     = 65536
  message_retention_period = 604800
  visibility_timeout       = 30
  polling_wait_seconds     = 0
}
