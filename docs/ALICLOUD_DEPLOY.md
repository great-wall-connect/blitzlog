# Deploying to Alicloud

Issue #125. This is the operator runbook for the Alicloud path —
the second cloud blitzlog supports.

The Alicloud tree is `infra/alicloud/{bootstrap,dev,prod,modules/core}/`.
It mirrors the AWS tree one-for-one, with the same resource names and
the same `/blitzlog/<env>/...` secret paths (the Alicloud `SecretStore`
implementation is KMS Secrets Manager, and the paths are byte-for-byte
identical to the AWS SSM paths).

**The Alicloud deploy is opt-in.** `vars.BLITZLOG_PROVIDER=alicloud`
alone does not apply anything — `vars.ENABLE_ALICLOUD_DEPLOY=true` is
also required. The Alicloud path reaches a real account, so a stray
repo variable must not be able to apply to it by accident.

---

## 1. One-time account setup (console)

Two things cannot be created by Terraform and must be done in the
Alicloud console:

### 1a. The OIDC identity provider

The Alicloud Terraform provider (v2.x) has **no** resource for the RAM
OIDC provider, so it is registered out-of-band — the same shape the
AWS side had before `infra/aws/bootstrap/github-oidc-provider.tf` was
added.

**RAM console → Identity Management → Identity Providers → Create
OIDC identity provider:**

| Field | Value |
|---|---|
| Provider name | `blitzlog` |
| IdP URL | `https://token.actions.githubusercontent.com` |
| Client ID | `sts.aliyuncs.com` |
| Thumbprint | `06d927fecd0a84aeba28aad1d808139470fe95c3` |

The resulting ARN is
`acs:ram::<account-id>:oidc-provider/great-wall-connect/blitzlog` —
note the `<org>/<repo>` path, which is how Alicloud names a GitHub
OIDC provider. The workflow hard-codes that shape.

The client id MUST be `sts.aliyuncs.com`: it is the `aud` claim the
workflow requests and the value the role's trust policy conditions
on.

### 1b. The account id

```bash
gh variable set ALICLOUD_ACCOUNT_ID --body <account-id>
```

Every Alicloud role ARN in the workflows is built from it.

### 1c. The agent network ids (after the first bootstrap apply)

The CI plan needs the shared VPC + VSwitches, and CI has no tfvars
file — so they come from repo variables:

```bash
gh variable set ALICLOUD_VPC_ID --body <vpc-id>
gh variable set ALICLOUD_VSWITCH_IDS --body '<id1>,<id2>,<id3>'
```

Get the values from the bootstrap stack (§3):

```bash
cd infra/alicloud/bootstrap && terraform output vpc_id
cd infra/alicloud/bootstrap && terraform output vswitch_ids
```

---

## 2. Bootstrap

Creates the shared OSS buckets (agent-logs, STT models), the KMS CMK,
the two CI roles (the Packer build role and the deploy role), and the
**shared agent VPC + VSwitches** (`network.tf`).

```bash
cd infra/alicloud/bootstrap
cp terraform.tfvars.example terraform.tfvars   # if not already present
cp bootstrap-backend.hcl.example bootstrap-backend.hcl
# edit terraform.tfvars: alicloud_region, bucket names
terraform init -backend-config=bootstrap-backend.hcl
terraform apply
```

The state bucket (`gwc-infra-tf-state`) and its region
(`cn-hongkong`) come from `bootstrap-backend.hcl`. Note that the real
`*-backend.hcl` files are **gitignored** (`.gitignore`:
`infra/**/*-backend.hcl`) — the committed `.example` files are the
templates you copy.

### Credentials for the local apply

The `oss` backend resolves credentials in this order:

1. `-backend-config="access_key=..." -backend-config="secret_key=..."`
2. the `ALICLOUD_ACCESS_KEY` / `ALICLOUD_SECRET_KEY` env vars (or the
   `ALIBABA_CLOUD_*` equivalents)
3. the `profile` setting, which reads `~/.aliyun/config.json`

For local work, option 3 is usually the most convenient:

```bash
aliyun configure --profile default
# → writes ~/.aliyun/config.json
```

The profile name must match the `profile` key in the backend config.

> **Region gotcha.** An OSS bucket name is globally unique but its
> **region is not**. Pointing the backend at a region other than the
> bucket's fails with `Authorization header is invalid` — not with a
> clear "no such bucket". The Alicloud region is `cn-hongkong`
> everywhere in this repo for that reason.

---

## 3. Per-env stack

```bash
cd infra/alicloud/dev
cp alicloud.tfvars.example alicloud.tfvars   # gitignored
# fill in the GitHub App credentials, the API key, the bucket names
terraform init -backend-config=dev-backend.hcl
terraform apply
```

`dev-backend.hcl` is the operator-side copy of
`dev-backend.hcl.example`:

```bash
cp dev-backend.hcl.example dev-backend.hcl
```

The apply creates the KMS secrets (GitHub App id / private key /
installation id, webhook secret, OpenCode key, STT config) with
placeholder values. Set the real values afterwards:

```bash
aliyun kms PutSecretValue \
  --SecretName /blitzlog/dev/github-app/private-key \
  --SecretData "$(cat private-key.pem)" \
  --region cn-hongkong
```

### The agent network

