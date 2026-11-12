# The shared agent network for the blitzlog Alicloud deployment.
#
# Alicloud has no default VPC per region (unlike AWS), so a VPC and at
# least one VSwitch must exist before an ECS instance can be launched.
#
# It lives in the bootstrap rather than the per-env module because dev
# and prod share one account, and Alicloud rejects a second VPC with an
# overlapping CIDR (`OperationDenied.CidrBlockOverlapped`) — so per-env
# VPCs would need per-env CIDRs. The per-env stacks take `vpc_id` +
# `vswitch_ids` as required inputs; the operator copies them out of
# this stack's outputs.

resource "alicloud_vpc" "agent" {
  vpc_name    = "blitzlog-agent-vpc"
  cidr_block  = var.vpc_cidr_block
  description = "Shared VPC for the blitzlog ECS agents (dev + prod)"
}

# One VSwitch per zone: the spot launcher picks one by AZ, so more
# than one gives it somewhere to land when the cheapest zone is full.
resource "alicloud_vswitch" "agent" {
  count        = length(var.vswitch_zone_ids)
  vswitch_name = "blitzlog-agent-vswitch-${count.index}"
  cidr_block   = cidrsubnet(var.vpc_cidr_block, 8, count.index)
  vpc_id       = alicloud_vpc.agent.id
  zone_id      = var.vswitch_zone_ids[count.index]
  description  = "VSwitch for the blitzlog agents (${var.vswitch_zone_ids[count.index]})"
}
