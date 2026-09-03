import base64
import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import boto3
import jwt
import requests
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SSM_PATH = "/blitzlog"

_HERE = os.path.dirname(os.path.abspath(__file__))
_WHISPER_STT_SHIM_CANDIDATES = (
    os.path.join(_HERE, "packages", "whisper-stt-shim", "server.js"),
    os.path.join(_HERE, "..", "packages", "whisper-stt-shim", "server.js"),
)
WHISPER_STT_SHIM_SOURCE = ""
for _candidate in _WHISPER_STT_SHIM_CANDIDATES:
    try:
        with open(_candidate, "r", encoding="utf-8") as _f:
            WHISPER_STT_SHIM_SOURCE = _f.read()
            break
    except OSError:
        continue

_WHISPER_STT_SHIM_PACKAGE_JSON = """{
  "name": "@blitzlog/whisper-stt-shim",
  "version": "0.1.0",
  "private": true,
  "description": "Whisper-compatible HTTP shim wrapping whisper.cpp on the blitzlog EC2 agent instance.",
  "main": "server.js",
  "engines": { "node": ">=20" },
  "scripts": { "start": "node server.js" },
  "dependencies": {
    "busboy": "^1.6.0",
    "ffmpeg-static": "^5.2.0"
  }
}
"""

ec2 = boto3.client("ec2", config=Config(retries={"max_attempts": 1}))
s3 = boto3.client("s3")
ssm = boto3.client("ssm")

BOT_POOL_LOCK_PREFIX = "bot-pool-locks"
BOT_POOL_LOCK_TTL_HOURS = 4


def get_ssm_param(name: str, with_decryption: bool = True) -> str:
    resp = ssm.get_parameter(
        Name=f"{SSM_PATH}/{name}",
        WithDecryption=with_decryption,
    )
    return resp["Parameter"]["Value"]


def list_bot_pool(sender_login: str) -> dict[str, str]:
    paginator = ssm.get_paginator("get_parameters_by_path")
    pages = paginator.paginate(
        Path=f"{SSM_PATH}/users/{sender_login}/telegram/pool",
        WithDecryption=True,
    )
    bots: dict[str, str] = {}
    for page in pages:
        for param in page.get("Parameters", []):
            name = param["Name"].split("/")[-1]
            bots[name] = param["Value"]
    return bots


def get_telegram_user_id(sender_login: str) -> str | None:
    try:
        resp = ssm.get_parameter(
            Name=f"{SSM_PATH}/users/{sender_login}/telegram/allowed-user-id",
            WithDecryption=False,
        )
        return resp["Parameter"]["Value"]
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ParameterNotFound", "404"):
            return None
        raise


def _lock_key(sender_login: str, bot_name: str) -> str:
    return f"{BOT_POOL_LOCK_PREFIX}/{sender_login}/{bot_name}.json"


def _get_lock(sender_login: str, bot_name: str, bucket: str) -> dict | None:
    try:
        resp = s3.get_object(Bucket=bucket, Key=_lock_key(sender_login, bot_name))
        data = json.loads(resp["Body"].read().decode())
        acquired_at = datetime.fromisoformat(data["acquired_at"])
        if datetime.now(timezone.utc) - acquired_at > timedelta(
            hours=BOT_POOL_LOCK_TTL_HOURS
        ):
            logger.info(
                "Stale lock for %s (acquired %s), treating as free",
                bot_name,
                data["acquired_at"],
            )
            return None
        return data
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def acquire_bot_token(
    sender_login: str, instance_id: str, repo: str, issue_number: int
) -> tuple[str, str] | None:
    bucket = os.environ.get("S3_LOGS_BUCKET", "<your-agent-logs-bucket>")
    pool = list_bot_pool(sender_login)
    if not pool:
        logger.error("No bot pool configured for user %s", sender_login)
        return None

    for bot_name in sorted(pool.keys()):
        lock = _get_lock(sender_login, bot_name, bucket)
        if lock is not None:
            logger.info(
                "Bot %s (user %s) is locked by %s",
                bot_name,
                sender_login,
                lock.get("instance_id", "unknown"),
            )
            continue

        lock_payload = json.dumps(
            {
                "instance_id": instance_id,
                "issue_number": issue_number,
                "repo": repo,
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        try:
            s3.put_object(
                Bucket=bucket,
                Key=_lock_key(sender_login, bot_name),
                Body=lock_payload.encode(),
            )
        except ClientError as e:
            logger.warning(
                "Failed to write lock for %s/%s: %s", sender_login, bot_name, e
            )
            continue

        logger.info(
            "Acquired bot %s for user %s, issue #%d",
            bot_name,
            sender_login,
            issue_number,
        )
        return (bot_name, pool[bot_name])

    logger.warning("All bots in pool for user %s are locked", sender_login)
    return None


def _update_lock_instance_id(
    sender_login: str, bot_name: str, instance_id: str, repo: str, issue_number: int
) -> None:
    bucket = os.environ.get("S3_LOGS_BUCKET", "<your-agent-logs-bucket>")
    lock_payload = json.dumps(
        {
            "instance_id": instance_id,
            "issue_number": issue_number,
            "repo": repo,
            "acquired_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    try:
        s3.put_object(
            Bucket=bucket,
            Key=_lock_key(sender_login, bot_name),
            Body=lock_payload.encode(),
        )
    except ClientError as e:
        logger.warning("Failed to update lock for %s/%s: %s", sender_login, bot_name, e)


def get_github_app_token(repo: str) -> str:
    app_id = get_ssm_param("github-app/id", with_decryption=False)
    private_key_b64 = get_ssm_param("github-app/private-key")
    private_key = base64.b64decode(private_key_b64).decode()
    installation_id = get_ssm_param("github-app/installation-id", with_decryption=False)

    now = datetime.now(timezone.utc)
    app_jwt = jwt.encode(
        {
            "iat": now,
            "exp": now + timedelta(minutes=10),
            "iss": app_id,
        },
        private_key,
        algorithm="RS256",
    )

    repo_name = repo.split("/", 1)[-1] if "/" in repo else repo
    request_body = {"repositories": [repo_name]}

    resp = requests.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=request_body,
    )
    if not resp.ok:
        body_snippet = resp.text[:500] if resp.text else ""
        logger.error(
            "GitHub App access_tokens request failed: status=%s app_id=%s "
            "installation_id=%s repo=%s request_body=%s response_body=%s",
            resp.status_code,
            app_id,
            installation_id,
            repo,
            request_body,
            body_snippet,
        )
        resp.raise_for_status()
    return resp.json()["token"]


def verify_github_signature(secret: str, payload: bytes, signature: str) -> bool:
    if not signature.startswith("sha256="):
        return False
    expected = (
        "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature)


def extract_event_data(payload: dict) -> dict | None:
    if "detail" in payload:
        detail = payload["detail"]
        action = detail.get("action", "")
        issue = detail.get("issue", {})
        repo = payload.get("repository", detail.get("repository", {}))
        sender = detail.get("sender", payload.get("sender", {}))
    else:
        action = payload.get("action", "")
        issue = payload.get("issue", {})
        repo = payload.get("repository", {})
        sender = payload.get("sender", {})

    if not action or not issue:
        return None

    return {
        "action": action,
        "issue_number": issue.get("number"),
        "repo_full_name": repo.get("full_name", ""),
        "labels": {label["name"] for label in issue.get("labels", [])},
        "sender_login": sender.get("login", ""),
        "sender_id": sender.get("id", ""),
    }


def get_latest_al2023_ami() -> str:
    resp = ssm.get_parameter(
        Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
    )
    return resp["Parameter"]["Value"]


def get_instance_profile_arn() -> str:
    iam = boto3.client("iam")
    resp = iam.get_instance_profile(
        InstanceProfileName=os.environ["EC2_INSTANCE_PROFILE_NAME"]
    )
    return resp["InstanceProfile"]["Arn"]


SPOT_INSTANCE_TYPES = ["t4g.medium", "t4g.large", "t4g.xlarge"]


def get_spot_prices(instance_types: list[str]) -> list[tuple[str, str, float]]:
    try:
        resp = ec2.describe_spot_price_history(
            InstanceTypes=instance_types,
            ProductDescriptions=["Linux/UNIX"],
            StartTime=datetime.now(timezone.utc),
            EndTime=datetime.now(timezone.utc),
        )
        results: list[tuple[str, str, float]] = []
        for entry in resp.get("SpotPriceHistory", []):
            price = float(entry["SpotPrice"])
            results.append((entry["AvailabilityZone"], entry["InstanceType"], price))
        results.sort(key=lambda x: x[2])
        return results
    except ClientError as e:
        logger.warning("DescribeSpotPriceHistory failed: %s", e)
        return []


def get_az_subnet_map() -> dict[str, str]:
    vpc_id = os.environ.get("VPC_ID", "")
    if not vpc_id:
        return {}
    try:
        resp = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
        return {
            subnet["AvailabilityZone"]: subnet["SubnetId"]
            for subnet in resp.get("Subnets", [])
        }
    except ClientError as e:
        logger.warning("DescribeSubnets failed: %s", e)
        return {}


def launch_ec2_spot_instance(
    repo: str,
    issue_number: int,
    github_token: str,
    mode: str,
    user_data_builder,
    sender_login: str = "",
    sender_id: str = "",
) -> str:
    ephemeral_param = f"/blitzlog/ephemeral/github-token-{issue_number}"
    ssm.put_parameter(
        Name=ephemeral_param,
        Value=github_token,
        Type="SecureString",
        Overwrite=True,
    )

    user_data = user_data_builder(repo, issue_number, sender_login, sender_id)

    s3_bucket = os.environ.get("S3_LOGS_BUCKET", "<your-agent-logs-bucket>")
    s3_key = f"user-data/{mode}-issue-{issue_number}-{uuid.uuid4().hex[:8]}.sh"
    s3.put_object(Bucket=s3_bucket, Key=s3_key, Body=user_data.encode())
    logger.info(
        "Uploaded user-data to s3://%s/%s (%d bytes)",
        s3_bucket,
        s3_key,
        len(user_data.encode()),
    )

    downloader = _build_s3_downloader_script(s3_bucket, s3_key)
    user_data_b64 = base64.b64encode(downloader.encode()).decode()

    image_id = get_latest_al2023_ami()
    instance_profile_arn = get_instance_profile_arn()

    common_params = {
        "MinCount": 1,
        "MaxCount": 1,
        "ImageId": image_id,
        "SecurityGroupIds": [os.environ["EC2_SECURITY_GROUP_ID"]],
        "UserData": user_data_b64,
        "InstanceInitiatedShutdownBehavior": "terminate",
        "IamInstanceProfile": {"Arn": instance_profile_arn},
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {
                    "VolumeSize": 20,
                    "VolumeType": "gp3",
                    "DeleteOnTermination": True,
                },
            },
        ],
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Purpose", "Value": "autonomous-agent"},
                    {"Key": "Mode", "Value": mode},
                    {"Key": "Issue", "Value": str(issue_number)},
                    {
                        "Key": "Name",
                        "Value": f"opencode-agent-{mode}-issue-{issue_number}",
                    },
                ],
            },
        ],
    }

    az_subnet_map = get_az_subnet_map()
    spot_prices = get_spot_prices(SPOT_INSTANCE_TYPES)

    if spot_prices and az_subnet_map:
        for az, instance_type, price in spot_prices:
            subnet_id = az_subnet_map.get(az)
            if not subnet_id:
                logger.info("No subnet in %s, skipping", az)
                continue
            try:
                logger.info("Trying spot %s in %s ($%.6f)...", instance_type, az, price)
                resp = ec2.run_instances(
                    **common_params,
                    SubnetId=subnet_id,
                    InstanceType=instance_type,
                    InstanceMarketOptions={
                        "MarketType": "spot",
                        "SpotOptions": {
                            "SpotInstanceType": "one-time",
                            "InstanceInterruptionBehavior": "terminate",
                        },
                    },
                )
                return resp["Instances"][0]["InstanceId"]
            except ClientError as e:
                logger.warning("Spot %s in %s failed: %s", instance_type, az, e)
                continue
    else:
        fallback_subnet = os.environ.get("EC2_SUBNET_ID", "")
        for instance_type in SPOT_INSTANCE_TYPES:
            try:
                logger.info("Trying spot %s (fallback order)...", instance_type)
                resp = ec2.run_instances(
                    **common_params,
                    SubnetId=fallback_subnet,
                    InstanceType=instance_type,
                    InstanceMarketOptions={
                        "MarketType": "spot",
                        "SpotOptions": {
                            "SpotInstanceType": "one-time",
                            "InstanceInterruptionBehavior": "terminate",
                        },
                    },
                )
                return resp["Instances"][0]["InstanceId"]
            except ClientError as e:
                logger.warning("Spot %s failed: %s", instance_type, e)
                continue

    logger.warning("All spot types failed, falling back to on-demand t4g.medium")
    resp = ec2.run_instances(
        **common_params,
        SubnetId=os.environ.get("EC2_SUBNET_ID", ""),
        InstanceType="t4g.medium",
    )
    return resp["Instances"][0]["InstanceId"]


