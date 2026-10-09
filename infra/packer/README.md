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

## Release workflow (canonical entry point)

The Packer bake is now driven by the release pipeline
(`.github/workflows/release.yml`, issue #91). There is no separate
Packer workflow to run — `release.yml` handles everything:

```bash
# PR-test: build + push image, bake a dev AMI, post a PR comment.
# Mode is auto-detected from the ref's open-PR status.
gh workflow run release.yml --ref <feature-branch>

# Release: bump source + commit + tag + image + :latest, bake a prod AMI.
gh workflow run release.yml --ref main
```

Mode → Packer var mapping (see `release.yml`'s "Resolve Packer vars from
mode" step):

| `release.yml` mode | Packer `environment` | `agent_image_tag`          | SSM param |
|--------------------|----------------------|----------------------------|-----------|
| `pr-test`          | `dev`                | `v{X.Y.Z}-pr{N}`           | `/blitzlog/dev/agent-ami-id-docker-ubuntu` |
| `release`          | `prod`               | `v{X.Y.Z}`                 | `/blitzlog/prod/agent-ami-id-docker-ubuntu` |

The OIDC role (`blitzlog-packer-build-role`) and SSM/S3/EC2 policy
covering this are in `infra/bootstrap/packer-role.tf`. The workflow
assumes the repo or org variable `vars.AWS_ACCOUNT_ID` is set.

## Variable files

- `variables-docker-ubuntu.pkr.hcl` — variables for `agent-docker-ubuntu.pkr.hcl`
- `docker-images.pkrvars.hcl` — default `agent_image_tag` for local debug
  builds. `release.yml` overrides this via `-var agent_image_tag=...` at
  bake time, so the value in this file is only consulted when a maintainer
  runs `packer build` by hand (see "Local debug build" below).

## Local debug build

The "Release workflow" section above is the canonical entry point. Use a
local `packer build` only when you need to inspect a partially-built AMI
on failure (Packer 1.16.1+ defaults to `--on-error=cleanup`, which
deregisters the AMI and deletes the snapshot the moment a build fails
— see the "Build behavior on failure" section below).

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

For prod, replace `dev` with `prod` and use the prod STT bucket. Add
`-var "agent_image_tag=<tag>"` to override the pkrvars.hcl default.
The post-processor (`05-publish-ami-id.sh`) writes the resulting AMI id
to `/blitzlog/<env>/agent-ami-id-docker-ubuntu`; if you want the build
to skip that step, add `-except=publish-ami-id`.

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

This is the **default** for local debug builds. The `release.yml`
workflow overrides it via `-var agent_image_tag=...` at bake time, so
bumping this file is no longer the canonical way to ship a new image
version — the release workflow's image tag is the source of truth. The
file's value only matters when a maintainer runs `packer build` by hand.

## See also

- [`../../docs/DOCKER.md`](../../docs/DOCKER.md) — operator-facing doc on
  the Docker runtime
- [`../../packages/images/agent/Dockerfile`](../../packages/images/agent/Dockerfile) —
  the container image definition
- [`../../.github/workflows/release.yml`](../../.github/workflows/release.yml) —
  the workflow that now drives the bake
- [`../../lambda/ec2.py`](../../lambda/ec2.py) — `get_agent_ami()` reads
  the SSM parameter this pipeline writes to
