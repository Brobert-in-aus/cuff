"""Pairing a headset with Cuff for PC, so nothing else on the network can use it.

  1. pair.ps1 on the PC opens pairing for OPEN_S (POST /pair/open, from the PC itself only);
     the announcement says so (beacon.py), so the headset notices.
  2. The headset makes up a 4-digit code, shows it on the wrist, and asks to pair
     (POST /pair/request {"id", "name", "x"}). The code itself never crosses the network: x is
     the headset's half of a SPAKE2 exchange keyed by it (below). Anyone may ask while pairing
     is open, each address at most once a second and PER_ADDRESS requests at a time; with
     MAX_REQUESTS waiting, a new one pushes out the oldest rather than being refused.
  3. The person types the code the wrist shows into pair.ps1 (POST /pair/confirm {"code"},
     from the PC itself only). The PC answers every waiting request with its half of the
     exchange (y) and a new token sealed with the key it shares with whoever knows the code
     ("box", "mac"), and waits up to CONFIRM_S for one to come back.
  4. The headset collects that (GET /pair/result?id=..., from the address that asked), checks
     it was made with its code, opens the token, and proves it did (POST /pair/finish {"id",
     "round", "mac"}). Only then is the token paired, and pair.ps1 says so; otherwise it says
     no headset is showing that code. A device that doesn't know the code gets one guess per
     typed code at most: it can't pose as the PC to the headset, nor pair in its place.

After that the token never crosses the network. Each request the headset makes is signed
with it (check): "Authorization: Cuff v=1, id=<headset id>, ts=<unix time>, nonce=<random>,
mac=<hex HMAC-SHA256(token, method\\npath?query\\nts\\nnonce\\nsha256(body) in hex)>". The
headset id is the short hash of its token the app page shows. A request more than SKEW_S
from the PC's clock, or with a nonce seen before, is refused. The PC signs its answer to a
signed request (response_mac: header X-Cuff-Mac, HMAC-SHA256(token, "response\\nnonce\\n
status\\nsha256(body)")), and the headset acts on nothing that isn't signed, so a device
copying the announcement can neither learn the token nor type on the headset. This PC's id
(pc_id: random, made once and kept with the tokens) is in the announcement, /health and the
pairing answer, so the headset tells this PC from another using its name.

Tokens live in %LOCALAPPDATA%\\cuff-pc\\paired.json. STT_TOKEN (or --token), a token set by
hand on both ends, is also accepted signed, or as "Authorization: Bearer <token>" for other
dictation clients. The app page lists the paired headsets by a short hash of the token, never
the token, with when each was last heard from (kept in memory only), and can unpair one.
"""
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time

OPEN_S = 300
CONFIRM_S = 10  # after the code is typed, how long the headset has to collect and finish
DONGLE_NET = "10.35.78."  # the Steam Frame's own access point, which the dongle joins
MAX_REQUESTS = 8
PER_ADDRESS = 2
SKEW_S = 60  # a signed request this far from the PC's clock is refused
MAX_NONCES = 4096
FILE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share"), "cuff-pc", "paired.json")

# SPAKE2 over RFC 3526's 2048-bit group (cuff/pc.py has the same): both ends mix the code into
# a Diffie-Hellman exchange, so they share a key only if they used the same code, and someone
# watching or posing as one end learns nothing that lets them test guesses at it offline.
P = int("FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
        "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
        "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
        "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
        "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
        "15728E5A8AACAA68FFFFFFFFFFFFFFFF", 16)
Q = (P - 1) // 2  # the order of the group 2 generates
G = 2


def _element(label):
    """A fixed group element whose discrete log nobody knows: a hash of label, squared."""
    data = b"".join(hashlib.sha256(f"{label}\n{i}".encode()).digest() for i in range(8))
    return pow(int.from_bytes(data, "big") % P, 2, P)


M, N = _element("cuff pair M"), _element("cuff pair N")


def _in_group(v):
    return 1 < v < P - 1 and pow(v, Q, P) == 1


