# Changelog

All notable changes to Blitzlog are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once the project reaches `1.0.0`.

## [Unreleased]

### Added
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