def _read_secrets_from_ssm_script(issue_number: int) -> str:
    return f"""
TOKEN=$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/dynamic/instance-identity/document | python3 -c "import sys,json; print(json.load(sys.stdin)['region'])")
export AWS_DEFAULT_REGION=$REGION

_CC_GITHUB_TOKEN=$(aws ssm get-parameter --name "/blitzlog/ephemeral/github-token-{issue_number}" --with-decryption --query Parameter.Value --output text --region "$REGION")
export _CC_GITHUB_TOKEN

OPENCODE_API_KEY=$(aws ssm get-parameter --name "/blitzlog/opencode/api-key" --with-decryption --query Parameter.Value --output text --region "$REGION")
export OPENCODE_API_KEY

STT_API_URL=$(aws ssm get-parameter --name "/blitzlog/stt/api-url" --query Parameter.Value --output text --region "$REGION")
export STT_API_URL
STT_API_KEY=$(aws ssm get-parameter --name "/blitzlog/stt/api-key" --with-decryption --query Parameter.Value --output text --region "$REGION")
export STT_API_KEY
STT_MODEL=$(aws ssm get-parameter --name "/blitzlog/stt/model" --query Parameter.Value --output text --region "$REGION")
export STT_MODEL
STT_LANGUAGE=$(aws ssm get-parameter --name "/blitzlog/stt/language" --query Parameter.Value --output text --region "$REGION")
export STT_LANGUAGE
STT_MODELS_BUCKET=$(aws ssm get-parameter --name "/blitzlog/stt/models-bucket" --query Parameter.Value --output text --region "$REGION")
export STT_MODELS_BUCKET
"""


def _decode_api_errors_script() -> str:
    return """
# Decode known LLM-provider API errors into actionable log lines.
# Run after the opencode process exits and before session export.
if [ -f "$LOG_FILE" ] && grep -qE "insufficient_balance|insufficient balance|\\(1008\\)" "$LOG_FILE"; then
    log "ACTIONABLE: LLM provider returned insufficient balance / HTTP 1008."
    log "ACTIONABLE: The token-plan account for the configured provider (${OPENCODE_MODEL:-unknown}) has zero credits."
    log "ACTIONABLE: Top up at the provider console (e.g. https://platform.minimax.io) before retrying."
    log "ACTIONABLE: Verify the API key at SSM parameter /blitzlog/opencode/api-key belongs to a funded account."
fi
if [ -f "$LOG_FILE" ] && grep -qE "Unauthorized|invalid_api_key|\\(401\\)|Authentication" "$LOG_FILE"; then
    log "ACTIONABLE: LLM provider rejected the API key as unauthorized (HTTP 401)."
    log "ACTIONABLE: Rotate /blitzlog/opencode/api-key in SSM and re-run terraform apply."
fi
if [ -f "$LOG_FILE" ] && grep -qE "rate.?limit|quota.?exceeded|too.?many.?requests|\\(429\\)" "$LOG_FILE"; then
    log "ACTIONABLE: LLM provider returned a rate-limit / quota error (HTTP 429)."
    log "ACTIONABLE: Wait for the quota window to reset or upgrade the plan, then re-trigger."
fi
"""


def _install_system_packages_script() -> str:
    return """
dnf install -y spal-release
dnf install -y git ripgrep amazon-ssm-agent
dnf install -y 'dnf-command(config-manager)'
dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo
dnf install -y gh
systemctl start amazon-ssm-agent || true
hash -r
"""


