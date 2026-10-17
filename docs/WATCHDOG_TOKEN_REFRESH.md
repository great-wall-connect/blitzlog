# Watchdog token-refresh mechanism

This document is the **debugger's field guide** for the issue #145
token-refresh chain. The runtime test path is
`tests/test_watchdog_credential_poll_e2e.py` (Levels 1 + 2, ~10 s
on a workstation); the manual full-mechanism path is the dev-spot
smoke test wrapped in `scripts/test-dev-spot.sh` (Level 3, ~10 min,
~$0.01).

> **Companion visual:** [`docs/architecture/auth-overview.html`](architecture/auth-overview.html)
> (renders to `auth-overview.png`) shows the four-layer lifecycle
> (launch-time mint, EC2 boot, chain self-perpetuation, 5-min poll)
> at a glance. Read this doc when you need to debug; read the
> diagram when you need to orient.

## Self-perpetuation status (current)

**Healthy** as of PR #154. The chain mints every 50 min as designed;
the first auto-mint lands at `instance_launch + 50 min`, and the
bind-mount picks it up within the next 5-min poll. A 60-min
dev-instance monitor will see:

- `Ver=1` for the first ~50 min
- `Ver=2` with a fresh `iat` at the +50-min mark
- `match=True` between SSM and bind mount on every iteration
- The poll-loop log fires "Token change detected" exactly once
  (on the genuine rotation), not on every 5-min tick

The `iam:PassRole` regression that killed the chain at iteration 0
on the dev instance is fixed in PR #154 commit `b8152e2` and
pinned by `tests/test_lambda_iam.py`.

### Instance teardown: one-call group delete

The watchdog's post-container cleanup block cascade-deletes every
chain event for the instance in a single AWS API call. The flow:

1. `aws ssm delete-parameter` on the per-issue token
   (`/blitzlog/<env>/ephemeral/gh/<owner>/<repo>/<issue>`)
2. `aws scheduler delete-schedule-group` on the per-issue group
   (`blitzlog-<env>-token-refresh-<sha256(owner|repo|issue)[:16]>`) —
   cascade-deletes all 10 chain events for this instance
3. `aws ec2 terminate-instances`

The per-issue group name is hashed from
`(owner, repo, issue)` (16 hex chars of SHA-256) so two concurrent
instances in the same env never share a group. One instance's
cleanup cannot nuke another instance's chain. The per-issue scope
also matches the per-issue SSM namespace shape.

The order matters: steps 1 and 2 run **before** step 3. Once
`terminate-instances` is called, the instance is shutting down and
subsequent AWS calls from this script may fail. (The dev-spot
test's cleanup function at `scripts/test-dev-spot.sh` doesn't have
this constraint because it runs **outside** the instance; the
watchdog runs **inside** it.)

Pinned by `tests/test_watchdog_credential_poll_e2e.py`:
`test_watchdog_teardown_deletes_ssm_token`,
`test_watchdog_teardown_deletes_per_issue_schedule_group`,
`test_watchdog_teardown_aws_state_runs_before_terminate_instances`.

## Testing levels

| Level | What it tests | Where | How to run | Cost | Time |
|---|---|---|---|---|---|
| **1** | Bash library, watchdog wiring, file format | `tests/test_watchdog_credential_poll_e2e.py` (Level 1 sections) | `pytest tests/test_watchdog_credential_poll_e2e.py` | $0 | < 2 s |
| **2** | Real `aws ssm` round-trip on a workstation | `tests/test_watchdog_credential_poll_e2e.py` (Level 2 section) | `mise run test-integration` | ~$0.0003/run | ~10 s |
| **3** | Full mechanism including the in-container `git push` / `gh` against the bind-mounted credentials | `scripts/test-dev-spot.sh` + this doc | `mise run test-dev-spot` | ~$0.01/run | ~10 min |

The three levels are **additive** — each catches a different bug
class. None of them subsumes the others.

### Level 1 — unit with shim

The 8 tests in the "Level 1" sections of the pytest file:

- 3 static-analysis tests on `watchdog.sh` text (the call signatures
  the watchdog passes to the poll library, the bind-mount source
  vs. write path).
