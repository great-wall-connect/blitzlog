# ZeroClaw on the scrummaster

`infra/aws/modules/core/scrummaster/` (via scrummaster.tf in the same module) hosts one [ZeroClaw](https://github.com/zeroclaw-labs/zeroclaw)
container per user on a long-lived t4g.nano. It is operator hosting —
blitzlog-agent runs must not modify it (see `AGENTS.md`). ZeroClaw is
**never installed on the host**; the host only runs `dockerd`, the same
Docker-first pattern as the main agent (`docs/DOCKER.md`).

## Image pinning policy

The upstream image `ghcr.io/zeroclaw-labs/zeroclaw:<tag>` is pinned by
digest at Packer build time
(`infra/aws/packer/scripts-scrummaster-docker-ubuntu/01-system.sh`):

1. `docker buildx imagetools inspect ghcr.io/zeroclaw-labs/zeroclaw:${zeroclaw_image_tag}`
   resolves the tag to a `sha256:…` digest.
2. Packer pulls `ghcr.io/zeroclaw-labs/zeroclaw@sha256:…` (`linux/arm64`),
   `docker save`s it to `/opt/zeroclaw/images/zeroclaw.tar.gz`, and
   records the reference in `/opt/zeroclaw/images/zeroclaw.ref`.
3. The digest is substituted for the `@ZEROCLAW_IMAGE_DIGEST@`
   placeholder in `systemd/zeroclaw@.service` before it is installed,
   so the unit never refers to a mutable tag.
4. `zeroclaw-image-pull.service` (oneshot, enabled by `user-data.sh`)
   runs `docker load -i /opt/zeroclaw/images/zeroclaw.tar.gz` on boot.
   It is idempotent; a failure is logged and does not block boot.

If the upstream tag is re-pointed, the running scrummaster is unaffected
until the next Packer rebuild. To upgrade:

```bash
cd infra/aws/packer
packer build -var environment=dev -var zeroclaw_image_tag=<new-tag> scrummaster-zeroclaw-docker-ubuntu.pkr.hcl
```

then set `zeroclaw_image_tag` in `infra/aws/dev/terraform.tfvars` to
the same value (the stack refuses an AMI whose `ZeroclawTag` differs).
A new AMI means a new instance, which `prevent_destroy` blocks — lift it
deliberately in a commit when replacing the host.

`cosign verify` of the image signature before the pull is a planned
follow-up, not part of #135.

## Multi-user Docker pattern

| Identity | Purpose |
|---|---|
| `ubuntu` | Packer default user; SSH bootstrap |
| `zeroclaw-host` | Only non-root member of the `docker` group; the only identity that calls `docker run` / `docker exec` |
| `zeroclaw-<login>` | Per-user (created in #136); owns that user's bind-mount dirs; never calls docker |

`zeroclaw@<login>.service` runs as `User=zeroclaw-host` and passes the
per-user UID/GID into the container (`--user $(id -u zeroclaw-<login>):$(id -g zeroclaw-<login>)`).
Bind mounts:

| Host | Container | Mode |
|---|---|---|
| `/var/lib/zeroclaw/<login>` | `/var/lib/zeroclaw` | rw |
| `/etc/zeroclaw/<login>` | `/etc/zeroclaw` | ro |
| `/var/log/zeroclaw/<login>` | `/var/log/zeroclaw` | rw |

The daemon reads `/etc/zeroclaw/config.toml` inside the container (host
path `/etc/zeroclaw/<login>/config.toml`). The container is
`--read-only` with a 64 MB `/tmp` tmpfs and `no-new-privileges`.

`--env-file /etc/zeroclaw/<login>/env` is read by the docker CLI on the
host, i.e. as `zeroclaw-host`, so #136 must make that file readable by
`zeroclaw-host` (e.g. group-owned by `zeroclaw-host`, mode 0640).

`user-data.sh` only creates the three root-owned (0755) parent dirs.
The per-user subdirs, accounts and service activation land in #136,
`config.toml` in #137. Until then `zeroclaw@*.service` is installed but
not enabled, by design.

## Per-issue plugin install convention

Plugins are installed per issue, starting with #138, via an
`ExecStartPre=` added to `systemd/zeroclaw@.service` (there is none
today). Each issue that adds a plugin:

- adds its own `ExecStartPre=` line (or a drop-in under
  `zeroclaw@.service.d/`), running as `zeroclaw-host`;
- installs into the per-user state dir (`/var/lib/zeroclaw/<login>`),
  never into the read-only image or the host;
- pins the plugin version the same way the image is pinned (no floating
  `latest`).
