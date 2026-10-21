# Analogue of infra/aws/modules/core/main.tf.
#
# The provider block is the only difference: AWS uses
# `hashicorp/aws`; Alicloud uses `aliyun/alicloud`. Backends
# (S3 / OSS) live in the per-env wrappers, NOT here.
#
# Provider currency: aliyun/alicloud 2.x (released 2026-10-12).
# The PR started on `~> 1.0` (locked at 1.294.0). The v2 upgrade
# guide's breaking changes (api_gateway_instance to_connect_vpc_ip_block,
# cr_repo, cs_kubernetes*, db_instance_classes, instance_types) do
# NOT touch any resource blitzlog uses, so we jump straight to 2.0.
# The `hashicorp/null` and `hashicorp/archive` providers are also
# declared explicitly to avoid implicit-source drift in lock files
# (this module owns the null_resource + data archive_file only).

terraform {
  required_version = ">= 1.10"

  required_providers {
    alicloud = {
      source  = "aliyun/alicloud"
      version = "~> 2.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.0"
    }
  }
}

provider "alicloud" {
  region = var.alicloud_region
}
