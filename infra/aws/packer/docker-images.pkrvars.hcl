# Default `agent_image_tag` for local Packer debug builds only.
#
# The canonical source of truth is the release workflow
# (.github/workflows/release.yml, issue #91). It pushes the agent image
# with tag `v{X.Y.Z}` (release) or `v{X.Y.Z}-pr{N}` (pr-test) and
# bakes a corresponding AMI, passing `agent_image_tag` to Packer via
# `-var`. The value in this file is only consulted when a maintainer
# runs `packer build` by hand from this directory.
#
agent_image_tag = "pr-58-final"  # PR #58 — pre-merge candidate for E2E AMI test