- 2 file-format tests against the real `git credential fill` /
  PyYAML parsers (the same parsers the container uses).
- 3 subprocess tests with a fake `aws` shim in `PATH` (the diff-
  rewrite loop, the no-op-on-stable behaviour, the SIGTERM trap).

These run on every PR via the `Lint + test (Python)` CI job. They
catch wiring typos, format drift, and shim-level control flow but
NOT real AWS round-trips — the shim returns canned strings without
ever talking to SSM.

### Level 2 — real-AWS round-trip on a workstation

The 1 test in the "Level 2" section of the pytest file:

- `test_watchdog_poll_loop_rewrites_against_real_ssm` —
  `@pytest.mark.integration`. Real `aws ssm put-parameter` writes
  drive the round-trip; the library's `_bl_tokenpoll_read_ssm`
  runs the real `aws` CLI binary against the real SSM service. The
  test spawns a long-lived bash subprocess that runs the poll loop
  with a 1 s interval, then drives 3 SSM value changes (A → B → C)
  and asserts the bind-mounted files are rewritten each time.

Skipped by default; opt in with `BLITZLOG_RUN_INTEGRATION=1` (or
`--run-integration`). The scheduled `.github/workflows/integration-test.yml`
job runs it nightly against the dev account via OIDC.

Catches what Level 1 cannot: a `--with-decryption` flag drift on
SecureString parameters, a region-default disagreement, a real
network timeout on `ssm:GetParameter`, a bash-vs-Python encoding
mismatch on the SSM value.

### Level 3 — full mechanism on a dev spot instance

`scripts/test-dev-spot.sh` (also documented below as a manual
procedure). Launches a single t4g.medium spot from the already-baked
dev AMI, runs the watchdog by hand with a 30 s poll interval, drives
10 refresh iterations via real `aws lambda invoke --payload
'{"action":"refresh",...}'` calls, observes the host-side
`/root/.git-credentials.d/github` and
`/root/gh-config/<instance>/hosts.yml` get rewritten within
`BLITZLOG_TOKEN_POLL_INTERVAL` seconds of each refresh, and
verifies the chain ends naturally at iteration 9 (no iter-10
schedule is created).

Catches what Levels 1 and 2 cannot: the in-container `git push` and
`gh` calls against a real GitHub remote reading the bind-mounted
credentials, the real `--with-decryption` SSM read, the real
`docker run` lifecycle, and the real `set -e` abort path in the
watchdog.

## What the mechanism is

```
       ┌──────────────────────────────┐
       │  EventBridge Scheduler       │  ── 10 events, 50 min apart
       │  (chain of 10, at(<t+50m>))  │
       └──────────┬───────────────────┘
                  │ action == "refresh"
                  ▼
       ┌──────────────────────────────┐
       │  blitzlog Lambda             │  re-mints GitHub App token via
       │  _handle_refresh()           │  get_github_app_token(); writes
       │                              │  SecureString overwrite to
       │                              │  /blitzlog/<env>/ephemeral/
       │                              │  github-token-<issue>
       └──────────┬───────────────────┘
                  │ ssm put-parameter (overwrite)
                  ▼
       ┌──────────────────────────────┐
       │  AWS Systems Manager         │
       └──────────┬───────────────────┘
                  │ ssm get-parameter (5-min host poll)
                  ▼
       ┌──────────────────────────────┐
       │  blitzlog-agent EC2 host     │  watchdog.sh sources
       │  /usr/local/bin/             │  blitzlog_token_poll.sh; the
       │  blitzlog_token_poll.sh      │  poll loop reads SSM every
       │                              │  5 min (default; override with
       │                              │  BLITZLOG_TOKEN_POLL_INTERVAL)
       └──────────┬───────────────────┘
                  │ in-place rewrite of
                  │ /root/.git-credentials.d/github
                  │ /root/gh-config/<id>/hosts.yml
                  ▼
       ┌──────────────────────────────┐
       │  blitzlog-agent container    │  `git push` reads the new
       │                              │  credential via git's
       │                              │  credential-store helper;
       │                              │  `gh ...` reads the new
       │                              │  oauth_token from hosts.yml
       └──────────────────────────────┘
```

