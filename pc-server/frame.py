"""Setting up the Steam Frame from the PC app, over SSH: install, update or remove Cuff and its
optional parts, see what's installed, and pair the headset with this PC.

- Connecting: the headset's address and user ("steamos"). This app has its own SSH key
  (%LOCALAPPDATA%\\cuff-pc\\frame_ed25519, made on first use). The first time, the person types
  the headset's password into the app; it's used once to add the app's key to the headset's
  ~/.ssh/authorized_keys and is never stored. A key of the person's own (~/.ssh, or their SSH
  agent) works too, but is only offered to a headset whose identity this app already knows.
- The headset's identity: its SSH host key, pinned in frame.json at the first connection that
  works (with the headset's name), whatever address it's at. A host key that isn't pinned is
  refused before any password or key is offered, and the page shows its fingerprint; the
  person can then choose to trust it (a reinstalled headset, or a second one). Only the very
  first headset is trusted on first use.
- Installing: this app's copy of Cuff (the folder above pc-server) is uploaded to ~/src/cuff
  on the headset, replacing what was there, and install.sh --here runs there with the parts
  chosen. It runs on a terminal (a pty), so it can ask: its yes/no questions and password
  prompts come back to the app page as a prompt, and the answer goes back as input.
  Answers to password prompts are never logged or kept.
- Status and uninstall run the installed copy of install.sh (~/.local/share/cuff/app), or
  ~/src/cuff's on a headset installed before that copy was kept.
- Pairing over SSH: the connection already proves it's the person's headset, so instead of
  a code the app makes a token (pairing.Pairing.add) and writes it to the headset's
  ~/.config/cuff/pc-pairing.json under this PC's name, then restarts Cuff there. A headset
  that already holds a working token for this PC is left alone.
- Cuff's settings: python -m cuff.remote on the headset (cuff/remote.py) lists them with their
  choices and saves changes; Cuff takes up most at once and the rest at a restart.
- The SSH connection belongs to Frame: other modules (videos.py, linkquality.py) use run(),
  sftp() and channel(), which raise ConnectionError without a connection. Connecting and
  forgetting are serialised, and a background reconnect that a Forget overtook is dropped.

One job runs at a time. state() is what the app page shows.
"""
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import socket
import subprocess
import tarfile
import threading
import time

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

DATA = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share"), "cuff-pc")
KEY_FILE = os.path.join(DATA, "frame_ed25519")
KNOWN_HOSTS = os.path.join(DATA, "frame_known_hosts")  # before host keys were pinned: read once, to carry them over
SETTINGS = os.path.join(DATA, "frame.json")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # this app's copy of Cuff
REMOTE_SRC = "src/cuff"  # under the headset's home
APP_DIR = ".local/share/cuff"  # Cuff as installed
# The installed install.sh (install.sh keeps a copy of itself there), else the uploaded one.
INSTALLER = f'f=$HOME/{APP_DIR}/app/install.sh; [ -f "$f" ] || f=$HOME/{REMOTE_SRC}/install.sh; sh "$f" --here'
PAIRING_FILE = ".config/cuff/pc-pairing.json"  # the headset's tokens, by PC name (cuff/pc.py)
# What install.sh on the headset uses, and only that: an installed Cuff for PC's folder also
# holds its downloaded packages and speech model (cache/, gigabytes), uv.exe and its uninstaller.
UPLOAD = ("cuff", "charms", "recorder", "install.sh", "cuff.service", "requirements.txt", "LICENSE")
EXCLUDE = {".git", "__pycache__", "target", ".venv", "media", "node_modules"}
PARTS = ("recorder", "helper", "player")
KINDS = ("status", "install", "uninstall")
MAX_LINES = 400
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PROMPTS = [  # (pattern on the unfinished last line, kind)
    (re.compile(r"\[(Y/n|y/N)\]\s*$"), "yesno"),
    (re.compile(r"password[^:]*:\s*$", re.I), "password"),
    (re.compile(r"[:?]\s*$"), "text"),
]
RECONNECT_S = 60
PROMPT_IDLE_S = 0.4  # output paused this long on an unfinished line: it's waiting for an answer
CURRENT = None  # the app's Frame, for busy()


def busy():
    """Whether a headset job (an install, say) is running: updates.py won't restart the app then."""
    return bool(CURRENT and CURRENT.job_running())


