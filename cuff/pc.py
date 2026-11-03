"""The PC app (pc-server/, "Cuff for PC"): where it is, whether it's there, and pairing.

It announces itself every 2 s with a UDP broadcast on the PC's home networks (port 8766),
so it's heard over home Wi-Fi and over the Frame's dongle link alike, with its id: random,
made once on the PC. pc = "http://..." in config.toml overrides that.

Anyone can send that announcement, so it only says where to look. An address is used only
once the PC there has proved it holds this headset's token: a signed GET /health (below)
whose answer is signed with the token and names the PC id. The check is redone once a minute,
which is also the hello that has the PC app show this headset as connected. Addresses are kept
per PC id; a malformed announcement is ignored.

Pairing (pc-server/pairing.py): while the announcement says pairing is open (pair.ps1 on the
PC) and there's no token for that PC, the headset makes up a 4-digit code, shows it as an
alert toast that stays up while pairing waits, and asks to pair. The code never crosses the
network: the request carries the headset's half of a SPAKE2 exchange keyed by it, and the
PC's answer, made once the code is typed on the PC, opens only with the same code (a few
wrong answers end it). An alert then says it paired, or that pairing closed first.

Tokens are kept in ~/.config/cuff/pc-pairing.json, mode 600: {<PC name>: token, ...,
"@ids": {<PC id>: <PC name>}}. A token is used for a PC id once that PC has proved it holds
it; one written under the PC's name alone (by an older Cuff, or by the PC app over SSH) is
tried with the PC of that name and bound to its id the first time it proves it.

The token itself never crosses the network. headers() gives a request to the PC a marker
instead (X-Cuff-Pc: <PC id>), and _Signer, installed into urllib, signs a marked request as it
leaves, but only to an address that has passed the check: "Authorization: Cuff v=1, id=<this
headset's id>, ts, nonce, mac" (pairing.py has the format). It checks the answer too: a 2xx
must carry X-Cuff-Mac, the PC's MAC over the request's nonce and the answer, or the caller
gets an error instead, so nothing typed or dictated comes from anyone but the paired PC.

Typing from the PC (pc-server/typing_link.py): while the paired PC's announcement says
"typing", it long-polls GET /typing/next and types what comes through Steam (steam.py), with
"Typing from <PC>" pinned on the wrist while text arrives. Settings can refuse it
(typing_allowed, the "typing" alert switch). Nothing typed is logged.

state() is "connected" (a checked address heard in the last LOST_S, or configured, and
paired), "unpaired" (there but not paired), "away" (heard before, remembered in
~/.cache/cuff/pc.json, but not now: away from home, the PC off; or there with a token that
hasn't checked out yet), or "missing" (never heard: not installed). Charms show PC-only fields
as needing the PC app instead of standing in headset figures.
"""
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.response

from .config import CACHE_DIR, CONFIG, CONFIG_FILE

PORT = 8766
LOST_S = 10.0
TYPING_IDLE_S = 8.0  # "Typing from <PC>" stays up this long after the last text
HELLO_S = 50.0  # a checked address is checked again this often: the hello (the PC app wants one a minute)
RECHECK_S = 30.0  # an address that failed the check is tried again after this
FILE = os.path.join(CACHE_DIR, "pc.json")
TOKENS = os.path.join(os.path.dirname(CONFIG_FILE), "pc-pairing.json")
IDS = "@ids"  # in TOKENS: PC id -> the PC name its token is under ("@" isn't allowed in one)
PAIR_WAIT_S = 330.0  # a backstop past the PC's 5-minute window (it says when it closes)
PAIR_TRIES = 3  # answers made with another code before pairing gives up (a typo, or a guess)
PAIR_AGAIN_S = 30.0  # after pairing ends, before asking again
MARKER = "X-cuff-pc"  # headers()' marker for _Signer, as urllib spells a header's name
MAX_PCS, MAX_ADDRESSES = 16, 8
PC_ID = re.compile(r"[0-9A-Za-z_-]{8,64}")

# SPAKE2 over RFC 3526's 2048-bit group, as pc-server/pairing.py: both ends mix the code into a
# Diffie-Hellman exchange, so they share a key only if they used the same code.
P = int("FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
        "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
        "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
        "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
        "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
        "15728E5A8AACAA68FFFFFFFFFFFFFFFF", 16)
