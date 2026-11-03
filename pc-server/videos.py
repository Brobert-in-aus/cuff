"""The headset's clips and recordings, from the PC app: list them with a thumbnail and length,
download them, delete them on the headset, and (if turned on) download new ones by themselves.

- Listing: python -m cuff.remote videos on the headset; each video's length and a thumbnail
  come from the headset's ffprobe and ffmpeg, once each, cached on the PC
  (%LOCALAPPDATA%\\cuff-pc\\thumbs).
- Downloading: over SFTP into the chosen folder (default: Videos\\Cuff, clips in clips\\), as
  name.part until it's whole. One at a time, with progress. A video already there at the same
  size counts as downloaded.
- Automatic downloads: every AUTO_EVERY_S while connected, new videos (clips, recordings or
  both) that haven't changed for SETTLED_S (so not one still being recorded) are downloaded.
  "Then delete them from the headset" removes each only after its download is checked.
- Deleting: only files directly in ~/Videos/framecorder or its clips/ folder.

Settings in %LOCALAPPDATA%\\cuff-pc\\videos.json.
"""
import json
import os
import posixpath
import re
import threading
import time

import paramiko

from frame import DATA

SETTINGS = os.path.join(DATA, "videos.json")
THUMBS = os.path.join(DATA, "thumbs")
REMOTE = "Videos/framecorder"  # under the headset's home, as cuff.remote reports
SAFE = re.compile(r"^(clips/)?[\w.-]+\.mp4$")
AUTO_EVERY_S = 60
SETTLED_S = 20
DEFAULTS = {"folder": os.path.join(os.path.expanduser("~"), "Videos", "Cuff"), "auto": False,
            "auto_clips": True, "auto_recordings": True, "delete_after": False}


