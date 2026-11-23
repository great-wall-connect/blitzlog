# Deploying to Alicloud

Issue #125. This is the operator runbook for the Alicloud path —
the second cloud blitzlog supports.

The Alicloud tree is `infra/alicloud/{bootstrap,dev,prod,modules/core}/`.
It mirrors the AWS tree one-for-one, with the same resource names and
the same `/blitzlog/<env>/...` secret paths (the Alicloud `SecretStore`
implementation is the **Aliyun OOS Parameter Store**, and the paths
mirror the AWS SSM paths minus the leading slash — OOS names its
parameters `blitzlog/<env>/...`).

> **Why OOS and not KMS Secrets Manager.** On KMS 3.0 a secret can
> only be created inside a dedicated KMS instance: `CreateSecret`
> without a `DKMSInstanceId` is rejected with `UnsupportedOperation`
> ("This action is not supported"), and a KMS instance is a billable
> resource (USD 500/month subscription, USD 4.50/day pay-as-you-go,
> plus a secret-quota pack). The OOS Parameter Store is free, needs
> no KMS instance, and its API maps 1:1 onto the `SecretStore`
> interface. The per-env CMK (`modules/core/kms.tf`) is still
> created — it encrypts the parameters, and a KMS *default master
> key* is free of charge.

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

### 1c. The MNS / Simple Message Queue service

The function's dead-letter queue lives in MNS (rebranded *Simple
Message Queue* on the international site). The bootstrap stack creates
the **subscription** for it (`services.tf`), but the service itself is
opened from the console — the subscription order does not flip the
service flag on the international site, so `CreateQueue` keeps
failing with `ServiceNotEnable` ("Service not open") until you do:

1. Open the MNS / SMQ console for the account
   (<https://mns.console.aliyun.com/> on the China site,
   <https://mns.console.alibabacloud.com/> on the international
   site).
2. Click **开通** (activate) for the region (`cn-hongkong`).

Activating the service is free. It cannot be closed again.

### 1d. The agent network ids (after the first bootstrap apply)

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

Creates the shared OSS buckets (agent-logs, STT models), the two CI
roles (the Packer build role and the deploy role), and the **shared
agent VPC + VSwitches** (`network.tf`). The per-env KMS CMK lives in
the per-env stack (`modules/core/kms.tf`).

It also **opens two account-level services** (`services.tf`), which
the per-env stacks need before they can create anything:

| Service | Why | Failure without it |
|---|---|---|
| API Gateway | the webhook group / API / app live in it | `ServiceUnOpen` — "Your Gateway service has not been opened." Run the bootstrap apply (`infra/alicloud/bootstrap`) — it opens it (`services.tf`). |
| MNS | the function's dead-letter queue lives in it | `ServiceNotEnable` — "Service not open". The bootstrap apply creates the *subscription*, but the service is opened from the MNS console — see §1c. |

Both are free and one-way (a service cannot be closed once opened),
and opening them means agreeing to the product's Terms of Service —
which is why it happens in this operator-run stack and not in the CI
deploy path.

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
cp terraform.tfvars.example terraform.tfvars   # gitignored
# fill in the GitHub App credentials, the API key, the bucket names
terraform init -backend-config=dev-backend.hcl
terraform apply
```

`dev-backend.hcl` is the operator-side copy of
`dev-backend.hcl.example`:

```bash
cp dev-backend.hcl.example dev-backend.hcl
```

The apply creates the OOS parameters (GitHub App id / private key /
installation id, webhook secret, OpenCode key, STT config) with
placeholder values. Set the real values afterwards:

```bash
aliyun oos UpdateSecretParameter \
  --Name blitzlog/dev/github-app/private-key \
  --Value "$(cat private-key.pem)" \
  --region cn-hongkong
