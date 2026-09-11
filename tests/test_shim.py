"""Tests for the whisper-stt-shim HTTP server in
`packages/whisper-stt-shim/server.py`. These exercise the shim's
multipart parser, audio converter, transcribe helper, broken-pipe
handling, and end-to-end HTTP handler — independent of the Lambda
code path.
"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from io import BytesIO, StringIO
from unittest.mock import MagicMock, patch


def _load_shim_server():
    """Load packages/whisper-stt-shim/server.py via importlib so we can
    test the actual HTTP handler (the file is embedded in the bootstrap
    heredoc, not installed as a package)."""
    # The shim imports pywhispercpp at module load. The test environment
    # doesn't have pywhispercpp installed, so inject a stub before the
    # import so exec_module doesn't blow up on a missing dependency.
    # The actual transcribe logic is mocked in each test.
    pywhispercpp_stub = types.ModuleType("pywhispercpp")
    pywhispercpp_stub.__dict__["__path__"] = []
    pywhispercpp_model = types.ModuleType("pywhispercpp.model")
    pywhispercpp_model.Model = (
        object  # placeholder; tests mock get_model() or transcribe()
    )
    pywhispercpp_stub.model = pywhispercpp_model
    sys.modules.setdefault("pywhispercpp", pywhispercpp_stub)
    sys.modules.setdefault("pywhispercpp.model", pywhispercpp_model)

    # Same trick for imageio_ffmpeg — the shim imports it eagerly and
    # calls get_ffmpeg_exe() at module load. Stub a fake one so the
    # import doesn't require the real package in the test venv.
    imageio_ffmpeg_stub = types.ModuleType("imageio_ffmpeg")
    imageio_ffmpeg_stub.get_ffmpeg_exe = lambda: "/usr/bin/ffmpeg"
    sys.modules.setdefault("imageio_ffmpeg", imageio_ffmpeg_stub)

    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    shim_path = os.path.join(repo_root, "packages", "whisper-stt-shim", "server.py")
    spec = importlib.util.spec_from_file_location("shim_server", shim_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestParseMultipart(unittest.TestCase):
    """Unit tests for the shim's `parse_multipart` helper.

    These exercise the real python_multipart callback flow with real
    multipart bodies — no mocking of the parser itself.
    """

    @classmethod
    def setUpClass(cls):
        cls.shim_server = _load_shim_server()

    @staticmethod
    def _multipart(fields, boundary="----TestBoundary"):
        """Build a real multipart/form-data body from a list of fields.

        Each field is one of:
          - (name, value)              — text field, value is str
          - (name, filename, bytes, ct) — file field
        Returns (boundary_str, body_bytes).
        """
        body = bytearray()
        for f in fields:
            name = f[0]
            if len(f) == 2:
                value = f[1]
                body += (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode()
            else:
                _filename, content, ctype = f[1], f[2], f[3]
                body += (
                    (
                        f"--{boundary}\r\n"
                        f'Content-Disposition: form-data; name="{name}"; filename="{_filename}"\r\n'
                        f"Content-Type: {ctype}\r\n\r\n"
                    ).encode()
                    + content
                    + b"\r\n"
                )
        body += f"--{boundary}--\r\n".encode()
        return boundary, bytes(body)

    def test_parse_multipart_extracts_file_field(self):
        boundary, body = self._multipart(
            [
                ("file", "audio.wav", b"FAKE_WAV_DATA", "audio/wav"),
            ]
        )
        parsed = self.shim_server.parse_multipart(body, boundary.encode("ascii"))
        self.assertEqual(parsed["file"], b"FAKE_WAV_DATA")
        self.assertIsNone(parsed["prompt"])
        self.assertEqual(parsed["fields"], [b"file"])

    def test_parse_multipart_extracts_prompt_field(self):
        boundary, body = self._multipart(
            [
                ("file", "audio.wav", b"DATA", "audio/wav"),
                ("prompt", "The following text is..."),
            ]
        )
        parsed = self.shim_server.parse_multipart(body, boundary.encode("ascii"))
        self.assertEqual(parsed["file"], b"DATA")
        self.assertEqual(parsed["prompt"], b"The following text is...")
        self.assertEqual(parsed["fields"], [b"file", b"prompt"])

    def test_parse_multipart_empty_body(self):
        # Empty body — parse_multipart returns an empty result (no parts).
        result = self.shim_server.parse_multipart(b"", b"----boundary")
        self.assertIsNone(result.get("file"))
        self.assertIsNone(result.get("prompt"))
        self.assertEqual(result.get("fields"), [])

    def test_shim_installs_python_multipart(self):
        """python-multipart (new name: python_multipart) is the modern,
        robust multipart parser. cgi is deprecated in 3.11, removed in
        3.13. The bootstrap installs it as a replacement."""
        from _common import _install_whisper_stt_script

        script = _install_whisper_stt_script()
        self.assertIn("python-multipart", script)


class TestAudioConversion(unittest.TestCase):
    """ensure_wav converts non-WAV inputs to 16kHz mono WAV via ffmpeg,
    and passes through inputs that are already RIFF/WAVE."""

    @classmethod
    def setUpClass(cls):
        cls.shim_server = _load_shim_server()

    def _write_tmp(self, content):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        return path

    def test_ensure_wav_passthrough_for_wav(self):
        # RIFF header (4 bytes) + chunk size + WAVE (4 bytes) = first 12 bytes.
        wav_header = b"RIFF\x00\x00\x00\x00WAVEfmt "
        src = self._write_tmp(wav_header + b"\x00" * 64)
        try:
            with patch("subprocess.run") as mock_run:
                result = self.shim_server.ensure_wav(src)
            self.assertEqual(result, src, "Should return WAV input as-is")
            mock_run.assert_not_called()  # no ffmpeg invocation
        finally:
            os.unlink(src)

    def test_ensure_wav_converts_ogg_to_wav(self):
        # OGG/S stream starts with "OggS" (0x4F 0x67 0x67 0x53).
        src = self._write_tmp(b"OggS\x00\x02" + b"\x00" * 1024)
        # expected WAV path: same name without the .ogg suffix.
        dst = src.rsplit(".", 1)[0] + ".wav"
        try:
            fake_completed = MagicMock(returncode=0, stderr=b"")
            with patch("subprocess.run", return_value=fake_completed) as mock_run:
                result = self.shim_server.ensure_wav(src)
            self.assertEqual(result, dst)
            self.assertTrue(mock_run.called, "ffmpeg must be invoked for non-WAV")
            called_args = mock_run.call_args[0][0]  # first positional arg of call
            self.assertIn("-i", called_args)
            self.assertIn(src, called_args)
            self.assertIn(dst, called_args)
            self.assertIn("-ar", called_args)
            self.assertIn("16000", called_args)
            self.assertIn("-ac", called_args)
            self.assertIn("1", called_args)
        finally:
            os.unlink(src)
            if os.path.exists(dst):
                os.unlink(dst)

    def test_ensure_wav_raises_when_ffmpeg_fails(self):
        src = self._write_tmp(b"OggS\x00\x02" + b"\x00" * 64)
        try:
            fake_completed = MagicMock(returncode=1, stderr=b"some ffmpeg error\n")
            with patch(
                "subprocess.run", return_value=fake_completed
            ), self.assertRaises(RuntimeError) as cm:
                self.shim_server.ensure_wav(src)
            # Error message should include stderr so debugging is easy.
            self.assertIn("ffmpeg", str(cm.exception).lower())
            self.assertIn("some ffmpeg error", str(cm.exception))
        finally:
            os.unlink(src)
            dst = src.rsplit(".", 1)[0] + ".wav"
            if os.path.exists(dst):
                os.unlink(dst)

    def test_shim_install_script_installs_imageio_ffmpeg(self):
        """The EC2 bootstrap must install `imageio-ffmpeg` — pywhispercpp's
        internal audio decoder only handles WAV, so the shim converts
        upstream OGG/Opus via a bundled static ffmpeg provided by the
        `imageio-ffmpeg` pip package. The Node.js shim did the same with
        `ffmpeg-static` (npm); this is the Python equivalent."""
        from _common import _install_whisper_stt_script

        script = _install_whisper_stt_script()
        self.assertRegex(script, r"pip\s+install\s+.*\bimageio-ffmpeg\b")

    def test_shim_install_script_does_not_install_ffmpeg_via_dnf(self):
        """Regression: `ffmpeg` is now bundled via imageio-ffmpeg pip
        wheel — no system install needed. Ensure `dnf install ffmpeg`
        does not slip back in."""
        from _common import _install_whisper_stt_script

        script = _install_whisper_stt_script()
        self.assertNotRegex(
            script,
            r"dnf\s+install\s+.*\bffmpeg\b",
            "ffmpeg should be bundled via imageio-ffmpeg pip wheel, "
            "not installed via dnf",
        )


class TestTranscribeTextExtraction(unittest.TestCase):
    """pywhispercpp's Model.transcribe() returns list[Segment] (default)
    or dict (when transcribe_with_meta=True). transcribe() must extract
    the text correctly in both cases — and fall back to str() for
    unknown shapes."""

    @classmethod
    def setUpClass(cls):
        cls.shim_server = _load_shim_server()

    class _Segment:
        """Minimal stand-in for pywhispercpp.model.Segment."""

        def __init__(self, text, t0=0, t1=0, probability=0.0):
            self.text = text
            self.t0 = t0
            self.t1 = t1
            self.probability = probability

    def _make_model(self, transcribe_return):
        fake_model = MagicMock()
        fake_model.transcribe.return_value = transcribe_return
        return fake_model

    def test_transcribe_concatenates_segment_text(self):
        # pywhispercpp default: list of Segment objects.
        segs = [
            self._Segment("Hello", t0=0, t1=200, probability=1.0),
            self._Segment(" world", t0=200, t1=500, probability=1.0),
        ]
        with patch.object(
            self.shim_server, "get_model", return_value=self._make_model(segs)
        ):
            result = self.shim_server.transcribe("/tmp/dummy.wav", "en")
        self.assertEqual(result, "Hello world")

    def test_transcribe_returns_dict_text_when_meta_mode(self):
        # transcribe_with_meta=True: a dict with a `text` key.
        fake_result = {"text": "Hello world", "language": "en", "segments": []}
        with patch.object(
            self.shim_server, "get_model", return_value=self._make_model(fake_result)
        ):
            result = self.shim_server.transcribe("/tmp/dummy.wav", "en")
        self.assertEqual(result, "Hello world")

    def test_transcribe_does_not_use_str_of_segment_list(self):
        """Regression: the old code did `str(result)` for non-dict
        results, producing "[t0=0, t1=200, text=Hello world,
        probability=nan]" instead of "Hello world"."""
        segs = [self._Segment("Hello world", t0=0, t1=200, probability=float("nan"))]
        with patch.object(
            self.shim_server, "get_model", return_value=self._make_model(segs)
        ):
            result = self.shim_server.transcribe("/tmp/dummy.wav", "en")
        # Must NOT include "t0=" / "t1=" / "probability=" — those are
        # Segment __repr__ artifacts from the bug.
        self.assertNotIn("t0=", result)
        self.assertNotIn("t1=", result)
        self.assertNotIn("probability=", result)

    def test_transcribe_returns_empty_for_empty_segment_list(self):
        with patch.object(
            self.shim_server, "get_model", return_value=self._make_model([])
        ):
            result = self.shim_server.transcribe("/tmp/dummy.wav", "en")
        self.assertEqual(result, "")

    def test_transcribe_handles_unknown_shape(self):
        # An exotic return type — fall back to str().
        with patch.object(
            self.shim_server, "get_model", return_value=self._make_model(42)
        ):
            result = self.shim_server.transcribe("/tmp/dummy.wav", "en")
        self.assertEqual(result, "42")


