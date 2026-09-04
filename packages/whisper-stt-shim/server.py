#!/usr/bin/env python3
"""Whisper.cpp STT shim for blitzlog.

Exposes an OpenAI Whisper-compatible /v1/audio/transcriptions endpoint
backed by pywhispercpp (Python bindings for whisper.cpp). The model
lives at $WHISPER_MODEL on disk; inference is in-memory (no JSON file
written to disk).
"""

import cgi
import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer

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


def transcribe(wav_path, language):
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
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers)
        if "file" not in form:
            # TEMPORARY diagnostic — removed once the Telegram bot's
            # multipart shape is known. Helps us distinguish "wrong field
            # name" (keys=['audio']) from "cgi parse failure" (keys=[]).
            sys.stderr.write(
                f"DEBUG cgi parse: content_type={self.headers.get('Content-Type')!r} "
                f"content_length={self.headers.get('Content-Length')!r} "
                f"keys={list(form.keys())}\n"
            )
            sys.stderr.flush()
            self._send_json(400, {"error": "missing 'file' field"})
            return
        f = form["file"]
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(f.file.read())
            tmp_path = tmp.name
        try:
            text = transcribe(tmp_path, LANGUAGE)
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
