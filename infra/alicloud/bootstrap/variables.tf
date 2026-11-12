variable "alicloud_region" {
  description = "Alicloud region for the bootstrap stack. Defaults to cn-hongkong — the region the blitzlog Alicloud account's OSS buckets live in (the state bucket `gwc-infra-tf-state` and the shared agent-logs / STT-models buckets). AWS analogue: aws_region."
  type        = string
  default     = "cn-hongkong"
}

variable "agent_logs_bucket_name" {
  description = "Name of the shared agent-logs OSS bucket. Must be globally unique across OSS. AWS analogue: infra/aws/bootstrap/variables.tf:agent_logs_bucket_name."
  type        = string
}

variable "stt_models_bucket_name" {
  description = "Name of the shared STT-models OSS bucket. Must be globally unique across OSS."
  type        = string
}

variable "vpc_cidr_block" {
  description = "CIDR block for the shared agent VPC (see network.tf). One VPC per account+region — dev and prod share it, so there is no overlap pressure. It is a variable so a second VPC (e.g. in another region) can be added without editing code."
  type        = string
  default     = "10.0.0.0/16"
}

variable "vswitch_zone_ids" {
  description = "Availability zones to create the agent VSwitches in. One VSwitch per zone; the spot launcher picks one by AZ. The defaults are the zones this account actually has — Alicloud rejects the others with ResourceNotAvailable ('Resource you requested is not available in this region or zone'), and the zone set is per-account, not per-region: this account has cn-hongkong-b/c/d and NO cn-hongkong-a. Verify with 'aliyun vpc DescribeZones --RegionId <region>'."
  type        = list(string)
  default     = ["cn-hongkong-b", "cn-hongkong-c", "cn-hongkong-d"]
}
