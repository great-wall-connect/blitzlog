# Analogue of infra/aws/modules/core/ec2.tf.
#
# Aliyun ECS + VPC + VSwitch + SecurityGroup mirror the AWS EC2 +
# VPC + Subnet + SecurityGroup setup. The agent host is launched from
# the FC function via `AlicloudCompute.run_instances` (same shape as
# the AWS-side `AwsCompute.run_instances`), so this file does NOT
# create the ECS instance itself — only the network plumbing.
#
# Aliyun SGs default-deny inbound just like AWS, so the
# SSH-from-admin rule ports 1-to-1. The `count = length(cidrs) > 0`
# guard at infra/aws/modules/core/ec2.tf:27 is preserved.

resource "alicloud_vpc" "agent" {
  count      = var.vpc_id == "" ? 1 : 0
  vpc_name   = "blitzlog-${var.environment}-vpc"
  cidr_block = "10.0.0.0/16"
}

# One VSwitch per element of var.vswitch_ids. If the operator passes
# a single ID, the count is 1; the AWS path uses a single
# ec2_subnet_id. The list shape future-proofs multi-AZ.
resource "alicloud_vswitch" "agent" {
  count        = length(var.vswitch_ids)
  vswitch_name = "blitzlog-${var.environment}-vswitch-${count.index}"
  cidr_block   = cidrsubnet("10.0.0.0/16", 8, count.index)
  vpc_id       = var.vpc_id
  zone_id      = element(["cn-hangzhou-a", "cn-hangzhou-b", "cn-hangzhou-c", "cn-hangzhou-d", "cn-hangzhou-e", "cn-hangzhou-f", "cn-hangzhou-g", "cn-hangzhou-h", "cn-hangzhou-i"], count.index)
  description  = "VSwitch for the blitzlog agent (env: ${var.environment}, AZ ${count.index})"
}

resource "alicloud_security_group" "agent_sg" {
  name        = "blitzlog-${var.environment}-agent-sg"
  description = "Security group for autonomous coding agent ECS instances (env: ${var.environment})"
  vpc_id      = var.vpc_id

  # Aliyun security_group_rule uses a single resource per direction.
  # The egress rule mirrors infra/aws/modules/core/ec2.tf:6-18.
  # (The alicloud_security_group_rule resource is the analogue of
  # aws_security_group_rule.)
}

resource "alicloud_security_group_rule" "egress_all" {
  type              = "egress"
  ip_protocol       = "all"
  port_range        = "-1/-1"
  security_group_id = alicloud_security_group.agent_sg.id
  cidr_ip           = "0.0.0.0/0"
  description       = "All outbound"
}

resource "alicloud_security_group_rule" "ssh_from_admin" {
  count             = length(var.ssh_allowed_cidrs)
  type              = "ingress"
  ip_protocol       = "tcp"
  port_range        = "22/22"
  security_group_id = alicloud_security_group.agent_sg.id
  cidr_ip           = var.ssh_allowed_cidrs[count.index]
  description       = "SSH from admin CIDRs"
}
