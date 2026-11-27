# The deploy-time parameters the CI reads.
#
# Analogue of infra/aws/bootstrap/secrets.tf. The bootstrap owns the
# values a deploy needs, so a deploy can run on an account whose
# per-env stacks have never been applied: without them the CI would
# read the per-env stack's OWN parameters — and the CI is the one
# that creates those, so the first deploy could never happen.
#
# These are the SOURCE OF TRUTH for the secrets. The per-env stack
# keeps its own parameters — the ones the runtime reads — and they
# follow this set (infra/alicloud/modules/core/secrets.tf reads them
# with decryption), so a rotation reaches the agents on the next
# apply.
#
# The leaf names are flat and map 1:1 onto the TF variables the
# per-env stacks declare (`github-app-private-key` ->
# `github_app_private_key`), so the deploy legs render them
# mechanically instead of carrying a mapping table.
#
# The VALUES are operator-owned: the apply creates the parameters and
# the real value goes in afterwards with `aliyun oos
# UpdateSecretParameter`. The parameters read their live value back
# (see the data source below), so an apply never writes a value over
# the one the operator set out-of-band — and an update to a parameter
# carries the value with it.

locals {
  # The deploy-time parameters, per env. The leaf names are flat
  # (hyphens-not-underscores) and match the per-env stacks' TF
  # variables one-to-one.
  deploy_parameter_leaves = [
    "github-app-id",
    "github-app-private-key",
    "github-app-installation-id",
    "github-webhook-secret",
    "opencode-api-key",
    "stt-api-url",
    "stt-api-key",
    "stt-model",
    "stt-language",
    "stt-models-bucket-name",
  ]

  deploy_envs = ["dev", "prod"]

  # The value written when the operator has not supplied one. The
  # deploy legs REFUSE a parameter that still holds it — a
  # placeholder that reached the function would be a secret that is
  # not a secret.
  placeholder = "unset"

  # Per-leaf defaults, used when the operator's map does not carry
  # the leaf. The secrets have no default: they get the placeholder,
  # and the operator sets them out-of-band.
  leaf_defaults = {
    "stt-api-url"  = "http://127.0.0.1:7878/v1"
    "stt-api-key"  = "placeholder-not-used-by-localhost-shim"
    "stt-model"    = "base.en"
    "stt-language" = "en"
  }

  # The defaults that depend on the env. The STT models bucket name
  # follows the per-env derivation in
  # infra/alicloud/modules/core/locals.tf (`stt_models_bucket`).
  env_leaf_defaults = {
    dev = {
      "stt-models-bucket-name" = "blitzlog-dev-stt-models"
    }
    prod = {
      "stt-models-bucket-name" = "blitzlog-prod-stt-models"
    }
  }

  # The operator-supplied per-env maps. A missing key falls through
  # to `leaf_defaults`, and a leaf with neither is the placeholder.
  env_values = {
    for env in local.deploy_envs : env => {
      for leaf in local.deploy_parameter_leaves :
      leaf => lookup(
        { dev = var.dev, prod = var.prod }[env],
        leaf,
        lookup(local.leaf_defaults, leaf, lookup(local.env_leaf_defaults[env], leaf, local.placeholder)),
      )
    }
  }

  # The parameters themselves, keyed "<env>/<leaf>" — the same keys
  # the resource's for_each uses.
  deploy_parameters = {
    for combo in setproduct(local.deploy_parameter_leaves, local.deploy_envs) :
    "${combo[1]}/${combo[0]}" => {
      leaf = combo[0]
      env  = combo[1]
      name = "blitzlog/${combo[1]}/${combo[0]}"
    }
  }
}

# The deploy-time parameters, read back WITH decryption.
#
# The provider reads an OOS secret parameter back WITHOUT decryption,
# so terraform's copy of a value is whatever was last APPLIED — empty
# for a parameter the operator filled in after its creation, and
# empty for an imported one. That matters because the provider's
# update ALWAYS sends the value: any change to a parameter (its
# description, say) would write that empty value back and wipe the
# secret.
#
# Reading the live value here is what makes an update safe: it writes
# the value that is actually in OOS.
data "alicloud_oos_secret_parameters" "current" {
  for_each = local.deploy_parameters

  secret_parameter_name = each.value.name
  enable_details        = true
  with_decryption       = true
}

# The parameters that already exist — an account that was set up
# before the deploy-time set existed has them (the operator creates
# them, or copies them from the runtime's parameters). They are
# adopted rather than created: the create would fail with "already
# exists".
import {
  for_each = {
    for key, parameter in local.deploy_parameters : key => parameter
    if length(data.alicloud_oos_secret_parameters.current[key].parameters) > 0
  }
  to = alicloud_oos_secret_parameter.deploy[each.key]
  id = each.value.name
}

resource "alicloud_oos_secret_parameter" "deploy" {
  for_each = local.deploy_parameters

  secret_parameter_name = each.value.name
  # The live value, so that an update (a description change, say)
  # cannot write an empty value over the operator's. `try(...)` falls
  # back to the operator's map for a parameter that does not exist
  # yet — the first apply.
  value       = try(data.alicloud_oos_secret_parameters.current[each.key].parameters[0].value, local.env_values[each.value.env][each.value.leaf])
  type        = "Secret"
  key_id      = alicloud_kms_key.secrets.id
  description = "Deploy-time value for ${each.value.leaf} (env: ${each.value.env})"
}