```

The parameter names carry no leading slash — that is how OOS names
them, and the RAM grants in `infra/alicloud/modules/core/ram.tf`
spell the ARNs as `acs:oos:<region>:<account>:secretparameter/<name>`.

### The webhook URL

The stack publishes the URL the GitHub App POSTs to as the
`webhook_url` output:

```bash
cd infra/alicloud/dev && terraform output webhook_url
# https://<api-gateway-group>.cn-hongkong.alicloudapi.com/
```

Three details of the Alicloud webhook path, each of which cost a
debugging round trip:

- The API is **anonymous** (`auth_type = "ANONYMOUS"`). GitHub
  cannot present API Gateway app credentials, and the `APP` default
  answers every request with "Header `X-Ca-Key` is Required". The
  authentication that matters is the GitHub signature the function
  verifies itself.
- The API is **deployed to the RELEASE stage** (`stage_names`). An
  API that is not deployed to a stage has no route and the group's
  domain answers 404.
- The function has an **HTTP trigger**, and the API's FC backend is
  the `HttpTrigger` kind. The `FCEvent` backend hands the function
  the bare body and drops the headers — the `X-Hub-Signature-256`
  would never arrive, so the signature check could not pass.

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
# infra/alicloud/dev/terraform.tfvars
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

The parameter names mirror the AWS SSM paths (without the leading
slash):

| OOS parameter | TF variable |
|---|---|
| `blitzlog/<env>/github-app/id` | `github_app_id` |
| `blitzlog/<env>/github-app/private-key` | `github_app_private_key` |
| `blitzlog/<env>/github-app/installation-id` | `github_app_installation_id` |
| `blitzlog/<env>/github-webhook/secret` | `github_webhook_secret` |
| `blitzlog/<env>/opencode/api-key` | `opencode_api_key` |
| `blitzlog/<env>/stt/api-key` | `stt_api_key` |
| `blitzlog/<env>/stt/model` | `stt_model` |
| `blitzlog/<env>/stt/language` | `stt_language` |
| `blitzlog/<env>/stt/models-bucket` | `stt_models_bucket_name` |

CI reads these same parameters (the "Render TF_VAR_* env from OOS"
step), so there is no second copy of a secret anywhere.

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

The bake publishes the new image id to the OOS Parameter Store at
`blitzlog/<env>/agent-ami-id-ecs-ubuntu`, which is where the
Function Compute function looks it up at launch time. If the secret is
missing or the image is retired, the function falls back to the
public Ubuntu image — it degrades to "no baked agent image" rather
than failing the launch.

### Running the bake locally

The CI job needs three repo variables — the VPC and the VSwitch the
bake instance is launched into, and the elastic IP it gets its
public address from:

```bash
gh variable set ALICLOUD_VPC_ID --body "$(cd infra/alicloud/bootstrap && terraform output -raw vpc_id)"
gh variable set ALICLOUD_VSWITCH_ID --body vsw-j6cnyi1xrg6gbi3zc5ddg
gh variable set ALICLOUD_EIP_ID --body "$(cd infra/alicloud/bootstrap && terraform output -raw packer_eip_id)"
```

The VSwitch must be in a zone where the ARM64 instance type is
offered (`cn-hongkong-b` and `cn-hongkong-d` today; `cn-hongkong-c`
has no ARM64 spot inventory).

The EIP is not optional. The build instance lives in a VPC, and the
`alicloud-ecs` builder only reaches a VPC instance over an elastic
IP — it cannot use the instance's own public IP, and it cannot
allocate the EIP itself either: it sends the same
`internet_max_bandwidth_out` to both the RunInstances call and the
AllocateEipAddress call, and the two constraints collide (the EIP
call rejects a bandwidth below 1, while any non-zero bandwidth makes
ECS hand the instance an ECS-managed public IP that cannot be
replaced by an EIP). The bootstrap stack therefore owns the EIP and
the bake takes it as `-var eip_id=...`.

Locally, the same bake runs with:

```bash
cd infra/alicloud/packer
packer init agent-ecs-ubuntu.pkr.hcl

