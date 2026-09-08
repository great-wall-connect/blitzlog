# Changelog

All notable changes to Blitzlog are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once the project reaches `1.0.0`.

## [Unreleased]

### Added
- **Bootstrap stack for shared S3 buckets (refines #50).** The `agent_logs` and `stt_models` S3 buckets are now owned by a separate one-time stack at `infra/bootstrap/` instead of being created by every per-env stack. Each env (`infra/prod/`, `infra/dev/`, ...) references the same buckets via `data "aws_s3_bucket"` resources — so a dev `terraform apply` no longer fails with `BucketAlreadyOwnedByYou`, and encryption / versioning / lifecycle / public-access-block configuration is centralized in the bootstrap stack. State migration for the existing prod deploy is documented in the README's "Migrating S3 buckets out of the prod stack into the bootstrap stack" section.
- **Dev/prod segregation (closes #50).** Blitzlog now runs in two isolated environments inside the same AWS account. `infra/prod/` and `infra/dev/` are thin Terraform wrappers that call `infra/modules/core/` with `environment = "prod"` or `"dev"`. Each env has its own state file (`prod/blitzlog.tfstate` vs `dev/blitzlog.tfstate`), its own resource names (`blitzlog-prod-handler` vs `blitzlog-dev-handler`, etc.), its own SSM namespace (`/blitzlog/prod/*` vs `/blitzlog/dev/*`), and its own IAM roles scoped to its own prefix — so the prod Lambda role literally cannot read `/blitzlog/dev/*` and vice versa. The user-pool sub-module takes a required `environment` variable and namespaces its SSM parameters accordingly. The Lambda reads `BLITZLOG_ENV` from its env vars (set by Terraform from `var.environment`) and templates all SSM paths under `/blitzlog/<env>/`. The README's new "Environments (prod / dev)" section documents the workflow.
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

[Unreleased]: https://github.com/great-wall-connect/blitzlog