# Docker runtime

Blitzlog agents run on EC2 spot instances that boot from a Packer-built
**minimal** AMI and launch a single Docker container that contains the
entire agent toolchain. The AMI is lean (~600MB), the cold start is fast
(~22-60s), and image iteration is fast (CI rebuild + push in ~5min vs a
~10min Packer rebuild for the older pre-baked AMIs).

## Why Docker, not pre-baked AMIs

An earlier plan baked the opencode CLI, whisper.cpp, model files, and
Python venv directly into the OS at Packer build time. That worked, but
every tool bump required a Packer rebuild (~10min, large AMI). The Docker
runtime keeps the AMI minimal and puts the toolchain in a container image
that's rebuilt and pushed to `ghcr.io/great-wall-connect/blitzlog-agent`
on every change.

The AMI bakes **one** image tarball (`/opt/blitzlog/images/blitzlog-agent.tar.gz`)
plus host extras (`awscli`, `git`, `jq`) and the host-side watchdog
(`/usr/local/bin/watchdog.sh`, `/usr/local/bin/load-image.sh`).

## Image

- **Repository**: `ghcr.io/great-wall-connect/blitzlog-agent`
- **Tags**: semver (`2.1.0`) for prod; `latest` mirrors the most recent push
- **Architecture**: `linux/arm64`
- **Base**: `python:3.12-slim` (debian, glibc) — multi-stage build
- **Contents**:
  - whisper.cpp prebuilt aarch64-linux-gnu CLI (pinned to `v1.7.6`)
  - `pywhispercpp`, `python-multipart`, `imageio-ffmpeg` (Python deps)
  - `opencode-ai` CLI (npm)
  - `@grinev/opencode-telegram-bot` (npm)
  - opencode plugins (baked from `packages/opencode-session-archive/` etc.)
  - `git`, `curl`, `jq`, `libgomp1`, `ca-certificates`
- **No `awscli` / no `boto3`**: the container has zero AWS credentials. The
  host does all SSM/S3 work and passes results via env vars + mounted files.

The whisper model file (`ggml-<stt_model>.bin`) is **not** baked into the
image. It's downloaded by the host at boot from
`s3://${STT_MODELS_BUCKET}/models/`, so the image is model-agnostic and
switching models doesn't require an image rebuild.

## Image version pinning

`infra/packer/docker-images.pkrvars.hcl` is the single source of truth:

```hcl
agent_image_tag = "2.0.0"
```

Both Packer pipelines (AL2023 + Ubuntu) read this. Bump the version:
edit the file, commit, push. CI rebuilds the image and the monthly Packer
cron rebuilds both AMIs with the new tag baked in.

## AMI

Two AMIs per env (`prod` / `dev`), published to:

- `/blitzlog/<env>/agent-ami-id-docker-al2023` (ECS-optimized AL2023 base)
- `/blitzlog/<env>/agent-ami-id-docker-ubuntu` (Canonical Ubuntu 24.04 LTS)

The Lambda reads these SSM parameters via `get_agent_ami()` in
`lambda/handler.py`, with fallback to the corresponding upstream minimal
AMI (the same one Packer uses as `source_ami_filter`).

Build pipelines:
- `infra/packer/agent-docker.pkr.hcl` (AL2023)
- `infra/packer/agent-docker-ubuntu.pkr.hcl` (Ubuntu)

Per-env selection via the `agent_os_family` Terraform variable
(`al2023` | `ubuntu`, default `al2023`).

## Spot interruption handling

The container does NOT poll IMDS (no `--network=host` — that would be a
security regression). The host's watchdog polls IMDS every 5 seconds for
the spot interruption notice and sends SIGTERM via:

```bash
docker stop --time=120 blitzlog-agent
```

The 2-minute window matches AWS's reclamation notice exactly. The
container's `entrypoint.sh` traps SIGTERM and lets `opencode run --agent
build` commit any in-progress work before exiting.

This replaces the previous JS-based `_SPOT_WATCHDOG_PLUGIN_JS` plugin that
polled IMDS from inside the opencode process. The plugin still exists in
`lambda/handler.py` as a deprecated no-op stub for back-compat.

## Host user-data (collapsed)

The current user-data is ~400 lines of install glue. With the Docker
runtime, it shrinks to ~30 lines:

```bash
# SSM reads
# Tailscale install (apt-get on Ubuntu, dnf on AL2023)
# Configure git credentials on host (mounted into container)
# Download whisper model from S3
# Write opencode.json (mounted into container)
# Write opencode plugins (mounted into container)
# Either:
#   - autonomous: /usr/local/bin/watchdog.sh
#   - assisted: docker run -d --name blitzlog-agent --restart unless-stopped ...
```

## Cold start math

| Component | Time |
|---|---|
| AMI boot | ~5-10s |
| systemd + dockerd | ~2s |
| User-data (SSM, Tailscale, git creds, model download, plugins) | ~5-10s |
| `docker load` (~150MB gzipped) | ~2-3s |
| Container startup + git clone + mise + bootstrap | ~5-30s |
| **Total** | **~22-60s** |

## CI

- **`.github/workflows/docker-images.yml`** — builds the single image,
  pushes to ghcr.io on main, Trivy gate, image-size gate (500MB max
  uncompressed).
- **`.github/workflows/packer-build.yml`** (TODO) — rebuilds both AMIs
  monthly and on workflow_dispatch.

## Operational notes

### Switching OS families

1. Trigger the Packer build for the new family via workflow_dispatch.
2. Confirm the new SSM parameter is populated.
3. Update `infra/<env>/terraform.tfvars`: `agent_os_family = "ubuntu"`.
4. `terraform apply` (no Lambda code change — the env var drives the dispatch).

### Rolling out a new image version

1. Edit `infra/packer/docker-images.pkrvars.hcl`: bump `agent_image_tag`.
2. Push. CI builds and pushes the image.
3. Trigger a Packer build for each env (workflow_dispatch). AMIs are
   rebuilt with the new tag baked in.
4. New agent runs use the new image. No Lambda or Terraform change.

### Rollback

- **Image rollback**: push the previous image tag, rebuild AMIs.
- **AMI rollback**: keep old AMIs in the account (Packer tags with
  timestamps; don't deregister old ones until new ones are validated).
- **Per-launch rollback**: `aws ssm delete-parameter --name
  /blitzlog/<env>/agent-ami-id-docker-<family>` falls back to the
  upstream minimal AMI.

## Security: container has no AWS credentials

The container's only network access is to the loopback (whisper-stt-shim
on `127.0.0.1:7878`) and the opencode serve port. It cannot reach IMDS,
SSM, S3, or any AWS API. A compromised agent cannot exfiltrate via AWS
APIs — the credentials simply aren't in the container.

This closes a residual risk flagged in the README
(issue #38, "lock down EC2 egress via nftables when local LLM is
configured"): with Docker, the egress lockdown becomes a much simpler
default Docker network policy on the container.
