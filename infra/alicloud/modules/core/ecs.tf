# Analogue of infra/aws/modules/core/ec2.tf.
#
# The agent host is launched from the FC function via
# `AlicloudCompute.run_instances`, so this file creates only the
# security group — not the instance.
#
# The VPC and VSwitches are NOT here: they live in the bootstrap stack
# (infra/alicloud/bootstrap/network.tf). Alicloud has no default VPC
# per region and dev + prod share one account, so a per-env VPC would
# have to dodge the overlapping-CIDR rejection. The per-env stacks take
# `vpc_id` + `vswitch_ids` as required inputs. (The AWS side has no
# `aws_vpc` resource either — AWS has a default VPC.)
#
# Aliyun SGs default-deny inbound just like AWS, so the
# SSH-from-admin rule ports 1-to-1. The `count = length(cidrs) > 0`
# guard at infra/aws/modules/core/ec2.tf:27 is preserved.

resource "alicloud_security_group" "agent_sg" {
  security_group_name = "blitzlog-${var.environment}-agent-sg"
  description         = "Security group for autonomous coding agent ECS instances (env: ${var.environment})"
  vpc_id              = var.vpc_id

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