class TestBrokenPipeHandling(unittest.TestCase):
    """When the client disconnects mid-response, _send_json should
    silently drop the write instead of raising BrokenPipeError."""

    @classmethod
    def setUpClass(cls):
        cls.shim_server = _load_shim_server()

    def _make_handler(self, write_side_effect):
        handler = self.shim_server.Handler.__new__(self.shim_server.Handler)
        handler.wfile = MagicMock()
        handler.wfile.write.side_effect = write_side_effect
        # BaseHTTPRequestHandler.send_response() → log_request() calls
        # self.log_message('"%s" %s %s', self.requestline, str(code),
        # str(size)). Even though our shim overrides log_message to a
        # no-op, Python still evaluates `self.requestline` to build the
        # args. Set all three attrs to bypass the descriptor access.
        handler.__dict__["requestline"] = "POST / HTTP/1.1"
        handler.__dict__["command"] = "POST"
        handler.client_address = ("test", 0)
        handler.server = None
        handler.request_version = "HTTP/1.1"
        return handler

    def test_send_json_silently_drops_when_client_gone(self):
        """_send_json must NOT raise BrokenPipeError to its caller
        when the wire write fails — there's nothing useful to do."""
        handler = self._make_handler(BrokenPipeError(32, "Broken pipe"))
        # Must not raise.
        handler._send_json(200, {"text": "hello"})
        # And must NOT have produced a 500 traceback either.
        handler.wfile.write.assert_called_once()

    def test_send_json_silently_drops_on_connection_reset(self):
        handler = self._make_handler(ConnectionResetError(104, "Connection reset"))
        handler._send_json(200, {"text": "hello"})
        handler.wfile.write.assert_called_once()

    def test_handler_logs_client_disconnected_when_transcribe_succeeds_but_pipe_broken(
        self,
    ):
        """Happy-path transcription + broken pipe at response write:
        we must log `client disconnected` (NOT `transcription failed`
        and NOT `returned response: status=200`)."""
        boundary, body = type(self)._multipart_body_static(
            self.shim_server,
            [("file", "audio.wav", b"FAKE_WAV", "audio/wav")],
        )

        # Build a handler whose wfile breaks on every write.
        handler = self.shim_server.Handler.__new__(self.shim_server.Handler)
        handler.rfile = BytesIO(body)
        handler.headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        }
        handler.__dict__["rfile"] = handler.rfile
        handler.__dict__["headers"] = handler.headers
        handler.wfile = MagicMock()
        handler.wfile.write.side_effect = BrokenPipeError(32, "Broken pipe")
        handler.path = "/v1/audio/transcriptions"
        handler.command = "POST"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "POST /v1/audio/transcriptions HTTP/1.1"
        handler.client_address = ("test", 0)
        handler.server = None

        # Stderr capture.
        old_stderr = sys.stderr
        captured = StringIO()
        sys.stderr = captured
        original_transcribe = self.shim_server.transcribe
        original_ensure = self.shim_server.ensure_wav
        self.shim_server.ensure_wav = lambda p: p
        self.shim_server.transcribe = lambda *a, **kw: "actual transcription"
        try:
            handler.do_POST()
        finally:
            self.shim_server.ensure_wav = original_ensure
            self.shim_server.transcribe = original_transcribe
            sys.stderr = old_stderr

        log = captured.getvalue()
        self.assertIn("received file", log)
        self.assertIn("transcribed audio", log)
        self.assertIn("client disconnected", log)
        # The misleading "transcription failed" line must NOT appear,
        # and neither should "returned response: status=200" (we never
        # actually returned 200 — the write was broken).
        self.assertNotIn("transcription failed", log)

    @staticmethod
    def _multipart_body_static(shim_server, fields, boundary="----TestBoundary"):
        """Mirror of TestShimHttpHandler._multipart_body, callable as a
        staticmethod so we don't depend on that class's setUpClass
        running first."""
        body = bytearray()
        for f in fields:
            name = f[0]
            if len(f) == 2:
                value = f[1]
                body += (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode()
            else:
                _filename, content, ctype = f[1], f[2], f[3]
                body += (
                    (
                        f"--{boundary}\r\n"
                        f'Content-Disposition: form-data; name="{name}"; filename="{_filename}"\r\n'
                        f"Content-Type: {ctype}\r\n\r\n"
                    ).encode()
                    + content
                    + b"\r\n"
                )
        body += f"--{boundary}--\r\n".encode()
        return boundary, bytes(body)


class TestFirstPostLog(unittest.TestCase):
    """`_log("first POST received ...")` fires exactly once across the
    shim's lifetime — useful for diagnosing whether the shim ever saw a
    request at all (e.g. wrong port, traffic not reaching the shim)."""

    @classmethod
    def setUpClass(cls):
        cls.shim_server = _load_shim_server()

    def setUp(self):
        # Reset the per-process flag before each test so tests are
        # independent.
        self.shim_server._first_post_logged = False

    def _send(self):
        boundary, body = TestShimHttpHandler._multipart_body(
            [("file", "audio.wav", b"FAKE_WAV", "audio/wav")],
        )
        handler = self.shim_server.Handler.__new__(self.shim_server.Handler)
        handler.rfile = BytesIO(body)
        handler.headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        }
        handler.__dict__["rfile"] = handler.rfile
        handler.__dict__["headers"] = handler.headers
        handler.wfile = BytesIO()
        handler.path = "/v1/audio/transcriptions"
        handler.command = "POST"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "POST /v1/audio/transcriptions HTTP/1.1"
        handler.client_address = ("test", 0)
        handler.server = None

        old_stderr = sys.stderr
        captured = StringIO()
        sys.stderr = captured
        original_transcribe = self.shim_server.transcribe
        original_ensure = self.shim_server.ensure_wav
        self.shim_server.ensure_wav = lambda p: p

        def mock_transcribe(_path, language, prompt=None):
            return "ok"

        self.shim_server.transcribe = mock_transcribe
        try:
            handler.do_POST()
        finally:
            self.shim_server.ensure_wav = original_ensure
            self.shim_server.transcribe = original_transcribe
            sys.stderr = old_stderr

        return captured.getvalue()

    def test_first_post_emits_first_post_log(self):
        log = self._send()
        self.assertIn("first POST received at /v1/audio/transcriptions", log)

    def test_subsequent_posts_omit_first_post_log(self):
        self._send()
        log2 = self._send()
        # Second POST must NOT re-emit the first-post marker.
        self.assertEqual(
            log2.count("first POST received at /v1/audio/transcriptions"),
            0,
            "first-post marker should fire at most once across the "
            f"shim's lifetime; got: {log2!r}",
        )


