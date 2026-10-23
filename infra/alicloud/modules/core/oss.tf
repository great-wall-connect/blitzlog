# Analogue of infra/aws/modules/core/storage.tf.
#
# Both OSS buckets are owned by the infra/alicloud/bootstrap/ stack
# (one-time setup). Each env references them as data sources so prod
# and dev share the same physical buckets without conflicting on
# `terraform apply`. Mirrors the AWS preamble in
# infra/aws/modules/core/storage.tf:1-7.
#
# NOTE: unlike the AWS path, the bootstrap stack is REQUIRED on
# Alicloud because OSS does not have a separate "bootstrap operator"
# pattern — there is no equivalent of "the AWS S3 buckets were
# created manually and are referenced by data sources". The bootstrap
# also provisions the RAM deploy role for the GHA workflow.
#
# There is NO `terraform_data.stt_model_upload` analogue here. The
# AWS-side upload provisioner uses `aws s3 cp`; the Alicloud
# equivalent would need `ossutil` on the operator's PATH, which
# cannot be assumed. Phase 5 follow-up.

data "alicloud_oss_buckets" "agent_logs" {
  name_regex = var.agent_logs_bucket_name
}

data "alicloud_oss_buckets" "stt_models" {
  name_regex = local.stt_models_bucket
}

# Construct the OSS bucket ARNs from the data-source names.
# Alicloud OSS bucket ARN format:
#   acs:oss:<region>:<account-id>:<bucket-name>
# The data source returns names but not ARNs, so we build them.
locals {
  agent_logs_bucket_name_resolved = data.alicloud_oss_buckets.agent_logs.buckets[0].name
  stt_models_bucket_name_resolved = data.alicloud_oss_buckets.stt_models.buckets[0].name

  agent_logs_bucket_arn = "acs:oss:${var.alicloud_region}:${data.alicloud_account.current.id}:${local.agent_logs_bucket_name_resolved}"
  stt_models_bucket_arn = "acs:oss:${var.alicloud_region}:${data.alicloud_account.current.id}:${local.stt_models_bucket_name_resolved}"
}
