output "instance_id" {
  description = "EC2 instance id of the scrummaster"
  value       = aws_instance.scrummaster.id
}

output "public_ip" {
  description = "Elastic IP of the scrummaster (ssh ubuntu@<public_ip>)"
  value       = aws_eip.scrummaster.public_ip
}

output "ami_id" {
  description = "AMI the scrummaster was launched from"
  value       = data.aws_ami.scrummaster.id
}

output "zeroclaw_image_tag" {
  description = "Upstream zeroclaw tag the AMI was baked with (the running digest is in /opt/zeroclaw/images/zeroclaw.ref on the host)"
  value       = var.zeroclaw_image_tag
}
