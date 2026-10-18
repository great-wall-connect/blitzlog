# Analogue of infra/aws/modules/core/main.tf.
#
# The provider block is the only difference: AWS uses
# `hashicorp/aws`; Alicloud uses `aliyun/alicloud`. Backends
# (S3 / OSS) live in the per-env wrappers, NOT here.

terraform {
  required_version = ">= 1.0"

  required_providers {
    alicloud = {
      source  = "aliyun/alicloud"
      version = "~> 1.0"
    }
  }
}

provider "alicloud" {
  region = var.alicloud_region
}
