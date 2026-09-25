# Changelog

All notable changes to Blitzlog are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once the project reaches `1.0.0`.

## [Unreleased]

### Added
- **Pre-built agent AMI with baked-in Docker image (Amazon Linux 2023 + Ubuntu 24.04).** Two minimal arm64 AMIs per env ship with `dockerd` + awscli + git + jq + the host-side watchdog (`/usr/local/bin/watchdog.sh`) + a gzipped `ghcr.io/great-wall-connect/blitzlog-agent` container image tarball at `/opt/blitzlog/images/`. The container has zero AWS credentials; the host user-data does all SSM/S3 work and passes results via env vars + mounted files. Cold start drops from ~3-5 min (today) to ~22-60s. See `docs/DOCKER.md` for the operator-facing doc. Closes #55.
- **Per-run EC2 Tailscale enrollment for local-LLM transport.** New `tailscale_auth_key` user-pool variable + SSM param at `/blitzlog/users/<login>/local-llm/tailscale-auth-key` (SecureString). When set, the bootstrap installs `tailscale` (AL2023 repo), runs `tailscale up --authkey=... --hostname=blitzlog-agent-<issue>-<ts> --ephemeral --accept-routes=false`, and waits for `BackendState=Running` before the existing `preflight_local_llm()` probe. Ephemeral nodes auto-remove when the spot instance terminates — no manual cleanup. The auth key should be generated at https://login.tailscale.com/admin/settings/keys with `Ephemeral: enabled`, `Reusable: enabled`, `Tags: tag:blitzlog-agent`. The user's Tailnet ACL must grant `tag:blitzlog-agent` access to the LLM endpoint's IP (out of scope for blitzlog to manage). README "Transports → Tailscale" section documents the recommended flags and ACL requirement. This makes the local-LLM feature verifiable end-to-end without provisioning a static subnet router, exit node, or VPN endpoint in the dev VPC.