The chain ends naturally at iteration 9 — no 11th event is
scheduled. If the EC2 instance dies before the chain ends, the
remaining events fire and no-op against the missing SSM parameter
(~$0.0000023 per event).

## Files involved

| File | Role |
|---|---|
| `lambda/handler.py` | The Lambda's `action == "refresh"` branch + the 10-iter `schedule_next_refresh` chain. |
| `infra/modules/core/scheduler.tf` | The per-env EventBridge Scheduler IAM role + schedule group. |
| `infra/packer/scripts-docker-ubuntu/watchdog.sh` | The host-side lifecycle manager. Sources the poll library, starts the loop, SIGTERMs it on container exit. |
| `infra/packer/scripts-docker-ubuntu/blitzlog_token_poll.sh` | The poll library: 5-min `ssm get-parameter` + diff-checked in-place rewrite of the two bind-mounted credential files. |
| `tests/test_watchdog_credential_poll.py` | 6 library-in-isolation tests. |
| `tests/test_watchdog_credential_poll_e2e.py` | 9 tests spanning Level 1 (8) + Level 2 (1). |
| `tests/conftest.py` | Registers the `integration` marker + auto-skip hook. |
| `scripts/test-dev-spot.sh` | Level 3 wrapper around the manual procedure below. |
| `.github/workflows/integration-test.yml` | Nightly schedule for Level 2. |
| `infra/bootstrap/integration-test-role.tf` | Narrow-OIDC IAM role for the nightly integration job. |

## Nightly CI

`.github/workflows/integration-test.yml` runs Level 2 every night
at 03:00 UTC. It uses OIDC to assume
`blitzlog-integration-test-role` (narrow trust: only this
workflow can assume it) with `ssm:GetParameter` / `PutParameter` /
`DeleteParameter` scoped to
`/blitzlog/<env>/ephemeral/gh/integration-test/repo/integration-test-*`. A
failed nightly run surfaces in the GitHub Actions UI under the
`integration-test` workflow.

To trigger an ad-hoc run, use `gh workflow run integration-test.yml`
from the repo root.

## Manual Level 3 procedure

`scripts/test-dev-spot.sh` automates this. The manual version
below is for operators who want to debug a specific iteration or
inspect the host state mid-run.

### Cost and time

- 1 × t4g.medium spot in ap-east-1 for ~10 min ≈ $0.01.
- 10 refresh iterations × 35 s = ~6 min observation.
- Total wall clock: ~10 min.

### Step 0 — pick a throwaway issue number

Pick an issue number that is NOT in use as a blitzlog issue. The
token's SSM param is namespaced by issue, so a throwaway number
keeps the test isolated:

```bash
ISSUE=999999
```

### Step 1 — find the current dev AMI

```bash
AMI_ID=$(aws ssm get-parameter \
    --name /blitzlog/dev/agent-ami-id-docker-ubuntu \
    --region ap-east-1 \
    --query Parameter.Value --output text)
echo "Will launch: $AMI_ID"
```

### Step 2 — launch a t4g.medium spot

Pick the dev VPC + subnet + security group. The instance profile
must be `blitzlog-dev-agent-instance-profile` (the same one
real blitzlog runs use; the role grants SSM read on
`/blitzlog/dev/ephemeral/*` and the watchdog's other AWS calls).

```bash
INSTANCE_ID=$(aws ec2 run-instances \
    --image-id "$AMI_ID" \
    --instance-type t4g.medium \
    --subnet-id "$SUBNET_ID" \
    --security-group-ids "$SG_ID" \
    --iam-instance-profile Name=blitzlog-dev-agent-instance-profile \
    --instance-market-options 'MarketType=spot' \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=blitzlog-token-refresh-debug}]' \
    --query 'Instances[0].InstanceId' --output text)
echo "Launched: $INSTANCE_ID"
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
PUBLIC_IP=$(aws ec2 describe-instances \
    --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' \
    --output text)
echo "Public IP: $PUBLIC_IP"
```

### Step 3 — stop the systemd unit, run the watchdog by hand

The AMI's systemd starts `blitzlog-agent.service` on boot, which
runs `watchdog.sh`. We don't want that — we want to control the
watchdog's environment to use the throwaway issue + a 30-second
poll interval.

```bash
ssh -i ~/.ssh/blitzlog-dev.pem ubuntu@"$PUBLIC_IP" <<'EOF'
sudo systemctl stop blitzlog-agent
sudo systemctl disable blitzlog-agent
EOF
```

### Step 4 — set up the env the way the bootstrap would

Pick a real image that's already pushed (any tag from a recent
release will do — the watchdog only `docker load`s it):

