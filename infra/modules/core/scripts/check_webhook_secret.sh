#!/usr/bin/env bash
# Post-apply webhook secret drift check (issue #30).
#
# Reads the freshly-applied GitHub webhook HMAC secret from SSM, lists every
# active webhook on each configured repo via the GitHub API, and POSTs a
# signed probe to each one. If the receiver accepts (2xx) the secrets match;
# if it rejects with 401 the configured SSM secret has drifted from what
# GitHub has, and we emit a WARNING naming the webhook so the operator can
# patch it via `gh api` before any real deliveries start 401ing.
#
# This script is invoked by terraform_data.webhook_drift_check in
# infra/modules/core/. It exits 0 unconditionally — drift is a warning,
# not an apply blocker (legitimate temporary rotations are common).
#
# Required environment:
#   BLITZLOG_GITHUB_TOKEN      GitHub token (PAT with repo:hooks read or fine-grained webhooks:read)
#   BLITZLOG_GITHUB_REPOS      JSON array of "<owner>/<repo>" strings, e.g. '["acme/widgets"]'
#   BLITZLOG_SSM_PARAM_NAME    Full SSM parameter name holding the webhook secret
#   BLITZLOG_AWS_REGION        AWS region for the SSM read
# Optional:
#   BLITZLOG_AWS_PROFILE       AWS profile (passed via local-exec environment)
#   BLITZLOG_GITHUB_API        GitHub API base (default https://api.github.com)

set -u
# NOTE: deliberately NOT `set -e` — the script must keep going past per-webhook
# failures so one broken webhook doesn't suppress warnings for the rest. Each
# step sets its own exit code into a local we check explicitly.

GITHUB_API="${BLITZLOG_GITHUB_API:-https://api.github.com}"

