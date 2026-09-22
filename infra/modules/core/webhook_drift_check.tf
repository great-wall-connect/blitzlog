# Post-apply webhook secret drift check (issue #30).
#
# When the operator sets github_webhook_check_token AND github_webhook_check_repos
# (both must be non-empty), this resource runs the drift-check script after the
# SSM parameter has been written. The script:
#
#   1. Reads the freshly-applied GitHub webhook secret from SSM
#   2. Lists every active webhook on each repo via the GitHub API
#   3. POSTs a signed probe to each one
#   4. Emits a WARNING (non-fatal) when the receiver rejects with 401, naming
#      the webhook and the exact `gh api` invocation to fix it
#
# Drift is a warning, not an apply blocker — legitimate temporary rotations
# (e.g. an incident where someone rotates the secret in tfvars and pastes the
# new value into GitHub moments later) would otherwise force every apply to
# race against a manual UI click. The script itself exits 0 unconditionally.

resource "terraform_data" "webhook_drift_check" {
  count = (
    var.github_webhook_check_token != null
    && length(var.github_webhook_check_repos) > 0
  ) ? 1 : 0

  input = {
    token  = var.github_webhook_check_token
    repos  = var.github_webhook_check_repos
    ssm    = local.ssm_github_webhook_secret_name
    region = var.aws_region
  }

  # depends_on guarantees the SSM parameter is written before we try to read it.
  # Without this, an apply that just changed github_webhook_secret could race
  # the drift check and probe with the OLD value.
  depends_on = [aws_ssm_parameter.github_webhook_secret]

  provisioner "local-exec" {
    # Sensitive vars (the GitHub token) flow through `environment`, NOT the
    # `command` string. Terraform redacts environment values from plan/apply
    # output the same way it redacts sensitive variables, so the token never
    # appears in the apply log. The non-sensitive vars (repo list, SSM path,
    # AWS region) go straight into the command via ${...}.
    environment = merge(
      {
        BLITZLOG_GITHUB_TOKEN   = var.github_webhook_check_token
        BLITZLOG_GITHUB_REPOS   = jsonencode(var.github_webhook_check_repos)
        BLITZLOG_SSM_PARAM_NAME = local.ssm_github_webhook_secret_name
        BLITZLOG_AWS_REGION     = var.aws_region
      },
      var.aws_profile != "" ? { AWS_PROFILE = var.aws_profile } : {},
    )

    command = "bash ${path.module}/scripts/check_webhook_secret.sh"
  }
}
