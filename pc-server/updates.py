"""Updates for an installed Cuff for PC (pc-server/installer): the GitHub repository's latest
release, and installing it.

- Only an installed copy updates itself: one with a VERSION file and no .git (a git checkout
  updates with git).
- Checking: once a day, and when the Overview page asks. The release's tag against VERSION.
  A release carries the installer as CuffForPC-Setup-<tag>.exe and its SHA-256 as
  CuffForPC-Setup-<tag>.exe.sha256 (build.ps1 writes both). A release without both, named for
  its own tag, isn't offered.
- Installing: the installer is downloaded (with a timeout) into a folder of its own in %TEMP%,
  as .part until it's whole and matches both the size GitHub gives and the published SHA-256,
  then run silently (/VERYSILENT). It stops this app, replaces its files and starts it again.
  The person clicks Update on the page; nothing installs by itself, and not while a headset
  job (an install, say) is running.
- Starting the installer: this app runs inside its logon task's job object, and the installer
  stops that task, so it's started outside the job (CREATE_BREAKAWAY_FROM_JOB), or, where the
  job doesn't allow that, by a one-shot scheduled task (TASK). If this app is still running
  RESTART_WAIT_S later, the update is reported as not done.
"""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

REPO = "Brobert-in-aus/cuff"
# CUFF_PC_RELEASES points the check at another "latest release" JSON, in GitHub's shape: a
# test release served locally, to try an update end to end before publishing one.
API = os.environ.get("CUFF_PC_RELEASES") or f"https://api.github.com/repos/{REPO}/releases/latest"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK_EVERY_S = 24 * 3600
TIMEOUT_S = 30  # for each read of the download: a stalled one gives up
RESTART_WAIT_S = 5 * 60  # the installer has replaced this app by then
TASK = "Cuff for PC update"  # the one-shot task that starts the installer outside this app's job
TEMP_PREFIX = "cuff-update-"
HEADERS = {"User-Agent": "cuff-pc"}
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class UpdateError(Exception):
    pass


def asset_name(tag):
    return f"CuffForPC-Setup-{tag}.exe"


def installed_version():
    """The VERSION of an installed copy, or None for a git checkout."""
    if os.path.isdir(os.path.join(ROOT, ".git")):
        return None
    try:
        with open(os.path.join(ROOT, "VERSION")) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _key(version):
    """A version as a tuple to compare: v1.2.3 as (1, 2, 3, 0, 0, 0); a release candidate,
    v1.2.3-rc4, as (1, 2, 3, -1, 4, 0), before v1.2.3; a build after either (git describe's
    v1.2.3-5-gabc) adds the 5 at the end. () for anything else (a bare commit, from a build
    before the first release tag), which never counts as older."""
    m = re.match(r"^v?(\d+(?:\.\d+)+)(?:-rc(\d+))?(?:-(\d+)-g[0-9a-f]+)?(?=-|$)", version or "")
    if not m:
        return ()
    nums = [int(n) for n in m.group(1).split(".")][:3]
    nums += [0] * (3 - len(nums))
    rc, after = m.group(2), m.group(3)
    return (*nums, -1 if rc else 0, int(rc or 0), int(after or 0))


def _frame_busy():
    try:
        import frame
    except ImportError:
        return False
    return frame.busy()


def _checksum(url):
    """The SHA-256 a .sha256 asset gives (sha256sum's "<hex>  <name>", or the bare hex)."""
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=TIMEOUT_S) as r:
        text = r.read(4096).decode("ascii", "replace")
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if not m:
        raise UpdateError("the release's checksum file has no SHA-256 in it")
    return m.group(1).lower()


