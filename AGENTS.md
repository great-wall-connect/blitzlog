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