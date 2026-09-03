# @blitzlog/whisper-stt-shim

Tiny Node.js HTTP shim that exposes a Whisper-compatible `POST /v1/audio/transcriptions` endpoint and forwards requests to a local `whisper.cpp` CLI binary. Used by the blitzlog EC2 agent instance so that the [`@grinev/opencode-telegram-bot`](https://www.npmjs.com/package/@grinev/opencode-telegram-bot) (which natively supports Whisper-format STT) can transcribe voice notes with no external dependency.

## Endpoints

- `GET /healthz` → `{"status":"ok"}` once `whisper-cli` and the model are loaded.
- `POST /v1/audio/transcriptions` — `multipart/form-data` with a `file` field. Optional `model`, `language`, `temperature` form fields. Returns `{"text":"..."}` matching the OpenAI Whisper API.

## Environment variables

| Var | Default | Notes |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address. Keep on loopback. |
| `PORT` | `7878` | Bind port. |
| `WHISPER_CLI` | `/opt/whisper-stt/bin/whisper-cli` | Absolute path to the whisper.cpp CLI binary. |
| `WHISPER_MODEL` | `/opt/whisper-stt/models/ggml-<stt_model>.bin` | Path to the ggml model file. |
| `WHISPER_LANGUAGE` | `en` | `--language` arg passed to whisper-cli; `auto` lets the model detect. |
| `REQUEST_TIMEOUT_MS` | `60000` | Per-request wall-clock timeout. |

## Local development

```bash
# 1. Install whisper.cpp (Ubuntu)
sudo apt install -y build-essential cmake git ffmpeg
git clone --depth 1 https://github.com/ggml-org/whisper.cpp.git ~/whisper.cpp
cmake -B ~/whisper.cpp/build -S ~/whisper.cpp -DCMAKE_BUILD_TYPE=Release
cmake --build ~/whisper.cpp/build -j$(nproc)
sh ~/whisper.cpp/models/download-ggml-model.sh base.en

# 2. Install shim deps
cd packages/whisper-stt-shim
npm install

# 3. Run
WHISPER_CLI=$HOME/whisper.cpp/build/bin/whisper-cli \
WHISPER_MODEL=$HOME/whisper.cpp/models/ggml-base.en.bin \
npm start
```

In another shell:

```bash
curl -s http://127.0.0.1:7878/healthz
# → {"status":"ok"}

curl -s -X POST http://127.0.0.1:7878/v1/audio/transcriptions \
  -F file=@/path/to/voice.ogg
# → {"text":"hello world"}
```

## Production layout (EC2 instance)

| Path | Source |
|---|---|
| `/opt/whisper-stt/bin/whisper-cli` | downloaded from a `whisper.cpp` GitHub release (ARM64 Linux build) |
| `/opt/whisper-stt/models/ggml-<name>.bin` | `aws s3 cp` from `s3://blitzlog-stt-models/models/` (defined in `infra/storage.tf`) |
| `/opt/whisper-stt/server.js` | embedded in `lambda/handler.py` and written by the EC2 user-data script |
| `/opt/whisper-stt/package.json` | ditto |
| `/opt/whisper-stt/node_modules/` | `npm install --omit=dev` at boot |
| `/etc/systemd/system/whisper-stt-shim.service` | `systemd/whisper-stt-shim.service` |