def _load_settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _app_key():
    """This app's SSH key, made on first use."""
    if not os.path.exists(KEY_FILE):
        os.makedirs(DATA, exist_ok=True)
        key = ed25519.Ed25519PrivateKey.generate()
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                serialization.NoEncryption())
        with open(KEY_FILE, "wb") as f:
            f.write(pem)
    return paramiko.Ed25519Key.from_private_key_file(KEY_FILE)


def fingerprint(key):
    """A host key's fingerprint as OpenSSH shows it (ssh-keygen -lf): SHA256:<base64>."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


def _migrated_keys():
    """The host keys the app trusted before pinning (frame_known_hosts), as pinned entries."""
    if not os.path.exists(KNOWN_HOSTS):
        return {}
    try:
        hk = paramiko.HostKeys(KNOWN_HOSTS)
    except (OSError, paramiko.SSHException, ValueError):
        return {}
    out = {}
    for host in hk.keys():
        for key in hk[host].values():
            out[fingerprint(key)] = {"key": f"{key.get_name()} {key.get_base64()}", "name": None,
                                     "address": None if host.startswith("|") else host, "added": None}
    return out


class HostKeyUnknown(paramiko.SSHException):
    """The headset answered with a host key this app hasn't pinned."""

    def __init__(self, key):
        super().__init__(f"unknown host key {fingerprint(key)}")
        self.key, self.fingerprint = key, fingerprint(key)


class _Pinned(paramiko.MissingHostKeyPolicy):
    """Accepts a pinned host key; the one the person chose to trust; or, with none pinned yet,
    the first headset's. Anything else is refused before authentication starts, so no password
    or key goes to it."""

    def __init__(self, pinned, trust=None):
        self.pinned, self.trust = pinned, trust
        self.key = None
        self.known = False  # pinned before this connection
        self.new = False  # to pin once the connection works

    def missing_host_key(self, client, hostname, key):
        fp = fingerprint(key)
        if fp in self.pinned:
            self.key, self.known = key, True
        elif not self.pinned or (self.trust and self.trust == fp):
            self.key, self.new = key, True
        else:
            raise HostKeyUnknown(key)


def _try_connect(common, policy, **auth):
    client = paramiko.SSHClient()  # no known_hosts: every host key goes through the policy
    client.set_missing_host_key_policy(policy)
    try:
        client.connect(**common, **auth)
    except BaseException:
        client.close()
        raise
    return client


def _exec(client, cmd, data=None, timeout=60):
    """(exit code, stdout, stderr) of a short command without a terminal."""
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    if data is not None:
        stdin.write(data)
        stdin.channel.shutdown_write()
    out, err = stdout.read().decode("utf-8", "replace"), stderr.read().decode("utf-8", "replace")
    return stdout.channel.recv_exit_status(), out, err


_version = (0.0, None)


def version():
    """This app's copy of Cuff: its git commit, or the VERSION file a release carries (cached
    for a minute: the page asks every few seconds)."""
    global _version
    if time.monotonic() - _version[0] > 60:
        _version = (time.monotonic(), _read_version())
    return _version[1]


