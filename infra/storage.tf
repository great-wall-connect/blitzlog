resource "aws_s3_bucket" "agent_logs" {
  bucket = var.agent_logs_bucket_name
}

resource "aws_s3_bucket_server_side_encryption_configuration" "agent_logs" {
  bucket = aws_s3_bucket.agent_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket" "stt_models" {
  bucket        = var.stt_models_bucket_name
  force_destroy = false
}

resource "aws_s3_bucket_versioning" "stt_models" {
  bucket = aws_s3_bucket.stt_models.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "stt_models" {
  bucket = aws_s3_bucket.stt_models.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "stt_models" {
  bucket = aws_s3_bucket.stt_models.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "stt_models" {
  bucket = aws_s3_bucket.stt_models.id

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

resource "terraform_data" "stt_model_upload" {
  count = var.upload_stt_model && var.stt_model != "" ? 1 : 0

  input = {
    bucket      = aws_s3_bucket.stt_models.bucket
    model       = var.stt_model
    source_base = var.stt_model_source_url
    region      = var.aws_region
  }

  provisioner "local-exec" {
    environment = var.aws_profile != "" ? { AWS_PROFILE = var.aws_profile } : {}

    command = <<-EOT
      set -eu
      BUCKET="${self.input.bucket}"
      KEY="models/ggml-${self.input.model}.bin"
      REGION="${self.input.region}"

      # Fail fast with diagnostics if AWS credentials aren't accessible.
      if ! err=$(aws sts get-caller-identity --region "$REGION" 2>&1); then
        echo "ERROR: AWS credentials not accessible in this local-exec subprocess." >&2
        echo "" >&2
        echo "  Detected environment:" >&2
        echo "    AWS_PROFILE:     $${AWS_PROFILE:-<unset>}" >&2
        echo "    AWS_REGION:      $${AWS_REGION:-<unset>}" >&2
        echo "    AWS_ACCESS_KEY_ID: $${AWS_ACCESS_KEY_ID:+<set>}$${AWS_ACCESS_KEY_ID:-<unset>}" >&2
        echo "    PATH: $PATH" >&2
        echo "" >&2
        echo "  aws sts get-caller-identity output:" >&2
        echo "    $err" >&2
        echo "" >&2
        echo "  Common causes for credential_process setups:" >&2
        echo "    - The credential_process command failed (jq/sh PATH, missing cache files, etc.)" >&2
        echo "    - Subprocess PATH differs from your interactive shell PATH" >&2
        echo "    - Expired or missing login cache (~/.aws/login/cache/*.json)" >&2
        echo "" >&2
        echo "  Test outside Terraform to isolate:" >&2
        echo "    AWS_PROFILE=terraform aws sts get-caller-identity --region $REGION" >&2
        echo "    AWS_PROFILE=terraform aws configure get credential_process --profile terraform" >&2
        exit 1
      fi

      if aws s3api head-object --bucket "$BUCKET" --key "$KEY" --region "$REGION" >/dev/null 2>&1; then
        echo "Model already in s3://$BUCKET/$KEY, skipping upload"
        exit 0
      fi

      TMP=$(mktemp /tmp/whisper-model.XXXXXX.bin)
      trap "rm -f $TMP" EXIT
      echo "Downloading ${self.input.source_base}/ggml-${self.input.model}.bin ..."
      curl -fsSL "${self.input.source_base}/ggml-${self.input.model}.bin" -o "$TMP"

      echo "Uploading to s3://$BUCKET/$KEY ..."
      aws s3 cp "$TMP" "s3://$BUCKET/$KEY" --region "$REGION"
      echo "Done."
    EOT
  }
}