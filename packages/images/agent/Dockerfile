# syntax=docker/dockerfile:1.7

# Blitzlog agent — single image that runs all services (whisper-stt-shim,
# opencode serve, telegram bot, opencode run) inside one container. The
# container has NO AWS credentials; the host does all SSM/S3 work and passes
# results via env vars and mounted files.
#
# Multi-stage: build stage compiles Python deps + Node deps + builds the
# whisper.cpp CLI from source, then the runtime stage copies only the
# artifacts.

ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=24
ARG WHISPER_CPP_VERSION=v1.7.6
ARG OPENCODE_AI_VERSION=latest
ARG OPENCODE_TELEGRAM_BOT_VERSION=latest

# ===== Stage 1: build (discarded) =====
FROM python:${PYTHON_VERSION}-slim AS build

# Re-declare ARGs inside the build stage so they can be referenced by
# subsequent RUN instructions. ARGs declared before FROM are only
# available in the FROM line itself — not in stage-level instructions.
ARG WHISPER_CPP_VERSION
ARG OPENCODE_AI_VERSION
ARG OPENCODE_TELEGRAM_BOT_VERSION

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false

# Build deps for whisper.cpp: cmake + a C/C++ toolchain (whisper.cpp no
# longer publishes prebuilt `whisper-bin-*-linux-gnu.zip` archives on its
# GitHub releases — they only ship xcframework / Docker images now). We
# build from source instead, which adds ~3 min to the Docker build but
# avoids the 404s we hit trying to fetch the prebuilt zip.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential cmake curl git ca-certificates unzip libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Build whisper.cpp from source for the arm64 / linux-gnu target.
WORKDIR /tmp
RUN WHISPER_CPP_SRC="https://github.com/ggml-org/whisper.cpp/archive/refs/tags/${WHISPER_CPP_VERSION}.tar.gz" \
    && curl --retry 3 --retry-delay 5 --retry-connrefused -fsSL -o whisper.cpp.tar.gz "$WHISPER_CPP_SRC" \
    && mkdir -p whisper.cpp \
    && tar -xzf whisper.cpp.tar.gz --strip-components=1 -C whisper.cpp \
    && cmake -S whisper.cpp -B whisper.cpp/build \
              -DCMAKE_BUILD_TYPE=Release \
              -DBUILD_SHARED_LIBS=OFF \
              -DWHISPER_BUILD_TESTS=OFF \
    && cmake --build whisper.cpp/build --config Release -j"$(nproc)" --target whisper-cli \
    && cp whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper-cli \
    && chmod +x /usr/local/bin/whisper-cli \
    && rm -rf whisper.cpp whisper.cpp.tar.gz \
    && /usr/local/bin/whisper-cli --help >/dev/null

# Python deps (pywhispercpp for the shim's in-memory inference; python-multipart
# for multipart parsing; imageio-ffmpeg to bundle a static ffmpeg so the
# container has no system ffmpeg dependency)
RUN pip install --no-cache-dir \
    pywhispercpp \
    python-multipart \
    imageio-ffmpeg

# Node deps (opencode CLI + telegram-bot package).
# We install opencode and the bot into a temp prefix, then merge them
# into /usr/local. The bot's transitive `better-sqlite3` dep ships
# prebuilt native binaries via prebuild-install for arm64-linux; if
# prebuild-install finds a matching binary, no native compilation is
# attempted and the Python gyp toolchain isn't needed. We install
# python3 + make + g++ as a defensive fallback for the build-from-
# source path; the runtime container doesn't need them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        nodejs npm python3 python3-dev make g++ \
    && rm -rf /var/lib/apt/lists/* \
    && npm config set fund false \
    && npm config set audit false \
    && export npm_config_target_arch=arm64 \
    && export npm_config_target_platform=linux \
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
