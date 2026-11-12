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
  required_version = ">= 1.10"

  required_providers {
    alicloud = {
      source  = "aliyun/alicloud"
      version = "~> 2.0"
    }
  }

  # The OSS backend. The bucket / key / region are supplied via
  # `-backend-config` (see bootstrap-backend.hcl.example); the
  # credential comes from the ALICLOUD_ACCESS_KEY /
  # ALICLOUD_SECRET_KEY env vars, or from the `profile` below, which
  # reads `~/.aliyun/config.json`.
  #
  # The default region here matches the account's bucket region
  # (cn-hongkong). The backend's region MUST match the bucket's
  # region — an OSS bucket name is globally unique, but its region
  # is not, so a mismatch fails with "Authorization header is
  # invalid" rather than a clear "wrong region" error.
  backend "oss" {
    bucket = ""
    key    = ""
    region = "cn-hongkong"
    # The profile name in ~/.aliyun/config.json (written by
    # `aliyun configure --profile <name>`). Empty by default so the
    # env-var path is used unless the operator opts in.
    profile = ""
  }
}

provider "alicloud" {
  region = var.alicloud_region
}

resource "alicloud_oss_bucket" "agent_logs" {
  bucket = var.agent_logs_bucket_name

  # The provider requires ignore_changes whenever a standalone
  # sub-resource manages the same setting, or the bucket resource
  # reverts the sub-resource's change on every apply.
  #
  # `versioning` is ignored although this bucket has no versioning
  # sub-resource: the attribute is read back from OSS, so enabling
  # versioning here by any means makes the bucket resource plan to
  # disable it — and OSS refuses to un-enable versioning.
  lifecycle {
    ignore_changes = [
      acl,
      server_side_encryption_rule,
      versioning,
    ]
  }
}

# Bucket ACL was deprecated in provider v1.220.0; it now lives on its
# own resource. We keep the same `private` ACL so the existing OSS
# permission model is unchanged.
resource "alicloud_oss_bucket_acl" "agent_logs" {
  bucket = alicloud_oss_bucket.agent_logs.bucket
  acl    = "private"
}

# AES256 (Aliyun-managed key) for the shared bootstrap buckets, to
# match the AWS-side default. Per-env buckets use KMS via the per-env
# CMK in infra/alicloud/modules/core/kms.tf.
resource "alicloud_oss_bucket_server_side_encryption" "agent_logs" {
  bucket        = alicloud_oss_bucket.agent_logs.bucket
  sse_algorithm = "AES256"
}

resource "alicloud_oss_bucket" "stt_models" {
  bucket        = var.stt_models_bucket_name
  force_destroy = false

  # The provider docs (alicloud_oss_bucket, "Using sub-resources with
  # ignore_changes"): "you must add a lifecycle block with
  # ignore_changes for the corresponding attribute on alicloud_oss_bucket
  # ... Without ignore_changes, Terraform may attempt to revert changes
  # made by the sub-resource on every apply."
  #
  # `versioning` matters most here: OSS cannot return a bucket to the
  # unversioned state, so a bucket whose versioning was enabled
  # out-of-band makes the bucket resource plan a change the API
  # refuses to apply.
  lifecycle {
    ignore_changes = [
      acl,
      server_side_encryption_rule,
      versioning,
    ]
  }
}

resource "alicloud_oss_bucket_acl" "stt_models" {
  bucket = alicloud_oss_bucket.stt_models.bucket
  acl    = "private"
}

# Versioning is a one-way door on Alicloud: "Once enabled, versioning
# cannot return to the Disabled state. It can only be suspended."
#
# The provider has no `alicloud_oss_bucket_lifecycle_rule` resource —
# lifecycle is a JSON `policy` attribute on the bucket itself, so the
# bootstrap leaves it unset to match the AWS-side default. Operators
# wanting the 30-day noncurrent expiration the AWS side configures add
# it via the OSS console.
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