def _configure_git_script(git_user_name: str = "", git_user_email: str = "") -> str:
    identity = ""
    if git_user_name:
        identity = f"""
git config --global user.name "{git_user_name}"
git config --global user.email "{git_user_email}"
"""
    return f"""
mkdir -p /root/.git-credentials.d
echo "https://x-access-token:${{_CC_GITHUB_TOKEN}}@github.com" > /root/.git-credentials.d/github
chmod 600 /root/.git-credentials.d/github
git config --global credential.helper 'store --file /root/.git-credentials.d/github'
{identity}
echo "${{_CC_GITHUB_TOKEN}}" | gh auth login --with-token
export GITHUB_TOKEN="${{_CC_GITHUB_TOKEN}}"
"""


def _install_opencode_script() -> str:
    return """
curl -fsSL https://opencode.ai/install | bash
export PATH=/root/.opencode/bin:$PATH
hash -r
opencode --version
"""


_WHISPER_CPP_VERSION = "v1.7.6"
_WHISPER_CPP_RELEASE_URL = (
    f"https://github.com/ggml-org/whisper.cpp/releases/download/{_WHISPER_CPP_VERSION}"
    f"/whisper-bin-aarch64-linux-gnu.zip"
)
_WHISPER_CPP_RELEASE_FALLBACK_URL = (
    f"https://github.com/ggml-org/whisper.cpp/releases/download/{_WHISPER_CPP_VERSION}"
    f"/whisper-bin-aarch64-linux-gnu.tar.gz"
)
_WHISPER_CPP_SOURCE_TARBALL_URL = f"https://github.com/ggml-org/whisper.cpp/archive/refs/tags/{_WHISPER_CPP_VERSION}.tar.gz"


