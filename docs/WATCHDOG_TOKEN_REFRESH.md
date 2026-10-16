# Watchdog token-refresh mechanism

This document is the **debugger's field guide** for the issue #145
token-refresh chain. The runtime test path is
`tests/test_watchdog_credential_poll_e2e.py` (8 tests, < 2 s); the
manual path is the dev-spot-instance smoke test described at the end
of this file.

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
| `tests/test_watchdog_credential_poll_e2e.py` | 8 e2e tests covering the watchdog's wiring + the container-side file format. |

## What the runtime tests cover

`tests/test_watchdog_credential_poll_e2e.py` runs in < 2 s on a
workstation, no EC2, no AWS spend. Eight tests, four parts:

1. **Static wiring** (3 tests) — parse `watchdog.sh` as text and
   assert the call signatures:
   - The initial credential writes use the
     `_bl_tokenpoll_write_git_creds` / `_bl_tokenpoll_write_gh_hosts_yml`
     helpers, with `git config --global credential.helper 'store
     --file /root/.git-credentials.d/github'` right after.
   - The `blitzlog_start_token_poll_loop` call passes the
     bind-mounted paths and the `BLITZLOG_TOKEN_POLL_INTERVAL`
     override.
   - The bind mount `-v "/root/gh-config/${INSTANCE_ID}:/root/.config/gh"`
     maps the host write path onto the container's read path.

2. **File format** (2 tests) — source the library, run
   `_bl_tokenpoll_write_*`, then parse the output with the real
   readers the container uses:
   - `git credential fill` against the git-credential file
     (HOME-isolated so no system cache interferes).
   - PyYAML against the `gh` hosts.yml (the same YAML parser
     `gh` uses upstream).

3. **End-to-end through the watchdog's call signature** (2 tests) —
   start the poll loop with the same args the watchdog uses, drive
   an SSM-value change, assert the files rewrite (and that a
   stable SSM value leaves them untouched).

4. **SIGTERM cleanup** (1 test) — verbatim reproduction of
   `watchdog.sh:277-294`'s `kill $PID` + 100 ms grace + SIGKILL
   escalation. Asserts the loop is reaped within the grace window.

These four parts together cover every wiring decision the
watchdog makes about the poll library. A typo in the path
arguments, a wrong bind-mount source, a hand-rolled `printf`
replacing the library call, or a format drift on either side is
caught at PR-review time without standing up an instance.

## Manual smoke test (dev spot instance)

Use this when you've changed something in the chain that the
runtime tests can't see (e.g. the Lambda's `action == "refresh"`
branch, a real `aws ssm get-parameter` against real AWS, a real
`git push` against a real remote). Skips Packer entirely — uses
the already-baked dev AMI.

### Cost and time

- 1 × t4g.medium spot in ap-east-1 for ~10 min ≈ $0.01.
- 5 refresh iterations × 30 s = 2.5 min observation; the full
  10-iteration chain is ~5 min.
- Total wall clock: ~10 min.

### Step 0 — pick a throwaway issue number

Pick an issue number that is NOT in use as a blitzlog issue.
The token's SSM param is namespaced by issue, so a throwaway
number keeps the test isolated:

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
IMAGE_TAG=$(aws ssm get-parameter \
    --name /blitzlog/dev/agent-image-tag \
    --region ap-east-1 \
    --query Parameter.Value --output text 2>/dev/null || echo "v0.5.0")
```

Write the env files. Note `BLITZLOG_TOKEN_POLL_INTERVAL=30` is
the override the user chose for this debug session.

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
GITHUB_TOKEN_SSM_PARAM=/blitzlog/dev/ephemeral/github-token-${ISSUE}
BLITZLOG_TOKEN_POLL_INTERVAL=30
AWS_REGION=ap-east-1
ENVEOF
EOF
```

### Step 5 — seed SSM with a known initial token

```bash
INITIAL_TOKEN=$(aws lambda invoke \
    --function-name blitzlog-dev-get-token \
    --payload '{}' \
    --cli-binary-format raw-in-base64-out \
    /tmp/initial.json \
    --region ap-east-1 \
    --query 'Payload' --output text 2>/dev/null \
    | jq -r '.token // empty')
# Or use any long-lived installation token from a GitHub App
# you've already minted. The test only needs the loop to read a
# value, not a real one.
aws ssm put-parameter \
    --name "/blitzlog/dev/ephemeral/github-token-${ISSUE}" \
    --type SecureString \
    --value "${INITIAL_TOKEN:-ghp_initial_debug_token_aaaa}" \
    --overwrite \
    --region ap-east-1
```

### Step 6 — run the watchdog in the foreground

In one terminal:

```bash
ssh -i ~/.ssh/blitzlog-dev.pem ubuntu@"$PUBLIC_IP"
sudo -E /usr/local/bin/watchdog.sh
```

The watchdog will:
1. `sudo docker load -i /opt/blitzlog/images/blitzlog-agent.tar.gz` — succeeds (the AMI bakes the image in).
2. Read SSM, write `/root/.git-credentials.d/github` and `/root/gh-config/<instance>/hosts.yml`.
3. Start the poll loop (you'll see `Started token refresh poll loop (pid=…, interval=30s)`).
4. `sudo docker run --name blitzlog-agent …` — the container starts.

In another terminal, watch the host log:

```bash
ssh -i ~/.ssh/blitzlog-dev.pem ubuntu@"$PUBLIC_IP" \
    'tail -f /var/log/backend-bootstrap.log'
```

### Step 7 — drive a refresh

In a third terminal, invoke the Lambda directly with
`action == "refresh"` (the user's chosen driver):

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
    sleep 30
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
  successful refresh (after the 50-min-apart chain has been
  re-issued at 30s intervals for the debug session).
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
    --name "/blitzlog/dev/ephemeral/github-token-${ISSUE}" \
    --region ap-east-1
# (Optional) clean up any leftover schedules in case iteration 9
# was interrupted before the chain could end.
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
| `Token change detected` doesn't fire within 30 s | Poll loop isn't alive; check `kill -0 $BLITZLOG_TOKEN_POLL_PID`. If dead, the watchdog's post-container cleanup or a `set -e` abort killed it. |
| File mtime updates but content is wrong | The library's printf format changed; check `_bl_tokenpoll_write_git_creds` / `_bl_tokenpoll_write_gh_hosts_yml` against `git credential fill` / `yaml.safe_load`. |
| `gh auth status` shows the old token in the container | The bind mount source (`/root/gh-config/${INSTANCE_ID}`) and the write path drifted apart. Compare `watchdog.sh:124` (mkdir) vs `watchdog.sh:133` (write) vs `watchdog.sh:254` (bind mount). |
| Lambda returns 200 but SSM doesn't change | `get_github_app_token` returned an empty string (likely bad GitHub App credentials). Check `aws ssm get-parameter-history --name /blitzlog/dev/ephemeral/github-token-${ISSUE} --region ap-east-1` to see what the Lambda actually wrote. |
| `blitzlog-dev-webhook` invocation returns 500 | The Lambda's `action == "refresh"` branch is throwing; check CloudWatch logs for the `blitzlog-dev-webhook` log group. |
| `kill -0 $BLITZLOG_TOKEN_POLL_PID` after SIGTERM still succeeds | The loop's `trap 'exit 0' TERM` was removed; the SIGKILL escalation at `watchdog.sh:290-293` is the safety net. |
