# Agent Conventions

## Branch Naming

Format: `feat/issue-{N}-{slug}`

Where:
- `N` is the GitHub issue number
- `slug` is a short lowercase hyphenated description (max 50 chars)

Examples:
- `feat/issue-42-user-authentication`
- `feat/issue-137-fix-caching-bug`

## Commit Conventions

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>[optional scope]: <description>

[optional body]

[optional footer(s)]
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`

Examples:
- `feat: add user authentication via OAuth`
- `fix: handle null pointer in cache lookup`
- `docs: update API documentation`

## Trigger Labels

The labels `autonomous` and `assisted` cause the blitzlog webhook Lambda to launch an EC2 agent for the labeled issue. **Agents must never apply these labels** — neither when opening new issues, nor when editing existing ones. Use any other appropriate label (`bug`, `enhancement`, `dependencies`, etc.).

Why the rule exists:

- Applying `autonomous` or `assisted` to a blitzlog repo issue spawns an EC2 instance that runs the very agent that applied the label — i.e. the agent triggers itself. That is almost never what the agent wants (it's working on this repo, not asking for a separate agent to work on it), and it doubles the spot spend for one issue.
- Two agents labelling the same issue, or one agent labelling twice, spawns duplicate EC2 instances on the same issue — the bug class blitzlog is meant to prevent for *its users* (issue #87's context).
- An agent that genuinely wants blitzlog to pick the issue up should commit and push the work directly to a branch and open a PR. The maintainer can then add the trigger label themselves if they want a follow-up autonomous run.

Filing or batching issues in this repo is, by definition, an agent task — so this rule applies to every issue created by an automated agent on this repository.

### Model labels are a privilege, not a preference

The labels `model:<value>` select the inference model for a single task (issue #165). **Agents must never apply them either** — same rule, same reasoning.

A model label is not cosmetic. It decides which provider the run's credential authenticates to, and therefore which endpoint receives the key and which catalogue the model is drawn from. Picking that for someone else is a privilege decision, and the blast radius is a credential.

The rules that make it safe for humans are the same ones that make it unsafe for an agent to do it silently:

- A **raw** `model:<provider>/<model>` label requires a per-user credential and is restricted to the operator's approved-provider list. The org's shared credential is never reachable from a raw label.
- An **alias** (`model:<alias>`, registered by the operator in `opencode_model_profiles`) may run on the org credential, because the operator chose what the alias points at.
- A credential is only ever used with a model whose provider matches it. A mismatch fails the run fast — the key is never sent to the wrong provider.

So: leave the choice to the human who owns the credential. If a task genuinely needs a different model, say so in the issue body and let the maintainer apply the label.

## Agent Commit Attribution (Audit Trail)

A blitzlog agent's commits must be attributed to the human who triggered the run, **not** to the EC2 default (`root@<hostname>`), the worker hostname, or any fictional runner identity. "A bot cannot be audited. The owner — the person who chose to trigger the event — is responsible for the task."

The plumbing for this is in three places (issue #119):

- **EC2 runtime** (assisted and autonomous modes). The Lambda
  forwards the issue's `sender_login` / `sender_id` into the
  bootstrap (`scripts/autonomous.py`, `scripts/assisted.py`,
  `_common.py:script_header`); the bootstrap `git config
  --global user.{name,email}` on the host and exports
  `GIT_USER_NAME` / `GIT_USER_EMAIL` env vars. The
  Packer-baked watchdog (`infra/aws/packer/scripts-docker-ubuntu/
  watchdog.sh`) forwards them via `-e` flags into the container.
  The container's `entrypoint.sh` (lines 88-91) runs the matching
  `git config --global` so any commit the agent makes inside the
  container is attributed to the bot-pool `owner_login` (assisted)
  or the GitHub user who applied the `autonomous` label.
- **GHA worker** (`release.yml` Bump step,
  `terraform-apply-dev.yml` Validate step). The workflow reads
  `vars.GIT_AUTHOR_NAME` / `vars.GIT_AUTHOR_EMAIL` and fails loud
  (`FATAL: vars.GIT_AUTHOR_NAME and vars.GIT_AUTHOR_EMAIL must be
  set at the repo level`) when either is missing. The Bump step
  uses these vars for `git config user.{name,email}` in the
  chore(release) commit. To preserve the historical `release-bot`
  carve-out for `[skip ci]` bumps, the operator sets the two vars
  to `release-bot` / `release-bot@users.noreply.github.com`.

**Operational rule**: if you (or the automation that opened this PR) land a commit via the agent or via the GHA workflow, the commit author must be the human who chose to trigger the event — never `root@<hostname>`, never `blitzlog-agent`, never the runner hostname. Do not edit the bootstrap or the workflows to remove the identity wiring; if the operator-supplied vars are missing, surface that as a setup error to the operator instead of falling back to a fictional identity.

## Implementation Standards

1. **Read the issue** — Fetch full issue body and comments from GitHub API before starting
2. **Single purpose** — Each issue = one feature or fix; no bundled changes
3. **Tests required** — All new code must have tests (unit, integration, or e2e as appropriate)
4. **No breaking changes** — Unless explicitly requested in the issue
5. **Preserve coding style** — Match existing code conventions in the target repo
6. **Missing build tools are project-side, not agent-side** — If a build fails because a toolchain component (C compiler, linker, system headers, etc.) is missing, **stop and report**; do not run `apt install`, `dnf install`, `brew install`, or equivalent. The project owns its build dependencies via the `bootstrap` task convention documented in [`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md); if the project lacks a `bootstrap` task, the fix is a new commit to the target repo, not a self-healing install on the worker instance.