def _code_scalar(code, req_id):
    return int.from_bytes(hashlib.sha256(f"cuff pair w\n{req_id}\n{code}".encode()).digest(), "big") % Q


def _pake_key(req_id, x, y, z):
    return hashlib.sha256(f"cuff pair key\n{req_id}\n{x:x}\n{y:x}\n{z:x}".encode()).digest()


def _pake_mac(key, text):
    return hmac.new(key, text.encode(), hashlib.sha256).hexdigest()


def _seal(key, token):
    """The 32-character token XORed with a pad from the shared key (hex); the same opens it."""
    pad = hmac.new(key, b"box", hashlib.sha256).digest()
    return bytes(a ^ b for a, b in zip(token.encode(), pad)).hex()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def request_mac(token, method, path, ts, nonce, body):
    """The MAC a headset's request carries (cuff/pc.py makes the same)."""
    message = f"{method}\n{path}\n{ts}\n{nonce}\n{_sha(body)}"
    return hmac.new(token.encode(), message.encode(), hashlib.sha256).hexdigest()


def response_mac(token, nonce, status, body):
    """The MAC on the PC's answer to a signed request: X-Cuff-Mac (cuff/pc.py checks it)."""
    message = f"response\n{nonce}\n{status}\n{_sha(body)}"
    return hmac.new(token.encode(), message.encode(), hashlib.sha256).hexdigest()