def _install_whisper_stt_script() -> str:
    shim_source = WHISPER_STT_SHIM_SOURCE
    package_json = _WHISPER_STT_SHIM_PACKAGE_JSON
    systemd_unit = (
        "[Unit]\n"
        "Description=Blitzlog whisper.cpp STT shim\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "User=root\n"
        "WorkingDirectory=/opt/whisper-stt\n"
        "Environment=HOST=127.0.0.1\n"
        "Environment=PORT=7878\n"
        "Environment=WHISPER_CLI=/opt/whisper-stt/bin/whisper-cli\n"
        "EnvironmentFile=-/etc/blitzlog/whisper-stt.env\n"
        "ExecStart=/usr/bin/node server.js\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "StandardOutput=append:/var/log/whisper-stt-shim.log\n"
        "StandardError=append:/var/log/whisper-stt-shim.log\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    return f"""
log "Installing whisper.cpp STT backend..."

mkdir -p /opt/whisper-stt/bin /opt/whisper-stt/models /opt/whisper-stt/runtime
cd /opt/whisper-stt

# 1. Acquire whisper.cpp CLI binary.
#    Prefer prebuilt release; fall back to building from source if the
#    prebuilt asset is unavailable for the current release tag.
WHISPER_CLI=/opt/whisper-stt/bin/whisper-cli
if [ ! -x "$WHISPER_CLI" ]; then
    log "Downloading whisper.cpp {_WHISPER_CPP_VERSION} prebuilt (aarch64-linux-gnu)..."
    if curl -fsSL "{_WHISPER_CPP_RELEASE_URL}" -o /tmp/whisper-prebuilt.zip; then
        dnf install -y unzip || true
        unzip -q -o /tmp/whisper-prebuilt.zip -d /tmp/whisper-prebuilt
        find /tmp/whisper-prebuilt -name whisper-cli -type f -exec cp {{}} "$WHISPER_CLI" \\;
        chmod +x "$WHISPER_CLI"
    elif curl -fsSL "{_WHISPER_CPP_RELEASE_FALLBACK_URL}" -o /tmp/whisper-prebuilt.tar.gz; then
        tar -xzf /tmp/whisper-prebuilt.tar.gz -C /tmp/whisper-prebuilt
        find /tmp/whisper-prebuilt -name whisper-cli -type f -exec cp {{}} "$WHISPER_CLI" \\;
        chmod +x "$WHISPER_CLI"
    else
        log "Prebuilt download failed; building whisper.cpp from source (this takes a few minutes)..."
        dnf install -y cmake gcc gcc-c++ make
        curl -fsSL "{_WHISPER_CPP_SOURCE_TARBALL_URL}" -o /tmp/whisper-src.tar.gz
        tar -xzf /tmp/whisper-src.tar.gz -C /opt
        cmake -S /opt/whisper.cpp-{_WHISPER_CPP_VERSION.lstrip('v')} -B /opt/whisper.cpp-{_WHISPER_CPP_VERSION.lstrip('v')}/build -DCMAKE_BUILD_TYPE=Release
        cmake --build /opt/whisper.cpp-{_WHISPER_CPP_VERSION.lstrip('v')}/build --config Release -j$(nproc)
        cp /opt/whisper.cpp-{_WHISPER_CPP_VERSION.lstrip('v')}/build/bin/whisper-cli "$WHISPER_CLI"
    fi
fi
"$WHISPER_CLI" --help > /dev/null && log "whisper-cli ready: $("$WHISPER_CLI" --help 2>&1 | head -1)"

# 2. Download the configured model from the blitzlog-stt-models S3 bucket.
MODEL_FILE="ggml-${{STT_MODEL}}.bin"
MODEL_DEST="/opt/whisper-stt/models/${{MODEL_FILE}}"
if [ ! -f "$MODEL_DEST" ]; then
    log "Downloading whisper model ${{MODEL_FILE}} from s3://${{STT_MODELS_BUCKET}}/models/..."
    aws s3 cp "s3://${{STT_MODELS_BUCKET}}/models/${{MODEL_FILE}}" "$MODEL_DEST" --region "${{AWS_DEFAULT_REGION}}"
fi
test -s "$MODEL_DEST" && log "Whisper model ready: $MODEL_DEST ($(du -h "$MODEL_DEST" | cut -f1))"

# 3. Write shim source and package.json, install npm deps.
log "Writing whisper-stt-shim source and installing dependencies..."
cat > /opt/whisper-stt/server.js <<'__WHISPER_SHIM_JS__'
{shim_source}
__WHISPER_SHIM_JS__

cat > /opt/whisper-stt/package.json <<'__WHISPER_SHIM_PKG__'
{package_json}
__WHISPER_SHIM_PKG__

cd /opt/whisper-stt
npm install --omit=dev --no-audit --no-fund 2>&1 | tail -20

# 4. Write systemd unit and start the shim.
cat > /etc/systemd/system/whisper-stt-shim.service <<'__WHISPER_SHIM_UNIT__'
{systemd_unit}
__WHISPER_SHIM_UNIT__

mkdir -p /etc/blitzlog
cat > /etc/blitzlog/whisper-stt.env <<ENVEOF
WHISPER_MODEL=/opt/whisper-stt/models/${{MODEL_FILE}}
WHISPER_LANGUAGE=${{STT_LANGUAGE}}
REQUEST_TIMEOUT_MS=60000
ENVEOF

systemctl daemon-reload
systemctl enable whisper-stt-shim.service
systemctl restart whisper-stt-shim.service

# 5. Health-check the shim before the bot starts.
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:7878/healthz > /dev/null 2>&1; then
        log "whisper-stt-shim is healthy on http://127.0.0.1:7878"
        break
    fi
    if ! systemctl is-active --quiet whisper-stt-shim.service; then
        log "WARNING: whisper-stt-shim service is not active; bot will start without STT"
        break
    fi
    log "Waiting for whisper-stt-shim... ($i/30)"
    sleep 2
done
"""


def _write_opencode_config_script(autonomous: bool = True) -> str:
    compaction = '"auto": false'
    agent_prompt = (
        ""
        if autonomous
        else ',\n      "prompt": "You have a `shutdown` tool available. Use it when the user asks to shut down or terminate the instance."'
    )
    return (
        """
mkdir -p /root/.config/opencode
cat > /root/.config/opencode/opencode.json <<'OPENCODECFG'
{
  "$schema": "https://opencode.ai/config.json",
  "model": "{env:OPENCODE_MODEL}",
  "default_agent": "build",
  "compaction": {
"""
        + compaction
        + """
  },
  "agent": {
    "build": {
      "steps": 75"""
        + agent_prompt
        + """
    }
  },
  "provider": {
    "minimax-coding-plan": {
      "options": {
        "apiKey": "{env:OPENCODE_API_KEY}"
      }
    }
  }
}
OPENCODECFG
"""
    )


_SESSION_ARCHIVE_PLUGIN_JS = r"""export const SessionArchive = async ({ project, client, $, directory }) => {
  const bucket = process.env.SESSION_ARCHIVE_BUCKET;
  if (!bucket) return {};
  const prefix = process.env.SESSION_ARCHIVE_PREFIX || "";

  async function archiveSession(sessionId) {
    try {
      const tmpFile = `/tmp/session-archive-${sessionId}.json`;
      await $`opencode export ${sessionId} > ${tmpFile}`.quiet();
      const s3Key = `${prefix}/sessions/${sessionId}.json`;
      await $`aws s3 cp ${tmpFile} s3://${bucket}/${s3Key}`.quiet();

      const branch = (await $`git -C ${directory} branch --show-current`.text()).trim();
      const commit = (await $`git -C ${directory} rev-parse HEAD`.text()).trim();
      const metadata = JSON.stringify({ sessionId, branch, commit, timestamp: Date.now() });
      await $`echo ${metadata} > /tmp/session-archive-metadata.json`.quiet();
      await $`aws s3 cp /tmp/session-archive-metadata.json s3://${bucket}/${prefix}/metadata.json`.quiet();

      await client.app.log({
        body: { service: "session-archive", level: "info", message: `Session archived: ${sessionId}` },
      });
    } catch (e) {
      try {
        await client.app.log({
          body: { service: "session-archive", level: "error", message: `Failed to archive session: ${e?.message || e}` },
        });
      } catch {}
    }
  }

  return {
    event: async ({ event }) => {
      const sessionId = event?.properties?.sessionID;
      if (!sessionId) return;
      if (event.type === "session.created") {
        try {
          await client.app.log({
            body: { service: "session-archive", level: "info", message: `Session created: ${sessionId}` },
          });
        } catch {}
      }
      if (event.type === "session.idle" || event.type === "session.compacted") {
        await archiveSession(sessionId);
      }
      if (event.type === "session.deleted") {
        try {
          await client.app.log({
            body: { service: "session-archive", level: "info", message: `Session deleted: ${sessionId}` },
          });
        } catch {}
      }
    },
  };
};
"""


def _write_session_archive_plugin_script() -> str:
    escaped = _SESSION_ARCHIVE_PLUGIN_JS.replace("\\", "\\\\").replace("'", "'\\''")
    return f"""
mkdir -p /root/.config/opencode/plugins
cat > /root/.config/opencode/plugins/session-archive.js <<'PLUGIN_EOF'
{escaped}
PLUGIN_EOF
"""


_SHUTDOWN_TOOL_JS = """\
export default {
  description:
    "Shut down this assisted agent instance. Archives the session, uploads logs to S3, and terminates the EC2 instance. Use when the user says they are done, wants to shut down, or no longer needs the agent.",
  args: {},
  async execute() {
    await Bun.$`_SHUTDOWN_REASON=agent_requested /usr/local/bin/assisted-shutdown.sh`;
    return "Shutdown initiated. Session archived, logs uploaded. Instance terminating.";
  },
}
"""


_IDLE_WATCHDOG_PLUGIN_JS = r"""export const IdleWatchdog = async ({ $, client, directory }) => {
  const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
  const TELEGRAM_USER_ID = process.env.TELEGRAM_USER_ID;

  let autosaveTimer = null;
  let pingTimer = null;
  let shutdownTimer = null;
  let idleSessionId = null;

  function clearTimers() {
    if (autosaveTimer) { clearTimeout(autosaveTimer); autosaveTimer = null; }
    if (pingTimer) { clearTimeout(pingTimer); pingTimer = null; }
    if (shutdownTimer) { clearTimeout(shutdownTimer); shutdownTimer = null; }
    idleSessionId = null;
  }

  async function autosave(sessionId) {
    try {
      await client.app.log({
        body: { service: "idle-watchdog", level: "info", message: `Autosave timer fired for session: ${sessionId}` },
      });
      const branch = (await $`git -C ${directory} branch --show-current`.text()).trim();
      if (!branch) return;
      const issueNumber = process.env.ISSUE_NUMBER || "unknown";
      const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
      const autosaveBranch = `autosave/issue-${issueNumber}-${timestamp}`;
      await $`git -C ${directory} add -A`.quiet();
      await $`git -C ${directory} commit --no-verify -m ${"autosave: idle checkpoint"}`.quiet().catch(() => {});
      await $`git -C ${directory} branch -f ${autosaveBranch} HEAD`.quiet();
      await $`git -C ${directory} push --force --no-verify origin ${autosaveBranch}`.quiet();
      await $`git -C ${directory} checkout ${branch}`.quiet().catch(() => {});
      await client.app.log({
        body: { service: "idle-watchdog", level: "info", message: `Autosave pushed to ${autosaveBranch}` },
      });
    } catch (e) {
      try {
        await client.app.log({
          body: { service: "idle-watchdog", level: "error", message: `Autosave failed: ${e?.message || e}` },
        });
      } catch {}
    }
  }

  async function telegramPing() {
    if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_USER_ID) return;
    try {
      await client.app.log({
        body: { service: "idle-watchdog", level: "info", message: `Telegram ping timer fired` },
      });
      const text = "\u{1FAE0} Assisted agent idle 35min. Will shut down in ~2h25m without activity.";
      await $`curl -s -X POST https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage -d chat_id=${TELEGRAM_USER_ID} -d text=${text}`.quiet();
    } catch (e) {
      try {
        await client.app.log({
          body: { service: "idle-watchdog", level: "error", message: `Telegram ping failed: ${e?.message || e}` },
        });
      } catch {}
    }
  }

  async function idleShutdown() {
    try {
      await client.app.log({
        body: { service: "idle-watchdog", level: "info", message: `Idle shutdown timer fired, initiating shutdown` },
      });
      await $`_SHUTDOWN_REASON=${"idle_timeout"} /usr/local/bin/assisted-shutdown.sh`;
    } catch (e) {
      try {
        await client.app.log({
          body: { service: "idle-watchdog", level: "error", message: `Idle shutdown failed: ${e?.message || e}` },
        });
      } catch {}
    }
  }

  try {
    await client.app.log({
      body: { service: "idle-watchdog", level: "info", message: "IdleWatchdog plugin initialized" },
    });
  } catch {}

  return {
    event: async ({ event }) => {
      const sessionId = event?.properties?.sessionID;

      if (event.type === "session.idle") {
        clearTimers();
        idleSessionId = sessionId;
        autosaveTimer = setTimeout(() => autosave(sessionId), 5 * 60 * 1000);
        pingTimer = setTimeout(() => telegramPing(), 35 * 60 * 1000);
        shutdownTimer = setTimeout(() => idleShutdown(), 3 * 60 * 60 * 1000);
        try {
          await client.app.log({
            body: { service: "idle-watchdog", level: "info", message: `Session idle: ${sessionId}. Timers started.` },
          });
        } catch {}
      }

      if (event.type === "message.part.updated" && idleSessionId) {
        try {
          await client.app.log({
            body: { service: "idle-watchdog", level: "info", message: `Session reactivated (message.part.updated): ${sessionId}. Timers cleared.` },
          });
        } catch {}
        clearTimers();
      }

      if (event.type === "session.deleted") {
        clearTimers();
      }
    },
  };
};
"""


_SPOT_WATCHDOG_PLUGIN_JS = r"""export const SpotWatchdog = async ({ client, $, directory }) => {
  const bucket = process.env.SESSION_ARCHIVE_BUCKET;
  const prefix = process.env.SESSION_ARCHIVE_PREFIX || "";
  let pollInterval = null;
  let emergencySaveTriggered = false;

  async function emergencySave(sessionId) {
    if (emergencySaveTriggered) return;
    emergencySaveTriggered = true;
    try {
      await client.app.log({
        body: { service: "spot-watchdog", level: "warn", message: "Spot interruption detected — emergency save started" },
      });

      const issueNumber = process.env.ISSUE_NUMBER || "unknown";
      const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
      const autosaveBranch = `autosave/issue-${issueNumber}-interruption-${timestamp}`;

      const branch = (await $`git -C ${directory} branch --show-current`.text()).trim();
      await $`git -C ${directory} add -A`.quiet();
      await $`git -C ${directory} commit --no-verify -m ${"autosave: spot interruption"}`.quiet().catch(() => {});
      await $`git -C ${directory} branch -f ${autosaveBranch} HEAD`.quiet();
      await $`git -C ${directory} push --force --no-verify origin ${autosaveBranch}`.quiet();
      if (branch) {
        await $`git -C ${directory} checkout ${branch}`.quiet().catch(() => {});
      }

      if (sessionId && bucket) {
        try {
          const tmpFile = `/tmp/session-interruption-${sessionId}.json`;
          await $`opencode export ${sessionId} > ${tmpFile}`.quiet();
          await $`aws s3 cp ${tmpFile} s3://${bucket}/${prefix}/sessions/${sessionId}.json`.quiet();
        } catch (e) {
          await client.app.log({
            body: { service: "spot-watchdog", level: "error", message: `Session archive failed during emergency save: ${e?.message || e}` },
          });
        }
      }

      await client.app.log({
        body: { service: "spot-watchdog", level: "warn", message: `Emergency save pushed to ${autosaveBranch}` },
      });
    } catch (e) {
      try {
        await client.app.log({
          body: { service: "spot-watchdog", level: "error", message: `Emergency save failed: ${e?.message || e}` },
        });
      } catch {}
    }
  }

  async function checkSpotInterruption(sessionId) {
    try {
      const result = await $`curl -sf -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60'`.text();
      const token = result.trim();
      if (!token) return;
      const action = await $`curl -sf -H ${"X-aws-ec2-metadata-token: " + token} http://169.254.169.254/latest/meta-data/spot/instance-action`.text().catch(() => "");
      if (action.trim()) {
        clearInterval(pollInterval);
        pollInterval = null;
        await emergencySave(sessionId);
      }
    } catch {}
  }

  try {
    await client.app.log({
      body: { service: "spot-watchdog", level: "info", message: "SpotWatchdog plugin initialized" },
    });
  } catch {}

  return {
    event: async ({ event }) => {
      const sessionId = event?.properties?.sessionID;

      if (event.type === "session.created") {
        pollInterval = setInterval(() => checkSpotInterruption(sessionId), 5000);
        try {
          await client.app.log({
            body: { service: "spot-watchdog", level: "info", message: `Spot interruption polling started for session: ${sessionId}` },
          });
        } catch {}
      }

      if (event.type === "session.deleted") {
        if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
      }
    },
  };
};
"""


_PERIODIC_AUTOSAVE_PLUGIN_JS = r"""export const PeriodicAutosave = async ({ client, $, directory }) => {
  let saveInterval = null;

  async function periodicSave() {
    try {
      const issueNumber = process.env.ISSUE_NUMBER || "unknown";
      const autosaveBranch = `autosave/issue-${issueNumber}-latest`;

      const branch = (await $`git -C ${directory} branch --show-current`.text()).trim();
      await $`git -C ${directory} add -A`.quiet();
      await $`git -C ${directory} commit --no-verify --allow-empty -m ${"autosave: periodic checkpoint"}`.quiet().catch(() => {});
      await $`git -C ${directory} branch -f ${autosaveBranch} HEAD`.quiet();
      await $`git -C ${directory} push --force --no-verify origin ${autosaveBranch}`.quiet();
      if (branch) {
        await $`git -C ${directory} checkout ${branch}`.quiet().catch(() => {});
      }

      await client.app.log({
        body: { service: "periodic-autosave", level: "info", message: `Periodic autosave pushed to ${autosaveBranch}` },
      });
    } catch (e) {
      try {
        await client.app.log({
          body: { service: "periodic-autosave", level: "error", message: `Periodic autosave failed: ${e?.message || e}` },
        });
      } catch {}
    }
  }

  try {
    await client.app.log({
      body: { service: "periodic-autosave", level: "info", message: "PeriodicAutosave plugin initialized" },
    });
  } catch {}

  return {
    event: async ({ event }) => {
      const sessionId = event?.properties?.sessionID;

      if (event.type === "session.created") {
        saveInterval = setInterval(() => periodicSave(), 5 * 60 * 1000);
        try {
          await client.app.log({
            body: { service: "periodic-autosave", level: "info", message: `Periodic autosave started for session: ${sessionId}` },
          });
        } catch {}
      }

      if (event.type === "session.deleted") {
        if (saveInterval) { clearInterval(saveInterval); saveInterval = null; }
      }
    },
  };
};
"""


def _write_spot_watchdog_plugin_script() -> str:
    escaped = _SPOT_WATCHDOG_PLUGIN_JS.replace("\\", "\\\\").replace("'", "'\\''")
    return f"""