class TestContentLengthRead(unittest.TestCase):
    """Regression tests for the `self.rfile.read()` bug — reading until
    EOF instead of `Content-Length` bytes caused the 60-s symptom when
    the bot used HTTP/1.1 keep-alive."""

    @classmethod
    def setUpClass(cls):
        cls.shim_server = _load_shim_server()

    def _handler(self, body_bytes, content_length_value=None, extra_after_body=b""):
        """Build a handler whose rfile simulates a slow / keep-alive
        client: it has *body_bytes* followed by *extra_after_body* but
        will not EOF unless the caller reads past Content-Length."""
        boundary, _ = TestShimHttpHandler._multipart_body(
            [("file", "audio.wav", body_bytes, "audio/wav")],
        )
        # Recompose a real multipart body so the parser has work to do.
        _, real_body = TestShimHttpHandler._multipart_body(
            [("file", "audio.wav", body_bytes, "audio/wav")],
        )
        cl = (
            content_length_value
            if content_length_value is not None
            else str(len(real_body))
        )
        handler = self.shim_server.Handler.__new__(self.shim_server.Handler)
        # rfile has real_body + extra_after_body. With the bug, read()
        # would block waiting for the trailing sentinel; with the fix,
        # only the first content_length bytes are read.
        handler.rfile = BytesIO(real_body + extra_after_body)
        handler.headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": cl,
        }
        handler.__dict__["rfile"] = handler.rfile
        handler.__dict__["headers"] = handler.headers
        handler.wfile = BytesIO()
        handler.path = "/v1/audio/transcriptions"
        handler.command = "POST"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "POST /v1/audio/transcriptions HTTP/1.1"
        handler.client_address = ("test", 0)
        handler.server = None
        return handler, real_body

    def test_handler_returns_400_when_content_length_missing(self):
        handler = self.shim_server.Handler.__new__(self.shim_server.Handler)
        handler.rfile = BytesIO(b"--garbage\r\nfoo\r\n")
        # No Content-Length header at all.
        handler.headers = {
            "Content-Type": "multipart/form-data; boundary=----x",
        }
        handler.__dict__["rfile"] = handler.rfile
        handler.__dict__["headers"] = handler.headers
        handler.wfile = BytesIO()
        handler.path = "/v1/audio/transcriptions"
        handler.command = "POST"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "POST /v1/audio/transcriptions HTTP/1.1"
        handler.client_address = ("test", 0)
        handler.server = None

        old_stderr = sys.stderr
        sys.stderr = StringIO()
        try:
            handler.do_POST()
            response = handler.wfile.getvalue()
        finally:
            sys.stderr = old_stderr

        self.assertIn(b"400", response[:50])
        self.assertIn(b"Content-Length", response)
        # Critically: we did NOT block waiting for the missing-body
        # sentinel. (The empty BytesIO would EOF immediately, but the
        # assertion below is that we got a 400 because of the missing
        # header — not because of a parse failure.)

    def test_handler_reads_exact_content_length_bytes_not_past_it(self):
        # Sentinel: a byte marker that must NOT appear in rfile's tail
        # after the handler finishes (which would prove we read past
        # Content-Length).
        sentinel = b"SENTINEL_AFTER_BODY"
        body = (
            b"--xyz\r\n"
            b'Content-Disposition: form-data; name="file"; filename="a.wav"\r\n'
            b"Content-Type: audio/wav\r\n\r\n"
            b"FAKE_WAV\r\n"
            b"--xyz--\r\n"
        )
        cl = len(body)

        handler = self.shim_server.Handler.__new__(self.shim_server.Handler)
        handler.rfile = BytesIO(body + sentinel)
        handler.headers = {
            "Content-Type": "multipart/form-data; boundary=xyz",
            "Content-Length": str(cl),
        }
        handler.__dict__["rfile"] = handler.rfile
        handler.__dict__["headers"] = handler.headers
        handler.wfile = BytesIO()
        handler.path = "/v1/audio/transcriptions"
        handler.command = "POST"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "POST /v1/audio/transcriptions HTTP/1.1"
        handler.client_address = ("test", 0)
        handler.server = None

        original_transcribe = self.shim_server.transcribe
        original_ensure = self.shim_server.ensure_wav
        self.shim_server.ensure_wav = lambda p: p

        def mock_transcribe(_path, language, prompt=None):
            return "ok"

        self.shim_server.transcribe = mock_transcribe
        old_stderr = sys.stderr
        sys.stderr = StringIO()
        try:
            handler.do_POST()
            response = handler.wfile.getvalue()
        finally:
            self.shim_server.ensure_wav = original_ensure
            self.shim_server.transcribe = original_transcribe
            sys.stderr = old_stderr

        # The 200 path was reached.
        self.assertIn(b"200", response[:50])
        # And the sentinel is STILL in rfile's tail — proving the
        # handler did not consume past Content-Length.
        remaining = handler.rfile.read()
        self.assertEqual(remaining, sentinel)

    def test_handler_does_not_block_60s_on_keep_alive_body(self):
        """If we regress and start reading to EOF again, this test
        would hang for 60 s before the runtime harness times it out.
        We simulate a non-EOF keep-alive stream by writing a buffer
        and never closing rfile. We can't truly block-forever in
        BytesIO, but we can prove that Content-Length does in fact
        bound the read by checking the implementation detail."""
        # Implementation check: the handler's source must call
        # rfile.read(<integer>), not rfile.read() with no args.
        import inspect

        source = inspect.getsource(self.shim_server.Handler.do_POST)
        # Must read with a length bound — `self.rfile.read(N)` for some
        # non-zero N derived from Content-Length.
        self.assertIn(
            "self.rfile.read(content_length)",
            source,
            "do_POST must use a Content-Length-bounded read",
        )


