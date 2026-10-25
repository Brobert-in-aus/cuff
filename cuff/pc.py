"""The PC app (pc-server/, "Cuff for PC"): where it is, whether it's there, and pairing.

It announces itself every 2 s with a UDP broadcast on every network the PC is on (port
8766), so it's heard over home Wi-Fi and over the Frame's dongle link alike. pc = "http://..."
in config.toml overrides that.

Pairing (pc-server/pairing.py): while the announcement says pairing is open (pair.ps1 on the
PC) and there's no token for that PC, the headset makes up a 4-digit code, shows it as an
alert toast that stays up while pairing waits, asks to pair, and collects its token once the
code is typed on the PC. An alert then says it paired, or that pairing closed first. Tokens
are kept per PC name in ~/.config/cuff/pc-pairing.json and sent as a bearer token.

state() is "connected" (heard in the last LOST_S, or configured, and paired), "unpaired"
(there but not paired), "away" (heard before, remembered in ~/.cache/cuff/pc.json, but not
now: away from home, the PC off), or "missing" (never heard: not installed). Charms show
PC-only fields as needing the PC app instead of standing in headset figures.
"""
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

from .config import CACHE_DIR, CONFIG, CONFIG_FILE

PORT = 8766
LOST_S = 10.0
FILE = os.path.join(CACHE_DIR, "pc.json")
TOKENS = os.path.join(os.path.dirname(CONFIG_FILE), "pc-pairing.json")
PAIR_WAIT_S = 330.0  # a backstop past the PC's 5-minute window (it says when it closes)


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class Pc(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="pc")
        self.configured = (CONFIG.get("pc") or "").rstrip("/") or None
        self.heard = {}  # address -> (monotonic time, http port)
        self.name, self.pairing_open = None, False
        self.known = _read_json(FILE)
        self.tokens = _read_json(TOKENS) or {}  # PC name -> token
        self.notify = None  # set by the core: notify(text, haptic) raises a toast
        self.pin = None  # and pin(text) keeps one up (pin(None) takes it down)
        self.pairing = None  # (thread) while a pairing request is out

    def _remember(self):
        known = {"name": self.name, "addresses": sorted(self.heard)}
        if known != self.known:
            self.known = known
            try:
                os.makedirs(CACHE_DIR, exist_ok=True)
                with open(FILE, "w") as f:
                    json.dump(known, f)
            except OSError as e:
                print(f"pc: {e!r}", file=sys.stderr, flush=True)

    def run(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", PORT))
        except OSError as e:
            print(f"pc: can't listen for the PC app on UDP {PORT}: {e!r}", file=sys.stderr, flush=True)
            return
        while True:
            try:
                data, (addr, _) = s.recvfrom(2048)
                msg = json.loads(data)
            except (OSError, ValueError):
                continue
            if msg.get("app") != "cuff-pc":
                continue
            first = addr not in self.heard
            self.heard[addr] = (time.monotonic(), int(msg.get("port", 8765)))
            self.name, self.pairing_open = msg.get("name"), bool(msg.get("pairing"))
            if first:
                print(f"pc: Cuff for PC ({self.name}) at {addr}", flush=True)
                self._remember()
            if self.pairing_open and not self.token() and self.pairing is None:
                self.pairing = threading.Thread(target=self._pair, daemon=True, name="pc-pairing")
                self.pairing.start()

    # ---------------------------------------------------------------- where it is

    def addresses(self):
        """The PC's addresses heard in the last LOST_S, newest first."""
        now = time.monotonic()
        alive = [(t, a) for a, (t, _) in self.heard.items() if now - t < LOST_S]
        return [a for _, a in sorted(alive, reverse=True)]

    def _base(self):
        if self.configured:
            return self.configured
        for addr in self.addresses():
            return f"http://{addr}:{self.heard[addr][1]}"
        return None

    def token(self):
        return self.tokens.get(self.name or "") or (self.tokens.get("*") if self.configured else None)

    def url(self):
        """Cuff for PC's base URL while it's reachable and paired, else None."""
        return self._base() if self.token() else None

    def headers(self):
        token = self.token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def state(self):
        if self._base():
            return "connected" if self.token() else "unpaired"
        return "away" if self.known else "missing"

    # ---------------------------------------------------------------- pairing

    def _post(self, base, path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.load(r)

    def _pair(self):
        try:
            base, name = self._base(), self.name
            code = f"{secrets.randbelow(10000):04d}"
            req_id = secrets.token_urlsafe(16)
            self._post(base, "/pair/request", {"id": req_id, "code": code, "name": socket.gethostname()})
            print(f"pc: asked {name} to pair; showing pairing code {code}", flush=True)
            text = f"Type {code} in {name}'s pairing window"
            self._say(text, alert=True)
            self._pin(text)
            deadline = time.monotonic() + PAIR_WAIT_S
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(f"{base}/pair/result?id={req_id}", timeout=3) as r:
                        out = json.load(r)
                    if out.get("token"):
                        self.tokens[name] = out["token"]
                        os.makedirs(os.path.dirname(TOKENS), exist_ok=True)
                        with open(TOKENS, "w") as f:
                            json.dump(self.tokens, f)
                        os.chmod(TOKENS, 0o600)
                        print(f"pc: paired with {name}", flush=True)
                        self._pin(None)
                        self._say(f"Paired with {name}", alert=True)
                        return
                    if not out.get("open", True):
                        break  # pairing closed (timed out, or another headset paired)
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(2)
            print(f"pc: pairing with {name} closed before the code was typed", flush=True)
            self._pin(None)
            self._say(f"Pairing closed: run pair.ps1 on {name} again", alert=True)
        except (OSError, ValueError) as e:
            print(f"pc: pairing: {e!r}", file=sys.stderr, flush=True)
        finally:
            self._pin(None)
            self.pairing = None

    def _say(self, text, alert):
        if self.notify:
            self.notify(text, alert)

    def _pin(self, text):
        """Keep text on the wrist until _pin(None)."""
        if self.pin:
            self.pin(text)


def needs_pc(state):
    """What a PC-only field says instead of a value, or None when the PC is there."""
    return {"missing": "Install Cuff for PC", "away": "Cuff for PC not connected",
            "unpaired": "Pair Cuff for PC: run pair.ps1 on the PC"}.get(state)
