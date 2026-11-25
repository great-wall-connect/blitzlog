# Analogue of the CMK section of infra/aws/modules/core/iam.tf (the
# AWS-side doesn't provision a CMK explicitly; KMS in AWS uses the
# `aws/ssm` AWS-managed key). On Alicloud we own the CMK and it
# encrypts the OOS Parameter Store parameters (secrets.tf).
#
# The CMK is a KMS *default master key* — created without a KMS
# instance. KMS bills for instances (USD 500/month subscription,
# USD 4.50/day pay-as-you-go) and not for default master keys
# ("Default master keys are free of charge and are not subject to
# renewal"), so this key costs nothing. KMS Secrets Manager would
# have cost the instance: on KMS 3.0 a secret can only be created
# inside a dedicated KMS instance, which is why the secret store is
# the OOS Parameter Store instead.

resource "alicloud_kms_key" "secrets" {
  description            = "Customer master key for Blitzlog secrets (env: ${var.environment})"
  status                 = "Enabled"
  pending_window_in_days = 7
  # Rotation is operator-driven; we don't enable automatic rotation
  # because secrets have independent lifecycles (a rotation event for
  # one secret does not imply a rotation for another).
}
