# GitHub Actions OIDC provider. Originally created out-of-band on
# 2026-03-27 with thumbprint 2b18947a6a9fc7764fd8b5fb18a863b0c6dac24f;
# that cert rotated and `aws-actions/configure-aws-credentials` started
# failing AssumeRoleWithWebIdentity in `release.yml`'s "Configure AWS
# credentials (OIDC) for Packer" step. Now managed here so future
# rotations are caught by `terraform plan` instead of breaking CI.
#
# Verify the current cert thumbprint at any time with:
#   echo | openssl s_client -servername token.actions.githubusercontent.com \
#     -connect token.actions.githubusercontent.com:443 2>/dev/null \
#     | openssl x509 -fingerprint -sha1 -noout
#
# The client_id_list MUST be exactly "sts.amazonaws.com" to match the
# OIDC `aud` claim that GitHub Actions issues and the
# `aws-actions/configure-aws-credentials` action requests.
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  thumbprint_list = [
    "06d927fecd0a84aeba28aad1d808139470fe95c3",
  ]
}
