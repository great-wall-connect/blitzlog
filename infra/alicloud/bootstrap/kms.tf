# The customer master key that encrypts the OOS Parameter Store
# parameters.
#
# It lives in the BOOTSTRAP stack, not in the per-env stack, for two
# reasons:
#
#   * One key for the whole account. The bootstrap creates the
#     parameters the deploy legs read, and the per-env stacks create
#     the ones the runtime reads; both must be under the same key, or
#     every reader needs a grant per key.
#   * The bootstrap can NAME the key, so the CI deploy role's grant
#     is `kms:Decrypt` on `key/<id>` rather than on `key/*`. The
#     per-env stack cannot name it: the role that runs the apply is
#     created here, and a role cannot be granted access to a key that
#     does not exist yet.
#
# KMS bills for *instances*, and a default master key is free of
# charge ("Default master keys are free of charge and are not subject
# to renewal"), so this key costs nothing. KMS Secrets Manager would
# have cost the instance: on KMS 3.0 a secret can only be created
# inside a dedicated KMS instance, which is why the secret store is
# the OOS Parameter Store instead.
#
# ONE KEY PER REGION. Alicloud allows exactly one CMK per region per
# account ("Only one default key (CMK) can be created per region"),
# so this stack ADOPTS the key an account already has rather than
# creating a second one — creating one fails with
# `Rejected.LimitExceeded` ("user create resource limit was
# exceeded"). On an account where the key does not exist yet, the
# apply creates it; on one where it does, the operator imports it:
#
#   terraform import alicloud_kms_key.secrets <key-id>
#
# and the stack that created it stops managing it (`terraform state
# rm`), so the key is not destroyed underneath the parameters it
# encrypts.

resource "alicloud_kms_key" "secrets" {
  description            = "Customer master key for Blitzlog secrets"
  status                 = "Enabled"
  pending_window_in_days = 7
  # The key encrypts every secret in the account. Deletion protection
  # makes an accidental `terraform destroy` fail loudly at the KMS
  # API instead of scheduling the key for deletion — and when the key
  # goes, every parameter it encrypts becomes unreadable, a week
  # later, when nobody is looking at the plan that caused it.
  deletion_protection = "Enabled"
  # Rotation is operator-driven; we don't enable automatic rotation
  # because secrets have independent lifecycles (a rotation event for
  # one secret does not imply a rotation for another).
}

# The per-env stacks need the key's id: they name it on the
# parameters they create and in the RAM grants that decrypt it.
#
# It is published as a plain OOS parameter — the id is not a secret,
# and the deploy legs already read everything else from OOS. The CI
# renders it as a TF_VAR; a local apply copies it from
# `terraform output kms_key_id`.
#
# Publishing it (rather than hard-coding the id in the per-env
# stacks) is what keeps the grant honest: a key that is recreated
# would otherwise leave every reader pointing at a key that no longer
# exists.
resource "alicloud_oos_parameter" "kms_key_id" {
  parameter_name = "blitzlog/shared/kms-key-id"
  value          = alicloud_kms_key.secrets.id
  type           = "String"
  description    = "The id of the CMK that encrypts the OOS parameters"
}