class Pairing:
    def __init__(self, extra_token=""):
        self.lock = threading.Lock()
        self.finished = threading.Condition(self.lock)  # a headset finished pairing (finish)
        self.extra = extra_token
        self.open_until = 0.0
        self.requests = {}  # id -> {"name", "x", "address", "round": the PC's answer once the code is typed}
        self.asked = {}  # address -> when it last asked to pair
        self.rounds = 0  # codes typed, so the headset can tell a new answer from one it tried
        self.just_paired = None  # the name finish() paired, for confirm()
        self.paired, self.pc_id = {}, None
        self._load()
        self.seen = {}  # token -> (time.time(), address) of its last authorised request
        self.nonces = {}  # nonce of a signed request -> when it can be forgotten, oldest first

    def _load(self):
        try:
            with open(FILE) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        self.paired, self.pc_id = data.get("tokens") or {}, data.get("pc_id")
        if not isinstance(self.pc_id, str) or not self.pc_id:
            # Made once, so the headset can tell this PC from another one using its name.
            self.pc_id = secrets.token_hex(8)
            self._save()

    def _save(self):
        try:
            os.makedirs(os.path.dirname(FILE), exist_ok=True)
            with open(FILE, "w") as f:
                json.dump({"pc_id": self.pc_id, "tokens": self.paired}, f, indent=1)
        except OSError as e:
            print(f"pairing: saving: {e!r}", file=sys.stderr, flush=True)

    def is_open(self):
        return time.monotonic() < self.open_until

    def authorised(self, header, address=None):
        """Whether "Bearer <token>" names a paired token. In-process only (frame.py checks the
        token a headset already holds); requests from the network go through check()."""
        token = header[7:] if header and header.startswith("Bearer ") else None
        ok = bool(token) and (token in self.paired or (self.extra and token == self.extra))
        if ok and token in self.paired:
            self.seen[token] = (time.time(), address)
        return ok

    def check(self, header, method, path, body=b"", address=None):
        """A request's Authorization header -> (token, nonce, None) when a paired headset signed
        it; (token, None, None) for STT_TOKEN sent as a bearer token; else (None, None, why)."""
        header = header or ""
        if self.extra and header.startswith("Bearer ") and hmac.compare_digest(header[7:], self.extra):
            return self.extra, None, None
        if not header.startswith("Cuff "):
            return None, None, "not paired: run pair.ps1 on the PC"
        fields = dict(part.strip().partition("=")[::2] for part in header[5:].split(","))
        nonce, mac = fields.get("nonce", ""), fields.get("mac", "")
        try:
            ts = int(fields.get("ts", ""))
        except ValueError:
            ts = None
        if fields.get("v") != "1" or ts is None or not 16 <= len(nonce) <= 64:
            return None, None, "bad Authorization header"
        with self.lock:
            tokens = list(self.paired) + ([self.extra] if self.extra else [])
        token = next((t for t in tokens if self._id(t) == fields.get("id")), None)
        if token is None:
            return None, None, "not paired: run pair.ps1 on the PC"
        if not hmac.compare_digest(mac, request_mac(token, method, path, ts, nonce, body)):
            return None, None, "bad signature"
        now = time.time()
        if abs(now - ts) > SKEW_S:
            return None, None, "the headset's and the PC's clocks are more than a minute apart"
        with self.lock:
            while self.nonces and (len(self.nonces) >= MAX_NONCES or next(iter(self.nonces.values())) < now):
                self.nonces.pop(next(iter(self.nonces)))
            if nonce in self.nonces:
                return None, None, "replayed request"
            self.nonces[nonce] = now + 2 * SKEW_S
            if token in self.paired:
                self.seen[token] = (now, address)
        return token, nonce, None

    @staticmethod
    def _id(token):
        return hashlib.sha256(token.encode()).hexdigest()[:12]

    def headsets(self):
        """[{"id", "name", "paired", "seen_s" (seconds ago, or None), "address", "link"}], for the
        app page. link: "dongle" over the Frame's own access point (DONGLE_NET, the dongle's
        network; see firewall.ps1), else "network"."""
        out = []
        with self.lock:
            for token, info in self.paired.items():
                when, address = self.seen.get(token, (None, None))
                link = None if not address else "dongle" if address.startswith(DONGLE_NET) else "network"
                out.append({"id": self._id(token), "name": info.get("name", "headset"), "paired": info.get("paired"),
                            "seen_s": round(time.time() - when) if when else None, "address": address,
                            "link": link})
        return out

    def add(self, name):
        """Pair a headset without a code (the PC app, over SSH, which already proves it's
        the person's headset). Returns its token."""
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.paired[token] = {"name": str(name)[:40], "paired": time.strftime("%Y-%m-%d %H:%M")}
            self._save()
        print(f"pairing: paired {name!r} over SSH", flush=True)
        return token

    def unpair(self, headset_id):
        with self.lock:
            token = next((t for t in self.paired if self._id(t) == headset_id), None)
            if token is None:
                return 404, {"error": "no such headset"}
            name = self.paired.pop(token).get("name")
            self.seen.pop(token, None)
            self._save()
        print(f"pairing: unpaired {name!r}", flush=True)
        return 200, {"ok": True, "name": name}

    def open(self):
        with self.lock:
            if time.monotonic() >= self.open_until:  # if still open, keep the code the wrist shows
                self.requests.clear()
            self.open_until = time.monotonic() + OPEN_S
        print(f"pairing: open for {OPEN_S // 60} minutes", flush=True)
        return {"open_s": OPEN_S}

    def request(self, body, address=None):
        """A headset asking to pair: {"id", "name", "x"}, from address."""
        req_id, x = str(body.get("id", "")), body.get("x")
        if not self.is_open():
            return 403, {"error": "pairing isn't open: run pair.ps1 on the PC"}
        if "code" in body and x is None:
            return 400, {"error": "update Cuff on the headset to pair with this Cuff for PC"}
        try:
            x = int(x, 16) if isinstance(x, str) and len(x) <= 600 else 0
        except ValueError:
            x = 0
        if not 16 <= len(req_id) <= 64 or not _in_group(x):
            return 400, {"error": "bad request"}
        name = str(body.get("name", "headset"))[:40]
        now = time.monotonic()
        with self.lock:
            if now - self.asked.get(address, -60.0) < 1.0:
                return 429, {"error": "too many requests"}
            self.asked = {a: t for a, t in self.asked.items() if now - t < 60}
            self.asked[address] = now
            # Make room by dropping this address's oldest, then anyone's oldest, rather than
            # refusing, so a flood from elsewhere can't keep the headset from asking.
            mine = [k for k, r in self.requests.items() if r["address"] == address and k != req_id]
            while len(mine) >= PER_ADDRESS:
                self.requests.pop(mine.pop(0))
            while len(self.requests) >= MAX_REQUESTS and req_id not in self.requests:
                self.requests.pop(next(iter(self.requests)))
            self.requests[req_id] = {"name": name, "x": x, "address": address, "round": None}
        print(f"pairing: request from {name!r} at {address}", flush=True)
        return 200, {"ok": True}

    def confirm(self, body):
        """The code typed on the PC: answers every waiting request with it, and waits up to
        CONFIRM_S for the headset showing it to finish."""
        code = str(body.get("code", "")).strip()
        if not (code.isdigit() and len(code) == 4):
            return 404, {"error": "no headset is showing that code"}
        with self.lock:
            if not self.is_open():
                return 403, {"error": "pairing isn't open"}
            waiting = [(k, r["x"]) for k, r in self.requests.items()]
            self.rounds += 1
            n, self.just_paired = self.rounds, None
        answers = {}
        for req_id, x in waiting:  # a few modular exponentiations each, outside the lock
            w, y_secret = _code_scalar(code, req_id), secrets.randbelow(Q - 1) + 1
            y = pow(G, y_secret, P) * pow(N, w, P) % P
            key = _pake_key(req_id, x, y, pow(x * pow(M, Q - w, P) % P, y_secret, P))
            token = secrets.token_urlsafe(24)
            answers[req_id] = {"n": n, "y": y, "token": token, "key": key, "at": time.monotonic(),
                               "box": _seal(key, token), "mac": _pake_mac(key, f"pc\n{self.pc_id}\n{token}")}
        with self.lock:
            for req_id, answer in answers.items():
                if req_id in self.requests:
                    self.requests[req_id]["round"] = answer
            self.finished.wait_for(lambda: self.just_paired is not None or self.rounds != n, timeout=CONFIRM_S)
            name = self.just_paired if self.rounds == n else None
            for req in self.requests.values():
                if req["round"] and req["round"]["n"] == n:
                    req["round"] = None
        if name is None:
            return 404, {"error": "no headset is showing that code"}
        return 200, {"ok": True, "name": name}

    def result(self, req_id, address=None):
        """The PC's answer for the address that asked, once a code has been typed."""
        with self.lock:
            req = self.requests.get(req_id)
            if not req or req["address"] != address:
                return 404, {"error": "unknown request"}
            answer = req["round"]
            if not answer or time.monotonic() - answer["at"] > CONFIRM_S:
                return 202, {"pending": True, "open": self.is_open()}
            return 200, {"round": answer["n"], "y": f"{answer['y']:x}", "box": answer["box"], "mac": answer["mac"],
                         "pc_id": self.pc_id}

    def finish(self, body, address=None):
        """The headset proving it opened the answer with the same code: pairs its token."""
        req_id = str(body.get("id", ""))
        with self.lock:
            req = self.requests.get(req_id)
            answer = req and req["address"] == address and req["round"]
            if not answer or body.get("round") != answer["n"] or time.monotonic() - answer["at"] > CONFIRM_S:
                return 404, {"error": "unknown request"}
            if not hmac.compare_digest(str(body.get("mac", "")), _pake_mac(answer["key"], "headset")):
                return 403, {"error": "wrong code"}
            self.paired[answer["token"]] = {"name": req["name"], "paired": time.strftime("%Y-%m-%d %H:%M")}
            self._save()
            del self.requests[req_id]
            self.open_until = 0.0  # one headset per pairing
            self.just_paired = req["name"]
            self.finished.notify_all()
        print(f"pairing: paired {req['name']!r}", flush=True)
        return 200, {"ok": True}
