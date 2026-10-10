# Shared data sources for the bootstrap stack. Both the packer role
# (infra/bootstrap/packer-role.tf) and the deploy role
# (infra/bootstrap/deploy-role.tf) need the AWS account id and the
# admin-SSO role ARN; centralising them here keeps the two trust
# policies in lockstep and avoids "duplicate data source" errors
# when terraform init/validate runs.
#
# The admin-SSO lookup returns a NotFound on a fresh account that
# hasn't yet been federated; `cd infra/bootstrap && terraform init`
# will still succeed (the data source is lazy), but `terraform
# plan` will fail until the SSO instance exists. That's the
# intended signal that the operator hasn't run the AWS-side
# prerequisites yet.

data "aws_caller_identity" "current" {}

data "aws_iam_role" "admin_sso" {
  name = "AWSReservedSSO_AdministratorAccess_5305ac39caba2579"
}
