terraform {
  # >= 1.3 for `optional()` in the opencode_model_profiles variable's
  # object type constraint (issue #165). CI pins Terraform 1.16.6.
  required_version = ">= 1.3"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}
