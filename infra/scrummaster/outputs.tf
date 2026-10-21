output "instance_id" {
  description = "EC2 instance id of the scrummaster"
  value       = aws_instance.scrummaster.id
}

output "public_ip" {
  description = "Elastic IP of the scrummaster when var.enable_eip is true (ssh ubuntu@<public_ip>), otherwise null"
  value       = var.enable_eip ? aws_eip.scrummaster[0].public_ip : null
}

output "ami_id" {
  description = "AMI the scrummaster was launched from"
  value       = data.aws_ami.scrummaster.id
}

output "zeroclaw_image_tag" {
  description = "Upstream zeroclaw tag the AMI was baked with (the running digest is in /opt/zeroclaw/images/zeroclaw.ref on the host)"
  value       = var.zeroclaw_image_tag
}
