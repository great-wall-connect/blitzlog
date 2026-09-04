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
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO

from python_multipart.multipart import parse_form, parse_options_header
from pywhispercpp.model import Model

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "7878"))
MODEL_PATH = os.environ.get("WHISPER_MODEL", "/opt/whisper-stt/models/ggml-base.en.bin")
LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "en")
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")


def _log(event):
    ts = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    sys.stderr.write(f"{ts} {event}\n")
    sys.stderr.flush()


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
        _model = Model(name, models_dir=os.path.dirname(MODEL_PATH))
    return _model


def ensure_wav(src_path):
    """Convert arbitrary audio to 16kHz mono PCM WAV via ffmpeg.

    pywhispercpp.transcribe() uses Python's stdlib `wave` module, which
    only handles RIFF/WAVE files. Telegram voice notes are OGG/Opus, so
    we convert upstream of `transcribe()`. If `src_path` is already a
    WAV file (RIFF/WAVE magic), return it unchanged — no subprocess.

    Returns the path to a 16kHz mono WAV file (either `src_path` itself
    or a sibling path with a `.wav` suffix).
    """
    with open(src_path, "rb") as f:
        header = f.read(12)
    if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return src_path
    wav_path = src_path.rsplit(".", 1)[0] + ".wav"
    result = subprocess.run(
        [
            FFMPEG_BIN,
            "-y",
            "-loglevel",
            "error",
            "-i",
            src_path,
            "-ar",
            "16000",
            "-ac",
            "1",
            "-f",
            "wav",
            wav_path,
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg conversion failed ({result.returncode}): "
            f"{result.stderr.decode(errors='replace')}"
        )
    return wav_path


def transcribe(wav_path, language, prompt=None):
    """Run pywhispercpp on `wav_path` and return the concatenated text.

    pywhispercpp's `Model.transcribe()` returns different shapes depending
    on the `transcribe_with_meta` flag:

      - default:           list[Segment]    — each segment has `.text`
      - transcribe_with_meta=True: dict     — `{"text": "...", ...}`

    The previous version of this function fell through to `str(result)`
    for the list case, which produced the Segment's `__repr__`
    (e.g. `"[t0=0, t1=200, text=Hello world, probability=nan]"`) instead
    of the actual transcription text.
    """
    result = get_model().transcribe(wav_path, language=language)
    if isinstance(result, dict):
        # transcribe_with_meta=True path.
        text = result.get("text") or ""
    elif isinstance(result, list):
        # Default path: concatenate `.text` from each segment.
        text = "".join(getattr(seg, "text", "") for seg in result)
    else:
        # Unknown shape — best-effort stringify.
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
        _log(f"received file: bytes={len(file_bytes) if file_bytes is not None else 0}")

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

        # The multipart payload is whatever the bot uploaded (e.g. OGG/Opus
        # for Telegram voice notes). Use `.ogg` suffix so the format label
        # matches reality; ensure_wav() will convert via ffmpeg below.
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        wav_path = None
        try:
            wav_path = ensure_wav(tmp_path)
            text = transcribe(wav_path, LANGUAGE, prompt=prompt)
            preview = text if len(text) <= 80 else text[:77] + "..."
            _log(f"transcribed audio: text={preview!r}")
            if self._send_json(200, {"text": text}):
                _log("returned response: status=200")
            else:
                _log("client disconnected during response write")
        except (BrokenPipeError, ConnectionResetError) as e:
            # Client (Telegram bot) aborted the request — typically its
            # `AbortSignal.timeout(60000)` fired while we were still
            # transcribing. Nothing to send; just log and exit quietly.
            _log(f"client disconnected: error={e!r}")
        except Exception as e:  # noqa: BLE001 — handler must surface any failure
            _log(f"transcription failed: error={e!r}")
            self._send_json(500, {"error": f"transcription failed: {e}"})
        finally:
            for p in (tmp_path, wav_path):
                if p:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

    def _send_json(self, status, body):
        """Write a JSON response.

        Returns True if the response was fully written; False if the
        client disconnected before we could complete the write (in
        which case `BrokenPipeError`/`ConnectionResetError` are
        swallowed — there is nothing useful to do on a dead socket)."""
        try:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return False
        return True

    def log_message(self, fmt, *args):
        pass  # suppress access log


if __name__ == "__main__":
    print(
        f"whisper-stt-shim listening on http://{HOST}:{PORT}",
        file=sys.stderr,
        flush=True,
    )
    HTTPServer((HOST, PORT), Handler).serve_forever()
