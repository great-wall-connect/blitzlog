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

The release workflow (`.github/workflows/release.yml`) is the source of
truth: it tags each image build with `v{X.Y.Z}` (release) or
`v{X.Y.Z}-pr{N}` (pr-test) and bakes a corresponding AMI. The Packer
build receives `agent_image_tag` via `-var` from the workflow.

`infra/aws/packer/docker-images.pkrvars.hcl` is the **default** for local
debug builds only; the release workflow overrides it. Its current value
is a relic from before the release pipeline landed — bumping it is no
longer the canonical way to ship a new image version.

## AMI

One AMI per env (`prod` / `dev`), published to:

- `/blitzlog/<env>/agent-ami-id-docker-ubuntu` (Canonical Ubuntu 26.04 LTS arm64)

The Lambda reads this SSM parameter via `get_agent_ami()` in
`lambda/ec2.py`, with fallback to the Canonical-published Ubuntu 26.04
LTS arm64 minimal AMI (the same one Packer uses as `source_ami_filter`).

Build pipeline:
- `infra/aws/packer/agent-docker-ubuntu.pkr.hcl`

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
# Tailscale install (apt-get)
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

- **`.github/workflows/docker-images.yml`** — builds the single image.
  PRs that touch `packages/images/agent/**` build it but **do not** push
  it; `workflow_dispatch` builds and pushes to ghcr.io under a
  caller-chosen tag. The only gate is an **image-size gate** (2 GB max).
  There is **no Trivy / vulnerability gate** — an earlier version of
  this document claimed one; see issue #164.
- **`.github/workflows/release.yml`** — the release pipeline. Builds +
  pushes the agent image and bakes a corresponding AMI (pr-test → dev
  AMI, release → prod AMI). Replaces what was previously a separate
  `packer-build.yml` (issue #91).

## Operational notes

### Rolling out a new image version

1. Open a PR. CI builds and pushes the agent image with tag
   `v{X}-pr{N}` (no `:latest`).
2. While the PR is open, dispatch `release.yml` against the PR branch
   (`gh workflow run release.yml --ref <branch>`). The workflow bakes
   a dev AMI from the image and publishes its id to
   `/blitzlog/dev/agent-ami-id-docker-ubuntu`. Dev agents pick up the
   new image immediately.
3. To cut the release, dispatch `release.yml` against `main`
   (`gh workflow run release.yml --ref main`). The workflow bumps
   `lambda/version.py`, commits with `[skip ci]`, creates the `v{X.Y.Z}`
   tag, builds + pushes the `v{X.Y.Z}` + `:latest` image, and bakes
   the prod AMI from it, publishing its id to
   `/blitzlog/prod/agent-ami-id-docker-ubuntu`. Prod agents pick up
   the new image on next launch.
4. No Lambda or Terraform change is required.

### Rollback

- **Image rollback**: re-tag or push a previous image tag, then re-dispatch
  `release.yml` to re-bake the AMI from it.
- **AMI rollback**: keep old AMIs in the account (Packer tags with
  timestamps; don't deregister old ones until new ones are validated).
- **Per-launch rollback**: `aws ssm delete-parameter --name
  /blitzlog/<env>/agent-ami-id-docker-<family>` falls back to the
  upstream minimal AMI.

## Security: the container boundary

### No AWS credentials in the container

The container has **no IAM role, no AWS keys and no `aws` CLI**. The
host does all SSM/S3 work and passes results in via env vars and mounted
files. A compromised agent cannot call SSM, S3 or any other AWS API —
the credentials simply are not in the container.

### The container is *not* network-isolated

> **Correction (issue #164).** This document previously claimed *"The
> container's only network access is to the loopback (whisper-stt-shim on
> `127.0.0.1:7878`) and the opencode serve port."* That was **false**.
> The container runs on Docker's default bridge network and the EC2
> security group allows `0.0.0.0/0` (see
> `infra/aws/modules/core/ec2.tf`), so it has unrestricted general
> outbound egress. It **must** be able to reach the LLM provider API,
> `github.com` and `api.telegram.org` — that egress is what makes the
> agent work at all.

Egress lockdown is tracked separately in
[#38](https://github.com/great-wall-connect/blitzlog/issues/38).

### Runtime hardening

The `docker run` invocation that launches the agent carries a fixed set
of hardening flags. They live in **one** place —
`packages/images/agent/container-hardening.env` — which is sourced by the
host-side watchdog (baked into the AMI as
`/usr/local/bin/container-hardening.env`) and by every dev `docker run`
site in `mise.toml`, so the dev path exercises the same posture as
production.

| Flag | Effect |
|---|---|
| `--security-opt=no-new-privileges:true` | Blocks setuid elevation. Third-party npm code runs in-container at runtime (`npx -y @grinev/opencode-telegram-bot@latest`), so this is not theoretical. |
| `--cap-drop=ALL` | Drops Docker's default capability set (`CAP_NET_RAW`, `CAP_SYS_CHROOT`, `CAP_DAC_OVERRIDE`, `CAP_MKNOD`, `CAP_SETUID`/`CAP_SETGID`). The base agent path needs none of them. |
| `--pids-limit=1024` | Bounds the process count so a fork bomb cannot exhaust the host's PIDs. |
| `--init` | Runs `docker-init` as PID 1 so orphaned grandchildren are reaped instead of accumulating as zombies for the life of the run. |

**The one capability exemption.** When — and only when —
`TAILSCALE_AUTH_KEY` is set, the run additionally gets
`--cap-add=NET_ADMIN --cap-add=NET_RAW --device /dev/net/tun`. The
Tailscale daemon needs them to build the WireGuard tunnel. Every other
run gets `--cap-drop=ALL` and nothing else.

> The container-side Tailnet join is the **only** enrolment path in the
> project — the Lambda bootstrap no longer installs or joins Tailscale
> on the host. Before issue #164 the capability flags were passed
> *without* `--device /dev/net/tun`, so the join could never establish a
> tunnel: it logged `WARNING: tailscale up failed` and the local-LLM
> preflight fell back to cloud. The AMI now loads the `tun` module and
> creates `/dev/net/tun` (`infra/aws/packer/scripts-docker-ubuntu/01-system.sh`).
