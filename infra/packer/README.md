# Packer pipelines

This directory contains the Packer pipelines that build blitzlog agent
AMIs. Two pipelines are currently in scope:

| Pipeline | File | OS family | Source AMI | Publishes to |
|---|---|---|---|---|
| AL2023 agent (Docker runtime) | `agent-docker.pkr.hcl` | Amazon Linux 2023 | ECS-optimized AL2023 arm64 | `/blitzlog/<env>/agent-ami-id-docker-al2023` |
| Ubuntu agent (Docker runtime) | `agent-docker-ubuntu.pkr.hcl` | Ubuntu 24.04 LTS | Canonical Ubuntu 24.04 arm64 | `/blitzlog/<env>/agent-ami-id-docker-ubuntu` |

Both pipelines bake the **same** `ghcr.io/great-wall-connect/blitzlog-agent:<tag>`
container image into the AMI as `/opt/blitzlog/images/blitzlog-agent.tar.gz`.
The host user-data downloads the image into dockerd's local store at
first launch via `/usr/local/bin/load-image.sh`.

The AMI itself is minimal:
- AL2023: ECS-optimized AL2023 base (~300MB) + awscli + git + jq + watchdog.sh
- Ubuntu: Canonical Ubuntu 24.04 base (~300MB) + docker.io + awscli + git + jq + watchdog.sh

Total AMI size: ~600MB.

## Variable files

- `variables-docker.pkr.hcl` — variables for `agent-docker.pkr.hcl`
- `variables-docker-ubuntu.pkr.hcl` — variables for `agent-docker-ubuntu.pkr.hcl`
- `docker-images.pkrvars.hcl` — `agent_image_tag` (single source of truth
  for the container image tag)

## Build commands

```bash
# AL2023 dev
cd infra/packer
packer init agent-docker.pkr.hcl
packer build \
    -var "environment=dev" \
    -var-file=docker-images.pkrvars.hcl \
    -var "stt_models_bucket=blitzlog-dev-stt-models" \
    agent-docker.pkr.hcl

# Ubuntu dev
packer build \
    -var "environment=dev" \
    -var-file=docker-images.pkrvars.hcl \
    -var "stt_models_bucket=blitzlog-dev-stt-models" \
    agent-docker-ubuntu.pkr.hcl
```

For prod, replace `dev` with `prod` and use the prod STT bucket.

## Provisioner scripts

Each pipeline has four shell scripts in `scripts-docker/` (or
`scripts-docker-ubuntu/`):

1. `01-system.sh` — Install host extras (awscli, git, jq; on Ubuntu,
   also install dockerd from the official Docker repo)
2. `02-systemd.sh` — Enable dockerd; write `/usr/local/bin/load-image.sh`
   and `/usr/local/bin/watchdog.sh`
3. `03-bake-images.sh` — Start dockerd in the build VM, pull the
   `blitzlog-agent` container image, save as a gzipped tarball to
   `/opt/blitzlog/images/`. Also pre-downloads the whisper model so the
   first launch doesn't pay the cost.
4. `04-publish.sh` — Publish the resulting AMI id to the matching SSM
   parameter (`/blitzlog/<env>/agent-ami-id-docker-<family>`).

## Image version pinning

`docker-images.pkrvars.hcl`:

```hcl
agent_image_tag = "2.0.0"
```

Both Packer pipelines read this. Bump the version → push → CI rebuilds
the image → next monthly Packer cron rebuilds the AMIs with the new tag
baked in.

## See also

- [`../../docs/DOCKER.md`](../../docs/DOCKER.md) — operator-facing doc on
  the Docker runtime
- [`../../packages/images/agent/Dockerfile`](../../packages/images/agent/Dockerfile) —
  the container image definition
- [`../../lambda/handler.py`](../../lambda/handler.py) — `get_agent_ami()` reads
  the SSM parameters these pipelines write to