def _read_version():
    try:
        out = subprocess.run(["git", "-C", ROOT, "describe", "--always", "--dirty"], capture_output=True,
                             text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        with open(os.path.join(ROOT, "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def source_tar():
    """This app's copy of Cuff for the headset (UPLOAD) as a tar, with a VERSION file."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in UPLOAD:
            if not os.path.exists(os.path.join(ROOT, name)):
                continue
            tar.add(os.path.join(ROOT, name), arcname=name,
                    filter=lambda ti: None if set(ti.name.split("/")) & EXCLUDE else ti)
        data = version().encode() + b"\n"
        info = tarfile.TarInfo("VERSION")
        info.size, info.mtime, info.mode = len(data), int(time.time()), 0o644
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def parse_status(lines):
    """install.sh --status's "  Name: yes (...)" lines as {name: text}: only those under its last
    "What's installed" heading, since an install's notes before it can look the same. None
    without the heading (the installer stopped before it, or isn't the one expected)."""
    start = max((i for i, line in enumerate(lines) if "What's installed" in line), default=None)
    if start is None:
        return None
    out = {}
    for line in lines[start + 1:]:
        m = re.match(r"\s+([A-Z][\w ()']+?): (.+)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


class Frame:
    def __init__(self, pairing):
        global CURRENT
        self.pairing = pairing
        self.lock = threading.Lock()  # the job
        self._client_lock = threading.Lock()  # self.client, host, user and _gen: held briefly
        self._connect_lock = threading.Lock()  # one connection attempt at a time
        self._save_lock = threading.Lock()
        saved = _load_settings()
        self.host, self.user = saved.get("host", ""), saved.get("user", "steamos")
        self.host_keys = saved["host_keys"] if isinstance(saved.get("host_keys"), dict) else _migrated_keys()
        self.client = None
        self._gen = 0  # bumped by each Connect and Forget: an attempt from before them is dropped
        self.message = None  # the last connection problem, for the page
        self.untrusted = None  # {"host", "fingerprint"}: the last refused host key, until trusted
        self.job = None  # {"id", "kind", "running", "lines", "partial", "prompt", "exit", "chan"}
        self.status = saved.get("status") or {}
        self.jobs = 0
        if "host_keys" not in saved and self.host_keys:
            self._save()
        CURRENT = self
        threading.Thread(target=self._keep_connected, daemon=True, name="frame-connect").start()

    def _keep_connected(self):
        """Reconnect with the key in the background, so automatic downloads and the pages work
        whenever the headset's about."""
        while True:
            try:
                with self._client_lock:
                    host, user, gen = self.host, self.user, self._gen
                if host and not self.connected() and not self.job_running():
                    ok, message = self._connect(host, user, None, None, gen, quiet=True)
                    with self._client_lock:
                        if gen == self._gen or ok:
                            self.message = None if ok else message
            except Exception as e:  # noqa: BLE001 - the reconnecting must go on
                print(f"frame: reconnecting: {e!r}", flush=True)
            time.sleep(RECONNECT_S)

    # ---------------------------------------------------------------- connecting

    def _save(self):
        with self._client_lock:
            data = {"host": self.host, "user": self.user, "status": self.status, "host_keys": self.host_keys}
        with self._save_lock:
            try:
                os.makedirs(DATA, exist_ok=True)
                with open(SETTINGS + ".new", "w") as f:
                    json.dump(data, f, indent=1)
                os.replace(SETTINGS + ".new", SETTINGS)
            except OSError:
                pass

    def connected(self):
        with self._client_lock:
            client = self.client
        t = client.get_transport() if client else None
        return bool(t and t.is_active())

    def job_running(self):
        with self.lock:
            return bool(self.job and self.job["running"])

    def connect(self, host, user="steamos", password=None, quiet=False, trust=None):
        """(ok, message). Tries this app's key, then (for a headset already known) the person's
        own, then the password (which, if it works, adds this app's key to the headset).
        trust: a host key fingerprint the person chose to trust (the page's "Trust" button)."""
        host, user = (host or "").strip(), (user or "steamos").strip()
        if not host:
            return False, "Type the headset's address."
        with self._client_lock:
            self._gen += 1
            gen = self._gen
        return self._connect(host, user, password, trust, gen, quiet)

    def _connect(self, host, user, password, trust, gen, quiet):
        if self.job_running():
            return False, "Wait for the current job to finish."
        with self._connect_lock:
            with self._client_lock:
                if gen != self._gen:
                    return False, "Disconnected."
                pinned = set(self.host_keys)
            key = _app_key()
            policy = _Pinned(pinned, (trust or "").strip() or None)
            common = {"hostname": host, "username": user, "timeout": 8, "banner_timeout": 8, "auth_timeout": 8}
            used_password = False
            try:
                try:
                    client = _try_connect(common, policy, pkey=key, allow_agent=False, look_for_keys=False)
                except paramiko.AuthenticationException:
                    client = None
                    if policy.known:  # a headset this app knows: the person's own keys are safe to offer
                        try:
                            client = _try_connect(common, policy, pkey=key, allow_agent=True, look_for_keys=True)
                        except paramiko.AuthenticationException:
                            pass
                    if client is None:
                        if not password:
                            return False, ("The headset wants its password the first time "
                                           "(after that, this app uses its own key).")
                        client = _try_connect(common, policy, password=password, pkey=None, allow_agent=False,
                                              look_for_keys=False)
                        used_password = True
            except HostKeyUnknown as e:
                with self._client_lock:
                    if gen == self._gen:
                        self.untrusted = {"host": host, "fingerprint": e.fingerprint}
                return False, (f"The headset at {host} has an SSH identity this app doesn't know: {e.fingerprint}. "
                               "If you reinstalled the headset, or this is another headset of yours, check that "
                               "fingerprint on it (ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub) and choose "
                               "Trust this headset. Otherwise don't: something else on the network may be "
                               "answering for it.")
            except paramiko.AuthenticationException:
                return False, "That password didn't work."
            except (socket.gaierror, socket.timeout, TimeoutError):
                return False, f"Couldn't find {host}. Check the address, and that the headset is on the same network."
            except (paramiko.SSHException, EOFError, OSError) as e:
                if "refused" in str(e).lower():
                    return False, "The headset isn't accepting SSH yet: see “Before the first install”."
                return False, f"Couldn't connect: {e}"
            with self._client_lock:
                stale = gen != self._gen  # a Forget (or another Connect) came first
                if not stale:
                    old, self.client, self.host, self.user = self.client, client, host, user
                    self.untrusted, self.message = None, None
            if stale:
                client.close()
                return False, "Disconnected."
            if old:
                old.close()
            if policy.new:  # pinned first: it's what keeps the next connection safe
                try:
                    name = _exec(client, "hostname", timeout=10)[1].strip() or None
                except (paramiko.SSHException, EOFError, OSError):
                    name = None
                self._pin(policy.key, host, name)
            if used_password:
                try:
                    self._authorise_key(client, key)
                except (paramiko.SSHException, EOFError, OSError) as e:
                    print(f"frame: couldn't add this app's key to the headset: {e!r}", flush=True)
            self._save()
            if not quiet:
                print(f"frame: connected to {user}@{host}", flush=True)
            return True, "Connected."

    def _pin(self, key, host, name):
        fp = fingerprint(key)
        with self._client_lock:
            self.host_keys[fp] = {"key": f"{key.get_name()} {key.get_base64()}", "name": name, "address": host,
                                  "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
        print(f"frame: trusting {name or host}'s host key {fp}", flush=True)

    @staticmethod
    def _authorise_key(client, key):
        line = f"{key.get_name()} {key.get_base64()} cuff-pc@{socket.gethostname()}"
        cmd = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
               f"chmod 600 ~/.ssh/authorized_keys && (grep -qF {shlex.quote(key.get_base64())} ~/.ssh/authorized_keys "
               f"|| echo {shlex.quote(line)} >> ~/.ssh/authorized_keys)")
        _exec(client, cmd)

    def forget(self):
        """Disconnect, and stop reconnecting (a reconnect under way is dropped when it finishes)."""
        with self._client_lock:
            self._gen += 1
            client, self.client, self.host = self.client, None, ""
            self.untrusted, self.message = None, None
        if client:
            client.close()
        self._save()

    def disconnect(self):
        with self._client_lock:
            client, self.client = self.client, None
        if client:
            client.close()

    # ---------------------------------------------------------------- using the connection

    def _client(self):
        with self._client_lock:
            client = self.client
        t = client.get_transport() if client else None
        if not (t and t.is_active()):
            raise ConnectionError("Not connected to the headset.")
        return client

    def run(self, cmd, data=None, timeout=60):
        """(exit code, stdout, stderr) of a short command on the headset, without a terminal.
        ConnectionError when not connected, or when the connection drops."""
        client = self._client()
        try:
            return _exec(client, cmd, data, timeout)
        except (paramiko.SSHException, EOFError) as e:
            raise ConnectionError(f"The connection to the headset dropped ({e}).") from e

    def channel(self):
        """A new session channel on the headset (ConnectionError when not connected)."""
        client = self._client()
        try:
            return client.get_transport().open_session()
        except (paramiko.SSHException, EOFError, AttributeError) as e:
            raise ConnectionError(f"The connection to the headset dropped ({e}).") from e

    @contextlib.contextmanager
    def sftp(self):
        """An SFTP session on the headset, closed afterwards. ConnectionError when not
        connected, or when the connection drops while it's in use."""
        client = self._client()
        try:
            sftp = client.open_sftp()
        except (paramiko.SSHException, EOFError) as e:
            raise ConnectionError(f"The connection to the headset dropped ({e}).") from e
        try:
            yield sftp
        except (paramiko.SSHException, EOFError) as e:
            raise ConnectionError(f"The connection to the headset dropped ({e}).") from e
        finally:
            sftp.close()

    # ---------------------------------------------------------------- jobs

    def start(self, kind, parts=(), pair=False):
        """kind: status | install | uninstall. (ok, message)."""
        if kind not in KINDS:
            return False, "Unknown job."
        if not self.connected():
            return False, "Connect to the headset first."
        with self.lock:
            if self.job and self.job["running"]:
                return False, "A job is already running."
            self.jobs += 1
            self.job = job = {"id": self.jobs, "kind": kind, "running": True, "lines": [], "partial": "",
                              "prompt": None, "exit": None, "chan": None, "changed": time.monotonic()}
        threading.Thread(target=self._job, args=(job, kind, tuple(parts), pair), daemon=True,
                         name="frame-job").start()
        return True, "Started."

    def _say(self, job, text):
        with self.lock:
            job["lines"].append(text)
            del job["lines"][:-MAX_LINES]

    def _job(self, job, kind, parts, pair):
        code = None
        try:
            if kind == "status":
                code = self._shell(job, f"{INSTALLER} --status")
            elif kind == "uninstall":
                code = self._shell(job, f"{INSTALLER} --uninstall")
            elif kind == "install":
                self._say(job, f"Uploading Cuff {version()} to ~/{REMOTE_SRC} on the headset...")
                tmp = f"{REMOTE_SRC}.new"
                rc, _, err = self.run(f"rm -rf ~/{tmp} && mkdir -p ~/{tmp} && tar -C ~/{tmp} -xf - && "
                                      f"rm -rf ~/{REMOTE_SRC} && mv ~/{tmp} ~/{REMOTE_SRC}", data=source_tar(), timeout=120)
                if rc:
                    self._say(job, f"Upload failed: {err.strip()}")
                    code = rc
                else:
                    opts = [f"--{p}" for p in parts if p in PARTS] or ["--only-cuff"]
                    code = self._shell(job, f"CUFF_FROM_PC_APP=1 sh ~/{REMOTE_SRC}/install.sh --here {' '.join(opts)}")
                    if code == 0 and pair:
                        self._pair(job)
            # What's installed changes only with a job that finished: a failed or cancelled one
            # keeps what was known (the headset's version, say).
            if code == 0:
                status = {} if kind == "uninstall" else parse_status(job["lines"])
                if status is not None:
                    self.status = status
                    self._save()
        except Exception as e:  # noqa: BLE001 - report it on the page, whatever it is
            self._say(job, f"Stopped: {e!r}" if not isinstance(e, ConnectionError) else f"Stopped: {e}")
            code = code if code is not None else -1
        with self.lock:
            job["running"], job["exit"], job["prompt"], job["chan"] = False, code, None, None
        print(f"frame: {kind} finished ({code})", flush=True)

    def _shell(self, job, cmd):
        """Run cmd on a terminal, collecting its output and noticing when it asks something."""
        chan = self.channel()
        chan.get_pty(term="dumb", width=100, height=40)
        chan.exec_command(cmd)
        with self.lock:
            job["chan"] = chan
        last = time.monotonic()
        while True:
            got = False
            while chan.recv_ready():
                self._feed(job, chan.recv(65536).decode("utf-8", "replace"))
                last, got = time.monotonic(), True
            if chan.exit_status_ready() and not chan.recv_ready():
                break
            if chan.closed:  # cancelled, or the connection dropped
                break
            if not got:
                if job["partial"] and time.monotonic() - last > PROMPT_IDLE_S and job["prompt"] is None:
                    self._prompt(job)
                time.sleep(0.05)
        if job["partial"]:
            self._say(job, job["partial"])
            job["partial"] = ""
        return chan.recv_exit_status() if chan.exit_status_ready() else -1

    def _feed(self, job, text):
        text = ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
        with self.lock:
            job["prompt"] = None
            parts = (job["partial"] + text).split("\n")
            job["partial"] = parts.pop()
            job["lines"].extend(parts)
            del job["lines"][:-MAX_LINES]

    def _prompt(self, job):
        line = job["partial"]
        for pattern, kind in PROMPTS:
            if pattern.search(line):
                with self.lock:
                    job["prompt"] = {"kind": kind, "text": line.strip()}
                return

    def answer(self, text):
        """Input for the running job's question. A password isn't logged or kept."""
        with self.lock:
            job = self.job
            if not job or not job["running"] or not job["chan"]:
                return False, "Nothing is waiting for an answer."
            job["prompt"] = None  # the terminal echoes the answer (not a password) onto the line
            chan = job["chan"]
        chan.send((str(text) + "\n").encode())
        return True, "Sent."

    def cancel(self):
        with self.lock:
            chan = self.job["chan"] if self.job else None
        if chan:
            chan.close()
            return True, "Stopped."
        return False, "Nothing is running."

    # ---------------------------------------------------------------- pairing

    def _pair(self, job):
        pc = socket.gethostname()
        path = PAIRING_FILE
        rc, out, _ = self.run(f"cat ~/{path} 2>/dev/null || true")
        try:
            tokens = json.loads(out) if out.strip() else {}
        except ValueError:
            tokens = {}
        # The headset binds a PC's name to the PC's id ("@ids", cuff/pc.py) and then refuses that
        # name to any other id. A reinstalled Cuff for PC has a new id, so the binding is written
        # here too: this PC's id for this name, and no other id for it.
        ids = tokens.get("@ids") if isinstance(tokens.get("@ids"), dict) else {}
        bound = {i: n for i, n in ids.items() if n != pc}
        bound[self.pairing.pc_id] = pc
        # (With the headset's address: authorised() records where it was last seen.)
        if tokens.get(pc) and self.pairing.authorised(f"Bearer {tokens[pc]}", self.host or None):
            if ids == bound:
                self._say(job, f"Pairing: already paired with {pc}.")
                return
            done = f"Pairing: {pc}'s pairing now names this copy of Cuff for PC; Cuff restarted to use it."
        else:
            rc, name, _ = self.run("hostname")
            tokens[pc] = self.pairing.add(name.strip() or "headset")
            done = f"Pairing: paired with {pc}; Cuff restarted to use it."
        tokens["@ids"] = bound
        rc, _, err = self.run(f"mkdir -p ~/.config/cuff && umask 077 && cat > ~/{path}.new && mv ~/{path}.new ~/{path} "
                              "&& systemctl --user restart cuff", data=json.dumps(tokens).encode())
        self._say(job, done if rc == 0 else f"Pairing failed: {err.strip()}")

    # ---------------------------------------------------------------- Cuff's settings

    def remote(self, args, data=None, timeout=30):
        """python -m cuff.remote <args> on the headset, in Cuff's own environment; its JSON."""
        cmd = ("env $(systemctl --user show cuff -p Environment --value) "
               f"PYTHONPATH=$HOME/{APP_DIR}/app $HOME/{APP_DIR}/venv/bin/python -m cuff.remote {args}")
        rc, out, err = self.run(cmd, data=None if data is None else json.dumps(data).encode(), timeout=timeout)
        if rc:
            raise RuntimeError(err.strip().splitlines()[-1] if err.strip() else f"cuff.remote failed ({rc})")
        return json.loads(out)

    def settings(self):
        return self.remote("dump")

    def apply_settings(self, changes):
        return self.remote("apply", data=changes)

    def restart_cuff(self):
        rc, _, err = self.run("systemctl --user restart cuff")
        return (True, "Cuff restarted.") if rc == 0 else (False, f"Couldn't restart Cuff: {err.strip()}")

    # ---------------------------------------------------------------- the page

    def state(self):
        with self.lock:
            job = self.job
            j = None if job is None else {
                "id": job["id"], "kind": job["kind"], "running": job["running"], "exit": job["exit"],
                "prompt": job["prompt"], "lines": job["lines"][-MAX_LINES:] + ([job["partial"]] if job["partial"] else [])}
        with self._client_lock:
            host, user, message, untrusted = self.host, self.user, self.message, self.untrusted
        on_headset = self.status.get("Version")
        return {"host": host, "user": user, "connected": self.connected(), "version": version(),
                "headset_version": on_headset if on_headset and on_headset != "unknown" else None,
                "behind": bool(self.status.get("Cuff", "").startswith("yes") and on_headset != version()),
                "message": message, "untrusted": untrusted,
                "status": self.status, "job": j, "suggest": self._suggestions()}

    def _suggestions(self):
        seen = [h["address"] for h in self.pairing.headsets() if h.get("address")]
        return [a for a in dict.fromkeys(seen + ([self.host] if self.host else []))]