### Changed
- **Per-user bot pool and local LLM config are now env-independent (refines #50).** The `infra/user-pool/` module is back at its original standalone location (out of `infra/modules/core/user-pool/`) and no longer takes an `environment` variable. Bot tokens and per-user local LLM config live under `/blitzlog/users/<login>/...` (no env prefix), so a user configures their pool once and both prod and dev Lambdas read from the same namespace. The Lambda reads these via a new `BOT_POOL_SSM_PATH = "/blitzlog/users"` constant, distinct from the env-scoped `SSM_PATH` used for env-specific resources (GitHub App creds, ephemeral tokens, STT config, opencode api key). The IAM policies in `infra/modules/core/iam.tf` reflect the new split: bot-pool and local-LLM grants use the literal `/blitzlog/users/*` ARN; ephemeral-token grants remain env-scoped under `${local.ssm_ephemeral_root}/*`.
- **Route agents to user-owned local LLMs (closes #24).** Per-user SSM params (`local_llm_endpoint`, `local_llm_model`, `local_llm_api_key`, `local_llm_endpoint_allow_private_cidrs`, `local_llm_fallback`) under `/blitzlog/users/<login>/local-llm/`. When configured, the bootstrap writes a `local` provider block in `opencode.json` and switches `OPENCODE_MODEL=local/<model-id>`; the cloud provider block is omitted and `OPENCODE_API_KEY` is not exported to the agent's environment (read on demand only for the assisted-mode cloud-fallback path). Endpoint safety guard hard-rejects public IPs and requires `local_llm_endpoint_allow_private_cidrs=true` for RFC1918/ULA/Tailscale CGNAT. Preflight probe runs 10×30s before opencode launches. Autonomous mode aborts on exhaustion; assisted mode prompts the user on Telegram with `[Retry]/[Abort]` (plus `[Use cloud fallback]` when `local_llm_fallback=cloud`) and waits up to 10 minutes for a reply. README documents three always-on transports (Tailscale, WireGuard, AWS Client VPN).
- `opencode serve` now binds to `--hostname 127.0.0.1 --port 4096` (was `--port 4096`).
- Defensive `unset OPENCODE_API_KEY HTTPS_PROXY HTTP_PROXY` at the top of every bootstrap.
- **Bootstrap stack for shared S3 buckets (refines #50).** The `agent_logs` and `stt_models` S3 buckets are now owned by a separate one-time stack at `infra/bootstrap/` instead of being created by every per-env stack. Each env (`infra/prod/`, `infra/dev/`, ...) references the same buckets via `data "aws_s3_bucket"` resources — so a dev `terraform apply` no longer fails with `BucketAlreadyOwnedByYou`, and encryption / versioning / lifecycle / public-access-block configuration is centralized in the bootstrap stack. State migration for the existing prod deploy is documented in the README's "Migrating S3 buckets out of the prod stack into the bootstrap stack" section.
- **Dev/prod segregation (closes #50).** Blitzlog now runs in two isolated environments inside the same AWS account. `infra/prod/` and `infra/dev/` are thin Terraform wrappers that call `infra/modules/core/` with `environment = "prod"` or `"dev"`. Each env has its own state file (`prod/blitzlog.tfstate` vs `dev/blitzlog.tfstate`), its own resource names (`blitzlog-prod-handler` vs `blitzlog-dev-handler`, etc.), its own SSM namespace (`/blitzlog/prod/*` vs `/blitzlog/dev/*`), and its own IAM roles scoped to its own prefix — so the prod Lambda role literally cannot read `/blitzlog/dev/*` and vice versa. Per-user bot pools and per-user local LLM config are explicitly NOT env-namespaced (see the `Changed` entry above). The Lambda reads `BLITZLOG_ENV` from its env vars (set by Terraform from `var.environment`) and templates all env-scoped SSM paths under `/blitzlog/<env>/`. The README's new "Environments (prod / dev)" section documents the workflow.
- Initial public release of Blitzlog — autonomous coding pipeline for GitHub issues.
- Lambda webhook handler with HMAC-SHA256 signature verification and GitHub App authentication.
- Terraform infrastructure: Lambda, API Gateway HTTP API, IAM roles, CloudWatch alarms, SQS DLQ, EC2 security group.
- OpenCode agent bootstrap scripts (autonomous + assisted modes).
- Assisted-mode Telegram bot pool with per-user sender-scoped routing.
- OpenCode session-archive plugin (S3-backed audit trail).
- Spot-watchdog and periodic-autosave plugins (resilience against spot interruption).
- Idle-watchdog plugin for assisted mode (autosave, Telegram ping, idle shutdown).
- Bootstrap-time diagnostic: effective opencode config log + actionable LLM-provider error decoding.
- Per-project bootstrap task via `mise.toml`.
- `resume-aborted-session` OpenCode skill.
- Open-source boilerplate: LICENSE, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY.

### Changed
- Per-user bot pool module (`infra/user-pool/`) uses local Terraform state instead of a shared S3 backend, eliminating S3 state-key collisions across users and the central DynamoDB lock-table requirement. `terraform.tfvars` remains the per-user source of truth; the local `terraform.tfstate` is a regenerable cache of resolved SSM ARNs.

### Notes
- This is the first public release. The git history is intentionally clean — LICENSE is commit 1, the full import is commit 2.
- The internal pre-release history (private repository) is not part of this codebase and contains different commit authors.

## [Unreleased] — Ubuntu 26.04 only

### Changed
- **Bump agent AMI base from Ubuntu 24.04 LTS to Ubuntu 26.04 LTS (resolute).** `infra/packer/agent-docker-ubuntu.pkr.hcl` source-AMI filter switched from `ubuntu-noble-24.04-arm64-server-*` to `ubuntu-resolute-26.04-arm64-server-*`. The 24.04 Packer provisioner scripts updated to use the `resolute` Docker apt repo. `lambda/ec2.py:get_latest_ubuntu_ami()` now reads the `/aws/service/canonical/ubuntu/server/26.04/stable/current/arm64/hvm/ebs-gp3/ami-id` SSM parameter (verified to exist in `ap-east-1`).

### Removed
- **Drop Amazon Linux 2023 agent AMI support.** The AL2023 Packer pipeline (`infra/packer/agent-docker.pkr.hcl`, `infra/packer/scripts-docker/`) is deleted. The `agent_os_family` Terraform variable and the `AGENT_OS_FAMILY` lambda env var are removed. `lambda/ec2.py:get_agent_ami()` is now Ubuntu-only and reads `/blitzlog/<env>/agent-ami-id-docker-ubuntu` directly. Tests in `tests/test_ec2.py` updated to drop the AL2023 dispatcher cases. The `/blitzlog/<env>/agent-ami-id-docker-al2023` SSM parameter and the `591542846629` owner (ECS-optimized AL2023) are no longer referenced. The Tailscale install in the bootstrap now uses apt (no dnf path).

### Fixed
- **Restore `git clone ... /workspace/repo` in the bootstrap.** The lambda user-data generated by `lambda/scripts/assisted.py` and `lambda/scripts/autonomous.py` ran `cd /workspace/repo` after the session-restore step without ever cloning the repo, crashing with `cd: /workspace/repo: No such file or directory` at line 401 of the rendered script. The clone block was lost during the handler.py → focused-modules split (PR #56). Restored as `_clone_repo_script(repo)` helper in `lambda/scripts/_common.py`; called from both modes right after `_configure_git_script()` and before `_session_restore_script()`. Regression test `TestCloneRepoScript` in `tests/test_scripts_common.py`.
- **Use Python f-string `{repo}` instead of Packer template `${{REPO}}` in `_clone_repo_script`.** The bootstrap never goes through Packer template substitution — the lambda renders the script and ships it directly to bash. The first fix used `${{REPO}}` (Packer syntax), which bash parsed as a bad substitution at line 75 (`https://github.com/${{REPO}}}.git: bad substitution`). Switched to f-string substitution so the resolved URL is baked into the bootstrap at lambda render time.

### Changed
- **Bootstrap now `docker run`s the pre-baked agent image instead of running opencode on the host.** The previous bootstrap ran `opencode serve` and `npx @grinev/opencode-telegram-bot@latest start` directly on the EC2 host, but those binaries only exist inside the `blitzlog-agent` Docker image (baked into the AMI by Packer). The new bootstrap loads `/opt/blitzlog/images/blitzlog-agent.tar.gz` into dockerd and runs `docker run -d --name blitzlog-agent` with the right env vars (`MODE`, `ISSUE_NUMBER`, `REPO`, `OPENCODE_*`, `TELEGRAM_*`) and volume mounts (`/opt/whisper-stt/models`, `/root/.config/opencode`, `/workspace`). The container's entrypoint runs `opencode serve` and the Telegram bot internally. The host-side shutdown helper still handles log uploads, session archival to S3, and instance termination (since the container has no AWS credentials).
- **Add `gh` to the `blitzlog-agent` Docker image runtime stage.** The bootstrap used `gh issue view` to fetch the issue title for the Telegram banner. With `gh` now in the container image, the host bootstrap no longer needs it.
- **Drop `git` and `gh` from the Packer AMI `01-system.sh`.** The host no longer needs git (the container clones in `entrypoint.sh`) or gh (the container has it baked in).
- **Delete `_configure_git_script()` and `_install_system_packages_script()` from `_common.py`.** Pure dead-code removal: both were AL2023/Docker-era holdovers. The first wrote host-side `git config` (no longer needed — container handles its own git). The second was an AL2023-only `dnf install gh` block.
- **Remove `_clone_repo_script()` and its calls.** Superseded by Item 7 — the container clones via `entrypoint.sh`, so the host no longer needs to clone.
- **Remove the `gh issue view` line in `assisted.py`.** The issue title is no longer in the Telegram banner (cosmetic regression: banner shows `Issue #2` instead of `Issue #2: <title>`). `gh` is available in the container if future bootstrap code wants to fetch it.

[Unreleased]: https://github.com/great-wall-connect/blitzlog