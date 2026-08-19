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