# The plugin reads the access key from ALICLOUD_ACCESS_KEY /
# ALICLOUD_SECRET_KEY and the STS token from SECURITY_TOKEN (not
# ALICLOUD_SECURITY_TOKEN), so export all three.
source ../../scripts/aliyun-env.sh
export SECURITY_TOKEN="$ALICLOUD_SECURITY_TOKEN"

packer build -force \
  -var environment=dev \
  -var stt_models_bucket=gwc-blitzlog-stt-models \
  -var agent_image_tag=v0.7.0 \
  -var vpc_id=vpc-j6csy1qafim6vio53korm \
  -var vswitch_id=vsw-j6cnyi1xrg6gbi3zc5ddg \
  -var zone_id=cn-hongkong-b \
  -var eip_id=eip-j6cba1fvruw554vebdt43 \
  -var github_token="$(grep -oP 'GITHUB_TOKEN=\K\S+' ../../aws/packer/.env)" \
  agent-ecs-ubuntu.pkr.hcl
```

Two things the bake needs that the AWS leg does not:

- **An ARM64 source image and instance type.** The ghcr agent
  container image is ARM64-only, so an x86 source image refuses to
  boot it (`InvalidInstanceType.NotSupported`). The default
  `source_image_id` is the ARM64 Ubuntu 26.04 image.
- **The whisper model in OSS.** The bake pre-downloads
  `models/ggml-<model>.bin` from the STT bucket, so that object must
  exist before the bake — the AWS side uploads it from
  `terraform apply`; the Alicloud side has no equivalent
  provisioner, so upload it once with `aliyun ossutil cp`.
- **A RAM role on the build instance.** The model download runs
  `ossutil` in `EcsRamRole` mode, which reads the instance's role
  credentials from the metadata service — the AWS analogue of the
  AMI's instance profile. The bake attaches the packer build role
  (`ecs_ram_role_name`), which carries `oss:GetObject` on the STT
  bucket and `ram:PassRole` on itself. Without it the download has
  no credentials.

`packer init` needs the plugin from the GitHub releases API. When
that API is rate-limited, install the release asset by hand into
`~/.config/packer/plugins/github.com/hashicorp/alicloud/` — the
binary is named `packer-plugin-alicloud_<version>_x5.0_linux_amd64`
and needs a sibling `<binary>_SHA256SUM` file holding the binary's
own sha256, or Packer refuses to load it as "possibly unsafe".

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
3. renders the `TF_VAR_*` values from the OOS Parameter Store,
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
| The OOS render step fails on a missing parameter | The dev/prod stack has not been applied, or the parameter still holds its placeholder value. |
| `ServiceUnOpen` / "Your Gateway service has not been opened." | The API Gateway product is not open. Run the bootstrap apply (`infra/alicloud/bootstrap`) — it opens it (`services.tf`). |
| `ServiceNotEnable` / "Service not open" | The MNS (Simple Message Queue) product is not open. Activate it from the MNS console — see §1c. The bootstrap apply only creates the subscription. |
| `InvalidAppName` / `InvalidGroupName` on the API Gateway resources | The names contain a hyphen. API Gateway names accept letters, digits and underscores only. |
| `packer init` cannot download the alicloud plugin | The `github.com/hashicorp/alicloud` Packer plugin is not reachable from the runner. |
| `Forbidden.RiskControl` on the bake's "Creating instance" | The account cannot purchase ECS instances — no payment method is bound and the balance is 0 (see Known limitations). |
| `MissingSecurityToken` from the `aliyun` CLI | The CLI does not read the `ALIBABA_CLOUD_*` environment variables for authentication. It needs a profile file in `StsToken` mode (the CI job writes one; locally `aliyun configure --profile default` does). |
| `InvalidInstanceType.NotSupported` / "not supported by the image architecture" | An x86 instance type with an ARM64 source image (or the reverse). The agent image is ARM64-only, so both the bake instance type and the runtime spot types must be ARM64 (`ecs.g8y.*`). |
| `EIP_CAN_NOT_ASSOCIATE_WITH_PUBLIC_IP` on the bake's "Error associating EIP" | The build instance already had an ECS-managed public IP ("natpublicip") — the template asks ECS for one, and Alicloud will not replace it with an EIP. The instance must get its public address from the EIP alone: leave `internet_charge_type` / `internet_max_bandwidth_out` unset and pass `-var eip_id=...`. |
| `IllegalParam.Bandwidth` on the bake's "Error allocating EIP" | The builder allocates the EIP with the same bandwidth the instance uses, and the EIP call rejects a bandwidth below 1. Same fix as above: pass the EIP in with `eip_id` instead of letting the builder allocate one. |
| `region must be set in sign version 4` on the bake's model download | ossutil signs with SigV4 and needs the region as its own parameter — the `-e` endpoint is not enough. The region must reach ossutil explicitly (`--region`), and the instance needs a RAM role for the `EcsRamRole` credentials. |

## Known limitations

- **No token-refresh chain.** The AWS path schedules an EventBridge
  Scheduler chain that re-mints the GitHub App installation token
  every 30 minutes (issue #145). Aliyun has no equivalent wired up,
  so the token is minted once at launch and the 1-hour GitHub cap is
  operationally visible on long runs.
- **No Alicloud scheduler teardown.** The AWS watchdog deletes the
  token-refresh schedule group before it terminates the instance;
  there is nothing equivalent to delete here.
- **The Packer build instance's network** is resolved from the
  bootstrap stack's VPC: the bake passes `vpc_id`, `vswitch_id`,
  `zone_id` and `eip_id` explicitly, so the plugin does not create a
  throwaway VPC of its own. In CI those come from the repo variables
  `ALICLOUD_VPC_ID` / `ALICLOUD_VSWITCH_ID` / `ALICLOUD_EIP_ID`;
  locally they are `-var` flags. The EIP is not optional: the
  `alicloud-ecs` builder only reaches a VPC instance over an elastic
  IP, and it cannot allocate one itself (its own EIP allocation and
  the RunInstances call share one bandwidth setting, and the two
  constraints contradict each other — see the troubleshooting rows
  above). The source image is the newest ARM64 Ubuntu 26.04 in
  the region (the agent container image is ARM64-only, so an x86
  source image would refuse to boot it).
- **ECS instance creation is blocked on this account.**
  `RunInstances` / `CreateInstance` fail with
  `Forbidden.RiskControl` — *"This operation is forbidden by Aliyun
  RiskControl system."* Aliyun's own error-code documentation
  (`GetErrorCodeSolutions` for `Forbidden.RiskControl` on Ecs) says
  this means *"您的账户出现异常，详情请联系客服 … 账号存在交易安全
  问题"* and that it is cleared only by **opening a support ticket**
  with the account holder's ID / business licence.

  It is an account-level block on ECS purchases, not a template or
  configuration problem. It reproduces identically for every instance
  type, image, zone, charge type and security group, while the free
  resources the bake needs (VPC, VSwitch, security group, key pair)
  are created without complaint. The underlying cause is visible in
  the billing API: the account has **no payment method bound** and
  **no balance** —

  ```
  aliyun bssopenapi QueryPrepaidCards --endpoint business.ap-southeast-1.aliyuncs.com
    → {"PrepaidCard":[]}
  aliyun bssopenapi QueryAccountBalance --endpoint business.ap-southeast-1.aliyuncs.com
    → {"AvailableAmount":"0.00","CreditAmount":"0.00","AvailableCashAmount":"0.00"}
  ```

  So the fix is on the account, not in the repo: bind a payment
  method (or top up the balance) in the billing console, and
  separately open the support ticket the error documentation asks
  for. Note the balance query needs the **international** billing
  endpoint (`business.ap-southeast-1.aliyuncs.com`) — the domestic
  `business.aliyuncs.com` rejects an international account with
  `NotApplicable: the caller site matches the API domain regionId`.

  Until then the bake cannot complete, and neither can the runtime
  agent instances (the FC function launches the same way). Everything
  else in the pipeline — the Packer template, the build scripts, the
  RAM policies and the OOS publish step — is verified working up to
  the point of instance creation.
