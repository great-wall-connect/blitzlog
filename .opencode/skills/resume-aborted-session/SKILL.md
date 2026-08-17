---
name: resume-aborted-session
description: >
  Resume an aborted assisted session for a GitHub issue by restoring the
  branch and session state from S3. Trigger phrases: "resume aborted issue N",
  "continue issue N", "pick up issue N", "restore session for issue N",
  "recover issue N", "where did we leave off on issue N".
---

# Resume Aborted Session

This skill restores a previously aborted assisted coding session so work can
continue in the current checkout without spinning up a new EC2 instance.

## Prerequisites

- The current directory is a git repository with a GitHub remote.
- `aws` CLI is installed and AWS credentials are configured.
- `opencode` CLI is available (for session import).

## Procedure

### 1. Parse the issue number

Extract the GitHub issue number **N** from the user's message. The number
usually appears right after the word "issue" (or is the only numeric token
in the request).

If no issue number can be determined, ask the user to specify one.

### 2. Pre-flight checks

Run through every item in the **recovery checklist** (see
`reference/recovery-checklist.md`). If any check fails, report the problem
to the user and stop — do not attempt partial recovery.

### 3. Derive S3 paths

```
REPO = <owner>/<name>   # from: git remote get-url origin
BUCKET = $S3_LOGS_BUCKET || "<your-agent-logs-bucket>"
PREFIX = "${REPO}/issue/${N}"
METADATA_URL = "s3://${BUCKET}/${PREFIX}/metadata.json"
```

### 4. Download and parse metadata

```bash
aws s3 cp "${METADATA_URL}" /tmp/session-metadata.json
```

Parse `/tmp/session-metadata.json` and extract:

| Field       | Use                                        |
|-------------|--------------------------------------------|
| `sessionId` | Locate the session JSON in S3              |
| `branch`    | `git fetch` + `git checkout`               |
| `commit`    | Informational — shown in resume summary    |
| `timestamp` | Informational — shown in resume summary    |

If `metadata.json` is missing, list candidate issues for the repo and stop:

```bash
aws s3 ls "s3://${BUCKET}/${REPO}/issue/"
```

### 5. Restore the branch (soft checkout)

```bash
git fetch origin "${BRANCH}"
git checkout "${BRANCH}"
```

**Do not** run `git reset --hard` unless the user explicitly asks.

**Edge cases:**

| Situation                                   | Action                                                    |
|---------------------------------------------|-----------------------------------------------------------|
| Working tree has uncommitted changes        | Warn the user. Offer `git stash`. Do **not** auto-stash.  |
| Local branch already exists with diverging history | Report divergence. Do **not** overwrite.           |
| Fetch fails (branch not on origin)          | Report. Offer to list `autosave/issue-${N}-*` refs.       |

### 6. Download and import the session

```bash
aws s3 cp "s3://${BUCKET}/${PREFIX}/sessions/${SESSION_ID}.json" /tmp/session-import.json
opencode import /tmp/session-import.json
```

If the session JSON is missing or empty, inform the user that only the
branch was restored (branch-only resume) and skip the import step.

If `opencode import` fails, surface the full error output and leave
`/tmp/session-import.json` in place for manual inspection.

### 7. Print resume summary

Display a summary including:

- Issue number
- Restored branch name
- Archived commit (short SHA)
- Archive timestamp
- Whether session chat history was imported
- Current `git status` (brief)

Then emit a templated continuation prompt:

```
Session for issue #N restored. Review AGENTS.md for conventions, then run
`git log --oneline -20` to inspect the resumed branch and continue where
we left off.
```
