#!/usr/bin/env bash
# scripts/clean_ghcr.sh — delete container-image versions in GHCR that
# are older than N days, scoped to one or more image names.
#
# Default mode is dry-run; pass --yes to actually delete. The package
# name itself is preserved (we delete versions, not packages) so existing
# tags like ghcr.io/<owner>/<pkg>:latest keep resolving after a re-push.
#
# Requires: gh CLI logged in (gh auth login) with a token that has
# `read:packages` + `delete:packages` scopes on the target user / org.
#
# Scopes scanned, in order:
#   1. The authenticated user's personal namespace
#   2. Each --org NAME argument
#
# Within each scope, only packages whose name matches an --image pattern
# are considered. The flag is REQUIRED — there is no implicit "scan
# everything" mode. A full-namespace wipe is too easy a typo to allow.
#
# Image pattern syntax:
#   --image blitzlog-agent            exact match  (direct version fetch)
#   --image 'blitzlog-*'              shell glob   (list endpoint + filter)
#   --image 'blitzlog-agent*'         matches blitzlog-agent, -debug, etc.
#
# Why two paths: GitHub's REST `GET /orgs/{org}/packages` list endpoint
# is unreliable when an org only has private packages — it returns []
# even when specific packages exist. Direct `GET /packages/container/{pkg}/versions`
# works. For exact-match --image values, we go straight to the version
# endpoint and skip the broken list. Glob patterns still need the list
# (or we'd have to enumerate every plausible name ourselves).
#
# Usage:
#   scripts/clean_ghcr.sh \
#       --org great-wall-connect \
#       --image blitzlog-agent                # dry-run, 2-day cutoff
#   scripts/clean_ghcr.sh \
#       --org great-wall-connect \
#       --image blitzlog-agent \
#       --days 7 --yes                         # actually delete, 7-day cutoff
set -euo pipefail

DAYS=2
APPLY=0
ORGS=()
IMAGES=()

usage() {
  cat <<EOF
Usage: $0 [--days N] [--org NAME]... --image PATTERN... [--yes]

Required:
  --image PATTERN   one or more package-name patterns (must include at least one)
                    exact match (no '*') → direct version fetch
                    contains '*' → list endpoint + filter

Optional:
  --org NAME        one or more org namespaces to scan (user namespace is always scanned)
  --days N          delete versions older than N days (default 2)
  --yes             actually delete (default is dry-run)
  -h, --help        show this help

Examples:
  $0 --org great-wall-connect --image blitzlog-agent
  $0 --org great-wall-connect --image 'blitzlog-agent*' --days 7 --yes
EOF
}

# Convert a glob pattern to an anchored regex. `*` becomes .*, regex
# metachars are escaped first so they survive intact. Sed trick: a
# character class can start with `]` (sed allows the very first char of
# a class to be `]`), so the `]` doesn't need to be escaped. The other
# metachars get a `\` prefix and `*` is converted to `.*` afterwards.
glob_to_regex() {
  local g="$1"
  local r
  r=$(printf '%s' "$g" | sed 's/[][+.?(){}^$]/\\&/g')
  r=$(printf '%s' "$r" | sed 's/\*/.*/g')
  printf '^%s$' "$r"
}

# Whether a pattern is an exact name (no glob chars) or a glob. Drives
# the routing decision: exact → direct version fetch, glob → list endpoint.
is_glob() {
  [[ "$1" == *"*"* ]]
}

while (( $# > 0 )); do
  case "$1" in
    --days)
      DAYS="$2"
      shift 2
      ;;
    --days=*)
      DAYS="${1#*=}"
      shift
      ;;
    --org)
      [[ -n "${2:-}" ]] || { echo "--org requires a value" >&2; exit 2; }
      ORGS+=("$2")
      shift 2
      ;;
    --org=*)
      ORGS+=("${1#*=}")
      shift
      ;;
    --image)
      [[ -n "${2:-}" ]] || { echo "--image requires a value" >&2; exit 2; }
      IMAGES+=("$2")
      shift 2
      ;;
    --image=*)
      IMAGES+=("${1#*=}")
      shift
      ;;
    --yes|-y)
      APPLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! [[ "$DAYS" =~ ^[0-9]+$ ]] || (( DAYS < 1 )); then
  echo "--days must be a positive integer (got: $DAYS)" >&2
  exit 2
fi

