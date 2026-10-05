# Packer pipelines

This directory contains the Packer pipeline that builds the blitzlog
agent AMI:

| Pipeline | File | OS family | Source AMI | Publishes to |
|---|---|---|---|---|
| Ubuntu agent (Docker runtime) | `agent-docker-ubuntu.pkr.hcl` | Ubuntu 26.04 LTS | Canonical Ubuntu 26.04 arm64 | `/blitzlog/<env>/agent-ami-id-docker-ubuntu` |

The pipeline bakes `ghcr.io/great-wall-connect/blitzlog-agent:<tag>` into
the AMI as `/opt/blitzlog/images/blitzlog-agent.tar.gz`. The host user-data
downloads the image into dockerd's local store at first launch via
`/usr/local/bin/load-image.sh`.

The AMI itself is minimal:

- Canonical Ubuntu 26.04 LTS arm64 base (~300MB) + docker.io + awscli + git + jq + watchdog.sh

Total AMI size: ~600MB.

## Variable files

- `variables-docker-ubuntu.pkr.hcl` — variables for `agent-docker-ubuntu.pkr.hcl`
- `docker-images.pkrvars.hcl` — `agent_image_tag` (single source of truth
  for the container image tag)

## Build commands

```bash
# Ubuntu dev
cd infra/packer
packer init agent-docker-ubuntu.pkr.hcl
packer build \
    -var "environment=dev" \
    -var-file=docker-images.pkrvars.hcl \
    -var "stt_models_bucket=blitzlog-dev-stt-models" \
    agent-docker-ubuntu.pkr.hcl
```

For prod, replace `dev` with `prod` and use the prod STT bucket.

## Build behavior on failure

Packer v1.16.1+ defaults to `--on-error=cleanup`, which automatically
**deregisters the AMI and deletes the snapshot** when a build fails
(including post-processor failures). This is the desired production
behavior — failed builds don't leave dangling AMI/snapshot pairs
incurring storage costs.

For debug flows where you want to inspect the partially-built AMI
on failure, pass `-on-error=abort`:

```bash
packer build -on-error=abort -var "environment=dev" ...
```

The `cleanup` strategy is the default; the build log will show
`Deregistering AMI ami-XXX` and `Deleting snapshot snap-XXX` lines
before the build exits. To republish a manually-built AMI's id to
SSM after a successful build:

```bash
AMI_ID=$(jq -r '.builds[-1].artifact_id' manifest.json | awk -F: '{print $2}')
aws ssm put-parameter \
    --name /blitzlog/<env>/agent-ami-id-docker-ubuntu \
    --type String --value "$AMI_ID" --overwrite --region <region>
```

## Provisioner scripts

Four shell scripts in `scripts-docker-ubuntu/`:

1. `01-system.sh` — Install host extras (awscli, git, jq; also installs
   dockerd from the official Docker repo)
2. `02-systemd.sh` — Enable dockerd; write `/usr/local/bin/load-image.sh`
   and `/usr/local/bin/watchdog.sh`
3. `03-bake-images.sh` — Start dockerd in the build VM, pull the
   `blitzlog-agent` container image, save as a gzipped tarball to
   `/opt/blitzlog/images/`. Also pre-downloads the whisper model so the
   first launch doesn't pay the cost.
4. `04-publish.sh` — Publish the resulting AMI id to
   `/blitzlog/<env>/agent-ami-id-docker-ubuntu`.

## Image version pinning

`docker-images.pkrvars.hcl`:

```hcl
agent_image_tag = "2.0.0"
```

The Packer pipeline reads this. Bump the version → push → CI rebuilds the
image → next monthly Packer cron rebuilds the AMI with the new tag
baked in.

## See also

- [`../../docs/DOCKER.md`](../../docs/DOCKER.md) — operator-facing doc on
  the Docker runtime
- [`../../packages/images/agent/Dockerfile`](../../packages/images/agent/Dockerfile) —
  the container image definition
- [`../../lambda/ec2.py`](../../lambda/ec2.py) — `get_agent_ami()` reads
  the SSM parameter this pipeline writes to