mkdir -p /root/.config/opencode/plugins
cat > /root/.config/opencode/plugins/spot-watchdog.js <<'PLUGIN_EOF'
{escaped}
PLUGIN_EOF
"""


def _write_periodic_autosave_plugin_script() -> str:
    escaped = _PERIODIC_AUTOSAVE_PLUGIN_JS.replace("\\", "\\\\").replace("'", "'\\''")
    return f"""
mkdir -p /root/.config/opencode/plugins
cat > /root/.config/opencode/plugins/periodic-autosave.js <<'PLUGIN_EOF'
{escaped}
PLUGIN_EOF
"""


def _install_toolchain_script() -> str:
    return r"""
log "Checking for mise.toml or .tool-versions..."
if [ -f /workspace/repo/mise.toml ] || [ -f /workspace/repo/.tool-versions ]; then
    log "Installing mise..."
    curl -fsSL https://mise.run | sh
    export PATH="/root/.local/bin:$PATH"

    cd /workspace/repo
    mise trust 2>/dev/null || true

    log "Installing project toolchains via mise..."
    export MISE_NODE_VERIFY=0
    mise install -y

    MISE_SHIMS="/root/.local/share/mise/shims"
    if [ -d "$MISE_SHIMS" ]; then
        echo "export PATH=$MISE_SHIMS:/root/.local/bin:\$PATH" > /etc/profile.d/mise.sh
        export PATH="$MISE_SHIMS:$PATH"
    fi

    log "Running project bootstrap if defined..."
    if mise tasks --name-only 2>/dev/null | grep -qx "bootstrap"; then
        mise run bootstrap
    fi

    log "Active toolchains:"
    mise current || true
else
    log "No mise.toml or .tool-versions found, skipping"
fi
"""


def _build_s3_downloader_script(bucket: str, key: str) -> str:
    return f"""#!/bin/bash
TOKEN=$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/dynamic/instance-identity/document | python3 -c "import sys,json; print(json.load(sys.stdin)['region'])")
aws s3 cp s3://{bucket}/{key} /tmp/bootstrap.sh --region "$REGION"
bash /tmp/bootstrap.sh
"""


def _session_export_to_s3_script() -> str:
    return """
log "Exporting session to S3..."
cd /workspace/repo
SESSION_ID=$(opencode session list --format json -n 1 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])" 2>/dev/null || echo "")
if [ -n "$SESSION_ID" ]; then
    TMP_SESSION="/tmp/session-export-${SESSION_ID}.json"
    opencode export "$SESSION_ID" > "$TMP_SESSION" 2>/dev/null || true
    if [ -s "$TMP_SESSION" ]; then
        aws s3 cp "$TMP_SESSION" "s3://${SESSION_ARCHIVE_BUCKET}/${SESSION_ARCHIVE_PREFIX}/sessions/${SESSION_ID}.json" --region "$REGION" 2>/dev/null || true
        BRANCH=$(git -C /workspace/repo branch --show-current 2>/dev/null || echo "")
        COMMIT=$(git -C /workspace/repo rev-parse HEAD 2>/dev/null || echo "")
        python3 -c "import json; print(json.dumps({'sessionId': '${SESSION_ID}', 'branch': '${BRANCH}', 'commit': '${COMMIT}', 'timestamp': $(date +%s000)}))" > /tmp/session-export-metadata.json 2>/dev/null || true
        aws s3 cp /tmp/session-export-metadata.json "s3://${SESSION_ARCHIVE_BUCKET}/${SESSION_ARCHIVE_PREFIX}/metadata.json" --region "$REGION" 2>/dev/null || true
        log "Session exported to S3: $SESSION_ID"
    fi
else
    log "WARNING: No session found to export"
fi
"""


def _session_restore_script(repo: str, issue_number: int, s3_bucket: str) -> str:
    s3_prefix = f"{repo}/issue/{issue_number}"
    return f"""
S3_RESTORE_PREFIX="{s3_prefix}"
S3_RESTORE_BUCKET="{s3_bucket}"
RESUMED=false

