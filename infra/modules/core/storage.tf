# Both S3 buckets are owned by the infra/bootstrap stack (one-time setup).
# Each env references them as data sources so prod and dev share the same
# physical buckets without conflicting on `terraform apply`.
#
# The bootstrap stack also configures encryption, versioning, lifecycle,
# and public-access-block on these buckets. Per-env stacks do not mutate
# the buckets — they only read/write keys under them.

data "aws_s3_bucket" "agent_logs" {
  bucket = var.agent_logs_bucket_name
}

data "aws_s3_bucket" "stt_models" {
  bucket = local.stt_models_bucket
}

resource "terraform_data" "stt_model_upload" {
  count = var.upload_stt_model && var.stt_model != "" ? 1 : 0

  input = {
    bucket      = data.aws_s3_bucket.stt_models.bucket
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