```bash
IMAGE_TAG=$(aws ec2 describe-images \
    --image-ids "$AMI_ID" \
    --query 'Images[0].Tags[?Key==`AgentTag`].Value' \
    --output text --region ap-east-1)
```

Write the env files. Note `BLITZLOG_TOKEN_POLL_INTERVAL=30` is
the override for the debug session.

```bash
ssh -i ~/.ssh/blitzlog-dev.pem ubuntu@"$PUBLIC_IP" <<EOF
sudo mkdir -p /root
sudo tee /root/blitzlog.env >/dev/null <<ENVEOF
AGENT_IMAGE_REPO=ghcr.io/great-wall-connect/blitzlog-agent
AGENT_IMAGE_TAG=${IMAGE_TAG}
ENVEOF
sudo tee /etc/blitzlog.env >/dev/null <<ENVEOF
MODE=assisted
ISSUE_NUMBER=${ISSUE}
REPO=great-wall-connect/blitzlog
BLITZLOG_ENV=dev
S3_LOGS_BUCKET=gwc-blitzlog-agent-logs
SESSION_ARCHIVE_BUCKET=gwc-blitzlog-agent-logs
SESSION_ARCHIVE_PREFIX=dev/sessions
S3_LOG_PREFIX=dev/logs
GITHUB_TOKEN_SSM_PARAM=/blitzlog/dev/ephemeral/gh/${REPO}/${ISSUE}
BLITZLOG_TOKEN_POLL_INTERVAL=30
AWS_REGION=ap-east-1
ENVEOF
EOF
```

### Step 5 — seed SSM with a known initial token

```bash
INITIAL_TOKEN="ghp_initial_debug_token_aaaa"
aws ssm put-parameter \
    --name "/blitzlog/dev/ephemeral/gh/${REPO}/${ISSUE}" \
    --type SecureString \
    --value "${INITIAL_TOKEN}" \
    --overwrite \
    --region ap-east-1
```

### Step 6 — run the watchdog in the background

In one terminal:

```bash
ssh -i ~/.ssh/blitzlog-dev.pem ubuntu@"$PUBLIC_IP"
sudo nohup /usr/local/bin/watchdog.sh > /tmp/watchdog.out 2>&1 &
```

In another terminal, watch the host log:

```bash
ssh -i ~/.ssh/blitzlog-dev.pem ubuntu@"$PUBLIC_IP" \
    'tail -f /var/log/backend-bootstrap.log'
```

You should see within ~30 s:

- `Started token refresh poll loop (pid=…, interval=30s)` (the
  loop came up).
- A first `Token change detected; rewriting credential files`
  after the loop reads the seeded SSM value (the loop's first
  tick always writes, even when the value matches `last`).

### Step 7 — drive a refresh

In a third terminal, invoke the Lambda directly with
`action == "refresh"`:

```bash
for ITER in 0 1 2 3 4 5 6 7 8 9; do
    echo "=== iteration $ITER ==="
    aws lambda invoke \
        --function-name blitzlog-dev-webhook \
        --payload "$(jq -nc \
            --argjson issue ${ISSUE} \
            --arg instance "${INSTANCE_ID#i-}" \
            --arg repo great-wall-connect/blitzlog \
            --argjson iter ${ITER} \
            '{action: "refresh", issue: $issue, instance: $instance, repo: $repo, iteration: $iter}')" \
        --cli-binary-format raw-in-base64-out \
        /tmp/refresh-${ITER}.json \
        --region ap-east-1 \
        --query 'StatusCode' --output text
    cat /tmp/refresh-${ITER}.json
    echo
    sleep 35
done
```

