# The deploy-time parameters the CI reads.
#
# Analogue of infra/aws/bootstrap/secrets.tf. The bootstrap owns the
# values a deploy needs, so a deploy can run on an account whose
# per-env stacks have never been applied: without them the CI would
# read the per-env stack's OWN parameters — and the CI is the one
# that creates those, so the first deploy could never happen.
#
# The per-env stacks keep their own parameters (the runtime's copy,
# under `blitzlog/<env>/github-app/id` and friends). The AWS side has
# the same two sets: the bootstrap's are the deploy-time values, the
# per-env ones are what the runtime reads.
#
# The leaf names are flat and map 1:1 onto the TF variables the
# per-env stacks declare (`github-app-private-key` ->
# `github_app_private_key`), so the deploy legs render them
# mechanically instead of carrying a mapping table.
#
# The VALUES are operator-owned, like every other OOS parameter in
# this deployment: the apply creates the parameters, and the real
# value goes in afterwards with `aliyun oos UpdateSecretParameter`
# (or `scripts/sync_oos_parameters.sh`). `ignore_changes = [value]`
# keeps terraform from writing a value back — the provider reads an
# OOS parameter back WITHOUT decryption, so its copy of the value is
# always empty, and without this every apply would plan an update
# that writes that empty value back and wipes the secret.

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
    "stt-api-key",
    "stt-model",
    "stt-language",
    "stt-models-bucket-name",
  ]

  deploy_envs = ["dev", "prod"]

  # Per-leaf defaults, used when the operator's map does not carry
  # the leaf. The secrets have no default: an absent value leaves
  # the parameter uncreated, and the deploy leg says so ("the OOS
  # parameter ... is missing or empty") rather than this stack
  # failing — the operator sets the real value out-of-band.
  leaf_defaults = {
    "stt-api-key"            = "placeholder-not-used-by-localhost-shim"
    "stt-model"              = "base.en"
    "stt-language"           = "en"
    "stt-models-bucket-name" = ""
  }

  # The operator-supplied per-env maps. A missing key falls through
  # to `leaf_defaults`, and a leaf with neither is empty.
  env_values = {
    for env in local.deploy_envs : env => {
      for leaf in local.deploy_parameter_leaves :
      leaf => lookup({ dev = var.dev, prod = var.prod }[env], leaf, lookup(local.leaf_defaults, leaf, ""))
    }
  }
}

resource "alicloud_oos_secret_parameter" "deploy" {
  for_each = {
    for combo in setproduct(local.deploy_parameter_leaves, local.deploy_envs) :
    "${combo[1]}/${combo[0]}" => {
      leaf = combo[0]
      env  = combo[1]
    }
    # Skip the leaves the operator supplied no value for — OOS
    # rejects an empty value, and the deploy leg reports the
    # parameter as missing with the command that sets it.
    #
    # `nonsensitive()` strips the sensitivity that would otherwise
    # propagate from `local.env_values` (var.dev and var.prod are
    # sensitive). The condition is just a bool — include or skip —
    # so no secret is exposed by marking it non-sensitive.
    if nonsensitive(
      local.env_values[combo[1]][combo[0]] == null
      || local.env_values[combo[1]][combo[0]] != ""
    )
  }

  secret_parameter_name = "blitzlog/${each.value.env}/${each.value.leaf}"
  value                 = local.env_values[each.value.env][each.value.leaf]
  type                  = "Secret"
  key_id                = alicloud_kms_key.secrets.id
  description           = "Deploy-time value for ${each.value.leaf} (env: ${each.value.env})"

  lifecycle {
    # The value is operator-owned — see the file header.
    ignore_changes = [value]
  }
}
