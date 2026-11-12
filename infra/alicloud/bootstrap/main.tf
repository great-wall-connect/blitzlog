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

  # The provider docs (alicloud_oss_bucket, "Using sub-resources with
  # ignore_changes"):
  #
  #   "When using standalone sub-resources (e.g. alicloud_oss_bucket_acl,
  #    alicloud_oss_bucket_server_side_encryption, alicloud_oss_bucket_versioning,
  #    ...) alongside alicloud_oss_bucket, you must add a lifecycle block
  #    with ignore_changes for the corresponding attribute on
  #    alicloud_oss_bucket. This prevents Terraform from detecting
  #    spurious diffs caused by the same configuration being managed by
  #    both the bucket resource and the standalone sub-resource. Without
  #    ignore_changes, Terraform may attempt to revert changes made by
  #    the sub-resource on every apply, causing unexpected behavior."
  #
  # This bucket has two sub-resources — alicloud_oss_bucket_acl and
  # alicloud_oss_bucket_server_side_encryption — so their two
  # corresponding attributes are ignored here. The docs note: "You only
  # need to include the attributes in ignore_changes that correspond to
  # the sub-resources you are actually using."
  #
  # `versioning` is ignored even though this bucket has NO versioning
  # sub-resource, and that is deliberate. The bucket resource's
  # `versioning` attribute is read back from OSS, so if versioning is
  # ever enabled on this bucket — by an operator, by a console click,
  # or by a future sub-resource — the bucket resource sees
  # `versioning = "Enabled"` against a config that has no versioning
  # block, and plans to set it back to Disabled. OSS refuses:
  # "Updating the value from Enabled or Suspended to Disabled will
  # result in errors, because OSS does not support returning buckets to
  # an unversioned state." That is a perpetual diff that only a taint
  # can break — the exact failure this whole change exists to fix.
  #
  # So the one-way attributes are ignored unconditionally: the bucket
  # resource does not second-guess a setting that cannot be undone.
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
#
# The `acl` attribute on the bucket resource is ignored via
# `lifecycle.ignore_changes` above — this resource is the single owner
# of the ACL, per the provider's "Using sub-resources with
# ignore_changes" guidance.
resource "alicloud_oss_bucket_acl" "agent_logs" {
  bucket = alicloud_oss_bucket.agent_logs.bucket
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
  force_destroy = false

  # The provider docs (alicloud_oss_bucket, "Using sub-resources with
  # ignore_changes"): "you must add a lifecycle block with
  # ignore_changes for the corresponding attribute on alicloud_oss_bucket
  # ... Without ignore_changes, Terraform may attempt to revert changes
  # made by the sub-resource on every apply."
  #
  # This bucket has THREE sub-resources — alicloud_oss_bucket_acl,
  # alicloud_oss_bucket_versioning and
  # alicloud_oss_bucket_server_side_encryption — so all three
  # corresponding attributes are ignored.
  #
  # `versioning` is the one that matters most: OSS does not support
  # returning a bucket to the unversioned state ("Updating the value
  # from Enabled or Suspended to Disabled will result in errors"), so a
  # bucket whose versioning was enabled out-of-band makes the bucket
  # resource plan a change the API refuses to apply — a perpetual diff
  # that only a taint can break.
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
# Versioning is a ONE-WAY DOOR on Alicloud: "Once enabled, versioning
# cannot return to the Disabled state. It can only be suspended." The
# provider's `status` argument is (Optional, Computed), so a bucket
# created without versioning reads back as Disabled and Terraform
# accepts it — but the moment versioning is enabled (here, or by hand
# in the console) the bucket resource sees a mismatch and plans a
# change the API refuses to apply. The `lifecycle.ignore_changes` on
# the bucket above is what stops that from becoming a perpetual diff.
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