> The former rule 7 ("`infra/scrummaster/*` is operator hosting — blitzlog-agent runs must not modify anything under it") was **removed** when the Scrummaster moved from a standalone stack at `infra/scrummaster/` into `infra/aws/modules/core/scrummaster.tf` (epic #134, skeleton #135). The Scrummaster is part of the core deployment surface — CI owns it, and blitzlog agents maintain it like any other Terraform. Note that its instance is a pet with `prevent_destroy = true` and `disable_api_termination = true`; flipping either guard is a deliberate operator act, not a routine edit.

The `### Carve-out for infra/scrummaster/` subsection under **Trigger Labels** above is unaffected — that rule governs who may apply the `autonomous` / `assisted` labels, not who may edit the files.

## Testing Commands

Run before pushing:

| Stack | Command |
|-------|---------|
| Rust | `cargo check && cargo clippy && cargo test` |
| Node.js/TypeScript | `npm run build && npm run lint && npm test` |
| Python | `pytest` (plus linter check) |

If the target repo has no existing test infrastructure, add minimal smoke tests.

## Linting

| Stack | Command |
|-------|---------|
| Rust | `cargo fmt --check && cargo clippy -- -D warnings` |
| Node.js/TypeScript | `npm run lint` |
| Python | `ruff check . && black --check .` |

## Pre-Push Checklist

**Always run every CI step locally and confirm it passes before committing and pushing — not just on PR.** CI rejects the push with a red check only after you've already burned the round-trip; running locally first is cheaper and faster.

For blitzlog, the Lint + test (Python) job runs three steps in this order; run all three:

```bash
python -m ruff check .
python -m black --check .
pytest
```

If any step fails, fix it locally, re-run the full sequence (a fix in one file can fail the linter in another), and only then commit and push. Do not push on the assumption that "CI will tell me what's wrong" — that wastes a CI cycle and slows you down.

For other stacks, mirror this with the equivalent "build + lint + test" sequence from the Testing Commands / Linting tables above.

## Agent Commit Attribution (Audit Trail, issue #119)

The agent never commits as a fictional identity. Every commit — EC2 host (assisted + autonomous) and GHA worker (release Bump step) — is attributed to the operator of this blitzlog deployment via two SSM parameters that the `infra/user-pool/` module manages:

- `/blitzlog/users/<owner_login>/git-author/name`
- `/blitzlog/users/<owner_login>/git-author/email`

The values default to `var.owner_login` and `"<owner_login>@users.noreply.github.com"`; override either in `infra/user-pool/terraform.tfvars` when you need a different attribution (e.g. the canonical `release-bot` carve-out for `[skip ci]` bumps).

Hard rules:

1. **Never `root@<hostname>`.** That happens when the user-pool stack hasn't been applied yet (no SSM params to read); the bootstrap's `2>/dev/null || echo ""` fallback is behaviour-preserving, but the operator must run `terraform apply` in `infra/user-pool/` so the params exist.
2. **Never a GHA repo variable like `vars.GIT_AUTHOR_NAME`.** The release Bump step reads from SSM, not from a repo variable. The only GHA-side knob is `vars.OWNER_LOGIN` (the pool whose git-author params to fetch).
3. **Never per-sender identity in the bootstrap.** The audit-trail identity is owned by the operator, not by the webhook `sender_login`. Assisted commits and autonomous commits get the same identity, sourced from the same SSM parameters.
4. **Surface setup errors loudly.** When the Bump step finds a missing `OWNER_LOGIN` or missing SSM param, it aborts with `FATAL: ... terraform apply in infra/user-pool/`. The EC2 bootstrap's `2>/dev/null || echo ""` swallow only covers the case where the user-pool stack simply hasn't been applied yet — never fall back to a fictional identity silently.

The container's `entrypoint.sh:88-91` reads `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` from env and runs `git config --global user.{name,email}`. The watchdog (`infra/aws/packer/scripts-docker-ubuntu/watchdog.sh`) passes both vars to the container via `-e`. The bootstrap writes them to `/etc/blitzlog.env` so the watchdog's rewrite preserves them.

## GitHub Token Freshness (Issue #145)

GitHub App installation tokens are capped to **1 hour** regardless of the requested `expires_at` — the 8 h default the Lambda historically requested is silently clamped. The agent's `git push` and `gh` calls are now backed by a token **at most 50 minutes old**, even on multi-hour assisted runs, because:

1. The Lambda mints a fresh token at webhook time and writes it to `/blitzlog/<env>/ephemeral/gh/<owner>/<repo>/<issue>` (per-(env, owner, repo, issue) so two issues numbered the same in different repos don't collide).
2. Right after the EC2 launch, the Lambda schedules a chain of 10 one-time EventBridge Scheduler events, 50 min apart, each of which re-mints the token (via the existing `get_github_app_token` path) and overwrites the same SecureString.
3. The watchdog on the EC2 instance runs a 5-min background poll loop (`infra/aws/packer/scripts-docker-ubuntu/blitzlog_token_poll.sh`, started from `watchdog.sh`) that re-reads SSM and rewrites the bind-mounted `/root/.git-credentials.d/github` and `/root/.config/gh/hosts.yml` whenever the value changes. The container picks up the new credentials on the next `git push` / `gh` invocation — no process restart.

**No agent-side code change is required.** The chain is fully Lambda-owned (no per-instance state, no continuous schedule); if the EC2 instance dies before the chain ends, the remaining events fire and no-op against the missing SSM parameter.

## Pull Request

1. **Create PR** against `main` (or `master` if that's the default)
2. **Title**: match commit convention (e.g., `feat: add user authentication`)
3. **Description**: reference the issue (`Closes #N`) and summarize changes
4. **Reviewers**: request review fromCODEOWNERS or the team
5. **Do not merge** — leave for human review

## Handling Failures

- If agent crashes or times out, the EC2 instance terminates automatically after 2 hours
- Check CloudWatch logs for the Lambda and EC2 instance for debugging
- Do NOT push incomplete or broken code
- If an issue is too complex for a single PR, document what was attempted and what remains

## Self-Termination

After the agent exits (success or failure), the instance self-terminates via the shutdown script. No manual cleanup needed.