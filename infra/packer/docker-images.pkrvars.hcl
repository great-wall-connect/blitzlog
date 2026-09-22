# Single source of truth for the blitzlog-agent container image tag.
# Bump this and push — CI rebuilds the image and the monthly Packer cron
# rebuilds both AMIs with the new tag baked in.
agent_image_tag = "2.0.0"