log "Checking S3 for existing session state..."
if aws s3 ls "s3://${{S3_RESTORE_BUCKET}}/${{S3_RESTORE_PREFIX}}/metadata.json" >/dev/null 2>&1; then
    log "Found previous session metadata, downloading..."
    aws s3 cp "s3://${{S3_RESTORE_BUCKET}}/${{S3_RESTORE_PREFIX}}/metadata.json" /tmp/session-metadata.json --region "$REGION" 2>/dev/null || true

    if [ -f /tmp/session-metadata.json ]; then
        RESTORE_BRANCH=$(python3 -c "import json; print(json.load(open('/tmp/session-metadata.json')).get('branch',''))" 2>/dev/null || echo "")
        RESTORE_SESSION_ID=$(python3 -c "import json; print(json.load(open('/tmp/session-metadata.json')).get('sessionId',''))" 2>/dev/null || echo "")

        if [ -n "$RESTORE_BRANCH" ]; then
            log "Checking out branch: $RESTORE_BRANCH"
            git -C /workspace/repo fetch origin "$RESTORE_BRANCH" 2>/dev/null || true
            git -C /workspace/repo checkout "$RESTORE_BRANCH" 2>/dev/null || log "WARNING: Could not checkout branch $RESTORE_BRANCH"
        fi

        if [ -n "$RESTORE_SESSION_ID" ]; then
            log "Downloading session $RESTORE_SESSION_ID..."
            aws s3 cp "s3://${{S3_RESTORE_BUCKET}}/${{S3_RESTORE_PREFIX}}/sessions/${{RESTORE_SESSION_ID}}.json" /tmp/session-import.json --region "$REGION" 2>/dev/null || true
        fi

        if [ -f /tmp/session-import.json ]; then
            RESUMED=true
            log "Session state downloaded for restore"
        fi
    fi
else
    log "No previous session state found"
fi
"""


def _upload_logs_and_terminate_script(repo: str, issue_number: int) -> str:
    s3_bucket = os.environ.get("S3_LOGS_BUCKET", "<your-agent-logs-bucket>")
    s3_prefix = f"{repo}/issue/{issue_number}"
    return f"""
TOKEN=$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/dynamic/instance-identity/document | python3 -c "import sys,json; print(json.load(sys.stdin)['region'])")
LOG_KEY="{s3_prefix}/logs/${{INSTANCE_ID}}-$(date +%Y%m%d-%H%M%S).log"
aws s3 cp /var/log/backend-bootstrap.log "s3://{s3_bucket}/${{LOG_KEY}}" --region "$REGION" || true
"""


def build_autonomous_user_data(
    repo: str, issue_number: int, sender_login: str = "", sender_id: str = ""
) -> str:
    prompt = (
        f"Work on GitHub issue #{issue_number}. Follow AGENTS.md for branch naming, "
        f"implementation standards, testing, linting and commit conventions. "
        f"Create a PR when done."
    )

    s3_bucket = os.environ.get("S3_LOGS_BUCKET", "<your-agent-logs-bucket>")
    opencode_model = os.environ.get("OPENCODE_MODEL", "minimax-coding-plan/MiniMax-M3")
    s3_archive_prefix = f"{repo}/issue/{issue_number}"

    git_user_name = sender_login or ""
    git_user_email = (
        f"{sender_id}+{sender_login}@users.noreply.github.com" if sender_login else ""
    )

    return f"""#!/bin/bash
set -euo pipefail
export HOME=/root
export GIT_TERMINAL_PROMPT=0

LOG_FILE="/var/log/backend-bootstrap.log"
log() {{
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}}

ISSUE_NUMBER={issue_number}
REPO="{repo}"
OPENCODE_NONINTERACTIVE=1
OPENCODE_PROMPT="{prompt}"
OPENCODE_MODEL="{opencode_model}"
S3_LOGS_BUCKET="{s3_bucket}"
SESSION_ARCHIVE_BUCKET="{s3_bucket}"
SESSION_ARCHIVE_PREFIX="{s3_archive_prefix}"
export ISSUE_NUMBER OPENCODE_MODEL S3_LOGS_BUCKET OPENCODE_NONINTERACTIVE OPENCODE_PROMPT SESSION_ARCHIVE_BUCKET SESSION_ARCHIVE_PREFIX

log "=== Cloud-coder bootstrap starting (autonomous) ==="
log "Repo: $REPO, Issue: $ISSUE_NUMBER"

log "Reading secrets from SSM..."
{_read_secrets_from_ssm_script(issue_number)}

log "Installing system packages..."
{_install_system_packages_script()}

log "Setting up git credentials..."
{_configure_git_script(git_user_name, git_user_email)}

log "Installing opencode..."
{_install_opencode_script()}

log "Cloning repository..."
mkdir -p /workspace
git clone "https://github.com/${{REPO}}.git" /workspace/repo
cd /workspace/repo

{_install_toolchain_script()}

log "Writing opencode config and session archive plugin..."
{_write_opencode_config_script(autonomous=True)}
{_write_session_archive_plugin_script()}

log "Effective opencode config: model=$OPENCODE_MODEL, provider=$(grep -oE '"minimax[a-z-]*"|"zai[a-z-]*"' /root/.config/opencode/opencode.json | head -1 | tr -d '\"'), api_key_prefix=${{OPENCODE_API_KEY:0:8}}..."

log "Writing spot watchdog and periodic autosave plugins..."
{_write_spot_watchdog_plugin_script()}
{_write_periodic_autosave_plugin_script()}

log "Setting up watchdog (timeout: 7200s)..."
cat > /etc/blitzlog.env <<ENVEOF
ISSUE_NUMBER={issue_number}
S3_LOGS_BUCKET={s3_bucket}
REPO={repo}
SESSION_ARCHIVE_BUCKET={s3_bucket}
SESSION_ARCHIVE_PREFIX={s3_archive_prefix}
ENVEOF
cat > /usr/local/bin/watchdog.sh << 'WDOG_SCRIPT'
#!/bin/bash
set -euo pipefail
source /etc/blitzlog.env
export HOME=/root
export PATH=/root/.opencode/bin:$PATH
export SESSION_ARCHIVE_BUCKET SESSION_ARCHIVE_PREFIX
LOG_FILE="/var/log/backend-bootstrap.log"
log() {{
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}}
TIMEOUT=7200
COMMAND="$@"
timeout $TIMEOUT $COMMAND 2>&1
EXIT_CODE=$?

# Upload logs to S3 before terminating
{_upload_logs_and_terminate_script(repo, issue_number)}
log "Logs uploaded to S3"

{_decode_api_errors_script()}

# Export session to S3
{_session_export_to_s3_script()}

# Terminate instance
if [ $EXIT_CODE -eq 124 ]; then
    echo "Watchdog triggered: command exceeded ${{TIMEOUT}}s"
fi
aws ec2 terminate-instances --instance-id "$INSTANCE_ID" --region "$REGION" || true
WDOG_SCRIPT
chmod +x /usr/local/bin/watchdog.sh

log "Launching opencode agent..."
cd /workspace/repo
OPENCODE_NONINTERACTIVE=1 /usr/local/bin/watchdog.sh opencode run --agent build "$OPENCODE_PROMPT" 2>&1 | tee -a "$LOG_FILE"
"""


def build_assisted_user_data(
    repo: str,
    issue_number: int,
    sender_login: str = "",
    sender_id: str = "",
    bot_name: str = "",
    bot_token: str = "",
    telegram_user_id: str = "",
) -> str:
    s3_bucket = os.environ.get("S3_LOGS_BUCKET", "<your-agent-logs-bucket>")
    opencode_model = os.environ.get("OPENCODE_MODEL", "minimax-coding-plan/MiniMax-M3")
    _, opencode_model_id = (
        opencode_model.split("/", 1) if "/" in opencode_model else ("", opencode_model)
    )
    s3_archive_prefix = f"{repo}/issue/{issue_number}"

    git_user_name = sender_login or ""
    git_user_email = (
        f"{sender_id}+{sender_login}@users.noreply.github.com" if sender_login else ""
    )

    return f"""#!/bin/bash
set -euo pipefail
export HOME=/root
export GIT_TERMINAL_PROMPT=0

LOG_FILE="/var/log/backend-bootstrap.log"
log() {{
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}}

ISSUE_NUMBER={issue_number}
REPO="{repo}"
OPENCODE_MODEL="{opencode_model}"
S3_LOGS_BUCKET="{s3_bucket}"
SESSION_ARCHIVE_BUCKET="{s3_bucket}"
SESSION_ARCHIVE_PREFIX="{s3_archive_prefix}"
export ISSUE_NUMBER OPENCODE_MODEL S3_LOGS_BUCKET SESSION_ARCHIVE_BUCKET SESSION_ARCHIVE_PREFIX

log "=== Cloud-coder bootstrap starting (assisted) ==="
log "Repo: $REPO, Issue: $ISSUE_NUMBER"

