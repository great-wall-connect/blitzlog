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

## Agent Commit Attribution (Audit Trail)

A blitzlog agent's commits must be attributed to the human who triggered the run, **not** to the EC2 default (`root@<hostname>`), the worker hostname, or any fictional runner identity. "A bot cannot be audited. The owner — the person who chose to trigger the event — is responsible for the task."

The plumbing for this is in three places (issue #119):

- **EC2 runtime** (assisted and autonomous modes). The Lambda
  forwards the issue's `sender_login` / `sender_id` into the
  bootstrap (`scripts/autonomous.py`, `scripts/assisted.py`,
  `_common.py:script_header`); the bootstrap `git config
  --global user.{name,email}` on the host and exports
  `GIT_USER_NAME` / `GIT_USER_EMAIL` env vars. The
  Packer-baked watchdog (`infra/packer/scripts-docker-ubuntu/
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