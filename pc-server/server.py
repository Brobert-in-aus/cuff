"""Cuff's PC app: speech-to-text for the dictation button (on the PC's GPU), the game's
side of a Steam Link stream for the Performance tab, and an announcement on the local
networks so the headset finds it (beacon.py).

  uv run server.py                      listen on 0.0.0.0:8765
  uv run server.py --selftest a.wav     transcribe one file and exit

API:
  POST /transcribe   body: WAV (any rate/channels; 16 kHz mono s16 is cheapest)
                     -> {"text": "...", "audio_s": 2.1, "infer_ms": 38}
  GET  /health       -> {"ok", "api", "id" (this PC's; pairing.py), "paired", "pairing", "model"}
  GET  /stream       -> {"pc", "link", "dongle_usb"}: the PC's live view of a Steam Link stream, for
                        the headset's Performance tab (stream.py)
  GET  /typing/next?after=<seq>&run=<id>  -> {"seq", "run", "items", "on"}: text and keys typed on the PC,
                        long-polled by the headset (typing_link.py)

  uv run server.py --stats-only   just /stream and /health, without loading the speech model

Every request but /health and pairing needs a paired headset's signature, `Authorization: Cuff
...` made with its token, which never crosses the network (pairing.py; pair.ps1 pairs one), and
the answer to a signed request is signed too (X-Cuff-Mac). STT_TOKEN (env or --token) is
accepted as a token too, signed or as a bearer token. From the PC itself, the app page's guard
(appapi.AppApi.allowed: a Host of 127.0.0.1 or localhost, and X-Cuff-App: 1) stands in for a
signature, so a web page in a browser on the PC can't use these. A JSON body must be sent as
Content-Type: application/json.

  POST /pair/request, GET /pair/result, POST /pair/finish   a headset pairing (pairing.py)
  POST /pair/open, /pair/confirm         pair.ps1 or the app page, from the PC itself only

The app page (docs/PC-APP.md; appapi.py lists its routes), from the PC itself only: the
paired headsets and pairing, dictation and the log; setting up the headset over SSH
(frame.py); its recordings (videos.py); and Cuff's settings on it.

  --no-beacon        don't announce on the network (a second copy for testing)
"""
import argparse
import io
import json
import os
import socket
import sys
import time
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

import numpy as np
import onnxruntime as ort

import appapi
import beacon
import frame as frame_link
import linkquality
import pairing
import stream
import typing_link
import updates as app_updates
import videos as video_store
from pairing import response_mac as pairing_mac

PAIRING = None
TYPING = None  # typing_link.Typing, for app.py's hotkey
LOG_FILE = None  # --log, shown on the app page
APP_PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.html")
LOG_LINES = 40

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
API = 1  # in /health: the protocol between the headset and this app, for both to compare


def log_tail(n=LOG_LINES):
    if not LOG_FILE:
        return ["(logging to the console: start with --log to see it here)"]
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 16384))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError as e:
        return [f"(can't read the log: {e})"]


def app_state(stt, pairing):
    cuda = "CUDAExecutionProvider" in ort.get_available_providers()
    return {"headsets": pairing.headsets(), "pairing": pairing.is_open(),
            "model": stt.name if stt else None,
            "device": ("GPU (CUDA)" if cuda else "CPU") if stt else None,
            "log": log_tail()}


