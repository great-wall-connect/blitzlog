terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket = ""
    key    = ""
    region = "ap-east-1"
  }
}

# Per-env map of { <env> = <bucket-name> }. The provider region is
# taken from var.dev (per-env; the operator can run dev and prod in
# different regions if they want).
locals {
  agent_logs_bucket_names = {
    dev  = var.dev["agent-logs-bucket-name"]
    prod = var.prod["agent-logs-bucket-name"]
  }
  stt_models_bucket_names = {
    dev  = var.dev["stt-models-bucket-name"]
    prod = var.prod["stt-models-bucket-name"]
  }
  # Distinct list of AWS regions the bootstrap is active in.
  # Used by the IAM role policies (deploy-role.tf, packer-role.tf)
  # to scope ARNs to every region the bootstrap touches — if
  # dev and prod run in the same region this list has one
  # element; if they differ, it has two.
  regions = toset([
    var.dev["aws-region"],
    var.prod["aws-region"],
  ])
}

provider "aws" {
  region = var.dev["aws-region"]
}

# ----------------------------------------------------------------------------
# Per-env S3 buckets
#
# One bucket pair (agent_logs + stt_models) per env, keyed on env.
# Names come from `var.<env>["...-bucket-name"]`; the operator
# supplies them via the bootstrap tfvars. The per-env stacks consume
# the same names from SSM via the deploy workflow's
# get-parameters-by-path fetch — no shared-name coupling between
# the bootstrap and the per-env stacks.
# ----------------------------------------------------------------------------

resource "aws_s3_bucket" "agent_logs" {
  for_each = local.agent_logs_bucket_names
  bucket   = each.value

  tags = {
    Purpose   = "blitzlog-agent-logs-${each.key}"
    ManagedBy = "blitzlog-bootstrap"
    Env       = each.key
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "agent_logs" {
  for_each = aws_s3_bucket.agent_logs
  bucket   = each.key

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket" "stt_models" {
  for_each      = local.stt_models_bucket_names
  bucket        = each.value
  force_destroy = false

  tags = {
    Purpose   = "blitzlog-stt-models-${each.key}"
    ManagedBy = "blitzlog-bootstrap"
    Env       = each.key
  }
}

resource "aws_s3_bucket_versioning" "stt_models" {
  for_each = aws_s3_bucket.stt_models
  bucket   = each.key
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "stt_models" {
  for_each = aws_s3_bucket.stt_models
  bucket   = each.key

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "stt_models" {
  for_each = aws_s3_bucket.stt_models
  bucket   = each.key

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "stt_models" {
  for_each = aws_s3_bucket.stt_models
  bucket   = each.key

  rule {
    id     = "expire-noncurrent"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
    expiration {
      expired_object_delete_marker = true
    }
  }
}
