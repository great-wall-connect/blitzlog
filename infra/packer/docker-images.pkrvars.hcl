# Single source of truth for the blitzlog-agent container image tag.
# Bump this and push — CI rebuilds the image and the monthly Packer cron
# rebuilds both AMIs with the new tag baked into the AMI.
#
agent_image_tag = "pr-58-final"  # PR #58 — pre-merge candidate for E2E AMI test