def make_handler(stt, pairing, app=None, typing=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cuff-pc/1"
        mac = None  # (token, nonce) of a signed request, whose answer is signed with them

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if self.mac:  # the answer to a signed request is signed too, for the headset to check
                token, nonce = self.mac
                self.send_header("X-Cuff-Mac", pairing_mac(token, nonce, code, body))
            self.end_headers()
            self.wfile.write(body)

        def _check(self, body=b""):
            """pairing.check on this request -> (token or None, why); a signed one gets a signed answer."""
            token, nonce, why = pairing.check(self.headers.get("Authorization"), self.command, self.path, body,
                                              self.client_address[0])
            self.mac = (token, nonce) if token and nonce else None
            return token, why

        def _authorised(self, body=b""):
            """A paired headset's signed request (body: what was sent), or the app page on the PC."""
            if self._from_app(self.path.partition("?")[0]):
                return True
            token, why = self._check(body)
            if token:
                return True
            self._json(401, {"error": why})
            return False

        def _from_app(self, path):
            """From the PC itself and past the app page's guard (appapi.py: the Host, and X-Cuff-App
            but for the page and thumbnails), so a web page in a browser on the PC can't do it."""
            return self.client_address[0] in LOCAL and app is not None and app.allowed(self, path)

        def _local(self, path):
            if self._from_app(path):
                return True
            self._json(403, {"error": "only from the PC"})
            return False

        def _reply(self, out):
            if len(out) == 3:
                code, body, kind = out
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json(*out)

        def _page(self):
            try:
                with open(APP_PAGE, "rb") as f:
                    body = f.read()
            except OSError:
                return self._json(404, {"error": "app.html is missing"})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _length(self):
            try:
                return int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return -1

        def _body(self, limit=4096):
            """The request's JSON object ({} for none), or None when it isn't sent as JSON: a web
            page can't send application/json elsewhere without the browser asking first."""
            n = self._length()
            if n == 0:
                return {}
            if n < 0:
                return None
            data = self.rfile.read(min(n, limit))
            if self.headers.get_content_type() != "application/json":
                return None
            try:
                out = json.loads(data)
            except ValueError:
                return {}
            return out if isinstance(out, dict) else {}

        def do_GET(self):
            self.mac = None
            path, _, query = self.path.partition("?")
            q = dict(urllib.parse.parse_qsl(query))
            if path == "/health":
                # Open to all, so the headset can tell "not paired" from "not there". A signed
                # request gets a signed answer, which tells the headset this is its PC.
                paired = bool(self.headers.get("Authorization")) and bool(self._check()[0])
                self._json(200, {"ok": True, "api": API, "id": pairing.pc_id, "paired": paired,
                                 "pairing": pairing.is_open(), "model": stt.name if stt and paired else None})
            elif path == "/stream":
                if self._authorised():
                    self._json(200, stream.snapshot())
            elif path == "/typing/next" and typing:
                if self._authorised():
                    try:
                        after = int(q.get("after", -1))
                    except ValueError:
                        after = -1
                    self._json(200, typing.next(after, q.get("run")))
            elif path == "/app":
                if self._local(path):
                    self._page()
            elif path.startswith("/app/") and app:
                if self._local(path):
                    self._reply(app.get(path, query))
            elif path == "/pair/result":
                self._json(*pairing.result(q.get("id", ""), self.client_address[0]))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            self.mac = None
            path = self.path.partition("?")[0]
            if path in ("/pair/request", "/pair/finish"):
                body = self._body()
                if body is None:
                    return self._json(415, {"error": "send JSON"})
                ask = pairing.request if path == "/pair/request" else pairing.finish
                return self._json(*ask(body, self.client_address[0]))
            if (path.startswith("/app/") and app) or path in ("/pair/open", "/pair/confirm"):
                if not self._local(path):
                    return
                body = self._body(65536)
                if body is None:
                    return self._json(415, {"error": "send JSON"})
                if path == "/pair/open":
                    return self._json(200, pairing.open())
                if path == "/pair/confirm":
                    return self._json(*pairing.confirm(body))
                return self._reply(app.post(path, body))
            if path != "/transcribe" or stt is None:
                return self._json(404, {"error": "not found"})
            n = self._length()
            if not 0 < n <= MAX_BODY:
                return self._json(413, {"error": "empty or too large"})
            data = self.rfile.read(n)  # first: it's signed, and the client then sees a 401, not a broken pipe
            if not self._authorised(data):
                return
            try:
                audio = wav_to_float(data)
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


class _Server(ThreadingHTTPServer):
    """The app's port, held alone: Python's servers set SO_REUSEADDR, which on Windows lets a
    second copy bind a port that's already taken, and requests then go to either copy."""
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--model", default=os.environ.get("STT_MODEL", DEFAULT_MODEL))
    ap.add_argument("--token", default=os.environ.get("STT_TOKEN", ""))
    ap.add_argument("--selftest", metavar="WAV")
    ap.add_argument("--log", metavar="FILE", help="append stdout/stderr here (line-buffered)")
    ap.add_argument("--stats-only", action="store_true", help="serve /stream without the speech model")
    ap.add_argument("--no-beacon", action="store_true", help="don't announce on the network")
    args = ap.parse_args()
    global LOG_FILE
    LOG_FILE = args.log
    if args.log:
        sys.stdout = sys.stderr = open(args.log, "a", buffering=1, encoding="utf-8")
        print(f"=== {time.strftime('%Y-%m-%dT%H:%M:%S')} starting", flush=True)

    # The port first, before the speech model loads: a busy port then fails at once.
    httpd = _Server((args.host, args.port), BaseHTTPRequestHandler)

    global PAIRING
    PAIRING = pairing.Pairing(args.token)
    stream.start()
    global TYPING
    TYPING = typing_link.Typing()
    if not args.no_beacon:
        beacon.Beacon(args.port, PAIRING, TYPING).start()
    stt = None if args.stats_only else STT(args.model)
    if args.selftest and stt:
        with open(args.selftest, "rb") as f:
            audio = wav_to_float(f.read())
        for _ in range(3):
            t = time.perf_counter()
            text = stt.recognize(audio)
            print(f"{len(audio) / 16000:.2f}s audio -> {(time.perf_counter() - t) * 1000:.0f}ms: {text}")
        return 0

    link = frame_link.Frame(PAIRING)
    app = appapi.AppApi(args.port, lambda: app_state(stt, PAIRING), PAIRING, link, video_store.Videos(link), TYPING,
                        app_updates.Updates(), linkquality.LinkQuality(link, PAIRING))
    httpd.RequestHandlerClass = make_handler(stt, PAIRING, app, TYPING)
    print(f"listening on {args.host}:{args.port}; {len(PAIRING.paired)} headset(s) paired", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