class Videos:
    def __init__(self, frame):
        self.frame = frame
        self.lock = threading.Lock()
        self.settings = dict(DEFAULTS)
        try:
            with open(SETTINGS) as f:
                self.settings.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass
        self.listing = []  # the last list from the headset
        self.listed_at = 0.0
        self.meta = {}  # remote path -> {"seconds", "thumb"}
        self.queue = []  # remote paths to download, in order
        self.current = None  # {"path", "done", "size"} while downloading
        self.errors = []
        self.auto_paths = set()  # queued by the automatic download (only those are deleted after)
        self.wake = threading.Event()
        threading.Thread(target=self._worker, daemon=True, name="videos").start()

    def save_settings(self, changes):
        with self.lock:
            for k, v in changes.items():
                if k in DEFAULTS:
                    self.settings[k] = str(v) if k == "folder" else bool(v)
            os.makedirs(DATA, exist_ok=True)
            with open(SETTINGS, "w") as f:
                json.dump(self.settings, f, indent=1)
        self.wake.set()

    # ---------------------------------------------------------------- the headset's videos

    def refresh(self):
        listing = self.frame.remote("videos")["videos"]
        with self.lock:
            self.listing, self.listed_at = listing, time.time()
        self._fill_meta(listing[:24])
        return listing

    def _fill_meta(self, listing):
        """Lengths for the videos without one (one ffprobe for all), on the headset."""
        need = [v["path"] for v in listing if v["path"] not in self.meta and SAFE.match(v["path"])
                and time.time() - v["mtime"] > SETTLED_S]
        if not need:
            return
        files = " ".join(f"~/{REMOTE}/{p}" for p in need)
        cmd = ("for f in " + files + "; do ffprobe -v error -show_entries format=duration -of csv=p=0 \"$f\" "
               "2>/dev/null || echo; done")
        _, out, _ = self.frame.run(cmd, timeout=60)
        for path, line in zip(need, out.splitlines()):
            try:
                self.meta[path] = {"seconds": round(float(line))}
            except ValueError:
                self.meta[path] = {"seconds": None}

    def thumbnail(self, path):
        """A JPEG of the video's first second, made on the headset once and kept here."""
        if not SAFE.match(path):
            raise ValueError("not a video")
        os.makedirs(THUMBS, exist_ok=True)
        local = os.path.join(THUMBS, path.replace("/", "_") + ".jpg")
        if not os.path.exists(local):
            data = b""
            for at in (1, 0):  # a second in; the first frame of a shorter clip
                chan = self.frame.channel()
                chan.exec_command(f"ffmpeg -v error -ss {at} -i ~/{REMOTE}/{path} -frames:v 1 -vf scale=320:-2 "
                                  "-q:v 5 -f image2 -")
                while True:
                    chunk = chan.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                if chan.recv_exit_status() == 0 and data:
                    break
                data = b""
            if not data:
                raise RuntimeError("no thumbnail")
            with open(local, "wb") as f:
                f.write(data)
        with open(local, "rb") as f:
            return f.read()

    def local_path(self, path):
        return os.path.join(self.settings["folder"], *path.split("/"))

    def downloaded(self, video):
        try:
            return os.path.getsize(self.local_path(video["path"])) == video["size"]
        except OSError:
            return False

    def delete(self, path):
        if not SAFE.match(path):
            return False, "Not a video."
        try:
            with self.frame.sftp() as sftp:
                sftp.remove(posixpath.join(sftp.normalize("."), REMOTE, path))
        except OSError as e:  # ConnectionError included
            return False, f"Couldn't delete it: {e}"
        with self.lock:
            self.listing = [v for v in self.listing if v["path"] != path]
        return True, "Deleted from the headset."

    def download(self, paths):
        with self.lock:
            for p in paths:
                if SAFE.match(p) and p not in self.queue and (not self.current or self.current["path"] != p):
                    self.queue.append(p)
        self.wake.set()
        return True, "Queued."

    # ---------------------------------------------------------------- the worker

    def _worker(self):
        last_auto = 0.0
        while True:
            self.wake.wait(timeout=5)
            self.wake.clear()
            try:
                if self.settings["auto"] and self.frame.connected() and time.time() - last_auto > AUTO_EVERY_S:
                    last_auto = time.time()
                    self._auto()
                while self.frame.connected():
                    with self.lock:
                        if not self.queue:
                            break
                        path = self.queue.pop(0)
                    self._fetch(path)
            except Exception as e:  # noqa: BLE001 - keep going; the page shows it
                self._error(f"{e}")

    def _auto(self):
        s = self.settings
        for v in self.refresh():
            wanted = s["auto_clips"] if v["kind"] == "clip" else s["auto_recordings"]
            if wanted and time.time() - v["mtime"] > SETTLED_S and not self.downloaded(v):
                self.auto_paths.add(v["path"])
                self.download([v["path"]])

    def _fetch(self, path):
        video = next((v for v in self.listing if v["path"] == path), None)
        local = self.local_path(path)
        retry = False
        try:
            os.makedirs(os.path.dirname(local), exist_ok=True)
            with self.frame.sftp() as sftp:
                remote = posixpath.join(sftp.normalize("."), REMOTE, path)
                size = sftp.stat(remote).st_size
                with self.lock:
                    self.current = {"path": path, "done": 0, "size": size}

                def progress(done, total):
                    self.current["done"] = done

                sftp.get(remote, local + ".part", callback=progress)
                if os.path.getsize(local + ".part") != size:
                    raise OSError("the download came out the wrong size")
                os.replace(local + ".part", local)
                if video:
                    os.utime(local, (video["mtime"], video["mtime"]))
                print(f"videos: downloaded {path}", flush=True)
                if path in self.auto_paths and self.settings["delete_after"]:
                    sftp.remove(remote)
                    print(f"videos: deleted {path} from the headset after downloading it", flush=True)
                    with self.lock:
                        self.listing = [v for v in self.listing if v["path"] != path]
        except (OSError, paramiko.SSHException, EOFError) as e:  # a lost connection is a ConnectionError (OSError)
            self._error(f"{path}: {e}")
            try:
                os.remove(local + ".part")
            except OSError:
                pass
            # The connection went: try it again once it's back, rather than losing it.
            retry = isinstance(e, ConnectionError) and not self.frame.connected()
        finally:
            if not retry:
                self.auto_paths.discard(path)
            with self.lock:
                self.current = None
                if retry and path not in self.queue:
                    self.queue.insert(0, path)

    def _error(self, text):
        print(f"videos: {text}", flush=True)
        with self.lock:
            self.errors = (self.errors + [f"{time.strftime('%H:%M')} {text}"])[-5:]

    def state(self):
        with self.lock:
            listing = [{**v, **self.meta.get(v["path"], {}), "downloaded": self.downloaded(v)} for v in self.listing]
            return {"settings": self.settings, "videos": listing, "listed_at": self.listed_at,
                    "queue": list(self.queue), "current": dict(self.current) if self.current else None,
                    "errors": list(self.errors)}
