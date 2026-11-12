# The shared agent network for the blitzlog Alicloud deployment.
#
# WHY THE BOOTSTRAP OWNS IT
#
# Alicloud has NO default VPC per region — unlike AWS, where every
# account comes with one. So an Alicloud deployment MUST create a VPC
# and at least one VSwitch before an ECS instance can be launched
# (`RunInstances` requires a `VSwitchId`).
#
# The VPC lives here, in the bootstrap stack, rather than in the
# per-env core module, for two reasons:
#
#   1. **One network per account+region.** dev and prod share one
#      Alicloud account. Alicloud rejects a second VPC whose CIDR
#      overlaps an existing one (`OperationDenied.CidrBlockOverlapped`,
#      HTTP 400), so per-env VPCs would need per-env CIDRs — and
#      would still break the moment an operator forgot to set one.
#      A single shared VPC sidesteps the whole problem.
#
#   2. **It matches the AWS bootstrap's shape.** The AWS bootstrap
#      owns the shared buckets and publishes their names as outputs
#      that the operator copies into the per-env tfvars. The VPC +
#      VSwitches are the same kind of shared, environment-independent
#      resource — the ECS instances in dev and prod run in the same
#      network, separated by their security groups.
#
# The per-env stacks take `vpc_id` + `vswitch_ids` as inputs (see
# infra/alicloud/modules/core/variables.tf). They are REQUIRED — an
# empty default would mean an ECS launch with no VSwitch, which fails
# at the API with an unhelpful message.
#
# Usage: apply this stack once, then copy its outputs into the
# per-env tfvars:
#
#   cd infra/alicloud/bootstrap && terraform output vpc_id
#   cd infra/alicloud/bootstrap && terraform output vswitch_ids

resource "alicloud_vpc" "agent" {
  vpc_name    = "blitzlog-agent-vpc"
  cidr_block  = var.vpc_cidr_block
  description = "Shared VPC for the blitzlog ECS agents (dev + prod)"
}

# One VSwitch per zone. `RunInstances` needs a VSwitch, and the
# spot-launch path picks one by AZ (see
# lambda/provider/alicloud.py::az_subnet_map), so more than one zone
# gives the launcher somewhere to land when the cheapest zone is
# out of capacity.
resource "alicloud_vswitch" "agent" {
  count        = length(var.vswitch_zone_ids)
  vswitch_name = "blitzlog-agent-vswitch-${count.index}"
  # A /24 per VSwitch, carved out of the VPC's block. cidrsubnet
  # handles any base, so a different vpc_cidr_block just works.
  cidr_block  = cidrsubnet(var.vpc_cidr_block, 8, count.index)
  vpc_id      = alicloud_vpc.agent.id
  zone_id     = var.vswitch_zone_ids[count.index]
  description = "VSwitch for the blitzlog agents (${var.vswitch_zone_ids[count.index]})"
}
