"""Pairing a headset with Cuff for PC, so nothing else on the network can use it.

  1. pair.ps1 on the PC opens pairing for OPEN_S (POST /pair/open, from the PC itself only);
     the announcement says so (beacon.py), so the headset notices.
  2. The headset makes up a 4-digit code, shows it on the wrist, and asks to pair
     (POST /pair/request {"id", "name", "code"}; anyone may ask while pairing is open).
  3. The person types the code the wrist shows into pair.ps1 (POST /pair/confirm {"code"},
     from the PC itself only). Only the request carrying that code is paired: another device
     asking at the same time doesn't know the code on the wrist.
  4. The headset collects its token (GET /pair/result?id=..., with the request's random id)
     and sends it as "Authorization: Bearer <token>" from then on.
Tokens live in %LOCALAPPDATA%\\cuff-pc\\paired.json; STT_TOKEN (or --token) is accepted too.
"""
import json
import os
import secrets
import sys
import threading
import time

OPEN_S = 300
MAX_REQUESTS = 8
FILE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share"), "cuff-pc", "paired.json")


class Pairing:
    def __init__(self, extra_token=""):
        self.lock = threading.Lock()
        self.extra = extra_token
        self.open_until = 0.0
        self.requests = {}  # id -> {"name", "code", "token" once confirmed}
        self.paired = self._load()

    @staticmethod
    def _load():
        try:
            with open(FILE) as f:
                return json.load(f).get("tokens", {})
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(FILE), exist_ok=True)
            with open(FILE, "w") as f:
                json.dump({"tokens": self.paired}, f, indent=1)
        except OSError as e:
            print(f"pairing: saving: {e!r}", file=sys.stderr, flush=True)

    def is_open(self):
        return time.monotonic() < self.open_until

    def authorised(self, header):
        token = header[7:] if header and header.startswith("Bearer ") else None
        return bool(token) and (token in self.paired or (self.extra and token == self.extra))

    def open(self):
        with self.lock:
            if time.monotonic() >= self.open_until:  # if still open, keep the code the wrist shows
                self.requests.clear()
            self.open_until = time.monotonic() + OPEN_S
        print(f"pairing: open for {OPEN_S // 60} minutes", flush=True)
        return {"open_s": OPEN_S}

    def request(self, body):
        """A headset asking to pair: {"id", "name", "code"}."""
        req_id, code = str(body.get("id", "")), str(body.get("code", ""))
        if not self.is_open():
            return 403, {"error": "pairing isn't open: run pair.ps1 on the PC"}
        if len(req_id) < 16 or not (code.isdigit() and len(code) == 4):
            return 400, {"error": "bad request"}
        with self.lock:
            if len(self.requests) >= MAX_REQUESTS and req_id not in self.requests:
                return 429, {"error": "too many requests"}
            self.requests[req_id] = {"name": str(body.get("name", "headset"))[:40], "code": code, "token": None}
        print(f"pairing: request from {body.get('name')!r}", flush=True)
        return 200, {"ok": True}

    def confirm(self, body):
        """The code typed on the PC: pairs the request that carries it."""
        code = str(body.get("code", "")).strip()
        with self.lock:
            if not self.is_open():
                return 403, {"error": "pairing isn't open"}
            matches = [r for r in self.requests.values() if r["code"] == code and not r["token"]]
            if len(matches) != 1:
                return 404, {"error": "no headset is showing that code"}
            req = matches[0]
            req["token"] = secrets.token_urlsafe(24)
            self.paired[req["token"]] = {"name": req["name"], "paired": time.strftime("%Y-%m-%d %H:%M")}
            self._save()
            self.open_until = 0.0  # one headset per pairing
        print(f"pairing: paired {req['name']!r}", flush=True)
        return 200, {"ok": True, "name": req["name"]}

    def result(self, req_id):
        with self.lock:
            req = self.requests.get(req_id)
            if not req:
                return 404, {"error": "unknown request"}
            if not req["token"]:
                return 202, {"pending": True, "open": self.is_open()}
            del self.requests[req_id]
            return 200, {"token": req["token"]}
