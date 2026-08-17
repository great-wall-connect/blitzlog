# Recovery Pre-flight Checklist

Complete every check before attempting session restore. If any check fails,
report the problem clearly to the user and stop.

## 1. Git repository

- [ ] Current directory is inside a git repository:
  ```bash
  git rev-parse --is-inside-work-tree
  ```
- [ ] `origin` remote points to a GitHub URL:
  ```bash
  git remote get-url origin
  ```
  Must match `github.com` (HTTPS or SSH).

## 2. AWS credentials

- [ ] `aws` CLI is installed:
  ```bash
  which aws
  ```
- [ ] Credentials are configured (any one is sufficient):
  ```bash
  aws sts get-caller-identity
  ```
  If this fails, tell the user to configure AWS credentials
  (`aws configure`, env vars, or instance profile).

## 3. S3 metadata exists

- [ ] `metadata.json` exists for the target issue:
  ```bash
  aws s3 ls "s3://${BUCKET}/${REPO}/issue/${N}/metadata.json"
  ```
  If absent, list available issues:
  ```bash
  aws s3 ls "s3://${BUCKET}/${REPO}/issue/"
  ```

## 4. Working tree is clean

- [ ] No uncommitted changes:
  ```bash
  git status --porcelain
  ```
  If dirty, **warn** the user and offer to `git stash`. Do **not**
  auto-stash or auto-commit.

## 5. Local branch conflict check

- [ ] If a local branch with the same name already exists, verify it does
  not diverge from the fetched ref:
  ```bash
  git fetch origin "${BRANCH}"
  git log --oneline HEAD..origin/"${BRANCH}" --max-count=5
  git log --oneline origin/"${BRANCH}"..HEAD --max-count=5
  ```
  If histories diverge, report both sides and stop. Do **not** overwrite.

## 6. Session JSON available (non-fatal)

- [ ] Session export file exists:
  ```bash
  aws s3 ls "s3://${BUCKET}/${REPO}/issue/${N}/sessions/${SESSION_ID}.json"
  ```
  If missing or empty, inform the user that only the branch will be
  restored (branch-only resume). This is not a hard failure.

## 7. opencode CLI available (non-fatal)

- [ ] `opencode` is on PATH:
  ```bash
  which opencode
  ```
  If missing, inform the user that session import will be skipped. Branch
  restore can still proceed.