class TestConnectionCloseHeader(unittest.TestCase):
    """Every response from _send_json must include `Connection: close`
    so HTTP/1.1 keep-alive sockets don't outlive the request."""

    @classmethod
    def setUpClass(cls):
        cls.shim_server = _load_shim_server()

    def test_send_json_emits_connection_close(self):
        handler = self.shim_server.Handler.__new__(self.shim_server.Handler)
        handler.wfile = BytesIO()
        handler.headers = {}
        handler.__dict__["wfile"] = handler.wfile
        handler.__dict__["headers"] = handler.headers
        handler.__dict__["requestline"] = "POST / HTTP/1.1"
        handler.__dict__["command"] = "POST"
        handler.client_address = ("test", 0)
        handler.server = None
        handler.request_version = "HTTP/1.1"
        handler._send_json(200, {"ok": True})

        captured = handler.wfile.getvalue()
        # The HTTP/1.1 status line must be the first line.
        first_line = captured.split(b"\r\n", 1)[0]
        self.assertIn(b" 200 ", first_line)
        # Connection: close must appear in the header block.
        self.assertIn(b"Connection: close", captured)


class TestShimHttpHandler(unittest.TestCase):
    """End-to-end HTTP tests: spin up the shim's handler, post a real
    multipart body, assert the response. Mocks `transcribe` so no model
    file is needed."""

    @staticmethod
    def _multipart_body(fields, boundary="----TestBoundary"):
        """Build a minimal multipart/form-data body from a list of fields.

        Each field is one of:
          - (name, str)              — text field
          - (name, filename, bytes, content_type) — file field
        Returns (boundary_str, body_bytes).
        """
        body = bytearray()
        for f in fields:
            name = f[0]
            if len(f) == 2:
                value = f[1]
                body += (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode()
            else:
                _filename, content, ctype = f[1], f[2], f[3]
                body += (
                    (
                        f"--{boundary}\r\n"
                        f'Content-Disposition: form-data; name="{name}"; filename="{_filename}"\r\n'
                        f"Content-Type: {ctype}\r\n\r\n"
                    ).encode()
                    + content
                    + b"\r\n"
                )
        body += f"--{boundary}--\r\n".encode()
        return boundary, bytes(body)

    @classmethod
    def setUpClass(cls):
        cls.shim_server = _load_shim_server()

    @staticmethod
    def _setup_handler(body, content_type):
        # Imported here so we capture the test class's shim_server at
        # call time. We can't use cls (the static method has no cls).
        handler = TestShimHttpHandler.shim_server.Handler.__new__(
            TestShimHttpHandler.shim_server.Handler
        )
        handler.rfile = BytesIO(body)
        handler.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        # Force attribute assignment via __dict__ to bypass any
        # descriptor magic in BaseHTTPRequestHandler.
        handler.__dict__["rfile"] = handler.rfile
        handler.__dict__["headers"] = handler.headers
        handler.wfile = BytesIO()
        handler.path = "/v1/audio/transcriptions"
        handler.command = "POST"
        handler.request_version = "HTTP/1.1"
        # send_response() calls log_message() which reads requestline;
        # bypass BaseHTTPRequestHandler via Handler.__new__.
        handler.requestline = "POST /v1/audio/transcriptions HTTP/1.1"
        handler.client_address = ("test", 0)
        handler.server = None

        # Capture stderr so we can assert diagnostic output.
        old_stderr = sys.stderr
        captured = StringIO()
        sys.stderr = captured
        return handler, captured, old_stderr

    def test_handler_400_when_no_file_field(self):
        """Multipart without a `file` field → 400 (not 500)."""
        boundary, body = self._multipart_body([("prompt", "no file")])
        handler, _captured, old_stderr = self._setup_handler(
            body, f"multipart/form-data; boundary={boundary}"
        )
        try:
            handler.do_POST()
            response = handler.wfile.getvalue()
        finally:
            sys.stderr = old_stderr

        self.assertIn(b"400", response[:50])
        self.assertIn(b"missing 'file' field", response)
        # Diagnostic should log which fields the bot actually sent.
        debug = _captured.getvalue()
        self.assertIn("DEBUG multipart", debug)
        self.assertIn("prompt", debug)  # saw the prompt field

    def test_handler_200_with_file_field(self):
        """Multipart with `file` → 200 + transcription text."""
        boundary, body = self._multipart_body(
            [
                ("file", "audio.wav", b"FAKE_WAV", "audio/wav"),
            ]
        )
        handler, _captured, old_stderr = self._setup_handler(
            body, f"multipart/form-data; boundary={boundary}"
        )

        captured_transcribe = {"prompt": None}
        original_transcribe = self.shim_server.transcribe
        captured_ensure = {"called": False, "path": None}
        original_ensure = self.shim_server.ensure_wav

        def mock_ensure(path):
            captured_ensure["called"] = True
            captured_ensure["path"] = path
            return path  # passthrough — no ffmpeg in tests

        def mock_transcribe(_path, language, prompt=None):
            captured_transcribe["prompt"] = prompt
            return "transcribed"

        self.shim_server.ensure_wav = mock_ensure
        self.shim_server.transcribe = mock_transcribe
        try:
            handler.do_POST()
            response = handler.wfile.getvalue()
        finally:
            self.shim_server.ensure_wav = original_ensure
            self.shim_server.transcribe = original_transcribe
            sys.stderr = old_stderr

        self.assertIn(b"200", response[:50])
        self.assertIn(b"transcribed", response)
        # No prompt field in this test → transcribe should have seen None.
        self.assertIsNone(captured_transcribe["prompt"])
        # ensure_wav was called with the multipart upload.
        self.assertTrue(captured_ensure["called"])

    def test_handler_forwards_prompt_field_to_transcribe(self):
        """Multipart with `file` + `prompt` → transcribe receives prompt."""
        boundary, body = self._multipart_body(
            [
                ("file", "audio.wav", b"FAKE_WAV", "audio/wav"),
                ("prompt", "The following text is..."),
            ]
        )
        handler, _captured, old_stderr = self._setup_handler(
            body, f"multipart/form-data; boundary={boundary}"
        )

        captured_transcribe = {"prompt": None, "language": None}
        original_transcribe = self.shim_server.transcribe
        original_ensure = self.shim_server.ensure_wav

        def mock_transcribe(_path, language, prompt=None):
            captured_transcribe["prompt"] = prompt
            captured_transcribe["language"] = language
            return "ok"

        self.shim_server.ensure_wav = lambda p: p  # passthrough
        self.shim_server.transcribe = mock_transcribe
        try:
            handler.do_POST()
            response = handler.wfile.getvalue()
        finally:
            self.shim_server.ensure_wav = original_ensure
            self.shim_server.transcribe = original_transcribe
            sys.stderr = old_stderr

        self.assertIn(b"200", response[:50])
        self.assertEqual(captured_transcribe["prompt"], b"The following text is...")
        self.assertEqual(captured_transcribe["language"], "en")

    def test_handler_logs_lifecycle_events(self):
        """Three timestamped lifecycle events must appear on stderr in
        order: received file -> transcribed audio -> returned response.
        """
        import re

        boundary, body = self._multipart_body(
            [
                ("file", "audio.wav", b"FAKE_WAV", "audio/wav"),
            ]
        )
        handler, _captured, old_stderr = self._setup_handler(
            body, f"multipart/form-data; boundary={boundary}"
        )
        original_transcribe = self.shim_server.transcribe
        original_ensure = self.shim_server.ensure_wav
        self.shim_server.ensure_wav = lambda p: p  # passthrough

        def mock_transcribe(_path, language, prompt=None):
            return "hello world"

        self.shim_server.transcribe = mock_transcribe
        try:
            handler.do_POST()
        finally:
            self.shim_server.ensure_wav = original_ensure
            self.shim_server.transcribe = original_transcribe
            sys.stderr = old_stderr

        log_output = _captured.getvalue()
        events = [
            "received file: bytes=",
            "transcribed audio: text=",
            "returned response: status=200",
        ]
        positions = [log_output.find(ev) for ev in events]
        self.assertGreaterEqual(
            positions[0],
            0,
            f"missing 'received file' log; got: {log_output!r}",
        )
        self.assertGreaterEqual(
            positions[1],
            0,
            f"missing 'transcribed audio' log; got: {log_output!r}",
        )
        self.assertGreaterEqual(
            positions[2],
            0,
            f"missing 'returned response' log; got: {log_output!r}",
        )
        self.assertLess(
            positions[0],
            positions[1],
            "received file must come before transcribed audio",
        )
        self.assertLess(
            positions[1],
            positions[2],
            "transcribed audio must come before returned response",
        )
        # Regression for the 60s-symptom: with the Content-Length fix,
        # the wall-clock gap between `first POST received` and
        # `received file` must be sub-second (the read is bounded by
        # Content-Length and not by client-side keep-alive EOF).
        first_ts_match = re.search(
            r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z) ",
            log_output,
        )
        received_file_match = re.search(
            r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z) received file",
            log_output,
            re.MULTILINE,
        )
        self.assertIsNotNone(first_ts_match)
        self.assertIsNotNone(received_file_match)
        from datetime import datetime

        # Replacers normalize the "Z" suffix back to "+00:00" for
        # datetime.fromisoformat's parser.
        first = datetime.fromisoformat(first_ts_match.group(1).replace("Z", "+00:00"))
        received = datetime.fromisoformat(
            received_file_match.group(1).replace("Z", "+00:00")
        )
        gap_s = abs((received - first).total_seconds())
        self.assertLess(
            gap_s,
            1.0,
            f"first POST → received file must be sub-second; "
            f"got {gap_s}s. The `self.rfile.read()` bug would cause "
            f"this to be ~60s.",
        )
        # Each line must have an ISO-8601 UTC timestamp prefix.
        iso_pat = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z ")
        for line in log_output.splitlines():
            if any(ev in line for ev in events):
                self.assertRegex(line, iso_pat, f"missing timestamp: {line!r}")


if __name__ == "__main__":
    unittest.main()
