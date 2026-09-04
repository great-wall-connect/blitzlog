#!/usr/bin/env python3
"""Whisper.cpp STT shim for blitzlog.

Exposes an OpenAI Whisper-compatible /v1/audio/transcriptions endpoint
backed by pywhispercpp (Python bindings for whisper.cpp). The model
lives at $WHISPER_MODEL on disk; inference is in-memory (no JSON file
written to disk).

Multipart parsing uses python_multipart (>=0.0.32) and its
callback API, since the stdlib `cgi` module is deprecated in 3.11 and
broken on 3.12 for non-stdlib multipart encodings (e.g. undici's).
"""

import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO

from python_multipart.multipart import parse_form, parse_options_header
from pywhispercpp.model import Model

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "7878"))
MODEL_PATH = os.environ.get("WHISPER_MODEL", "/opt/whisper-stt/models/ggml-base.en.bin")
LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "en")

_model = None


def get_model():
    global _model
    if _model is None:
        # If the configured MODEL_PATH points at an existing file, pass
        # the full path directly — pywhispercpp uses it without trying
        # to download. Otherwise strip the ggml- prefix and .bin suffix
        # to get the canonical model name (e.g. "base.en") so pywhispercpp
        # can download from HuggingFace.
        if os.path.isfile(MODEL_PATH):
            name = MODEL_PATH
        else:
            name = os.path.basename(MODEL_PATH)
            name = name.removeprefix("ggml-")
            name = name.removesuffix(".bin")
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


def parse_multipart(body, boundary):
    """Parse a multipart body via python_multipart's high-level
    callback API (`parse_form`).

    Returns a dict with keys:
      - "file": bytes | None  — the bytes of the "file" field, if any
      - "prompt": str | None  — the text of the "prompt" field, if any
      - "fields": list[str]   — the parsed field names in order
    """
    out = {"file": None, "prompt": None, "fields": []}

    headers = {
        "Content-Type": ("multipart/form-data; boundary=" + boundary.decode("latin-1")),
        "Content-Length": str(len(body)),
    }

    def on_field(field):
        out["fields"].append(field.field_name)
        if field.field_name == b"prompt":
            out["prompt"] = field.value

    def on_file(file):
        out["fields"].append(file.field_name)
        if file.field_name == b"file" and file.file_object is not None:
            obj = file.file_object
            obj.seek(0)
            out["file"] = obj.read()

    parse_form(headers, BytesIO(body), on_field=on_field, on_file=on_file)
    return out


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
        try:
            _main_type, options = parse_options_header(content_type)
            # parse_options_header returns bytes-keyed options.
            boundary = options.get(b"boundary", b"")
            parsed = parse_multipart(self.rfile.read(), boundary)
        except Exception as e:  # noqa: BLE001 — handler must surface parse errors
            sys.stderr.write(
                f"DEBUG multipart parse failed: content_type={content_type!r} "
                f"error={e}\n"
            )
            sys.stderr.flush()
            self._send_json(400, {"error": f"multipart parse failed: {e}"})
            return

        file_bytes = parsed["file"]
        prompt = parsed["prompt"]
        seen_field_names = parsed["fields"]

        if file_bytes is None:
            # TEMPORARY diagnostic — kept until end-to-end voice transcribes
            # successfully. Confirms which fields the bot actually sends.
            sys.stderr.write(
                f"DEBUG multipart: content_type={content_type!r} "
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