log "Reading secrets from SSM..."
{_read_secrets_from_ssm_script(issue_number)}
TELEGRAM_USER_ID="{telegram_user_id}"
TELEGRAM_BOT_TOKEN="{bot_token}"
export TELEGRAM_BOT_TOKEN TELEGRAM_USER_ID

log "Installing system packages..."
{_install_system_packages_script()}

log "Installing Node.js 24 via tarball (bypasses AL2023 dnf repo gaps)..."
NODE_MAJOR=$(node --version 2>/dev/null | cut -d. -f1 | tr -d 'v' || echo "0")
if [ "$NODE_MAJOR" -lt 24 ]; then
    curl -fsSL https://nodejs.org/dist/v24.6.0/node-v24.6.0-linux-arm64.tar.xz \
        | tar -xJ -C /usr/local --strip-components=1
fi
hash -r
export PATH="/usr/local/bin:$PATH"
node --version
npm --version

log "Setting up git credentials..."
{_configure_git_script(git_user_name, git_user_email)}

log "Installing opencode..."
{_install_opencode_script()}

log "Cloning repository..."
mkdir -p /workspace
git clone "https://github.com/${{REPO}}.git" /workspace/repo
cd /workspace/repo

{_install_toolchain_script()}

log "Restoring previous session state..."
{_session_restore_script(repo, issue_number, s3_bucket)}

log "Writing opencode config and session archive plugin..."
{_write_opencode_config_script(autonomous=False)}
{_write_session_archive_plugin_script()}

log "Effective opencode config: model=$OPENCODE_MODEL, provider=$(grep -oE '"minimax[a-z-]*"|"zai[a-z-]*"' /root/.config/opencode/opencode.json | head -1 | tr -d '\"'), api_key_prefix=${{OPENCODE_API_KEY:0:8}}..."

log "Writing spot watchdog and periodic autosave plugins..."
{_write_spot_watchdog_plugin_script()}
{_write_periodic_autosave_plugin_script()}

log "Writing shutdown tool..."
mkdir -p /root/.config/opencode/tools
cat > /root/.config/opencode/tools/shutdown.js <<'SHUTDOWN_TOOL_JS'
{_SHUTDOWN_TOOL_JS}
SHUTDOWN_TOOL_JS

log "Writing idle watchdog plugin..."
cat > /root/.config/opencode/plugins/idle-watchdog.js <<'IDLE_WATCHDOG_PLUGIN_JS'
{_IDLE_WATCHDOG_PLUGIN_JS}
IDLE_WATCHDOG_PLUGIN_JS

log "Starting opencode server on port 4096..."
cd /workspace/repo
export PATH=/root/.opencode/bin:$PATH
export SESSION_ARCHIVE_BUCKET SESSION_ARCHIVE_PREFIX
OPENCODE_SERVER_USERNAME=agent
OPENCODE_SERVER_PASSWORD=$(openssl rand -hex 16)
export OPENCODE_SERVER_USERNAME OPENCODE_SERVER_PASSWORD
opencode serve --port 4096 &
OPENCODE_PID=$!

for i in $(seq 1 30); do
    if curl -s http://localhost:4096/health > /dev/null 2>&1; then
        log "OpenCode server is healthy"
        break
    fi
    if ! kill -0 $OPENCODE_PID 2>/dev/null; then
        log "ERROR: OpenCode server process died"
        exit 1
    fi
    log "Waiting for opencode server... ($i/30)"
    sleep 2
done

log "Importing previous session if available..."
if [ "$RESUMED" = "true" ] && [ -f /tmp/session-import.json ]; then
    opencode import /tmp/session-import.json 2>&1 || log "WARNING: Session import failed"
    log "Session imported from S3"
fi

log "Configuring opencode-telegram-bot..."
mkdir -p /root/.config/opencode-telegram-bot
cat > /root/.config/opencode-telegram-bot/.env <<TELEGRAMCFG
TELEGRAM_BOT_TOKEN=${{TELEGRAM_BOT_TOKEN}}
TELEGRAM_ALLOWED_USER_ID=${{TELEGRAM_USER_ID}}
OPENCODE_API_URL=http://localhost:4096
OPENCODE_SERVER_USERNAME=agent
OPENCODE_SERVER_PASSWORD=${{OPENCODE_SERVER_PASSWORD}}
OPENCODE_MODEL_PROVIDER=minimax-coding-plan
OPENCODE_MODEL_ID={opencode_model_id}
BOT_LOCALE=en
TELEGRAM_FORCE_IPV4=true
STT_API_URL=${{STT_API_URL}}
STT_API_KEY=${{STT_API_KEY}}
STT_MODEL=${{STT_MODEL}}
STT_LANGUAGE=${{STT_LANGUAGE}}
STT_REQUEST_FORMAT=multipart
TELEGRAMCFG

