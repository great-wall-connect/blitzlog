const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { randomUUID } = require("crypto");
const Busboy = require("busboy");
const ffmpegPath = require("ffmpeg-static");

const HOST = process.env.HOST || "127.0.0.1";
const PORT = parseInt(process.env.PORT || "7878", 10);
const WHISPER_CLI = process.env.WHISPER_CLI || "/opt/whisper-stt/bin/whisper-cli";
const WHISPER_MODEL =
  process.env.WHISPER_MODEL || "/opt/whisper-stt/models/ggml-base.en.bin";
const WHISPER_LANGUAGE = process.env.WHISPER_LANGUAGE || "en";
const REQUEST_TIMEOUT_MS = parseInt(process.env.REQUEST_TIMEOUT_MS || "60000", 10);

function runProcess(bin, args, opts = {}) {
  return new Promise((resolve, reject) => {
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const proc = spawn(bin, args, { ...opts, timeout: REQUEST_TIMEOUT_MS });
    const timer = setTimeout(() => {
      timedOut = true;
      proc.kill("SIGKILL");
      reject(new Error(`${path.basename(bin)} timed out after ${REQUEST_TIMEOUT_MS}ms`));
    }, REQUEST_TIMEOUT_MS);
    if (proc.stdout) proc.stdout.on("data", (d) => (stdout += d.toString()));
    if (proc.stderr) proc.stderr.on("data", (d) => (stderr += d.toString()));
    proc.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    proc.on("close", (code, signal) => {
      clearTimeout(timer);
      if (timedOut) return;
      if (code !== 0) {
        reject(
          new Error(
            `${path.basename(bin)} exited ${code}${signal ? ` (signal ${signal})` : ""}: ${stderr.slice(-400)}`,
          ),
        );
        return;
      }
      resolve({ stdout, stderr });
    });
  });
}

async function convertToWav(inputPath, outputPath) {
  await runProcess(ffmpegPath, [
    "-y",
    "-loglevel",
    "error",
    "-i",
    inputPath,
    "-ar",
    "16000",
    "-ac",
    "1",
    "-c:a",
    "pcm_s16le",
    outputPath,
  ]);
}

async function transcribeWithWhisper(wavPath, modelPath, language) {
  const args = [
    "-m",
    modelPath,
    "-f",
    wavPath,
    "--language",
    language || "auto",
    "--no-timestamps",
    "--print-special",
    "false",
    "--output-json",
  ];
  const { stdout } = await runProcess(WHISPER_CLI, args);
  let parsed;
  try {
    parsed = JSON.parse(stdout);
  } catch (e) {
    throw new Error(`whisper-cli produced non-JSON output: ${stdout.slice(0, 200)}`);
  }
  const text = (parsed && typeof parsed.text === "string" ? parsed.text : "").trim();
  return text;
}

function parseMultipart(req) {
  return new Promise((resolve, reject) => {
    const busboy = Busboy({ headers: req.headers });
    const fields = {};
    let filePath = null;
    let finalize = null;

    busboy.on("field", (name, value) => {
      fields[name] = value;
    });
    busboy.on("file", (_name, stream, info) => {
      const ext = info.filename && info.filename.includes(".")
        ? path.extname(info.filename).slice(1)
        : "ogg";
      const tmp = path.join(os.tmpdir(), `stt-${randomUUID()}.${ext || "ogg"}`);
      filePath = tmp;
      const out = fs.createWriteStream(tmp);
      stream.pipe(out);
      finalize = new Promise((res, rej) => {
        out.on("finish", res);
        out.on("error", rej);
      });
    });
    busboy.on("error", reject);
    req.on("aborted", () => reject(new Error("client aborted upload")));
    busboy.on("close", () => {
      if (!filePath) {
        reject(new Error("multipart request had no file field"));
        return;
      }
      Promise.resolve(finalize)
        .then(() => resolve({ fields, filePath }))
        .catch(reject);
    });
    req.pipe(busboy);
  });
}

function sendJson(res, status, body) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

function unlinkQuietly(p) {
  if (!p) return;
  try {
    fs.unlinkSync(p);
  } catch (_) {
    /* best effort */
  }
}

const server = http.createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/healthz") {
    sendJson(res, 200, { status: "ok" });
    return;
  }
  if (req.method === "POST" && req.url === "/v1/audio/transcriptions") {
    let parsed;
    try {
      parsed = await parseMultipart(req);
    } catch (e) {
      sendJson(res, 400, { error: `bad multipart: ${e.message}` });
      return;
    }
    const { fields, filePath } = parsed;
    const wavPath = `${filePath}.wav`;
    try {
      await convertToWav(filePath, wavPath);
      const modelPath = fields.model
        ? `/opt/whisper-stt/models/ggml-${fields.model}.bin`
        : WHISPER_MODEL;
      const language = fields.language || WHISPER_LANGUAGE;
      const text = await transcribeWithWhisper(wavPath, modelPath, language);
      sendJson(res, 200, { text });
    } catch (e) {
      sendJson(res, 500, { error: e.message });
    } finally {
      unlinkQuietly(filePath);
      unlinkQuietly(wavPath);
    }
    return;
  }
  sendJson(res, 404, { error: "not found" });
});

server.listen(PORT, HOST, () => {
  console.log(`whisper-stt-shim listening on http://${HOST}:${PORT}`);
});