### Step 8 — observe the file rewrites

In the host (or another terminal), check that the credential
files reflect the new value within ≤ 30 s of each refresh:

```bash
ssh -i ~/.ssh/blitzlog-dev.pem ubuntu@"$INSTANCE_ID" \
    'cat /root/.git-credentials.d/github'
ssh -i ~/.ssh/blitzlog-dev.pem ubuntu@"$INSTANCE_ID" \
    'cat /root/gh-config/'"$INSTANCE_ID"'/hosts.yml'
ssh -i ~/.ssh/blitzlog-dev.pem ubuntu@"$INSTANCE_ID" \
    'tail -f /var/log/backend-bootstrap.log | grep -E "Token change|Started token|Scheduled refresh"'
```

You should see:
- `Started token refresh poll loop (pid=…, interval=30s)` once.
- `Token change detected; rewriting credential files` once per
  successful refresh.
- `Scheduled refresh event: name=refresh-issue-${ISSUE}-iter-…`
  per refresh invocation.

### Step 9 — verify the chain ends at iteration 9

After the iteration-9 invocation, confirm no iteration-10
schedule was created:

```bash
aws scheduler list-schedules \
    --group-name blitzlog-dev-token-refresh \
    --region ap-east-1 \
    --query "Schedules[?contains(Name, 'refresh-issue-${ISSUE}-iter-10')].Name" \
    --output text
```

The output must be empty. The chain ends naturally at
iteration 9.

### Step 10 — tear down

```bash
aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --region ap-east-1
aws ssm delete-parameter \
    --name "/blitzlog/dev/ephemeral/gh/${REPO}/${ISSUE}" \
    --region ap-east-1
for ITER in 0 1 2 3 4 5 6 7 8 9 10; do
    aws scheduler delete-schedule \
        --name "refresh-issue-${ISSUE}-iter-${ITER}" \
        --group-name blitzlog-dev-token-refresh \
        --region ap-east-1 2>/dev/null || true
done
```

## What to look for when something is wrong

| Symptom | Likely cause |
|---|---|
| `Token change detected` doesn't fire within `BLITZLOG_TOKEN_POLL_INTERVAL` | Poll loop isn't alive; check `kill -0 $BLITZLOG_TOKEN_POLL_PID`. If dead, the watchdog's post-container cleanup or a `set -e` abort killed it. |
| File mtime updates but content is wrong | The library's printf format changed; check `_bl_tokenpoll_write_git_creds` / `_bl_tokenpoll_write_gh_hosts_yml` against `git credential fill` / `yaml.safe_load`. |
| `gh auth status` shows the old token in the container | The bind mount source (`/root/gh-config/${INSTANCE_ID}`) and the write path drifted apart. Compare `watchdog.sh:124` (mkdir) vs `watchdog.sh:133` (write) vs `watchdog.sh:254` (bind mount). |
| Lambda returns 200 but SSM doesn't change | `get_github_app_token` returned an empty string (likely bad GitHub App credentials). Check `aws ssm get-parameter-history --name /blitzlog/dev/ephemeral/gh/${REPO}/${ISSUE} --region ap-east-1` to see what the Lambda actually wrote. |
| `blitzlog-dev-webhook` invocation returns 500 | The Lambda's `action == "refresh"` branch is throwing; check CloudWatch logs for the `blitzlog-dev-webhook` log group. |
| `kill -0 $BLITZLOG_TOKEN_POLL_PID` after SIGTERM still succeeds | The loop's `trap 'exit 0' TERM` was removed; the SIGKILL escalation at `watchdog.sh:290-293` is the safety net. |
| Nightly CI fails on Level 2 | Check the GitHub Actions run for the actual `aws ssm put-parameter` / `delete-parameter` errors. The most common cause is the integration-test IAM role not being applied (`terraform apply` in `infra/bootstrap/`). |
