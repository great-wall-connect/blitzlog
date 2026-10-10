# The bucket-name outputs that used to live here were references
# for the per-env stacks (which used shared buckets via data sources).
# With the per-env design (each env has its own bucket pair, names
# supplied via the bootstrap tfvars), the per-env stacks read the
# bucket names straight from SSM via the deploy workflow's
# `get-parameters-by-path` fetch — no bootstrap outputs needed.
#
# If a future consumer needs the per-env bucket ARNs, re-introduce
# them as a map output:
#
#   output "bucket_arns" {
#     value = {
#       for env in ["dev", "prod"] :
#       env => {
#         agent_logs = aws_s3_bucket.agent_logs[env].arn
#         stt_models = aws_s3_bucket.stt_models[env].arn
#       }
#     }
#   }
