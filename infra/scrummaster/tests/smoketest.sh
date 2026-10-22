#!/bin/bash
# infra/scrummaster/tests/smoketest.sh — post-apply smoke test (issue #135).
#
# Run from infra/scrummaster/ after `terraform apply`:
#   bash tests/smoketest.sh [ssh options...]
# e.g. bash tests/smoketest.sh -i ~/.ssh/my-ec2-keypair.pem
#
# Reads the EIP from `terraform output`, SSHes in as ubuntu, and asserts
# the #135 acceptance criteria that can be checked on the host.

set -euo pipefail

cd "$(dirname "$0")/.."

IP=$(terraform output -raw public_ip)
TAG=$(terraform output -raw zeroclaw_image_tag)
SSH=(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$@" "ubuntu@${IP}")

FAILED=0
check() {
    local desc="$1"
    shift
    if out=$("${SSH[@]}" "$@" 2>&1); then
        echo "PASS  ${desc}"
        printf '      %s\n' "$out" | head -5
    else
        echo "FAIL  ${desc}"
        printf '      %s\n' "$out" | head -5
        FAILED=1
    fi
}

echo "scrummaster ${IP} (zeroclaw tag ${TAG})"

check "docker installed" 'which docker && docker --version'
check "zeroclaw-host is in the docker group" 'id zeroclaw-host | grep -qw docker && id zeroclaw-host'
check "pinned image ref baked" 'grep -E "^ghcr.io/zeroclaw-labs/zeroclaw@sha256:[0-9a-f]{64}$" /opt/zeroclaw/images/zeroclaw.ref'
check "image loaded at pinned digest" 'sudo docker image inspect "$(cat /opt/zeroclaw/images/zeroclaw.ref)" --format "{{.Id}}"'
check "image runs" 'sudo docker run --rm "$(cat /opt/zeroclaw/images/zeroclaw.ref)" --version'
check "template unit pinned by digest" 'grep -q "zeroclaw@sha256:" /etc/systemd/system/zeroclaw@.service && ! grep -q "@ZEROCLAW_IMAGE_DIGEST@" /etc/systemd/system/zeroclaw@.service'
check "zeroclaw-image-pull enabled" 'test "$(systemctl is-enabled zeroclaw-image-pull)" = enabled'
check "no zeroclaw@ units active (by design until #136)" '! systemctl list-units --type=service --no-legend | grep -q "zeroclaw@"'
check "parent dirs present, root-owned 0755" \
    'for d in /var/lib/zeroclaw /etc/zeroclaw /var/log/zeroclaw; do test "$(stat -c "%U:%G %a" "$d")" = "root:root 755" || { echo "bad: $d"; exit 1; }; done; echo ok'
check "/opt/zeroclaw/images present" 'test -s /opt/zeroclaw/images/zeroclaw.tar.gz && ls -lh /opt/zeroclaw/images/'

if [ "$FAILED" -ne 0 ]; then
    echo "smoketest FAILED"
    exit 1
fi
echo "smoketest PASSED"