Q = (P - 1) // 2
G = 2


def _element(label):
    data = b"".join(hashlib.sha256(f"{label}\n{i}".encode()).digest() for i in range(8))
    return pow(int.from_bytes(data, "big") % P, 2, P)


M, N = _element("cuff pair M"), _element("cuff pair N")


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def headset_id(token):
    """This headset's id at the PC: the short hash of its token the PC app shows."""
    return _sha(token.encode())[:12]


def sign(token, method, path, body=b""):
    """-> (Authorization header for a request, its nonce). pc-server/pairing.py checks it."""
    ts, nonce = int(time.time()), secrets.token_hex(16)
    message = f"{method}\n{path}\n{ts}\n{nonce}\n{_sha(body)}"
    mac = hmac.new(token.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"Cuff v=1, id={headset_id(token)}, ts={ts}, nonce={nonce}, mac={mac}", nonce


def answer_ok(token, nonce, status, body, mac):
    """Whether the PC's answer to a signed request carries its MAC (X-Cuff-Mac)."""
    message = f"response\n{nonce}\n{status}\n{_sha(body)}"
    return hmac.compare_digest(mac or "", hmac.new(token.encode(), message.encode(), hashlib.sha256).hexdigest())


def parse_beacon(data):
    """An announcement -> (PC id, name, HTTP port, pairing, typing), or None if it isn't one."""
    try:
        msg = json.loads(data)
    except ValueError:
        return None
    if not isinstance(msg, dict) or msg.get("app") != "cuff-pc":
        return None
    pc_id, port, name = msg.get("id"), msg.get("port", 8765), msg.get("name")
    if not isinstance(pc_id, str) or not PC_ID.fullmatch(pc_id):
        return None  # an older Cuff for PC, which can't sign: it needs updating
    if type(port) is not int or not 0 < port < 65536:
        return None
    name = name[:64] if isinstance(name, str) and name and name not in ("*", IDS) else "PC"
    return pc_id, name, port, msg.get("pairing") is True, msg.get("typing") is True


class _Signer(urllib.request.BaseHandler):
    """Signs requests carrying headers()' marker as they leave, and checks the PC's answers."""
    handler_order = 400  # before urllib's own, which turn errors into HTTPError

    def __init__(self, pc):
        self.pc = pc

    def http_request(self, req):
        marker = req.get_header(MARKER)
        if marker is None:
            return req
        req.remove_header(MARKER)
        token = self.pc._token_for_request(marker, req.host)
        body = req.data if req.data is not None else b""
        if not token or not isinstance(body, bytes):
            raise urllib.error.URLError("Cuff for PC: not a checked address of the paired PC")
        header, nonce = sign(token, req.get_method(), req.selector, body)
        req.add_unredirected_header("Authorization", header)
        req.cuff_signed = (token, nonce)
        return req

    def http_response(self, req, response):
        signed = getattr(req, "cuff_signed", None)
        if not signed or response.status >= 400:
            return response  # unsigned, or an error, which urllib raises and nobody acts on
        if not 200 <= response.status < 300:
            raise urllib.error.URLError(f"Cuff for PC answered {response.status}")  # no redirects elsewhere
        body = response.read()
        if not answer_ok(*signed, response.status, body, response.headers.get("X-Cuff-Mac")):
            raise urllib.error.URLError("Cuff for PC's answer isn't signed by the paired PC")
        out = urllib.response.addinfourl(io.BytesIO(body), response.headers, response.url, response.status)
        out.msg = response.msg
        return out

    https_request, https_response = http_request, http_response


class Pc(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="pc")
        self.configured = (CONFIG.get("pc") or "").rstrip("/") or None
        self.lock = threading.Lock()
        # PC id -> {"name", "pairing", "typing", "heard", "addresses": {(address, port): heard}}. By
        # address and port, as checked is: an announcement's source address can be forged.
        self.pcs = {}
        self.checked = {}  # (PC id, address, port) -> (it proved it holds our token, monotonic time)
        self.checking = set()
        self.refused = set()  # PC ids whose /health said our token isn't paired there
        self.known = _read_json(FILE)
        tokens = _read_json(TOKENS)
        self.tokens = tokens if isinstance(tokens, dict) else {}
        self.notify = None  # set by the core: notify(text, haptic) raises a toast
        self.pin = None  # and pin(text) keeps one up (pin(None) takes it down)
        self.pairing = None  # (thread) while a pairing request is out
        self.pair_after = 0.0  # no new pairing before this (monotonic)
        self.typing_on = False  # the paired PC's announcement says it has typing for us
        self.typer = None  # (thread) fetching typed text
        self.typing_allowed = lambda: True  # set by the core from Settings
        self._bad_at = 0.0
        urllib.request.install_opener(urllib.request.build_opener(_Signer(self)))

    # ---------------------------------------------------------------- tokens

    def _ids(self):
        ids = self.tokens.get(IDS)
        return ids if isinstance(ids, dict) else {}

    def _token_for(self, pc_id, name):
        """-> (token, bound): the token bound to this PC id, else one under its name that no
        other PC id has (to be checked, then bound), else (None, False)."""
        ids = self._ids()
        if pc_id in ids:
            token = self.tokens.get(ids[pc_id])
            return (token, True) if isinstance(token, str) and token else (None, False)
        token = self.tokens.get(name)
        if name in ids.values() or not isinstance(token, str) or not token:
            return None, False
        return token, False

    def _save_tokens(self):
        try:
            os.makedirs(os.path.dirname(TOKENS), exist_ok=True)
            fd = os.open(TOKENS + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.tokens, f)
            os.replace(TOKENS + ".new", TOKENS)
        except OSError as e:
            print(f"pc: saving tokens: {e!r}", file=sys.stderr, flush=True)

    def _bind(self, pc_id, name, token=None):
        """Remember name's token (a new one from pairing, or the one there) as this PC id's."""
        with self.lock:
            if token:
                self.tokens[name] = token
            ids = {i: n for i, n in self._ids().items() if n != name}
            ids[pc_id] = name
            self.tokens[IDS] = ids
            self.refused.discard(pc_id)
            self._save_tokens()

    def _configured_token(self):
        token = self.tokens.get("*")
        if isinstance(token, str) and token:
            return token
        current = self._current()
        return self._token_for(current[0], current[3])[0] if current else None

    def _token_for_request(self, marker, host):
        """The token _Signer signs with for a request to host ("address:port"), or None."""
        if marker == "*":
            ok = self.configured and urllib.parse.urlsplit(self.configured).netloc == host
            return self._configured_token() if ok else None
        with self.lock:
            pc = self.pcs.get(marker)
            if not pc or not any(f"{a}:{port}" == host and self.checked.get((marker, a, port), (False,))[0]
                                 for a, port in pc["addresses"]):
                return None
            name = pc["name"]
        return self._token_for(marker, name)[0]

    # ---------------------------------------------------------------- hearing it

    def _remember(self, pc_id):
        pc = self.pcs.get(pc_id) or {}
        known = {"name": pc.get("name"), "id": pc_id, "addresses": sorted({a for a, _ in pc.get("addresses", {})})}
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
            except OSError:
                time.sleep(0.5)
                continue
            try:
                self.heard_from(addr, data)
            except Exception as e:  # noqa: BLE001 - one odd packet mustn't stop the listener
                if time.monotonic() - self._bad_at > 60:
                    self._bad_at = time.monotonic()
                    print(f"pc: announcement from {addr}: {e!r}", file=sys.stderr, flush=True)

    def heard_from(self, addr, data):
        """One announcement, from addr."""
        beacon = parse_beacon(data)
        if beacon is None:
            return
        pc_id, name, port, pairing, typing = beacon
        now = time.monotonic()
        with self.lock:
            pc = self.pcs.get(pc_id)
            if pc is None:
                self._forget_some(now)
                pc = self.pcs[pc_id] = {"addresses": {}}
            first = (addr, port) not in pc["addresses"]
            if first and len(pc["addresses"]) >= MAX_ADDRESSES:  # make room: unchecked ones first
                oldest = min(pc["addresses"], key=lambda w: (self.checked.get((pc_id, *w), (False,))[0],
                                                              pc["addresses"][w]))
                del pc["addresses"][oldest]
            pc["addresses"][(addr, port)] = now
            pc.update(name=name, pairing=pairing, typing=typing, heard=now)
        token, bound = self._token_for(pc_id, name)
        if first:
            print(f"pc: Cuff for PC ({name}) announced at {addr}:{port}", flush=True)
        if token:
            self._check_soon(pc_id, addr, port, name, token, bound)
        if pairing and (not token or pc_id in self.refused) and self.pairing is None and now >= self.pair_after:
            self.pairing = threading.Thread(target=self._pair, args=(pc_id, addr, port, name), daemon=True,
                                            name="pc-pairing")
            self.pairing.start()
        current = self._current()
        if current and current[0] == pc_id:
            self.typing_on = typing
            if typing and self.typer is None and self.typing_allowed():
                self.typer = threading.Thread(target=self._type_loop, daemon=True, name="pc-typing")
                self.typer.start()

    def _forget_some(self, now):
        """Under the lock: keep the PCs heard of to MAX_PCS, forgetting unbound ones heard longest ago."""
        ids = self._ids()
        while len(self.pcs) >= MAX_PCS:
            spare = [i for i in self.pcs if i not in ids] or list(self.pcs)
            gone = min(spare, key=lambda i: self.pcs[i].get("heard", 0))
            del self.pcs[gone]
            self.checked = {k: v for k, v in self.checked.items() if k[0] != gone}

    def _check_soon(self, pc_id, addr, port, name, token, bound):
        key = (pc_id, addr, port)
        with self.lock:
            ok, at = self.checked.get(key, (False, None))
            due = at is None or time.monotonic() - at > (HELLO_S if ok else RECHECK_S)
            if not due or key in self.checking or len(self.checking) >= 4:
                return
            self.checking.add(key)
        threading.Thread(target=self._check, args=(pc_id, addr, port, name, token, bound), daemon=True,
                         name="pc-check").start()

    def _check(self, pc_id, addr, port, name, token, bound):
        """A signed GET /health to addr: the PC there proves it holds token (it signs the answer)
        and is pc_id. Run once a minute while it answers, it's also the hello the PC app shows."""
        ok, refused = False, False
        try:
            out = self._signed(f"http://{addr}:{port}", "/health", token)
            ok = isinstance(out, dict) and out.get("id") == pc_id and out.get("paired") is True
        except urllib.error.HTTPError:
            pass
        except (OSError, ValueError) as e:
            refused = isinstance(e, PermissionError)
        with self.lock:
            was = self.checked.get((pc_id, addr, port), (False,))[0]
            self.checked[(pc_id, addr, port)] = (ok, time.monotonic())
            self.checking.discard((pc_id, addr, port))
            if len(self.checked) > 256:  # failures from forged announcements: keep the newest
                self.checked = dict(sorted(self.checked.items(), key=lambda kv: kv[1])[-128:])
            if refused:
                self.refused.add(pc_id)
            elif ok:
                self.refused.discard(pc_id)
        if ok and not bound:
            self._bind(pc_id, name)
            print(f"pc: {name} proved it holds this headset's token; it's PC {pc_id} from now on", flush=True)
        if ok != was:
            print(f"pc: {name} at {addr}:{port} {'checked out' if ok else 'did not check out'}", flush=True)
        if ok:
            self._remember(pc_id)

    def _signed(self, base, path, token, data=None, timeout=3):
        """A request signed with token straight to base (not through _Signer: it's how an address
        gets checked); its JSON answer once the answer's signature checks out."""
        header, nonce = sign(token, "GET" if data is None else "POST", path, data or b"")
        req = urllib.request.Request(base + path, data=data, headers={"Authorization": header})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body, status, mac = r.read(), r.status, r.headers.get("X-Cuff-Mac")
        out = json.loads(body)
        if not answer_ok(token, nonce, status, body, mac):
            if isinstance(out, dict) and out.get("paired") is False:
                raise PermissionError("Cuff for PC says this headset's token isn't paired there")
            raise ValueError("the answer isn't signed with this headset's token")
        return out

    # ---------------------------------------------------------------- where it is

    def _current(self):
        """(PC id, address, port, name) to use: a checked address heard in the last LOST_S, the
        newest."""
        now, best = time.monotonic(), None
        with self.lock:
            for pc_id, pc in self.pcs.items():
                for (addr, port), t in pc["addresses"].items():
                    if now - t < LOST_S and self.checked.get((pc_id, addr, port), (False,))[0]:
                        if best is None or t > best[0]:
                            best = (t, pc_id, addr, port, pc["name"])
        return best[1:] if best else None

    @property
    def name(self):
        current = self._current()
        if current:
            return current[3]
        known = self.known if isinstance(self.known, dict) else {}
        return known.get("name") or "the PC"

    def addresses(self):
        """The paired PC's checked addresses heard in the last LOST_S, newest first."""
        current, now = self._current(), time.monotonic()
        if not current:
            return []
        with self.lock:
            heard = self.pcs.get(current[0], {}).get("addresses", {}).items()
            alive = {a: t for (a, port), t in heard
                     if now - t < LOST_S and self.checked.get((current[0], a, port), (False,))[0]}
        return sorted(alive, key=alive.get, reverse=True)

    def url(self):
        """Cuff for PC's base URL while it's reachable and paired, else None."""
        if self.configured:
            return self.configured if self._configured_token() else None
        current = self._current()
        return f"http://{current[1]}:{current[2]}" if current else None

    def headers(self):
        """What a request to url() carries: a marker _Signer signs it by (never the token)."""
        if self.configured:
            return {MARKER: "*"} if self._configured_token() else {}
        current = self._current()
        return {MARKER: current[0]} if current else {}

    def state(self):
        if self.configured:
            return "connected" if self._configured_token() else "unpaired"
        if self._current():
            return "connected"
        now = time.monotonic()
        with self.lock:
            heard = [(i, pc["name"]) for i, pc in self.pcs.items() if now - pc.get("heard", 0) < LOST_S]
        if heard:
            waiting = any(self._token_for(i, n)[0] and i not in self.refused for i, n in heard)
            return "away" if waiting else "unpaired"
        return "away" if self.known else "missing"

    # ---------------------------------------------------------------- pairing

    def _post(self, base, path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.load(r)

    def _pair(self, pc_id, addr, port, name):
        base = f"http://{addr}:{port}"
        try:
            code = f"{secrets.randbelow(10000):04d}"
            req_id = secrets.token_urlsafe(16)
            w = int.from_bytes(hashlib.sha256(f"cuff pair w\n{req_id}\n{code}".encode()).digest(), "big") % Q
            x_secret = secrets.randbelow(Q - 1) + 1
            x = pow(G, x_secret, P) * pow(M, w, P) % P
            self._post(base, "/pair/request", {"id": req_id, "x": f"{x:x}", "name": socket.gethostname()})
            print(f"pc: asked {name} to pair; showing pairing code {code}", flush=True)
            text = f"Type {code} in {name}'s pairing window"
            self._say(text, alert=True)
            self._pin(text)
            deadline, tried, wrong = time.monotonic() + PAIR_WAIT_S, set(), 0
            while time.monotonic() < deadline and wrong < PAIR_TRIES:
                try:
                    with urllib.request.urlopen(f"{base}/pair/result?id={req_id}", timeout=3) as r:
                        out = json.load(r)
                    if not isinstance(out, dict):
                        raise ValueError("not an answer")
                    if "round" in out and out["round"] not in tried:
                        tried.add(out["round"])
                        token = self._open_answer(out, req_id, x_secret, w, x, pc_id)
                        if token is None:
                            wrong += 1
                            print(f"pc: {name}'s pairing answer wasn't made with this code", flush=True)
                        else:
                            self._post(base, "/pair/finish", {"id": req_id, "round": out["round"],
                                                              "mac": token[1]})
                            self._paired(pc_id, addr, port, name, token[0])
                            return
                    elif not out.get("open", True):
                        break  # pairing closed (timed out, or another headset paired)
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        break
                except (OSError, ValueError, TypeError) as e:
                    print(f"pc: pairing: {e!r}", file=sys.stderr, flush=True)
                time.sleep(2)
            print(f"pc: pairing with {name} ended before it worked", flush=True)
            self._pin(None)
            self._say(f"Pairing closed: run pair.ps1 on {name} again", alert=True)
        except urllib.error.HTTPError as e:
            print(f"pc: pairing: {name} refused: {e.code} {e.read()[:200]!r}", file=sys.stderr, flush=True)
        except (OSError, ValueError) as e:
            print(f"pc: pairing: {e!r}", file=sys.stderr, flush=True)
        finally:
            self._pin(None)
            self.pair_after = time.monotonic() + PAIR_AGAIN_S
            self.pairing = None

    @staticmethod
    def _open_answer(out, req_id, x_secret, w, x, pc_id):
        """The PC's answer -> (token, the proof for /pair/finish) if it was made with our code
        by pc_id, else None."""
        y = int(str(out.get("y", "0"))[:600], 16)
        box = bytes.fromhex(str(out.get("box", "")))
        if not 1 < y < P - 1 or pow(y, Q, P) != 1 or len(box) != 32 or out.get("pc_id") != pc_id:
            return None
        z = pow(y * pow(N, Q - w, P) % P, x_secret, P)
        key = hashlib.sha256(f"cuff pair key\n{req_id}\n{x:x}\n{y:x}\n{z:x}".encode()).digest()
        pad = hmac.new(key, b"box", hashlib.sha256).digest()
        try:
            token = bytes(a ^ b for a, b in zip(box, pad)).decode("ascii")
        except UnicodeDecodeError:
            return None
        mac = hmac.new(key, f"pc\n{pc_id}\n{token}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(str(out.get("mac", "")), mac):
            return None
        return token, hmac.new(key, b"headset", hashlib.sha256).hexdigest()

    def _paired(self, pc_id, addr, port, name, token):
        """Keep the new token, once the PC proves it paired it (a signed /health)."""
        out = self._signed(f"http://{addr}:{port}", "/health", token)
        if not (isinstance(out, dict) and out.get("id") == pc_id and out.get("paired") is True):
            raise ValueError("the PC didn't keep the pairing")
        self._bind(pc_id, name, token)
        with self.lock:
            self.checked[(pc_id, addr, port)] = (True, time.monotonic())
        self._remember(pc_id)
        print(f"pc: paired with {name} (PC {pc_id})", flush=True)
        self._pin(None)
        self._say(f"Paired with {name}", alert=True)

    def _say(self, text, alert):
        if self.notify:
            self.notify(text, alert)

    def _type_loop(self):
        """Fetch and type what the PC sends, until it stops offering it."""
        from . import steam
        seq, run, pinned, last = -1, "", False, 0.0
        print(f"pc: {self.name} has typing for this headset; listening", flush=True)
        try:
            while self.typing_on and self.typing_allowed():
                base = self.url()
                if not base:
                    break
                query = urllib.parse.urlencode({"after": seq, "run": run})
                req = urllib.request.Request(f"{base}/typing/next?{query}", headers=self.headers())
                try:
                    with urllib.request.urlopen(req, timeout=35) as r:  # _Signer checks the answer
                        got = json.load(r)
                    if not isinstance(got, dict) or not isinstance(got.get("items", []), list):
                        raise ValueError("not a typing answer")
                    seq = int(got.get("seq", seq))
                    run = str(got.get("run", run))[:32]
                except (OSError, ValueError, TypeError):
                    time.sleep(2)
                    continue
                for item in got.get("items", []):
                    try:
                        if not isinstance(item, dict):
                            continue
                        if isinstance(item.get("key"), str) and item["key"] in steam.KEYS:
                            steam.press_key(item["key"])
                        elif isinstance(item.get("text"), str) and item["text"]:
                            steam.type_text(item["text"])
                    except (OSError, ValueError, StopIteration, EOFError, ConnectionError) as e:
                        print(f"pc: typing: Steam didn't take it: {e!r}", file=sys.stderr, flush=True)
                    last = time.monotonic()
                if got.get("items") and not pinned:
                    pinned = True
                    self._pin(f"Typing from {self.name}", "typing")
                if pinned and time.monotonic() - last > TYPING_IDLE_S:
                    pinned = False
                    self._pin(None, "typing")
                if not got.get("on"):
                    break
        finally:
            if pinned:
                self._pin(None, "typing")
            self.typer = None
            print("pc: typing: stopped listening", flush=True)

    def _pin(self, text, key="pairing"):
        """Keep text on the wrist until _pin(None) (one pin per key)."""
        if self.pin:
            self.pin(text, key)


def needs_pc(state):
    """What a PC-only field says instead of a value, or None when the PC is there."""
    return {"missing": "Install Cuff for PC", "away": "Cuff for PC not connected",
            "unpaired": "Pair Cuff for PC: run pair.ps1 on the PC"}.get(state)