log()  { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
err()  { printf 'ERROR: %s\n'   "$*" >&2; }

require_env() {
    if [[ -z "${!1:-}" ]]; then
        err "missing required environment variable: $1"
        exit 1
    fi
}

require_env BLITZLOG_GITHUB_TOKEN
require_env BLITZLOG_GITHUB_REPOS
require_env BLITZLOG_SSM_PARAM_NAME
require_env BLITZLOG_AWS_REGION

# AWS_PROFILE is optional but, when set, AWS_PROFILE-aware awscli behavior is
# what we want — nothing else to do here.
if [[ -n "${BLITZLOG_AWS_PROFILE:-}" ]]; then
    export AWS_PROFILE="${BLITZLOG_AWS_PROFILE}"
fi

# ---------------------------------------------------------------------------
# Step 1: read the freshly-applied webhook secret from SSM.
# ---------------------------------------------------------------------------

log "Reading webhook secret from SSM parameter ${BLITZLOG_SSM_PARAM_NAME} ..."
if ! ssm_value=$(aws ssm get-parameter \
        --name "${BLITZLOG_SSM_PARAM_NAME}" \
        --with-decryption \
        --query 'Parameter.Value' \
        --output text \
        --region "${BLITZLOG_AWS_REGION}" 2>&1); then
    err "failed to read ${BLITZLOG_SSM_PARAM_NAME} from SSM:"
    err "  ${ssm_value}"
    err "  (the drift check is opt-in and non-fatal; failing here usually means"
    err "   AWS credentials are not accessible from the Terraform host. Verify"
    err "   AWS_PROFILE / aws sso / aws configure sso is configured.)"
    exit 0
fi

if [[ -z "${ssm_value}" ]]; then
    err "SSM parameter ${BLITZLOG_SSM_PARAM_NAME} is empty — cannot probe webhooks"
    exit 0
fi

# Probe payload. We deliberately pick action="opened" + label "drift-check"
# (no overlap with the trigger labels "autonomous" / "assisted") so the Lambda
# returns 200 {"message":"No relevant label"} after the signature check, without
# spawning an EC2 instance or hitting the bot-pool lock path.
PROBE_PAYLOAD='{"action":"opened","issue":{"number":1,"labels":[{"name":"drift-check"}]},"repository":{"full_name":"drift-check/placeholder"},"sender":{"login":"drift-check","id":0}}'

probe_signature() {
    # echo -n is required — the Lambda's verify_github_signature hashes the
    # raw body bytes, and a trailing newline would invalidate the signature.
    printf '%s' "${PROBE_PAYLOAD}" \
        | openssl dgst -sha256 -hmac "${ssm_value}" -binary \
        | openssl base64 -A
}

# ---------------------------------------------------------------------------
# Step 2: iterate configured repos.
# ---------------------------------------------------------------------------

# jq -r '.[]' turns the JSON array into one <owner>/<repo> per line.
mapfile -t repos < <(printf '%s' "${BLITZLOG_GITHUB_REPOS}" | jq -r '.[]')
if [[ ${#repos[@]} -eq 0 ]]; then
    err "BLITZLOG_GITHUB_REPOS is an empty array — nothing to check"
    exit 0
fi

any_drift=0
total_hooks_checked=0
total_drift_warnings=0

for repo in "${repos[@]}"; do
    log ""
    log "==> Listing webhooks for ${repo} ..."
    if ! hooks_json=$(curl -fsSL \
            -H "Authorization: Bearer ${BLITZLOG_GITHUB_TOKEN}" \
            -H "Accept: application/vnd.github+json" \
            -H "X-GitHub-Api-Version: 2022-11-28" \
            -H "User-Agent: blitzlog-terraform-drift-check" \
            "${GITHUB_API}/repos/${repo}/hooks" 2>&1); then
        warn "could not list webhooks for ${repo} (curl failed): ${hooks_json}"
        continue
    fi

    # Active hook IDs as a bash array — we re-curl each one to get the full
    # config (the list endpoint omits config for brevity).
    mapfile -t hook_ids < <(printf '%s' "${hooks_json}" \
        | jq -r '.[] | select(.active == true) | .id')
    if [[ ${#hook_ids[@]} -eq 0 ]]; then
        log "    no active webhooks on ${repo}, skipping"
        continue
    fi

    sig_header="sha256=$(probe_signature)"

    for hook_id in "${hook_ids[@]}"; do
        total_hooks_checked=$((total_hooks_checked + 1))
        log ""
        log "    webhook #${hook_id} on ${repo}:"

        if ! hook_json=$(curl -fsSL \
                -H "Authorization: Bearer ${BLITZLOG_GITHUB_TOKEN}" \
                -H "Accept: application/vnd.github+json" \
                -H "X-GitHub-Api-Version: 2022-11-28" \
                -H "User-Agent: blitzlog-terraform-drift-check" \
                "${GITHUB_API}/repos/${repo}/hooks/${hook_id}" 2>&1); then
            warn "    webhook #${hook_id} on ${repo}: GET /hooks/${hook_id} failed: ${hook_json}"
            continue
        fi

        hook_url=$(printf '%s' "${hook_json}" | jq -r '.config.url // ""')
        events=$(printf '%s' "${hook_json}" | jq -r '.events | join(",")')
        secret_marker=$(printf '%s' "${hook_json}" | jq -r '.config.secret // ""')

        if [[ -z "${hook_url}" ]]; then
            warn "    webhook #${hook_id} on ${repo}: missing config.url, skipping probe"
            continue
        fi

        log "      url=${hook_url}  events=${events}"

        if [[ -z "${secret_marker}" ]]; then
            warn "    webhook #${hook_id} on ${repo} (${hook_url}) has NO secret configured on GitHub"
            warn "      set it via:  gh api -X PATCH repos/${repo}/hooks/${hook_id} -f 'config[secret]=<value>'"
            any_drift=1
            total_drift_warnings=$((total_drift_warnings + 1))
            continue
        fi

        # POST the signed probe. -w '%{http_code}' prints the response code on
        # stdout after the body so we don't need a second --write-out dance.
        # --max-time 10 keeps one stuck webhook from blocking the whole apply.
        response_file=$(mktemp /tmp/blitzlog-drift.XXXXXX)
        http_code=$(curl -sS -o "${response_file}" -w '%{http_code}' \
            --max-time 10 \
            -X POST \
            -H "Content-Type: application/json" \
            -H "X-GitHub-Event: issues" \
            -H "X-GitHub-Delivery: drift-check-$(date +%s)-$$" \
            -H "X-Hub-Signature-256: ${sig_header}" \
            --data-binary "${PROBE_PAYLOAD}" \
            "${hook_url}" 2>/dev/null || echo "000")
        response_body=$(head -c 200 "${response_file}" 2>/dev/null || echo "")
        rm -f "${response_file}"

        case "${http_code}" in
            2??)
                log "      probe accepted (HTTP ${http_code}) — secret matches"
                ;;
            401|403)
                warn "    webhook #${hook_id} on ${repo} (${hook_url}) rejected the probe with HTTP ${http_code}"
                warn "      the SSM secret ${BLITZLOG_SSM_PARAM_NAME} no longer matches what GitHub has."
                warn "      receiver said: ${response_body}"
                warn "      fix via:       gh api -X PATCH repos/${repo}/hooks/${hook_id} -f 'config[secret]=<value from terraform.tfvars>'"
                any_drift=1
                total_drift_warnings=$((total_drift_warnings + 1))
                ;;
            000)
                warn "    webhook #${hook_id} on ${repo} (${hook_url}) probe failed (network/timeout)"
                warn "      receiver did not respond within 10s — check the webhook URL is reachable."
                ;;
            *)
                warn "    webhook #${hook_id} on ${repo} (${hook_url}) probe got HTTP ${http_code} (not a clean signal)"
                warn "      receiver said: ${response_body}"
                warn "      a non-401 non-2xx usually means the secret IS matching but the probe payload itself"
                warn "      is being rejected by some downstream check. Inspect CloudWatch logs for the Lambda"
                warn "      to confirm before assuming drift."
                ;;
        esac
    done
done

log ""
log "Drift check finished: ${total_hooks_checked} active webhook(s) probed across ${#repos[@]} repo(s), ${total_drift_warnings} warning(s) emitted."

if [[ "${any_drift}" -eq 1 ]]; then
    warn "one or more webhooks have drifted from the SSM secret — fix before the next real delivery"
    # NON-FATAL: exit 0 so a drift never blocks an apply. The whole point of
    # issue #30 was to surface drift BEFORE 401s start hitting production,
    # not to fail closed.
fi

exit 0
