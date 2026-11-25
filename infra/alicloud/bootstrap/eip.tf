# The elastic IP the Packer build instance gets its public address
# from (issue #125).
#
# The `alicloud-ecs` builder reaches a build instance that lives in a
# VPC over an elastic IP: the SSH communicator has no other host to
# connect to. The builder can get one either by allocating it itself
# or by being handed an existing one through `eip_id` — and it cannot
# allocate one here, because it sends the same `internet_charge_type`
# / `internet_max_bandwidth_out` to both the RunInstances call and
# the AllocateEipAddress call:
#
#   * the EIP call rejects a bandwidth below 1
#     (`IllegalParam.Bandwidth`), and
#   * any non-zero bandwidth makes ECS hand the instance an
#     ECS-managed public IP ("natpublicip"), which Alicloud refuses
#     to replace with an EIP
#     (`EIP_CAN_NOT_ASSOCIATE_WITH_PUBLIC_IP: instance already bind
#     natpublicip, cannot bind eip`).
#
# So the EIP is created here and passed to the builder as `eip_id`.
# The builder associates it with the build instance and drops the
# association when the build ends; the EIP itself survives for the
# next bake.
resource "alicloud_eip_address" "packer" {
  address_name = "blitzlog-packer-build"
  description  = "Public address for the Alicloud Packer build instance (issue #125)."
  payment_type = "PayAsYouGo"

  # An international account can only buy a pay-as-you-go EIP billed
  # by traffic — the `PayByBandwidth` combination fails with
  # `COMMODITY.INVALID_COMPONENT`. Billed by traffic, an idle EIP
  # costs nothing.
  internet_charge_type = "PayByTraffic"

  # The bake pulls the agent image (~450 MiB) and the whisper model
  # (~150 MiB) through this address, so the cap is what the pull
  # speed is. At 5 Mbps that is ~12 minutes of transfer; billed by
  # traffic, a wider cap costs nothing unless it is used.
  bandwidth = "100"
}