log "Auto-selecting project and session in bot settings..."
PROJECT_JSON=$(curl -sf -u agent:$OPENCODE_SERVER_PASSWORD http://localhost:4096/project 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
for p in data if isinstance(data, list) else [data]:
    if p.get('worktree','').startswith('/workspace'):
        print(json.dumps({{'id': p['id'], 'worktree': p['worktree'], 'name': p.get('name', p['worktree'])}}))
        break
" 2>/dev/null || echo "")

SESSION_JSON=""
if [ "$RESUMED" = "true" ] && [ -f /tmp/session-import.json ]; then
    RESTORE_SESSION_ID=$(python3 -c "import json; d=json.load(open('/tmp/session-import.json')); print(d.get('id',''))" 2>/dev/null || echo "")
    if [ -n "$RESTORE_SESSION_ID" ]; then
        SESSION_TITLE=$(curl -sf -u agent:$OPENCODE_SERVER_PASSWORD "http://localhost:4096/session/${{RESTORE_SESSION_ID}}" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps({{'id': d['id'], 'title': d.get('title',''), 'directory': d.get('directory','')}}))" 2>/dev/null || echo "")
        if [ -n "$SESSION_TITLE" ]; then
            SESSION_JSON=$SESSION_TITLE
        fi
    fi
fi

if [ -n "$PROJECT_JSON" ]; then
    if [ -n "$SESSION_JSON" ]; then
        cat > /root/.config/opencode-telegram-bot/settings.json <<SETTINGS_EOF
{{"currentProject": $PROJECT_JSON, "currentSession": $SESSION_JSON}}
SETTINGS_EOF
        log "Project and session pre-selected (resumed)"
    else
        cat > /root/.config/opencode-telegram-bot/settings.json <<SETTINGS_EOF
{{"currentProject": $PROJECT_JSON}}
SETTINGS_EOF
        log "Project pre-selected (new session)"
    fi
else
    log "WARNING: Could not auto-select project, user will need /projects"
fi

log "Pre-warming opencode-telegram-bot (downloads package to npx cache)..."
{_install_whisper_stt_script()}
npx -y @grinev/opencode-telegram-bot@latest status > /var/log/pre-warm.log 2>&1
PRE_WARM_EXIT=$?
if [ "$PRE_WARM_EXIT" -ne 0 ]; then
    log "WARNING: Pre-warm failed with exit code $PRE_WARM_EXIT; will attempt bot start anyway and notify user"
    curl -s -X POST "https://api.telegram.org/bot${{TELEGRAM_BOT_TOKEN}}/sendMessage" \\
        -d chat_id="${{TELEGRAM_USER_ID}}" \\
        -d parse_mode="Markdown" \\
        -d text="Assisted agent cannot be started [Bot: {bot_name}]

Repo: ${{REPO}}
[Issue #${{ISSUE_NUMBER}}: ${{ISSUE_TITLE}}](https://github.com/${{REPO}}/issues/${{ISSUE_NUMBER}})
Mode: Assisted (interactive via Telegram)$RESUME_STATUS" || true
fi

log "Sending Telegram notification..."
ISSUE_TITLE=$(gh issue view $ISSUE_NUMBER --json title --jq .title 2>/dev/null || echo "unknown")
RESUME_STATUS=""
if [ "$RESUMED" = "true" ]; then
    RESTORED_TITLE=$(echo "$SESSION_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('title',''))" 2>/dev/null || echo "")
    RESUME_STATUS="

Resumed session: ${{RESTORED_TITLE}}"
fi
curl -s -X POST "https://api.telegram.org/bot${{TELEGRAM_BOT_TOKEN}}/sendMessage" \\
    -d chat_id="${{TELEGRAM_USER_ID}}" \\
    -d parse_mode="Markdown" \\
    -d text="Assisted agent ready [Bot: {bot_name}]

Repo: ${{REPO}}
[Issue #${{ISSUE_NUMBER}}: ${{ISSUE_TITLE}}](https://github.com/${{REPO}}/issues/${{ISSUE_NUMBER}})
Mode: Assisted (interactive via Telegram)$RESUME_STATUS

Connect to this bot to start working on the task." || true

log "Installing shutdown helper..."
cat > /etc/blitzlog.env <<ENVEOF
ISSUE_NUMBER={issue_number}
S3_LOGS_BUCKET={s3_bucket}
REPO={repo}
SESSION_ARCHIVE_BUCKET={s3_bucket}
SESSION_ARCHIVE_PREFIX={s3_archive_prefix}
OPENCODE_SERVER_USERNAME=$OPENCODE_SERVER_USERNAME
OPENCODE_SERVER_PASSWORD=$OPENCODE_SERVER_PASSWORD
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_USER_ID=$TELEGRAM_USER_ID
ENVEOF
cat > /usr/local/bin/assisted-shutdown.sh << 'SHUTDOWN_SCRIPT'
#!/bin/bash
set -euo pipefail
source /etc/blitzlog.env
export HOME=/root
export PATH=/root/.opencode/bin:$PATH
export SESSION_ARCHIVE_BUCKET SESSION_ARCHIVE_PREFIX
export OPENCODE_SERVER_USERNAME OPENCODE_SERVER_PASSWORD
LOG_FILE="/var/log/backend-bootstrap.log"
log() {{
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}}

log "=== Assisted shutdown initiated ==="

TOKEN=$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/dynamic/instance-identity/document | python3 -c "import sys,json; print(json.load(sys.stdin)['region'])")

# Detect shutdown reason
SHUTDOWN_REASON="${{_SHUTDOWN_REASON:-}}"
if [ -z "$SHUTDOWN_REASON" ]; then
    SPOT_ACTION=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/spot/instance-action 2>/dev/null || echo "")
    if [ -n "$SPOT_ACTION" ]; then
        SHUTDOWN_REASON="spot_interruption"
    elif [ "${{_SKIP_SHUTDOWN:-}}" = "1" ]; then
        SHUTDOWN_REASON="system_shutdown"
    else
        SHUTDOWN_REASON="unknown"
    fi
fi
log "Shutdown reason: $SHUTDOWN_REASON"

# Export session to S3
{_session_export_to_s3_script()}

# Upload logs to S3
LOG_KEY="{s3_archive_prefix}/logs/${{INSTANCE_ID}}-$(date +%Y%m%d-%H%M%S).log"
aws s3 cp /var/log/backend-bootstrap.log "s3://{s3_bucket}/${{LOG_KEY}}" --region "$REGION" || true
log "Logs uploaded to S3"

# Release bot pool lock
S3_LOGS_BUCKET="{s3_bucket}"
if [ -n "{bot_name}" ]; then
    aws s3 rm "s3://${{S3_LOGS_BUCKET}}/bot-pool-locks/{sender_login}/{bot_name}.json" --region "$REGION" 2>/dev/null || true
    log "Released bot pool lock for {sender_login}/{bot_name}"
fi

# Notify via Telegram
curl -s -X POST "https://api.telegram.org/bot${{TELEGRAM_BOT_TOKEN}}/sendMessage" \\
    -d chat_id="${{TELEGRAM_USER_ID}}" \\
    -d text="Assisted agent shutting down [Bot: {bot_name}] (reason: ${{SHUTDOWN_REASON}}). Session archived to S3. Logs uploaded." || true

if [ "${{_SKIP_SHUTDOWN:-}}" != "1" ]; then
    log "Shutting down..."
    shutdown -h now
fi
SHUTDOWN_SCRIPT
chmod +x /usr/local/bin/assisted-shutdown.sh

log "Installing systemd shutdown service..."
cat > /etc/systemd/system/blitzlog-cleanup.service << 'CLEANUP_UNIT'
[Unit]
Description=Cloud Coder Assisted Cleanup
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/true
Environment=_SKIP_SHUTDOWN=1
ExecStop=/usr/local/bin/assisted-shutdown.sh
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
CLEANUP_UNIT
systemctl enable blitzlog-cleanup.service
systemctl start blitzlog-cleanup.service

log "Starting opencode-telegram-bot (foreground)..."
cd /workspace/repo
npx -y @grinev/opencode-telegram-bot@latest start 2>&1 | tee -a "$LOG_FILE"
"""


def lambda_handler(event, context):
    logger.info("Lambda invoked")
    raw_body = event.get("body", "")
    logger.info("isBase64Encoded: %s", event.get("isBase64Encoded", False))

    if event.get("isBase64Encoded", False):
        body_bytes = base64.b64decode(raw_body)
    else:
        body_bytes = raw_body.encode()

    payload = json.loads(body_bytes)

    headers = event.get("headers", {})
    signature = headers.get("x-hub-signature-256") or headers.get(
        "X-Hub-Signature-256", ""
    )
    logger.info("Signature present: %s", bool(signature))

    try:
        webhook_secret = get_ssm_param("github-webhook/secret")
    except ClientError as e:
        logger.error("Failed to read webhook secret from SSM: %s", e)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"SSM read failed: {e}"}),
        }

    if not verify_github_signature(webhook_secret, body_bytes, signature):
        logger.warning("Signature verification failed")
        return {"statusCode": 401, "body": json.dumps({"error": "Invalid signature"})}

    event_data = extract_event_data(payload)
    if not event_data:
        return {"statusCode": 400, "body": json.dumps({"error": "Malformed event"})}

    logger.info("Action: %s, Labels: %s", event_data["action"], event_data["labels"])

    if event_data["action"] != "labeled":
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "Not a label action"}),
        }

    labels = event_data["labels"]

    if "autonomous" in labels:
        mode = "autonomous"
    elif "assisted" in labels:
        mode = "assisted"
    else:
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "No relevant label"}),
        }

    issue_number = event_data["issue_number"]
    repo_full_name = event_data["repo_full_name"]
    sender_login = event_data.get("sender_login", "")
    sender_id = event_data.get("sender_id", "")

    try:
        github_token = get_github_app_token(repo_full_name)
    except Exception as e:
        logger.exception("GitHub App auth failed")

        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"GitHub App auth failed: {e}"}),
        }

    builder = (
        build_autonomous_user_data if mode == "autonomous" else build_assisted_user_data
    )

    bot_name = ""
    bot_token = ""
    telegram_user_id = ""

    if mode == "assisted":
        if not sender_login:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {"error": "Cannot determine sender for assisted mode"}
                ),
            }
        telegram_user_id = get_telegram_user_id(sender_login) or ""
        if not telegram_user_id:
            logger.warning("No Telegram user ID configured for %s", sender_login)
            return {
                "statusCode": 503,
                "body": json.dumps(
                    {"error": f"No bot pool configured for user {sender_login}"}
                ),
            }
        import uuid as _uuid

        tentative_id = f"i-{_uuid.uuid4().hex[:8]}"
        pool_result = acquire_bot_token(
            sender_login, tentative_id, repo_full_name, issue_number
        )
        if pool_result is None:
            logger.warning(
                "Bot pool exhausted for user %s, returning 503", sender_login
            )
            return {
                "statusCode": 503,
                "body": json.dumps(
                    {"error": f"No bot pool configured for user {sender_login}"}
                ),
            }
        bot_name, bot_token = pool_result

    def _builder(repo, issue_number, sender_login="", sender_id=""):
        if mode == "assisted":
            return builder(
                repo,
                issue_number,
                sender_login,
                sender_id,
                bot_name=bot_name,
                bot_token=bot_token,
                telegram_user_id=telegram_user_id,
            )
        return builder(repo, issue_number, sender_login, sender_id)

    try:
        instance_id = launch_ec2_spot_instance(
            repo_full_name,
            issue_number,
            github_token,
            mode,
            _builder,
            sender_login=sender_login,
            sender_id=sender_id,
        )
        logger.info("Launched EC2 instance (%s): %s", mode, instance_id)
        if mode == "assisted" and bot_name:
            _update_lock_instance_id(
                sender_login, bot_name, instance_id, repo_full_name, issue_number
            )
    except ClientError as e:
        logger.exception("EC2 launch failed")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"EC2 launch failed: {e}"}),
        }

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "message": f"EC2 spot instance launched ({mode}) for issue #{issue_number}",
                "instance_id": instance_id,
                "mode": mode,
            }
        ),
    }
