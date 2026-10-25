"""Cuff's PC app: speech-to-text for the dictation button (on the PC's GPU), the game's
side of a Steam Link stream for the Performance tab, and an announcement on the local
networks so the headset finds it (beacon.py).

  uv run server.py                      listen on 0.0.0.0:8765
  uv run server.py --selftest a.wav     transcribe one file and exit

API:
  POST /transcribe   body: WAV (any rate/channels; 16 kHz mono s16 is cheapest)
                     -> {"text": "...", "audio_s": 2.1, "infer_ms": 38}
  GET  /health       -> {"ok": true, "model": "...", "provider": "..."}
  GET  /stream       -> {"pc": {...} or null}: the PC's live view of a Steam Link stream, for
                        the headset's Performance tab (stream.py)

  uv run server.py --stats-only   just /stream and /health, without loading the speech model

Every request but /health and pairing needs a paired headset's token, `Authorization: Bearer
<token>` (pairing.py; pair.ps1 pairs one), or comes from the PC itself. STT_TOKEN (env or
--token) is accepted as a token too.

  POST /pair/request, GET /pair/result   a headset asking to pair, and collecting its token
  POST /pair/open, /pair/confirm         pair.ps1, from the PC itself only
"""
import argparse
import io
import json
import os
import sys
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

import numpy as np
import onnxruntime as ort

import beacon
import pairing
import stream

PAIRING = None

DEFAULT_MODEL = "nemo-parakeet-tdt-0.6b-v2"
MAX_BODY = 30 * 1024 * 1024  # ~15 min of 16 kHz s16; push-to-talk clips are seconds


def load_model(name):
    import onnx_asr

    ort.preload_dlls()  # CUDA/cuDNN from the nvidia-*-cu12 wheels
    providers = [("CUDAExecutionProvider", {"device_id": 0}), "CPUExecutionProvider"]
    model = onnx_asr.load_model(name, providers=providers)
    return model


def wav_to_float(data):
    """Decode WAV bytes to mono float32 at 16 kHz."""
    with wave.open(io.BytesIO(data)) as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"need 16-bit PCM WAV, got {width * 8}-bit")
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    if rate != 16000:
        # Linear resample is plenty for speech recognition input.
        n = int(round(len(audio) * 16000 / rate))
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio)
        audio = audio.astype(np.float32)
    return audio


class STT:
    def __init__(self, name):
        t = time.perf_counter()
        self.name = name
        self.model = load_model(name)
        self.lock = Lock()  # one GPU session; clips are tiny, so serialise
        # Warm up CUDA/cuDNN with a few lengths of noise (silence skips most of the decoder),
        # so the first real dictation doesn't pay ~250 ms of one-off setup.
        rng = np.random.default_rng(0)
        for secs in (1, 3, 6):
            self.recognize((rng.standard_normal(secs * 16000) * 0.05).astype(np.float32))
        print(f"loaded {name} in {time.perf_counter() - t:.1f}s", flush=True)

    def recognize(self, audio):
        with self.lock:
            return self.model.recognize(audio, sample_rate=16000).strip()


LOCAL = ("127.0.0.1", "::1")


def make_handler(stt, pairing):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cuff-pc/1"

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self):
            """A paired headset's token, or the request comes from the PC itself."""
            if self.client_address[0] in LOCAL or pairing.authorised(self.headers.get("Authorization")):
                return True
            n = int(self.headers.get("Content-Length") or 0)
            if 0 < n <= MAX_BODY:
                self.rfile.read(n)  # take the upload first, so the client sees the 401, not a broken pipe
            self._json(401, {"error": "not paired: run pair.ps1 on the PC"})
            return False

        def _body(self, limit=4096):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(min(n, limit)) or b"{}") if n else {}
            except ValueError:
                return {}

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/health":
                # Open to all, so the headset can tell "not paired" from "not there".
                paired = pairing.authorised(self.headers.get("Authorization"))
                self._json(200, {"ok": True, "paired": paired, "pairing": pairing.is_open(),
                                 "model": stt.name if stt and paired else None})
            elif path == "/stream":
                if self._authorised():
                    self._json(200, stream.snapshot())
            elif path == "/pair/result":
                req_id = dict(kv.partition("=")[::2] for kv in query.split("&") if kv).get("id", "")
                self._json(*pairing.result(req_id))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path == "/pair/request":
                return self._json(*pairing.request(self._body()))
            if self.path in ("/pair/open", "/pair/confirm"):
                if self.client_address[0] not in LOCAL:  # only from the PC itself
                    return self._json(403, {"error": "only from the PC"})
                return self._json(*((200, pairing.open()) if self.path == "/pair/open" else pairing.confirm(self._body())))
            if self.path != "/transcribe" or stt is None:
                return self._json(404, {"error": "not found"})
            if not self._authorised():
                return
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= MAX_BODY:
                return self._json(413, {"error": "empty or too large"})
            try:
                audio = wav_to_float(self.rfile.read(n))
            except (wave.Error, ValueError, EOFError) as e:
                return self._json(400, {"error": f"bad wav: {e}"})
            t = time.perf_counter()
            text = stt.recognize(audio) if len(audio) >= 1600 else ""
            ms = round((time.perf_counter() - t) * 1000)
            secs = round(len(audio) / 16000, 2)
            print(f"{self.client_address[0]} {secs}s -> {ms}ms: {text!r}", flush=True)
            self._json(200, {"text": text, "audio_s": secs, "infer_ms": ms})

        def log_message(self, *args):  # requests are logged in do_POST
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--model", default=os.environ.get("STT_MODEL", DEFAULT_MODEL))
    ap.add_argument("--token", default=os.environ.get("STT_TOKEN", ""))
    ap.add_argument("--selftest", metavar="WAV")
    ap.add_argument("--log", metavar="FILE", help="append stdout/stderr here (line-buffered)")
    ap.add_argument("--stats-only", action="store_true", help="serve /stream without the speech model")
    args = ap.parse_args()
    if args.log:
        sys.stdout = sys.stderr = open(args.log, "a", buffering=1, encoding="utf-8")
        print(f"=== {time.strftime('%Y-%m-%dT%H:%M:%S')} starting", flush=True)

    global PAIRING
    PAIRING = pairing.Pairing(args.token)
    stream.MONITOR.start()
    beacon.Beacon(args.port, PAIRING).start()
    stt = None if args.stats_only else STT(args.model)
    if args.selftest and stt:
        with open(args.selftest, "rb") as f:
            audio = wav_to_float(f.read())
        for _ in range(3):
            t = time.perf_counter()
            text = stt.recognize(audio)
            print(f"{len(audio) / 16000:.2f}s audio -> {(time.perf_counter() - t) * 1000:.0f}ms: {text}")
        return 0

    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(stt, PAIRING))
    print(f"listening on {args.host}:{args.port}; {len(PAIRING.paired)} headset(s) paired", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