Alicloud has **no default VPC per region** (unlike AWS, where every
account has one), so a VPC and at least one VSwitch must exist before
an ECS instance can be launched — `RunInstances` requires a
`VSwitchId`.

The bootstrap stack creates that network (`network.tf`): one VPC and
one VSwitch per zone. dev and prod **share** it — their security
groups keep them separate. (Alicloud rejects a second VPC with an
overlapping CIDR, so a per-env VPC would need a per-env CIDR; one
shared network sidesteps that.)

Copy the bootstrap's outputs into the env's tfvars:

```bash
cd infra/alicloud/bootstrap
terraform output vpc_id
terraform output vswitch_ids
```

```hcl
# infra/alicloud/dev/alicloud.tfvars
vpc_id      = "vpc-j6cx5ca2zktpkq7c89w8r"
vswitch_ids = ["vsw-...", "vsw-...", "vsw-..."]
```

Both are **required** — an empty value fails the plan with a clear
message rather than an ECS launch error at runtime.

> **The zone set is per-ACCOUNT, not per-region.** This account has
> `cn-hongkong-b`, `-c` and `-d` — there is **no** `cn-hongkong-a`,
> and creating a VSwitch there fails with `ResourceNotAvailable`
> ("Resource you requested is not available in this region or zone").
> Check yours with `aliyun vpc DescribeZones --RegionId <region>` and
> override `vswitch_zone_ids` in the bootstrap if they differ.

The secret names mirror the AWS SSM paths:

| KMS secret | TF variable |
|---|---|
| `/blitzlog/<env>/github-app/id` | `github_app_id` |
| `/blitzlog/<env>/github-app/private-key` | `github_app_private_key` |
| `/blitzlog/<env>/github-app/installation-id` | `github_app_installation_id` |
| `/blitzlog/<env>/github-webhook/secret` | `github_webhook_secret` |
| `/blitzlog/<env>/opencode/api-key` | `opencode_api_key` |
| `/blitzlog/<env>/stt/api-key` | `stt_api_key` |
| `/blitzlog/<env>/stt/model` | `stt_model` |
| `/blitzlog/<env>/stt/language` | `stt_language` |
| `/blitzlog/<env>/stt/models-bucket` | `stt_models_bucket_name` |

CI reads these same secrets (the "Render TF_VAR_* env from KMS" step),
so there is no second copy of a secret anywhere.

---

## 4. The image bake

The Alicloud ECS image is baked by the `alicloud-packer` job in
`release.yml`. It is gated off by default:

```bash
gh variable set ENABLE_ALICLOUD_BAKE --body true
```

It `needs: release`, because the bake pulls the agent container image
from ghcr.io and the `release` job is what pushes it. The two legs
read the same resolved version, so they cannot bake different agent
images.

The bake publishes the new image id to KMS at
`/blitzlog/<env>/agent-ami-id-ecs-ubuntu`, which is where the
Function Compute function looks it up at launch time. If the secret is
missing or the image is retired, the function falls back to the
public Ubuntu image — it degrades to "no baked agent image" rather
than failing the launch.

---

## 5. The deploy

```bash
gh variable set BLITZLOG_PROVIDER --body alicloud
gh variable set ENABLE_ALICLOUD_DEPLOY --body true
```

Then dispatch `release.yml` on the target ref. The deploy job:

1. assumes `blitzlog-deploy-role` via OIDC,
2. exports the credentials under both the `ALIBABA_CLOUD_*` (provider)
   and `ALICLOUD_*` (backend) names — the backend resolves its
   credentials before the provider is configured, so it needs its own
   names,
3. renders the `TF_VAR_*` values from KMS,
4. builds the FC bundle into `infra/alicloud/modules/core/build` with
   `requirements-alicloud.txt` (the alibaba SDKs do **not** ship to
   AWS),
5. plans and applies.

To go back to AWS:

```bash
gh variable delete BLITZLOG_PROVIDER     # it defaults to 'aws'
gh variable delete ENABLE_ALICLOUD_DEPLOY
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Authorization header is invalid` on `terraform init` | Wrong region for the state bucket (must be `cn-hongkong`), or no credentials exported. |
| `NoSuchBucket` / bucket not found | The bucket name is `gwc-infra-tf-state` (hyphenated). `gwc-infra-tfstate` does not exist. |
| `RegionId is empty` | `ALICLOUD_REGION` is not set and the CLI has no configured region. |
| The OIDC step produces no credentials | `vars.ALICLOUD_ACCOUNT_ID` unset, or the OIDC provider was never registered in the RAM console (§1a). |
| The KMS render step fails on a missing secret | The dev/prod stack has not been applied, or the secret still holds its placeholder value. |
| `packer init` cannot download the alicloud plugin | The `github.com/hashicorp/alicloud` Packer plugin is not reachable from the runner. |

## Known limitations

- **No token-refresh chain.** The AWS path schedules an EventBridge
  Scheduler chain that re-mints the GitHub App installation token
  every 30 minutes (issue #145). Aliyun has no equivalent wired up,
  so the token is minted once at launch and the 1-hour GitHub cap is
  operationally visible on long runs.
- **No Alicloud scheduler teardown.** The AWS watchdog deletes the
  token-refresh schedule group before it terminates the instance;
  there is nothing equivalent to delete here.
- **The Packer build instance's network** is not yet modelled: the
  `packer_security_group_id` and `source_image_id` variables default
  to empty and must be supplied for the bake to launch.
