resource "aws_security_group" "agent_sg" {
  name        = "blitzlog-${var.environment}-agent-sg"
  description = "Security group for autonomous coding agent EC2 instances (env: ${var.environment})"
  vpc_id      = var.vpc_id

  egress = [
    {
      description      = "All outbound"
      from_port        = 0
      to_port          = 0
      protocol         = "-1"
      cidr_blocks      = ["0.0.0.0/0"]
      ipv6_cidr_blocks = []
      prefix_list_ids  = []
      security_groups  = []
      self             = false
    },
  ]

  tags = {
    Name        = "blitzlog-${var.environment}-agent-sg"
    Environment = var.environment
  }
}

resource "aws_security_group_rule" "ssh_from_admin" {
  count             = length(var.ssh_allowed_cidrs) > 0 ? 1 : 0
  type              = "ingress"
  from_port         = 22
  to_port           = 22
  protocol          = "tcp"
  cidr_blocks       = var.ssh_allowed_cidrs
  security_group_id = aws_security_group.agent_sg.id
}
