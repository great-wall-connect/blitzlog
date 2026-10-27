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
  user/permissions and the writable-path contract (see
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
- **The container cannot reach the instance metadata service** — the
  watchdog installs a `DOCKER-USER` rule that drops traffic from the
  container's network to `169.254.169.254` (that chain only sees
  forwarded container traffic, so the host's own IMDS access is
  untouched) and passes `AWS_EC2_METADATA_DISABLED=true` to the
  container as a second layer. This is what makes the claim above
  true: without it, the container could assume the EC2 instance role
  and read every SSM parameter that role can read — including the
  GitHub App's private key and the webhook secret (issue #175). The
  block is **fail-closed**: if the rule cannot be installed, the
  watchdog refuses to start the agent and says so loudly. A boundary
  that fails silently cannot be told apart from one that works.
- **The EC2 instance role reads only what the host needs** — the
  per-issue GitHub token it refreshes for the container, the AMI id, the
  bot lock, and the log upload paths. It cannot read the GitHub App's
  private key, the app id, the installation id or the webhook secret:
  those belong to the Lambda, which is the only component that mints
  installation tokens. `tests/test_ec2_iam.py` pins that.
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
- **The capability set is the container's boundary, not the uid** — the
  container runs as uid 0, because `dpkg` needs root to unpack a
  package and to write its own database, and a project's `bootstrap`
  task installs its own build dependencies (issue #174). uid 0 here is
  a *capability-less* root: with `--cap-drop=ALL` the agent cannot
  mount anything (`CAP_SYS_ADMIN`), cannot bypass file permissions
  (`CAP_DAC_OVERRIDE`), cannot make raw sockets (`CAP_NET_RAW`) and
  cannot escalate (`CAP_SETUID`). `CapEff` is asserted by the image's
  smoke test, so a run that loses `--cap-drop=ALL` fails the image
  instead of quietly handing the agent Docker's default capabilities.
- **The container's filesystem is disposable** — the rootfs is writable
  (dpkg needs it), but the writable layer is discarded when the
  container exits, so a tampered binary does not outlive the run. The
  agent's own state lives on tmpfs mounts (`$HOME`, `/tmp`), and the
  only paths shared with the host are `/workspace` (the agent's own
  clone) and `/var/log/blitzlog` — both of which belong to the agent.
- **The GitHub credential store is a read-only mount** — the token is
  owned by the host and mounted read-only, so the agent can use it but
  cannot tamper with the host's copy. `git credential-store` rewrites
  that file on every authenticated operation, so the container ships a
  get-only credential helper instead of using the store. The token is
  also kept out of the container's environment entirely.
- **The `gh` CLI config is host-owned but writable** — the host
  refreshes `hosts.yml` in place on every token rotation, so the agent
  always sees a current token. It is writable because gh 2.x migrates
  its own config on first use and would fail against a read-only mount
  (issue #176); the alternative — a copied config — silently goes
  stale and breaks the token refresh it is meant to demonstrate. It
  costs nothing: the host runs no `gh` of its own, so that directory
  exists only for the container, and the agent already holds the
  token.
- **apt's sandbox is pinned to root in the image** — apt's own privilege
  drop to the `_apt` user needs `CAP_SETUID`/`CAP_SETGID`/`CAP_CHOWN`,
  which `--cap-drop=ALL` removes. The image pins apt's sandbox to root
  (`/etc/apt/apt.conf.d/99-no-sandbox`) rather than re-granting those
  capabilities on every agent run. The pin is what makes a project's
  `bootstrap` task able to install its build dependencies at runtime —
  see `docs/DOCKER.md`.

The container's runtime flags live in one place —
`packages/images/agent/container-hardening.env` — shared by the
host-side watchdog and every dev `docker run` site, and asserted by
`tests/test_container_hardening.py` so they cannot drift apart or
silently lose a flag.

If you propose a change that weakens any of these, expect pushback.

## Past advisories

None published yet.