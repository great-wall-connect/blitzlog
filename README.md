<img src="assets/blitzlog-mark.svg" alt="Blitzlog" width="120" />

# Blitzlog

**Pin, blitz, merge.**
*From issue to PR. With a trail.*

An autonomous coding pipeline for GitHub. Label an issue — Blitzlog spins up an EC2 spot instance, runs an OpenCode agent on your repo, opens a pull request, and shuts itself down.

---

## What it does

```
GitHub issue (labeled "autonomous")
  → GitHub webhook
  → API Gateway HTTP API
  → AWS Lambda
    → Verifies HMAC-SHA256 signature
    → Authenticates via GitHub App
    → Checks for the trigger label
    → Generates a repo-scoped GitHub token (~8h)
    → Launches an EC2 spot instance
  → EC2 spot instance
    → cloud-init: clones the repo, installs OpenCode
    → OpenCode agent reads the issue, branches, implements, tests
    → pushes a PR branch
    → exits
  → watchdog (2-hour timeout)
  → post-exit: self-terminate via IMDSv2
  → PR lands in your repo, ready for human review
```

Two modes:

- **Autonomous** — full-auto: the agent closes the issue end-to-end.
- **Assisted** — interactive via a Telegram bot, with the same underlying pipeline but the human stays in the loop.

---

## Architecture

| Component | Description |
|---|---|
| `lambda/handler.py` | Python Lambda: webhook verification, GitHub App auth, EC2 spot launch |
| `infra/main.tf` | Terraform root module with S3 backend |
| `infra/iam.tf` | IAM roles, policies, SSM parameters |
| `infra/ec2.tf` | Security group, key pair |
| `infra/lambda.tf` | Lambda function, CloudWatch logs, DLQ |
| `infra/apigateway.tf` | API Gateway HTTP API as webhook endpoint |
| `infra/alerting.tf` | SNS topic, SQS DLQ, CloudWatch alarm |
| `infra/user-pool/` | Per-user Telegram bot pool module (assisted mode) |
| `AGENTS.md` | Conventions the agent follows and contributors match: branch naming, commits, testing, PR process |
| `.opencode/skills/` | OpenCode skills bundled with the agent (e.g. `resume-aborted-session`) |

---

## Prerequisites

- Terraform >= 1.0
- AWS provider ~> 5.0
- An AWS account
- An existing VPC and subnet (Blitzlog needs to launch into one)
- An S3 bucket for Terraform state
- A GitHub App installed on the target repo

---

## Setup

### 1. Provide the required variables

Create `infra/terraform.tfvars`:

```hcl
aws_region              = "ap-east-1"             # or any region with spot capacity
vpc_id                  = "<your-vpc-id>"
ec2_subnet_id           = "<your-subnet-id>"
agent_logs_bucket_name  = "<your-agent-logs-bucket>"  # must exist or be created beforehand

github_app_id              = "123456"
github_app_private_key     = "<base64 encoded PEM>"
github_app_installation_id = "987654"
github_webhook_secret      = "your-secret-here"

alert_email         = "dev-team@example.com"     # optional; empty = no email subscription
opencode_model      = "<provider>/<model>"       # default: minimax-coding-plan/MiniMax-M3
opencode_api_key    = "<your-provider-api-key>"

# ssh_allowed_cidrs = ["1.2.3.4/32"]            # optional; empty = no SSH ingress
```

The state backend (`backend "s3"`) in `infra/main.tf` is generic — the `bucket` field is intentionally empty. Supply it via a `-backend.hcl` file:

```hcl
# infra/prod-backend.hcl  (gitignored)
bucket = "<your-tf-state-bucket>"
```

then run:

```bash
terraform init -backend-config=prod-backend.hcl
```

### 2. Deploy

```bash
cd infra
terraform init
terraform plan
terraform apply
```

GitHub App secrets are pushed into SSM `SecureString` parameters automatically; the Lambda reads them at runtime.

### 3. Register the GitHub webhook

After deployment, copy the webhook URL from `terraform output`:

```bash
terraform output webhook_url
# → https://xxx.execute-api.<region>.amazonaws.com/
```

Register in your GitHub repo → **Settings → Webhooks**:

- **Payload URL**: the webhook URL
- **Content type**: `application/json`
- **Secret**: same value as `github_webhook_secret`
- **Events**: `Issues`

### 4. Label an issue

Label any issue with **`autonomous`** to trigger the autonomous pipeline, or **`assisted`** (with a configured Telegram bot pool) to start an interactive session.

