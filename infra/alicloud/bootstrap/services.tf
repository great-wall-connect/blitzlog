# Account-level service activations.
#
# Two Alicloud products must be open before the per-env stacks can
# create anything in them:
#
#   * API Gateway — `CreateApiGroup` fails with `ServiceUnOpen`
#     ("Your Gateway service has not been opened.") until the product
#     is opened. The provider exposes the activation as a data
#     source: reading it with `enable = "On"` opens the service.
#   * MNS (now "Simple Message Queue" on the international site) —
#     `CreateQueue` fails with `ServiceNotEnable` ("Service not
#     open") until the product is opened. The provider exposes the
#     subscription as a resource (one per account; it cannot be
#     destroyed).
#
# Both live in the bootstrap stack rather than the per-env stacks
# because they are account-level, one-way (a service cannot be closed
# once opened) and need the account owner to accept the product's
# Terms of Service — so they belong to the operator's one-time setup,
# not to the CI deploy path (which would otherwise need
# `apigateway:OpenApiGatewayService` + `bssopenapi:CreateInstance`
# on the deploy role).
#
# Neither costs anything: the API Gateway's shared VPC instance
# (`api-shared-vpc-002`) and MNS' per-queue pricing are free.
#
# MNS still needs one manual step: the resource below creates the
# *subscription order*, but the service itself is opened from the MNS
# console ("开通") — the provider's order does not flip the service
# flag on the international site, so `CreateQueue` keeps failing with
# `ServiceNotEnable` until the operator clicks through. See
# docs/ALICLOUD_DEPLOY.md §1.

data "alicloud_api_gateway_service" "open" {
  enable = "On"
}

resource "alicloud_message_service_service" "open" {}
