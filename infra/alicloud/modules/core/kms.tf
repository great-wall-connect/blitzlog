# Analogue of the CMK section of infra/aws/modules/core/iam.tf (the
# AWS-side doesn't provision a CMK explicitly; KMS in AWS uses the
# `aws/ssm` AWS-managed key). On Alicloud we own the CMK and grant
# specific RAM roles the `kms:Decrypt` action explicitly.

resource "alicloud_kms_key" "secrets" {
  description            = "Customer master key for Blitzlog secrets (env: ${var.environment})"
  status                 = "Enabled"
  pending_window_in_days = 7
  # Rotation is operator-driven; we don't enable automatic rotation
  # because secrets have independent lifecycles (a rotation event for
  # one secret does not imply a rotation for another).
}
