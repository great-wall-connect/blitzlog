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
# NODE_VERSION must be a full semver (e.g. 24.4.1) — nodejs.org/dist only
# serves versioned dirs like v24.4.1/, not the floating v24/. Update
# both the Dockerfile and any workflow callers when bumping Node.
ARG NODE_VERSION=24.4.1
ARG WHISPER_CPP_VERSION=v1.7.6
ARG OPENCODE_AI_VERSION=latest
ARG OPENCODE_TELEGRAM_BOT_VERSION=latest

# ===== Stage 1: build (discarded) =====
FROM python:${PYTHON_VERSION}-slim AS build

# Re-declare ARGs inside the build stage so they can be referenced by
# subsequent RUN instructions. ARGs declared before FROM are only
# available in the FROM line itself — not in stage-level instructions.
ARG NODE_VERSION
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
#
# Debian trixie ships nodejs 20.x, but `@grinev/opencode-telegram-bot`
# and `better-sqlite3` require node >=22. Install node from the official
# tarball (matching the original bootstrap's approach) at build time
# so the bot's engines check passes.
#
# `better-sqlite3` ships N-API prebuilt binaries at
# `prebuilds/linux-arm64.node` inside its npm tarball — prebuild-install
# fetches them at install time without invoking node-gyp, so no Python
# gyp toolchain is needed for the happy path.
#
# We still install python3 + make + g++ as a defensive fallback for the
# build-from-source path (extremely unlikely on arm64 with our node version).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        npm python3 python3-dev make g++ xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Install Node ${NODE_VERSION} from the official tarball
RUN NODE_TARBALL="node-v${NODE_VERSION}-linux-arm64.tar.xz" \
    && curl --retry 3 --retry-delay 5 --retry-connrefused -fsSL \
        -o "/tmp/${NODE_TARBALL}" \
        "https://nodejs.org/dist/v${NODE_VERSION}/${NODE_TARBALL}" \
    && tar -xJ -C /usr/local -f "/tmp/${NODE_TARBALL}" \
    && rm "/tmp/${NODE_TARBALL}" \
    && ln -sf "/usr/local/node-v${NODE_VERSION}-linux-arm64/bin/node" /usr/local/bin/node \
    && ln -sf "/usr/local/node-v${NODE_VERSION}-linux-arm64/bin/npm" /usr/local/bin/npm \
    && ln -sf "/usr/local/node-v${NODE_VERSION}-linux-arm64/bin/npx" /usr/local/bin/npx \
    && node --version \
    && npm --version

# Install opencode + bot globally. `npm install` will pick up the
# better-sqlite3 prebuilt for our platform (arm64-linux + node ${NODE_VERSION}).
#
# `npm config set prefix /usr/local` is critical: the tarball-installed
# npm (at /usr/local/node-v${NODE_VERSION}-linux-arm64/bin/npm) computes
# its global prefix from its own install location by default, which puts
# new binaries in /usr/local/node-${NODE_VERSION}-linux-arm64/bin/. That
# directory isn't on the default $PATH, so `opencode --version` below
# would fail with `opencode: not found`. Pinning the prefix to /usr/local
# makes `npm install -g` write binaries to /usr/local/bin/.
RUN npm config set fund false \
    && npm config set audit false \
    && npm config set prefix /usr/local \
    && npm install -g \
        opencode-ai@${OPENCODE_AI_VERSION} \
        @grinev/opencode-telegram-bot@${OPENCODE_TELEGRAM_BOT_VERSION} \
    && npm cache clean --force \
    && opencode --version

# ===== Stage 2: runtime =====
FROM python:${PYTHON_VERSION}-slim

ARG NODE_VERSION

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/usr/local/bin:/usr/bin:/bin

# Runtime deps only. libgomp1 is required by whisper.cpp's matrix multiplications.
# nodejs is re-installed via tarball in this stage too (symlinked via
# /usr/local/bin/node), so we don't pull Debian's older nodejs here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates curl wget jq libgomp1 openssl xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /var/cache/apt/*

# Re-install Node ${NODE_VERSION} in the runtime layer (the COPY below
# only takes the symlinks and /usr/local/lib/node_modules; the rest of
# the node tree is built from tarball here so the runtime image is
# self-contained).
RUN NODE_TARBALL="node-v${NODE_VERSION}-linux-arm64.tar.xz" \
    && curl --retry 3 --retry-delay 5 --retry-connrefused -fsSL \
        -o "/tmp/${NODE_TARBALL}" \
        "https://nodejs.org/dist/v${NODE_VERSION}/${NODE_TARBALL}" \
    && tar -xJ -C /usr/local -f "/tmp/${NODE_TARBALL}" \
    && rm "/tmp/${NODE_TARBALL}" \
    && ln -sf "/usr/local/node-v${NODE_VERSION}-linux-arm64/bin/node" /usr/local/bin/node \
    && ln -sf "/usr/local/node-v${NODE_VERSION}-linux-arm64/bin/npm" /usr/local/bin/npm \
    && ln -sf "/usr/local/node-v${NODE_VERSION}-linux-arm64/bin/npx" /usr/local/bin/npx \
    && npm config set prefix /usr/local

# Copy built artifacts from stage 1
COPY --from=build /usr/local/bin/whisper-cli /usr/local/bin/whisper-cli
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=build /usr/local/bin/opencode /usr/local/bin/opencode
COPY --from=build /usr/local/bin/opencode-telegram /usr/local/bin/opencode-telegram

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