def _download(url, path, size):
    """Download url to path, checking its size as it comes; its SHA-256."""
    digest, got = hashlib.sha256(), 0
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=TIMEOUT_S) as r, \
            open(path, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            got += len(chunk)
            if got > size:
                raise UpdateError("the download came out bigger than the release says")
            digest.update(chunk)
            f.write(chunk)
    if got != size:
        raise UpdateError("the download came out the wrong size")
    return digest.hexdigest()


def _launch(args):
    """Start the installer outside this app's job object; how it was started."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(args, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, close_fds=True,
                         cwd=os.path.dirname(args[0]), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return "outside the logon task"
    except OSError as e:
        if getattr(e, "winerror", None) != 5:  # ERROR_ACCESS_DENIED: the job forbids breaking away
            raise
    # Task Scheduler starts it, so it's in no job of this app's. (Paths go in through the
    # environment, so no quoting can break.)
    script = ("$a = New-ScheduledTaskAction -Execute $env:CUFF_SETUP -Argument $env:CUFF_SETUP_ARGS; "
              "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
              "-ExecutionTimeLimit (New-TimeSpan -Hours 1); "
              "Register-ScheduledTask -TaskName $env:CUFF_TASK -Action $a -Settings $s -Force | Out-Null; "
              "Start-ScheduledTask -TaskName $env:CUFF_TASK")
    env = {**os.environ, "CUFF_SETUP": args[0], "CUFF_SETUP_ARGS": " ".join(args[1:]), "CUFF_TASK": TASK}
    out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                         capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
    if out.returncode != 0:
        raise UpdateError(f"couldn't start the installer ({(out.stderr or out.stdout).strip()[:200]})")
    return f"through the {TASK!r} task"


def _clean_temp():
    """Installers from earlier updates (each in its own folder in %TEMP%), a day on."""
    for folder in glob.glob(os.path.join(tempfile.gettempdir(), TEMP_PREFIX + "*")):
        try:
            if time.time() - os.path.getmtime(folder) > 24 * 3600:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass


class Updates:
    def __init__(self, busy=None):
        """busy: whether something must not be cut short by the app restarting (by default,
        a headset job: frame.busy)."""
        self.lock = threading.Lock()
        self.busy = busy or _frame_busy
        self.latest = None  # {"version", "url", "size", "sha_url", "notes", "page", "problem"} of the newest release
        self.checked_at = 0.0
        self.error = None
        self.installing = None  # a message while downloading or starting the installer
        threading.Thread(target=self._daily, daemon=True, name="updates").start()

    def _daily(self):
        _clean_temp()
        while True:
            if installed_version():
                self.check()
            time.sleep(CHECK_EVERY_S)

    def check(self):
        req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json", **HEADERS})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                rel = json.load(r)
            tag = str(rel.get("tag_name") or "")
            assets = {a.get("name"): a for a in rel.get("assets", []) if isinstance(a, dict)}
            # Only an installer named for the release's own tag counts, and only with its checksum.
            exe, sha = assets.get(asset_name(tag)), assets.get(asset_name(tag) + ".sha256")
            problem = (None if exe and sha else
                       f"The newest release ({tag}) has no installer named for it." if not exe else
                       f"The newest release ({tag}) has no checksum, so it isn't offered.")
            latest = {"version": tag, "page": rel.get("html_url"), "notes": (rel.get("body") or "")[:2000],
                      "url": exe["browser_download_url"] if exe and sha else None,
                      "size": exe["size"] if exe else None,
                      "sha_url": sha["browser_download_url"] if sha else None, "problem": problem}
            with self.lock:
                self.latest, self.error = latest, None
        except urllib.error.HTTPError as e:
            with self.lock:
                self.latest, self.error = None, None if e.code == 404 else f"GitHub said {e.code}"  # 404: no release yet
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            with self.lock:
                self.error = f"Couldn't check: {e}"
        self.checked_at = time.time()
        return self.state()

    @staticmethod
    def _newer(mine, latest):
        """Whether latest is a release to offer over mine (and whether it's newer at all)."""
        later = bool(mine and latest and _key(mine) and _key(latest["version"]) > _key(mine))
        return later and bool(latest["url"]), later

    def state(self):
        mine = installed_version()
        with self.lock:
            latest = dict(self.latest) if self.latest else None
            error, installing = self.error, self.installing
        newer, later = self._newer(mine, latest)
        if later and not newer and not error:
            error = latest["problem"]
        return {"installed": mine, "git": mine is None, "latest": latest, "newer": newer,
                "checked_at": self.checked_at, "error": error, "installing": installing}

    def install(self):
        """(ok, message): download the newer installer and run it, in the background."""
        if self.busy():
            return False, "Wait for the headset job to finish: updating restarts Cuff for PC."
        mine = installed_version()
        with self.lock:
            if self.installing:
                return False, self.installing
            latest = dict(self.latest) if self.latest else None
            if not self._newer(mine, latest)[0]:
                return False, "There's no newer version to install."
            self.installing, self.error = f"Downloading Cuff for PC {latest['version']}…", None
        threading.Thread(target=self._install, args=(latest,), daemon=True, name="update").start()
        return True, "Downloading the update…"

    def _install(self, latest):
        path = None
        try:
            folder = tempfile.mkdtemp(prefix=TEMP_PREFIX)
            path = os.path.join(folder, asset_name(latest["version"]))
            expected = _checksum(latest["sha_url"])
            if _download(latest["url"], path + ".part", latest["size"]) != expected:
                raise UpdateError("the download doesn't match the release's checksum")
            os.replace(path + ".part", path)
            if self.busy():
                raise UpdateError("a headset job started; update when it's done")
            with self.lock:
                self.installing = "Installing; Cuff for PC restarts in a moment…"
            how = _launch([path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
            print(f"updates: installing {latest['version']} ({how})", flush=True)
            timer = threading.Timer(RESTART_WAIT_S, self._not_replaced, args=(path,))
            timer.daemon = True
            timer.start()
        except (OSError, ValueError, UpdateError, subprocess.SubprocessError) as e:
            print(f"updates: {latest['version']} didn't install: {e}", flush=True)
            if path:
                try:
                    os.remove(path + ".part")
                except OSError:
                    pass
            with self.lock:
                self.installing, self.error = None, f"The update didn't install: {e}"

    def _not_replaced(self, path):
        """Still running long after the installer started: it didn't replace this copy."""
        with self.lock:
            self.installing = None
            self.error = f"The installer started but didn't replace Cuff for PC. You can run it yourself: {path}"
        print("updates: still running after the installer started; it didn't finish", flush=True)
