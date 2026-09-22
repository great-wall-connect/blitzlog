# syntax=docker/dockerfile:1.7

# Blitzlog agent — single image that runs all services (whisper-stt-shim,
# opencode serve, telegram bot, opencode run) inside one container. The
# container has NO AWS credentials; the host does all SSM/S3 work and passes
# results via env vars and mounted files.
#
# Multi-stage: build stage compiles Python deps + Node deps + downloads the
# whisper.cpp prebuilt, then the runtime stage copies only the artifacts.

ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=24
ARG WHISPER_CPP_VERSION=v1.7.6
ARG WHISPER_CPP_RELEASE_URL=https://github.com/ggml-org/whisper.cpp/releases/download/${WHISPER_CPP_VERSION}/whisper-bin-aarch64-linux-gnu.zip
ARG OPENCODE_AI_VERSION=latest
ARG OPENCODE_TELEGRAM_BOT_VERSION=latest

# ===== Stage 1: build (discarded) =====
FROM python:${PYTHON_VERSION}-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false

# Build deps for whisper.cpp (none on debian-slim — we use the prebuilt).
# We do need wget for the whisper.cpp zip download (curl is in the runtime stage).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        wget ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/*

# Download whisper.cpp prebuilt binary (aarch64-linux-gnu)
WORKDIR /tmp
RUN wget -q "${WHISPER_CPP_RELEASE_URL}" -O whisper-prebuilt.zip \
    && unzip -q whisper-prebuilt.zip -d whisper-prebuilt \
    && find whisper-prebuilt -name whisper-cli -type f -exec cp {} /usr/local/bin/whisper-cli \; \
    && chmod +x /usr/local/bin/whisper-cli \
    && rm -rf whisper-prebuilt whisper-prebuilt.zip \
    && /usr/local/bin/whisper-cli --help >/dev/null

# Python deps (pywhispercpp for the shim's in-memory inference; python-multipart
# for multipart parsing; imageio-ffmpeg to bundle a static ffmpeg so the
# container has no system ffmpeg dependency)
RUN pip install --no-cache-dir \
    pywhispercpp \
    python-multipart \
    imageio-ffmpeg

# Node deps (opencode CLI + telegram-bot package)
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g \
        opencode-ai@${OPENCODE_AI_VERSION} \
        @grinev/opencode-telegram-bot@${OPENCODE_TELEGRAM_BOT_VERSION} \
    && npm cache clean --force \
    && node --version \
    && npm --version \
    && opencode --version

# ===== Stage 2: runtime =====
FROM python:${PYTHON_VERSION}-slim

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/usr/local/bin:/usr/bin:/bin

# Runtime deps only. libgomp1 is required by whisper.cpp's matrix multiplications.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates curl wget jq libgomp1 openssl \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /var/cache/apt/*

# Copy built artifacts from stage 1
COPY --from=build /usr/local/bin/whisper-cli /usr/local/bin/whisper-cli
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin /usr/local/bin
COPY --from=build /usr/lib /usr/lib
COPY --from=build /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=build /usr/local/bin/opencode /usr/local/bin/opencode
COPY --from=build /usr/local/bin/opencode-telegram-bot /usr/local/bin/opencode-telegram-bot

# Shim + entrypoint
COPY server.py /opt/whisper-stt/server.py
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# Layout that the entrypoint expects
RUN mkdir -p /opt/whisper-stt/models /opt/blitzlog/plugins /workspace /var/log \
    && chmod +x /usr/local/bin/entrypoint.sh

# Sanity checks
RUN /usr/local/bin/whisper-cli --help >/dev/null \
    && python3 -c "import pywhispercpp, multipart, imageio_ffmpeg; print('python deps ok')" \
    && opencode --version

WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
