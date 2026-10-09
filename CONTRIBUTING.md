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

## Releases

Releases are cut by the `.github/workflows/release.yml` workflow,
dispatched manually. There is no automated release-on-merge.

The bump type is auto-detected from the conventional commits in
`git log LAST_TAG..BUILD_REF` by
[cocogitto](https://github.com/cocogitto/cocogitto) (a battle-hardened
conventional-commits parser). The same workflow handles both shapes:

- **PR-test path** (`gh workflow run release.yml --ref <branch-with-open-PR>`):
  builds the `blitzlog-agent` image with tag `v{X}-pr{N}` (no `:latest`),
  posts a PR comment summarizing the bump, and does not touch the source.
  Source is NOT modified by this run.

- **Release path** (`gh workflow run release.yml --ref main`):
  bumps `lambda/version.py` + `version-manifest.json`, commits with
  `[skip ci]`, pushes (with a fallback to a `release/vX.Y.Z-<ts>` branch +
  `gh pr create` if direct push to `main` is rejected by branch protection),
  creates the `vX.Y.Z` tag, pushes the image with `v{X}` + `:latest`, and
  uploads `infra/blitzlog-lambda.zip` to the GitHub Release.

Before any file is touched, the Detect step enforces an alignment invariant:
the version literal in `lambda/version.py` must equal the version in the
last `v*` tag. If they diverge, the workflow fails loudly. A misconfigured
`cog.toml` (`tag_prefix` mismatch with the workflow's tag step, or
`initial_tag` drift from the file) is caught at PR time by
`tests/test_version.py::TestCogAlignment`.

While the project is pre-1.0, `feat:` bumps the minor version (`0.1.0 → 0.2.0`)
and `fix:` bumps the patch (`0.1.0 → 0.1.1`). The 1.0 cut is a deliberate
decision tied to API stability and is not on this pipeline.

## Security issues

**Do not** file public GitHub issues for security vulnerabilities. See [SECURITY.md](SECURITY.md) for the private disclosure process.

## Contact

`admin@greatwallconnect.com`