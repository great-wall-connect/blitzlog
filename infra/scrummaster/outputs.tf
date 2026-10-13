output "instance_id" {
  description = "EC2 instance ID of the scrummaster host. Use this with aws cli / SSM Session Manager to reach the host."
  value       = aws_instance.scrummaster.id
}

output "public_ip" {
  description = "Public IPv4 address of the scrummaster host. Empty when the subnet does not auto-assign public IPs (rare for the standard blitzlog public subnets)."
  value       = aws_instance.scrummaster.public_ip
}

output "public_dns" {
  description = "Public DNS name of the scrummaster host (EC2-assigned). Empty when no public IP."
  value       = aws_instance.scrummaster.public_dns
}

output "security_group_id" {
  description = "Security group ID of the scrummaster host. Add to VPC endpoint / peering allowlists when extending the network."
  value       = aws_security_group.scrummaster.id
}

output "iam_role_arn" {
  description = "ARN of the scrummaster IAM role. Reference this when adding cross-account SSM access for the provider / Telegram GitHub App."
  value       = aws_iam_role.scrummaster.arn
}

output "iam_instance_profile_name" {
  description = "Name of the scrummaster instance profile. Already attached to the host; useful for reference / future IAM changes."
  value       = aws_iam_instance_profile.scrummaster.name
}

output "ami_id" {
  description = "AMI id the loc of the host was launched from. Empty string when the data source lookup failed (the operator should set var.ami_id explicitly in that case)."
  value       = aws_instance.scrummaster.ami
}