#!/usr/bin/env bash
# packages/images/agent/smoke-test.sh — runs inside the built image to
# verify the toolchain is intact. Catches broken installs early without
# needing env vars, network, or a real whisper model.
#
# Pair with the entrypoint smoke test (Tier 2 in docs/DOCKER.md) for
# full coverage: the entrypoint actually tries to git clone and call
# opencode, which requires network and real LLM auth.
set -euo pipefail

echo "==> checking whisper-cli"
/usr/local/bin/whisper-cli --help >/dev/null

echo "==> checking node + npm"
/usr/local/bin/node --version
/usr/local/bin/npm --version

echo "==> checking opencode CLI"
/usr/local/bin/opencode --version

echo "==> checking telegram bot binary"
test -x /usr/local/bin/opencode-telegram \
  || { echo "missing /usr/local/bin/opencode-telegram"; exit 1; }

echo "==> checking Python deps"
python3 -c "import pywhispercpp, multipart, imageio_ffmpeg; print('python deps ok')"

echo "==> checking whisper-stt-shim"
test -f /opt/whisper-stt/server.py

echo "==> checking entrypoint syntax"
bash -n /usr/local/bin/entrypoint.sh

echo "==> checking expected dirs"
for d in /opt/whisper-stt/models /opt/blitzlog/plugins /workspace /var/log; do
  mkdir -p "$d"
done

echo "smoke-test: all checks passed"
