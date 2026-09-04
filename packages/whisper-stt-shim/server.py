#!/usr/bin/env python3
"""Whisper.cpp STT shim for blitzlog.

Exposes an OpenAI Whisper-compatible /v1/audio/transcriptions endpoint
backed by pywhispercpp (Python bindings for whisper.cpp). The model
lives at $WHISPER_MODEL on disk; inference is in-memory (no JSON file
written to disk).

Multipart parsing uses python-multipart (FastAPI's parser) rather
than the deprecated stdlib `cgi` module — `cgi` is broken on Python
3.12 for the multipart format the Telegram bot (undici) sends.
"""

import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer

from multipart.multipart import MultipartParser, parse_options_header
from pywhispercpp.model import Model

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "7878"))
MODEL_PATH = os.environ.get("WHISPER_MODEL", "/opt/whisper-stt/models/ggml-base.en.bin")
LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "en")

_model = None


def get_model():
    global _model
    if _model is None:
        # ggml-base.en.bin -> "base.en" (pywhispercpp naming)
        name = os.path.basename(MODEL_PATH).removeprefix("ggml-").removesuffix(".bin")
        _model = Model(name, models_dir=os.path.dirname(MODEL_PATH))
    return _model


def transcribe(wav_path, language, prompt=None):
    args = [
        "-m",
        MODEL_PATH,
        "-f",
        wav_path,
        "--language",
        language or "auto",
        "--no-timestamps",
        "--print-special",
        "false",
        "--output-json",
    ]
    if prompt:
        args.extend(["--prompt", prompt])
    result = get_model().transcribe(wav_path, language=language)
    if isinstance(result, dict):
        text = result.get("text") or ""
    else:
        text = str(result)
    return text.strip()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/v1/audio/transcriptions":
            self.send_error(404)
            return

        content_type = self.headers.get("Content-Type", "")
        _main_type, options = parse_options_header(content_type)
        boundary = options.get("boundary", "").encode("ascii")
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        parser = MultipartParser(
            self.rfile,
            boundary=boundary,
            content_length=content_length,
        )

        file_bytes = None
        prompt = None
        seen_field_names = []
        try:
            for part in parser.parse():
                seen_field_names.append(part.name)
                if part.name == "file":
                    file_bytes = part.file.read()
                    part.file.close()
                elif part.name == "prompt":
                    # python-multipart exposes text fields via .text
                    # (decoded). Fall back to reading .file bytes if not.
                    raw = getattr(part, "text", None)
                    if raw is None and getattr(part, "file", None) is not None:
                        raw = part.file.read().decode("utf-8", errors="replace")
                        part.file.close()
                    prompt = raw
        except Exception as e:  # noqa: BLE001 — surface multipart parse errors
            sys.stderr.write(
                f"DEBUG multipart parse failed: content_type={content_type!r} "
                f"content_length={content_length} "
                f"error={e}\n"
            )
            sys.stderr.flush()
            self._send_json(400, {"error": f"multipart parse failed: {e}"})
            return

        if file_bytes is None:
            # TEMPORARY diagnostic — kept until end-to-end voice transcribes
            # successfully. Confirms which fields the bot actually sends.
            sys.stderr.write(
                f"DEBUG multipart: content_type={content_type!r} "
                f"content_length={content_length} "
                f"seen_fields={seen_field_names} "
                f"prompt_received={prompt is not None}\n"
            )
            sys.stderr.flush()
            self._send_json(400, {"error": "missing 'file' field"})
            return

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        try:
            text = transcribe(tmp_path, LANGUAGE, prompt=prompt)
            self._send_json(200, {"text": text})
        except Exception as e:  # noqa: BLE001 — handler must surface any failure
            self._send_json(500, {"error": f"transcription failed: {e}"})
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass  # suppress access log


if __name__ == "__main__":
    print(
        f"whisper-stt-shim listening on http://{HOST}:{PORT}",
        file=sys.stderr,
        flush=True,
    )
    HTTPServer((HOST, PORT), Handler).serve_forever()