---

## Instance lifecycle

1. **Launch** — Lambda spawns a `t4g.medium` (or `t4g.large` / `t4g.xlarge`) spot instance with user-data.
2. **Setup** — cloud-init configures git credentials, installs OpenCode, clones the target repo.
3. **Agent run** — OpenCode reads the issue, creates a `feat/issue-{N}-{slug}` branch, implements, tests, lints, commits, pushes.
4. **Watchdog** — `timeout 7200` (2 hours) forces termination if the agent hangs.
5. **Shutdown** — post-exit script calls `ec2:TerminateInstances` via IMDSv2.
6. **Cleanup** — git credentials are deleted after `git clone`; the GitHub installation token is repo-scoped with up to 8h lifetime (longer than the watchdog, intentionally).

---

## Manual instance management

```bash
# List running agent instances
aws ec2 describe-instances \
  --filters "Name=tag:Purpose,Values=blitzlog" "Name=instance-state-name,Values=running" \
  --query "Reservations[].Instances[].[InstanceId, Tags[?Key=='Issue'].value|[0], Tags[?Key=='Mode'].value|[0]]" \
  --output table --region <your-region>

# Terminate
aws ec2 terminate-instances --instance-ids <instance-id> --region <your-region>

# Or via SSM
aws ssm send-command \
  --instance-ids <instance-id> \
  --document-name "AWS-RunShellScript" \
  --parameters commands=["shutdown -h now"] \
  --region <your-region>
```

---

## Security

- **GitHub App** (not PAT) — per-repo scope, ~8h token lifetime.
- **HMAC-SHA256** webhook signature verification prevents spoofed events.
- **IMDSv2 only** — no IMDSv1 fallback; token-based metadata access.
- **SSM `SecureString`** for all credentials; never logged in plaintext.
- **Repo-scoped tokens** written to a file (not env var), deleted after `git clone`.
- **Tag-conditioned `ec2:TerminateInstances`** — only instances tagged `Purpose=blitzlog` can be terminated by the agent role.
- **SSH ingress disabled by default** — opt in via `ssh_allowed_cidrs`.

For vulnerability disclosure, see [SECURITY.md](SECURITY.md).

---

## Monitoring

- Lambda errors trigger a CloudWatch alarm → SNS → email (via `alert_email`).
- Failed Lambda invocations go to SQS DLQ (`blitzlog-lambda-dlq`).
- Lambda logs: CloudWatch log group `/aws/lambda/blitzlog` (14-day retention).
- Agent run logs (per-issue): uploaded to `s3://<agent_logs_bucket>/<repo>/issue/<N>/logs/...`.
- OpenCode session exports (audit trail): `s3://<agent_logs_bucket>/<repo>/issue/<N>/sessions/...`.

---

## Cost envelope

| Component | Approx. cost |
|---|---|
| Lambda | ~$0.20 per 1M requests (stateless, < 1s) |
| `t4g.medium` spot | ~$0.01/hr (varies by region) |
| API Gateway HTTP API | ~$1.00 per 1M requests |
| SQS / SNS | negligible |

A 30-minute autonomous run costs roughly the same as a large coffee.

---

## OpenCode configuration

The agent writes an `opencode.json` to `~/.config/opencode/opencode.json` on the EC2 instance, configuring the inference provider and model. Supply via `terraform.tfvars`:

```hcl
opencode_model   = "<provider>/<model>"
opencode_api_key = "<your-api-key>"
```

The default model is `minimax-coding-plan/MiniMax-M3`. Override for any provider that the [OpenCode CLI](https://opencode.ai) supports.

---

## Example

A worked example — labelled issue → PR — lives in a separate repo:

👉 **[blitzlog-example](https://github.com/great-wall-connect/blitzlog-example)** — a small Rust service (`taskforge`) with four demo issues that exercise the pipeline end-to-end.

---

## Conventions

This project follows the conventions in [AGENTS.md](AGENTS.md). The autonomous agent uses the same conventions; if you're contributing code, follow them too.

- **Branch naming**: `feat/issue-{N}-{slug}` / `fix/issue-{N}-{slug}`.
- **Commits**: [Conventional Commits](https://www.conventionalcommits.org/).
- **Tests required** for all new functionality.
- **No breaking changes** without an issue discussion.

---

## Licence

[MIT](LICENSE) © 2026 Great Wall Connect Limited.

Maintained by Great Wall Connect Limited — `admin@greatwallconnect.com`.