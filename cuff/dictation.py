"""Push-to-talk dictation for the quick row's first button.

A tap starts recording the default mic; a second tap stops it. The clip is transcribed and
the text typed into the focused window through Steam (steam.py). Transcription goes to:
  1. Cuff for PC (pc-server/, found on the local network by pc.py), and any [dictation]
     servers before it, if they answer; else
  2. the headset itself (stt_local.py, Parakeet int8 on the CPU), unless local = false: away
     from home, or with the PC off.
The local model runs in a helper process, started on the first tap (its ~2 s load overlaps
your speech) and gone after five idle minutes.

[dictation] in config.toml (all optional):
  servers = ["http://pc.lan:8765"]   other servers, tried before Cuff for PC
  token = ""                         their bearer token, if they want one
  local = true                       fall back to the headset's own model
  max_seconds = 60
  trailing_space = true
  command = ["~/bin/dictate", "ptt"] run this instead: an external push-to-talk command that
  env = { DICTATE_STATUS = "stdout" }   stops on SIGUSR1, types the text itself, and prints
                                         {"phase", "text"} JSON lines
With nowhere to transcribe, or no way to type, the button is dimmed and a tap only logs.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from . import steam
from .config import CONFIG, RUN_DIR

SETTINGS = CONFIG.get("dictation", {})
SERVERS = list(SETTINGS.get("servers", []))
LOCAL = bool(SETTINGS.get("local", True))
MAX_SECONDS = float(SETTINGS.get("max_seconds", 60))
TRAILING_SPACE = bool(SETTINGS.get("trailing_space", True))
PROBE_S = 1.5  # /health timeout per PC server, checked while you talk


def command():
    cmd = list(SETTINGS.get("command", []))
    return [os.path.expanduser(cmd[0])] + cmd[1:] if cmd else []


def health_urls():
    """/health URLs whose failure dims the button: only when there's no local fallback
    (built in), or for an external command, its [dictation] health and servers_file."""
    if not command():
        return [] if LOCAL else [u.rstrip("/") + "/health" for u in SERVERS]
    servers = list(SETTINGS.get("health", []))
    path = SETTINGS.get("servers_file")
    if path:
        try:
            with open(os.path.expanduser(path)) as f:
                servers += [u.rstrip("/") + "/health" for u in json.load(f).get("servers", [])]
        except (OSError, ValueError):
            pass
    return servers


class LocalModel:
    """The stt_local helper process: started on demand, restarted if it has exited."""

    def __init__(self):
        self.proc, self.lock = None, threading.Lock()

    def warm(self):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self.proc = subprocess.Popen([sys.executable, "-m", "cuff.stt_local"], stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, text=True, bufsize=1)
                self.ready = False

    def transcribe(self, path):
        self.warm()
        with self.lock:
            proc = self.proc
            if not self.ready:
                if json.loads(proc.stdout.readline() or "{}").get("ready") is not True:
                    raise RuntimeError("the local model didn't load (see the log)")
                self.ready = True
            proc.stdin.write(json.dumps({"wav": path}) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
        if not line:
            raise RuntimeError("the local model exited")
        out = json.loads(line)
        if "error" in out:
            raise RuntimeError(out["error"])
        return out


class Dictation:
    """state: {"phase": idle|listening|transcribing|done|error, "text": str, "configured": bool}"""

    def __init__(self, on_change, pc=None):
        self.on_change = on_change
        self.proc = None  # the recorder (built in) or the external command
        self.cmd = command()
        self.local = LocalModel() if LOCAL and not self.cmd else None
        self.pc = pc  # Cuff for PC (pc.Pc), tried after any [dictation] servers
        self.alive = None  # PC servers that answered the last probe (None: not probed yet)
        configured = bool(self.cmd) or bool(SERVERS) or LOCAL or pc is not None
        self.state = {"phase": "idle", "text": "Tap to dictate", "configured": configured}
        if configured and not self.cmd:
            # Typing goes through Steam's undocumented text-input call: check it once.
            threading.Thread(target=self._check_typing, daemon=True, name="dictation-check").start()

    def _check_typing(self):
        if not steam.typing_ok():
            print("dictation: Steam's text input (SteamClient.Input.ControllerKeyboardSendText) isn't "
                  "reachable; greying the button out", file=sys.stderr, flush=True)
            self.state = {**self.state, "configured": False}
            self.on_change()

    def _set(self, phase, text):
        self.state = {**self.state, "phase": phase, "text": text}
        self.on_change()

    def toggle(self):
        if not self.state["configured"]:
            print("dictation: nowhere to transcribe or no way to type; see [dictation] in config.toml",
                  file=sys.stderr, flush=True)
            return
        if self.proc and self.proc.poll() is None:
            if self.state["phase"] == "listening":
                self.proc.send_signal(signal.SIGUSR1 if self.cmd else signal.SIGINT)
            return  # still transcribing: ignore the tap
        if self.state["phase"] == "transcribing":
            return
        if self.cmd:
            self._start_command()
        else:
            self._start_recording()

    # ---------------------------------------------------------------- built in

    def _start_recording(self):
        os.makedirs(RUN_DIR, exist_ok=True)
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="dictation.", dir=RUN_DIR)
        os.close(fd)
        self.proc = subprocess.Popen(["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", path],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if self.local:
            threading.Thread(target=self.local.warm, daemon=True, name="dictation-warm").start()
        if self.servers():  # while you talk, find out which PC servers answer
            self.alive = None
            threading.Thread(target=self._probe, daemon=True, name="dictation-probe").start()
        self._set("listening", "Listening… tap to stop")
        threading.Thread(target=self._finish, args=(self.proc, path), daemon=True, name="dictation").start()

    def servers(self):
        """Where to send clips, in order: [dictation] servers, then Cuff for PC if it's there."""
        pc = self.pc.url() if self.pc else None
        return SERVERS + ([pc] if pc and pc not in SERVERS else [])

    def _headers(self, server):
        """Cuff for PC's pairing token for it, else [dictation] token for the others."""
        if self.pc and server == self.pc.url():
            return self.pc.headers()
        return {"Authorization": f"Bearer {SETTINGS['token']}"} if SETTINGS.get("token") else {}

    def _probe(self):
        alive = []
        for server in self.servers():
            req = urllib.request.Request(server.rstrip("/") + "/health", headers=self._headers(server))
            try:
                with urllib.request.urlopen(req, timeout=PROBE_S):
                    alive.append(server)
            except OSError:
                pass
        self.alive = alive

    def _finish(self, rec, path):
        started = time.monotonic()
        try:
            rec.wait(timeout=MAX_SECONDS)
        except subprocess.TimeoutExpired:
            rec.send_signal(signal.SIGINT)  # lets pw-record finish the WAV header
        try:
            rec.wait(timeout=3)
        except subprocess.TimeoutExpired:
            rec.kill()
        try:
            if time.monotonic() - started < 0.3 or os.path.getsize(path) < 16000:  # an accidental tap
                self._set("idle", "Tap to dictate")
                return
            self._set("transcribing", "Transcribing…")
            text, where = self._transcribe(path)
            if not text:
                self._set("done", "(heard nothing)")
                return
            steam.type_text(text + (" " if TRAILING_SPACE else ""))
            print(f"dictation: {where}: {text!r}", flush=True)
            self._set("done", text)
        except Exception as e:  # noqa: BLE001 - the button must come back to idle
            print(f"dictation: {e!r}", file=sys.stderr, flush=True)
            self._set("error", "Dictation failed")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _transcribe(self, path):
        """(text, where it was transcribed): the first PC server that answers, else local."""
        with open(path, "rb") as f:
            wav = f.read()
        errors = []
        # Servers the probe found answering; all of them if it hasn't finished yet.
        for server in self.servers() if self.alive is None or not self.local else self.alive:
            req = urllib.request.Request(server.rstrip("/") + "/transcribe", data=wav,
                                         headers={"Content-Type": "audio/wav", **self._headers(server)})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.load(r).get("text", "").strip(), server
            except (urllib.error.URLError, OSError, ValueError) as e:
                errors.append(f"{server}: {e}")
        if self.local is None:
            raise RuntimeError("no dictation server answered: " + "; ".join(errors))
        out = self.local.transcribe(path)
        return out.get("text", ""), f"headset ({out.get('audio_s')} s in {out.get('infer_ms')} ms)"

    # ---------------------------------------------------------------- external command

    def _start_command(self):
        if not os.path.exists(self.cmd[0]):
            self._set("error", f"{self.cmd[0]} is not installed")
            return
        env = {**os.environ, **{k: str(v) for k, v in SETTINGS.get("env", {}).items()}}
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, text=True, env=env)
        self._set("listening", "Listening… tap to stop")
        threading.Thread(target=self._read, args=(self.proc,), daemon=True, name="dictation").start()

    def _read(self, proc):
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("phase") == "listening":
                continue  # already shown, with our own wording
            if msg.get("phase") == "idle":
                self._set("idle", "Tap to dictate")
            else:
                self._set(msg.get("phase", "done"), msg.get("text", ""))
        code = proc.wait()
        if self.state["phase"] in ("listening", "transcribing"):
            print(f"dictation: {self.cmd[0]} exited {code}", file=sys.stderr, flush=True)
            self._set("error", f"Dictation failed ({code})")
