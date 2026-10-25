# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| Latest commit on `main` | ✅ |
| Older commits | ❌ |

Blitzlog does not backport security fixes. Always run from `main`.

## Reporting a vulnerability

**Please do not file public GitHub issues for security vulnerabilities.**

Send a private report to **`admin@greatwallconnect.com`** with:

- A clear description of the issue and its impact.
- Reproduction steps (Terraform plan, Lambda payload, EC2 user-data script, etc.).
- The commit SHA or release tag affected.
- Your contact details for follow-up.

We aim to acknowledge within **2 business days** and provide a remediation timeline within **5 business days** of confirmation.

## Scope

In-scope targets:

- **Lambda handler** (`lambda/handler.py`) — webhook signature verification, GitHub App auth, EC2 spot launch.
- **IAM policies** (`infra/iam.tf`) — least-privilege review, SSM `GetParameter` scope, `ec2:TerminateInstances` tag condition.
- **EC2 user-data bootstrap** (synthesised in `lambda/handler.py`) — secret handling, IMDSv2 use, agent configuration.
- **Agent container boundary** — the `docker run` flags, the image's
  user/permissions and the read-only rootfs (see
  `packages/images/agent/container-hardening.env`).
- **OpenCode configuration** written by the bootstrap — provider block, env-var interpolation, plugin installation.
- **Terraform backend** (`infra/main.tf`) — S3 state bucket, DynamoDB lock table (if added later).
- **Shell scripts** under `scripts/` — if any are reintroduced.

Out-of-scope:

- The default OpenCode provider/model — vendor-side issues belong to the provider.
- The OpenCode CLI itself — report upstream.
- AWS infrastructure primitives (IAM, Lambda, EC2) — report to AWS.

## Security considerations baked in

Blitzlog is designed around a few hardening principles — review these before proposing changes that weaken them:

- **GitHub App auth** over PATs (per-repo scope, ~8h token lifetime).
- **HMAC-SHA256** webhook signature verification (`verify_github_signature`).
- **IMDSv2 only** for EC2 instance metadata; no IMDSv1 fallback.
- **SSM `SecureString`** for all credentials, fetched at runtime.
- **Repo-scoped tokens** written to file (not env var), deleted after `git clone`.
- **Tag-conditioned `ec2:TerminateInstances`** — only instances tagged `Purpose=blitzlog` can be terminated by the agent role.
- **Watchdog timeout** (`timeout 7200`) caps any runaway agent.
- **No SSH ingress** by default; opt in via `ssh_allowed_cidrs`.
- **No AWS credentials in the agent container** — the container has no
  IAM role, no AWS keys and no `aws` CLI. The host does all SSM/S3 work
  and passes results in via env vars and mounted files.
- **Container capability drop** — the agent container runs with
  `--cap-drop=ALL` and `--security-opt=no-new-privileges:true`, so
  third-party npm code that executes in-container cannot escalate via
  setuid and the default capability set (`CAP_NET_RAW`,
  `CAP_SYS_CHROOT`, `CAP_DAC_OVERRIDE`, `CAP_MKNOD`, `CAP_SETUID`/
  `CAP_SETGID`) is not available to it. The **one** documented exemption
  is the Tailscale capability set (`NET_ADMIN` + `NET_RAW` +
  `/dev/net/tun`), granted only on runs that actually enrol in a
  Tailnet.
- **Container PID limit and init process** — `--pids-limit=1024` bounds
  the process count so a fork bomb cannot exhaust the host's PIDs, and
  `--init` puts `docker-init` at PID 1 so orphaned grandchildren are
  reaped instead of accumulating as zombies for the life of the run.

The container's runtime flags live in one place —
`packages/images/agent/container-hardening.env` — shared by the
host-side watchdog and every dev `docker run` site, and asserted by
`tests/test_container_hardening.py` so they cannot drift apart or
silently lose a flag.

If you propose a change that weakens any of these, expect pushback.

## Past advisories

None published yet.