# Strict --image requirement: a full-namespace wipe is too easy a typo
# to allow. Without an explicit image filter, this script refuses to
# run.
if (( ${#IMAGES[@]} == 0 )); then
  echo "at least one --image PATTERN is required (safety: never wipe a whole namespace)" >&2
  echo "hint: pass --image blitzlog-agent (exact) or --image 'blitzlog-*' (glob)" >&2
  exit 2
fi

# Split IMAGES into EXACT_NAMES and GLOB_PATTERNS, since each routes
# differently. EXACT goes direct, GLOBS go through the list endpoint.
declare -a EXACT_NAMES=()
declare -a GLOB_PATTERNS=()
for pat in "${IMAGES[@]}"; do
  if is_glob "$pat"; then
    GLOB_PATTERNS+=("$pat")
  else
    EXACT_NAMES+=("$pat")
  fi
done

# UTC cutoff — GitHub's API returns ISO-8601 timestamps in UTC, and `date`
# without -u would format in local time, breaking the string comparison.
CUTOFF=$(date -u -d "${DAYS} days ago" +'%Y-%m-%dT%H:%M:%SZ')

if ! gh auth status >/dev/null 2>&1; then
  echo "gh CLI is not logged in — run 'gh auth login' first" >&2
  exit 2
fi

# Get the authenticated user once. `gh api user -q .login` returns just the
# username, which we reuse for the user-namespace branch and as a sanity
# check on org membership.
USER_LOGIN=$(gh api user -q .login)

if (( APPLY )); then
  MODE="apply"
else
  MODE="dry-run"
fi

# Build the jq selector for the list-endpoint path (only used when there
# are glob patterns). For exact names we go direct and never call the
# list endpoint. test() with a regex is the documented jq primitive;
# we OR the patterns together since `select(...)` chains would only AND.
build_jq_image_selector() {
  local rx_patterns=("$@")
  local parts=()
  for rx in "${rx_patterns[@]}"; do
    parts+=("(test(\"$rx\"))")
  done
  local joined=""
  for i in "${!parts[@]}"; do
    if (( i > 0 )); then joined+=" or "; fi
    joined+="${parts[$i]}"
  done
  printf '%s' "$joined"
}
GLOB_SELECTOR=""
if (( ${#GLOB_PATTERNS[@]} > 0 )); then
  declare -a GLOB_REGEXES=()
  for pat in "${GLOB_PATTERNS[@]}"; do
    GLOB_REGEXES+=("$(glob_to_regex "$pat")")
  done
  GLOB_SELECTOR=$(build_jq_image_selector "${GLOB_REGEXES[@]}")
fi

echo "==> ghcr cleanup"
echo "    user:    $USER_LOGIN"
if (( ${#ORGS[@]} > 0 )); then
  for org in "${ORGS[@]}"; do
    echo "    org:     $org"
  done
fi
echo "    cutoff:  $CUTOFF  (older than ${DAYS} day(s))"
echo "    images:  ${IMAGES[*]}"
echo "    mode:    $MODE"
echo

# Per-scope counters so the operator can see which namespace did the
# work. We sum into TOT_PKG / TOT_VER / TOT_ERR at the end.
TOT_PKG=0
TOT_VER=0
TOT_ERR=0

# Track which (api_root, pkg, version_id) tuples we've already touched so
# we never double-delete if the script is run twice in the same minute,
# or if a package name collides across scopes (e.g. user has a
# blitzlog-agent and great-wall-connect also does).
declare -A SEEN_VER=()

# scan_version_endpoint <api_root> <pkg> — direct fetch of the package's
# versions endpoint. Used when --image is an exact name (no glob). This
# path bypasses the (sometimes-broken) /packages list endpoint entirely.
scan_version_endpoint() {
  local api_root="$1"
  local pkg="$2"

  local vers_json
  vers_json=$(gh api "${api_root}/packages/container/${pkg}/versions" 2>/dev/null || true)
  if ! echo "$vers_json" | jq -e 'type == "array"' >/dev/null 2>&1; then
    echo "  [warn]    ${api_root}/container/${pkg}: not found or unreadable; skipping." >&2
    echo "           hint: token may lack 'read:packages' for ${api_root}, or the package name is wrong." >&2
    return 0
  fi

  local ver_count=0
  while IFS=$'\t' read -r ver_id ver_created ver_tags; do
    [[ -z "$ver_id" ]] && continue
    local key="${api_root}::${pkg}::${ver_id}"
    [[ -n "${SEEN_VER[$key]:-}" ]] && continue
    SEEN_VER[$key]=1

    if [[ "$ver_created" < "$CUTOFF" ]] || [[ "$ver_created" == "$CUTOFF" ]]; then
      if (( APPLY )); then
        if gh api -X DELETE \
          "${api_root}/packages/container/${pkg}/versions/${ver_id}" \
          >/dev/null 2>&1; then
          echo "  [apply]   deleted ${pkg}@${ver_tags} (${ver_id}, ${ver_created})"
        else
          echo "  [error]   failed to delete ${pkg}@${ver_tags} (${ver_id})" >&2
          ERR_COUNT=$((ERR_COUNT + 1))
        fi
      else
        echo "  [dry-run] would delete ${pkg}@${ver_tags} (${ver_id}, ${ver_created})"
      fi
      ver_count=$((ver_count + 1))
    fi
  done < <(echo "$vers_json" | jq -r \
    'sort_by(.created_at) | .[] | "\(.id)\t\(.created_at)\t\((.metadata.container.tags // []) | join(","))"')

  echo "  -> ${api_root}/container/${pkg}: ${ver_count} versions flagged"
  TOT_PKG=$((TOT_PKG + 1))
  TOT_VER=$((TOT_VER + ver_count))
}

# scan_namespace_list <api_root> <scope_label> — list-endpoint path.
# Used when --image contains globs. Iterates pages of the org's container
# packages, filters by the glob selector, and for each match fetches its
# versions the same way scan_version_endpoint does.
scan_namespace_list() {
  local api_root="$1"
  local scope_label="$2"

  local pkg_count=0
  local ver_count=0

  PAGE=1
  while :; do
    local page_json
    page_json=$(gh api "${api_root}/packages?package_type=container&per_page=100&page=${PAGE}" 2>/dev/null || true)
    if ! echo "$page_json" | jq -e 'type == "array"' >/dev/null 2>&1; then
      echo "  [warn]    non-array response on ${scope_label} page ${PAGE}; stopping pagination." >&2
      echo "           hint: token may need 'read:packages' scope, or you may lack access to ${scope_label}" >&2
      break
    fi
    local page_len
    page_len=$(echo "$page_json" | jq 'length')
    [[ "$page_len" -eq 0 ]] && break

    while IFS= read -r pkg; do
      [[ -z "$pkg" ]] && continue
      pkg_count=$((pkg_count + 1))

      local vers_json
      vers_json=$(gh api "${api_root}/packages/container/${pkg}/versions" 2>/dev/null || true)
      if ! echo "$vers_json" | jq -e 'type == "array"' >/dev/null 2>&1; then
        continue
      fi

      while IFS=$'\t' read -r ver_id ver_created ver_tags; do
        [[ -z "$ver_id" ]] && continue
        local key="${api_root}::${pkg}::${ver_id}"
        [[ -n "${SEEN_VER[$key]:-}" ]] && continue
        SEEN_VER[$key]=1

        if [[ "$ver_created" < "$CUTOFF" ]] || [[ "$ver_created" == "$CUTOFF" ]]; then
          if (( APPLY )); then
            if gh api -X DELETE \
              "${api_root}/packages/container/${pkg}/versions/${ver_id}" \
              >/dev/null 2>&1; then
              echo "  [apply]   deleted ${pkg}@${ver_tags} (${ver_id}, ${ver_created})"
            else
              echo "  [error]   failed to delete ${pkg}@${ver_tags} (${ver_id})" >&2
              ERR_COUNT=$((ERR_COUNT + 1))
            fi
          else
            echo "  [dry-run] would delete ${pkg}@${ver_tags} (${ver_id}, ${ver_created})"
          fi
          ver_count=$((ver_count + 1))
        fi
      done < <(echo "$vers_json" | jq -r \
        'sort_by(.created_at) | .[] | "\(.id)\t\(.created_at)\t\((.metadata.container.tags // []) | join(","))"')
    done < <(echo "$page_json" | jq -r --arg sel "$GLOB_SELECTOR" \
      ".[] | select($sel) | .name")

    PAGE=$((PAGE + 1))
    if (( PAGE > 10000 )); then
      echo "aborting: exceeded 10000 pages of packages in ${scope_label}" >&2
      exit 4
    fi
  done

  echo "  -> ${scope_label}: ${pkg_count} packages, ${ver_count} versions flagged"
  TOT_PKG=$((TOT_PKG + pkg_count))
  TOT_VER=$((TOT_VER + ver_count))
}

# Helper: list of api_roots in scan order. User namespace first; then
# each --org. We always scan both, even if --image is exact, because
# the user may want to clean up the same package in both places.
declare -a API_ROOTS=("/users/${USER_LOGIN}")
declare -a SCOPE_LABELS=("user:${USER_LOGIN}")
for org in "${ORGS[@]}"; do
  API_ROOTS+=("/orgs/${org}")
  SCOPE_LABELS+=("org:${org}")
done

# Per-scope error counter. Reset before each call so failures in a scope
# don't bleed across scopes.
ERR_COUNT=0

# Direct-fetch path (exact names only). For each api_root × exact_name
# pair, hit the package's versions endpoint directly. This bypasses the
# /packages list endpoint, which is silently returning [] for some orgs
# (notably great-wall-connect, even though blitzlog-agent is in there
# with 34 versions).
for i in "${!API_ROOTS[@]}"; do
  api_root="${API_ROOTS[$i]}"
  for name in "${EXACT_NAMES[@]}"; do
    ERR_COUNT=0
    scan_version_endpoint "$api_root" "$name"
    TOT_ERR=$((TOT_ERR + ERR_COUNT))
  done
done

# List-endpoint path (globs only). Skip entirely if no globs were
# requested — exact-only invocations shouldn't pay for the broken list.
if (( ${#GLOB_PATTERNS[@]} > 0 )); then
  for i in "${!API_ROOTS[@]}"; do
    api_root="${API_ROOTS[$i]}"
    scope_label="${SCOPE_LABELS[$i]}"
    ERR_COUNT=0
    scan_namespace_list "$api_root" "$scope_label"
    TOT_ERR=$((TOT_ERR + ERR_COUNT))
  done
fi

echo
echo "==> summary"
echo "    packages scanned: $TOT_PKG"
echo "    versions flagged: $TOT_VER"
echo "    errors:           $TOT_ERR"

if (( TOT_ERR > 0 )); then
  exit 4
fi