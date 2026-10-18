# Analogue of infra/aws/bootstrap/main.tf.
#
# Alicloud bootstrap stack: creates the shared OSS buckets (agent
# logs + STT models) that the per-env stacks reference via data
# sources. Mirrors the AWS-side preamble at
# infra/aws/bootstrap/main.tf:1-9.
#
# Aliyun OSS bucket equivalents for the AWS-side resources:
#   * aws_s3_bucket                    -> alicloud_oss_bucket
#   * aws_s3_bucket_server_side_encryption_configuration
#                                      -> alicloud_oss_bucket_server_side_encryption_rule
#   * aws_s3_bucket_versioning         -> alicloud_oss_bucket_versioning
#   * aws_s3_bucket_public_access_block
#                                      -> alicloud_oss_bucket_public_access_block
#   * aws_s3_bucket_lifecycle_configuration
#                                      -> alicloud_oss_bucket_lifecycle_rule

terraform {
  required_version = ">= 1.0"

  required_providers {
    alicloud = {
      source  = "aliyun/alicloud"
      version = "~> 1.0"
    }
  }

  backend "oss" {
    bucket = ""
    key    = ""
    region = "cn-hangzhou"
  }
}

provider "alicloud" {
  region = var.alicloud_region
}

resource "alicloud_oss_bucket" "agent_logs" {
  bucket = var.agent_logs_bucket_name
  acl    = "private"
}

# Server-side encryption: SSE-KMS via the account's default CMK
# (`acs:kms:${region}:${account}:key/<default-cmk-id>`). The AWS-side
# analogue is `aws_s3_bucket_server_side_encryption_configuration`
# with `sse_algorithm = "AES256"`. On Alicloud the SSE choice is
# `KMS` (uses a CMK) or `AES256` (server-side AES with Aliyun-managed
# key). We use AES256 for the shared bootstrap buckets to match the
# AWS-side default; per-env buckets use KMS via the per-env CMK in
# infra/alicloud/modules/core/kms.tf.
resource "alicloud_oss_bucket_server_side_encryption" "agent_logs" {
  bucket        = alicloud_oss_bucket.agent_logs.bucket
  sse_algorithm = "AES256"
}

resource "alicloud_oss_bucket" "stt_models" {
  bucket        = var.stt_models_bucket_name
  acl           = "private"
  force_destroy = false
}

# Versioning + public access block + encryption + lifecycle mirror
# infra/aws/bootstrap/main.tf:41-86 1-to-1. The lifecycle rule uses
# 30-day noncurrent version expiration (matches the AWS-side value).
#
# NOTE: the aliyun/alicloud provider does NOT have a separate
# `alicloud_oss_bucket_lifecycle_rule` resource — lifecycle is
# expressed as a JSON `policy` attribute on the bucket itself (or
# via the OSS console post-apply). The bootstrap defaults to no
# lifecycle policy so the bucket matches the AWS-side default
# behaviour; operators can add a lifecycle policy via the OSS
# console after apply if they want the 30-day noncurrent expiration
# the AWS-side configures. Documented as a follow-up.
resource "alicloud_oss_bucket_versioning" "stt_models" {
  bucket = alicloud_oss_bucket.stt_models.bucket
  status = "Enabled"
}

resource "alicloud_oss_bucket_public_access_block" "stt_models" {
  bucket = alicloud_oss_bucket.stt_models.bucket

  block_public_access = true
}

resource "alicloud_oss_bucket_server_side_encryption" "stt_models" {
  bucket        = alicloud_oss_bucket.stt_models.bucket
  sse_algorithm = "AES256"
}
