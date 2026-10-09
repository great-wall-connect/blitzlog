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

`infra/packer/docker-images.pkrvars.hcl` is the **default** for local
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
- `infra/packer/agent-docker-ubuntu.pkr.hcl`

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

- **`.github/workflows/docker-images.yml`** — builds the single image,
  pushes to ghcr.io on main, Trivy gate, image-size gate (500MB max
  uncompressed).
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
3. Merge the PR. `release-please` opens a release PR; merging that
   triggers `release.yml` against `main`, which bakes the prod AMI
   from the `v{X}+:latest` image and publishes its id to
   `/blitzlog/prod/agent-ami-id-docker-ubuntu`.
4. No Lambda or Terraform change is required.

### Rollback

- **Image rollback**: re-tag or push a previous image tag, then re-dispatch
  `release.yml` to re-bake the AMI from it.
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
