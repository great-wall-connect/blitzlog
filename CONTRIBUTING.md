# Contributing to Blitzlog

Thanks for your interest in Blitzlog. **Pin, blitz, merge.** This document explains how to get involved.

## Code of Conduct

By participating, you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md). Please report unacceptable behaviour to `admin@greatwallconnect.com`.

## Reporting bugs

Open a GitHub issue using the **Bug report** template. Include:

- A clear, descriptive title.
- Reproduction steps (Terraform plan output, Lambda log excerpt, etc.).
- Expected vs actual behaviour.
- Blitzlog version (commit SHA) and environment (AWS region, OpenCode provider/model).

## Suggesting features

Open a GitHub issue using the **Feature request** template. Describe the use case first; the implementation can follow.

## Working on the codebase

Before you write code, read **[AGENTS.md](AGENTS.md)**. It defines the conventions the autonomous agent follows and that contributors are expected to match:

- **Branch naming**: `feat/issue-{N}-{slug}` or `fix/issue-{N}-{slug}`.
- **Commit messages**: [Conventional Commits](https://www.conventionalcommits.org/).
- **Tests required** for all new functionality. Run `mise run test` before pushing.
- **Linting required**: `mise run lint` (ruff + black for Python).
- **No breaking changes** without explicit discussion in an issue first.
- **One purpose per PR.** Bundle unrelated changes into separate PRs.

### Local development

```bash
# Clone
git clone <repo-url> blitzlog
cd blitzlog

# Install tools (Python, Terraform, Node 20)
mise install

# Install Python deps
pip install -r requirements.txt

# Install pre-commit hooks (gitleaks, terraform fmt, ruff, black)
pip install pre-commit
pre-commit install

# Run lint and tests
mise run lint
mise run test

# Build the Lambda package locally
mise run build

# Release a stuck bot pool lock (assisted mode)
python scripts/release-bot-lock.py --sender <github_login> --bot <bot_name>
```

### Updating Python dependencies

Direct dependencies live in `requirements-dev.in` and `lambda/requirements.in`. After editing an `.in` file, regenerate the hash-locked `.txt` next to it:

```bash
pip install pip-tools
pip-compile --allow-unsafe --generate-hashes --upgrade lambda/requirements.in
pip-compile --allow-unsafe --generate-hashes --upgrade requirements-dev.in
```

Commit both the `.in` and the regenerated `.txt` together. CI installs from the locked `.txt` files only.

### Updating Terraform providers

Provider versions are pinned in `infra/.terraform.lock.hcl` and `infra/user-pool/.terraform.lock.hcl`. To bump a provider deliberately (e.g. as part of a release):

```bash
cd infra
terraform init -upgrade
cd user-pool
terraform init -upgrade
```

Otherwise `terraform init` should be a no-op — the lockfile is the source of truth.

### Pull request process

1. Branch from `main`: `git checkout -b feat/issue-{N}-{slug}`.
2. Make your changes; commit per Conventional Commits.
3. Push: `git push origin HEAD`.
4. Open a PR against `main`. Fill out the PR template.
5. Wait for CI to pass and at least one review. **Do not merge without approval** — leave for human review.

## Security issues

**Do not** file public GitHub issues for security vulnerabilities. See [SECURITY.md](SECURITY.md) for the private disclosure process.

## Contact

`admin@greatwallconnect.com`
