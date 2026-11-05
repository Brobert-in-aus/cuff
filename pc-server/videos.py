"""The headset's clips, recordings and screenshots, from the PC app: list them with a
thumbnail (and a video's length), download them, delete them on the headset, and (if turned on)
download new ones by themselves.

- Listing: python -m cuff.remote videos on the headset; each video's length and a thumbnail
  come from the headset's ffprobe and ffmpeg, once each, cached on the PC
  (%LOCALAPPDATA%\\cuff-pc\\thumbs).
- Screenshots: Cuff's screenshot button saves a pair, <time>-vr.png (both eyes) and
  <time>-preview.png (one). cuff.remote videos lists them too, since v1.1.0; a headset with
  v1.0.0 (no "screenshots" in its answer) has them listed over SFTP from the default folder,
  ~/Pictures/Frame screenshots. A thumbnail is the preview, made smaller here (Pillow) and
  cached with the videos' ones.
- Downloading: over SFTP into the chosen folder (default: Videos\\Cuff, clips in clips\\,
  screenshots in screenshots\\), as name.part until it's whole. One file at a time, with
  progress; a screenshot is its two files. A file already there at the same size counts as
  downloaded.
- Automatic downloads: every AUTO_EVERY_S while connected, new videos (clips, recordings or
  both) and screenshots that haven't changed for SETTLED_S (so not one still being recorded)
  are downloaded. "Then delete them from the headset" removes each file only after its
  download is checked.
- Nudges: a headset on v1.2.0 or later sends POST /recordings/changed when it saves a
  screenshot, clip or recording (server.py). The list is read again at once (and fetched, if
  downloads are automatic), and again once SETTLED_S has passed.
- Deleting: only files directly in ~/Videos/framecorder or its clips/ folder, and screenshot
  pairs directly in the screenshot folder.

Settings in %LOCALAPPDATA%\\cuff-pc\\videos.json.
"""
import io
import json
import os
import posixpath
import re
import threading
import time

import paramiko
from PIL import Image

from frame import DATA

SETTINGS = os.path.join(DATA, "videos.json")
THUMBS = os.path.join(DATA, "thumbs")
REMOTE = "Videos/framecorder"  # under the headset's home, as cuff.remote reports
SAFE = re.compile(r"^(clips/)?[\w.-]+\.mp4$")
SHOTS_DEFAULT = "Pictures/Frame screenshots"  # under the headset's home: v1.0.0's cuff.remote doesn't say
SHOT_NAME = re.compile(r"^[\w.-]+$")  # a pair's <time>
SHOT_FILE = re.compile(r"^([\w.-]+)-(vr|preview)\.png$")
SHOT_DIR = "screenshots/"  # a screenshot file's download path is screenshots/<file>, beside clips/
AUTO_EVERY_S = 60
SETTLED_S = 20
DEFAULTS = {"folder": os.path.join(os.path.expanduser("~"), "Videos", "Cuff"), "auto": False,
            "auto_clips": True, "auto_recordings": True, "auto_screenshots": True, "delete_after": False}


def group_shots(files):
    """cuff.remote's screenshot list, from [(file name, size, mtime)] in the screenshot folder
    (for a headset whose cuff.remote doesn't list them)."""
    shots = {}
    for name, size, mtime in files:
        m = SHOT_FILE.match(name)
        if not m:
            continue
        shot = shots.setdefault(m[1], {"name": m[1], "vr": None, "preview": None, "sizes": {}, "size": 0, "mtime": 0})
        shot[m[2]] = name
        shot["sizes"][name] = size
        shot["size"] += size
        shot["mtime"] = max(shot["mtime"], mtime)
    return sorted(shots.values(), key=lambda s: s["mtime"], reverse=True)


def shot_files(shot):
    """A screenshot's files that are there, as download paths (screenshots/<file>)."""
    return [SHOT_DIR + shot[k] for k in ("vr", "preview") if shot.get(k)]


def downloadable(path):
    """A path the page may download: a video, or one of a screenshot's two files."""
    return bool(SAFE.match(path) or (path.startswith(SHOT_DIR) and SHOT_FILE.match(path[len(SHOT_DIR):])))


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
        self.shots = []  # its screenshots (cuff.remote's list, or group_shots')
        self.shot_folder = None  # where they are on the headset, once listed
        self.meta = {}  # remote path -> {"seconds", "thumb"}
        self.queue = []  # remote paths to download, in order
        self.current = None  # {"path", "done", "size"} while downloading
        self.errors = []
        self.auto_paths = set()  # queued by the automatic download (only those are deleted after)
        self.wake = threading.Event()
        self.nudged = False  # the headset said something new was saved (nudge)
        self.recheck_at = None  # and a second look once it's settled (SETTLED_S)
        threading.Thread(target=self._worker, daemon=True, name="videos").start()

    def nudge(self):
        """The headset just saved a screenshot, clip or recording (POST /recordings/changed): list
        them again now, so the page shows it without a Refresh, and fetch it if downloads are
        automatic. Automatic downloads skip anything newer than SETTLED_S, so it looks again then."""
        self.nudged = True
        self.recheck_at = time.time() + SETTLED_S + 2
        self.wake.set()

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
        out = self.frame.remote("videos")
        listing = out["videos"]
        if "screenshots" in out:
            folder, shots = out["screenshot_folder"], out["screenshots"]
        else:
            folder, shots = self._list_shots()
        shots = [s for s in shots if SHOT_NAME.match(s["name"])]
        with self.lock:
            self.listing, self.listed_at = listing, time.time()
            self.shots, self.shot_folder = shots, folder
        self._fill_meta(listing[:24])
        return listing

    def _list_shots(self):
        """(folder, screenshots) over SFTP from the default folder, for v1.0.0's cuff.remote,
        which doesn't list them."""
        with self.frame.sftp() as sftp:
            folder = posixpath.join(sftp.normalize("."), SHOTS_DEFAULT)
            try:
                files = [(a.filename, a.st_size, a.st_mtime) for a in sftp.listdir_attr(folder)]
            except FileNotFoundError:
                files = []  # no screenshots taken yet
        return folder, group_shots(files)

    def _shot_path(self, sftp, name):
        """A screenshot file's path on the headset."""
        return posixpath.join(self.shot_folder or posixpath.join(sftp.normalize("."), SHOTS_DEFAULT), name)

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

    def shot_thumbnail(self, name):
        """A JPEG of a screenshot file, 480 px across, made here once from its PNG and kept."""
        if not SHOT_FILE.match(name):
            raise ValueError("not a screenshot")
        os.makedirs(THUMBS, exist_ok=True)
        local = os.path.join(THUMBS, "screenshot_" + name[:-4] + ".jpg")
        if not os.path.exists(local):
            img = Image.open(io.BytesIO(self.shot_image(name)))
            img.thumbnail((480, 480))
            out = io.BytesIO()
            img.convert("RGB").save(out, "JPEG", quality=80)
            with open(local, "wb") as f:
                f.write(out.getvalue())
        with open(local, "rb") as f:
            return f.read()

    def shot_image(self, name):
        """A screenshot file's PNG: the downloaded copy, or the headset's."""
        if not SHOT_FILE.match(name):
            raise ValueError("not a screenshot")
        local = self.local_path(SHOT_DIR + name)
        if os.path.exists(local):
            with open(local, "rb") as f:
                return f.read()
        data = io.BytesIO()
        with self.frame.sftp() as sftp:
            sftp.getfo(self._shot_path(sftp, name), data)
        return data.getvalue()

    def local_path(self, path):
        return os.path.join(self.settings["folder"], *path.split("/"))

    def downloaded(self, video):
        try:
            return os.path.getsize(self.local_path(video["path"])) == video["size"]
        except OSError:
            return False

    def shot_downloaded(self, shot):
        """Whether a screenshot's files (both, or the one there is) are here at their sizes."""
        try:
            return all(os.path.getsize(self.local_path(SHOT_DIR + n)) == size for n, size in shot["sizes"].items())
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

    def delete_shot(self, name):
        """Delete a screenshot's two files on the headset."""
        if not SHOT_NAME.match(name):
            return False, "Not a screenshot."
        try:
            with self.frame.sftp() as sftp:
                for kind in ("vr", "preview"):
                    try:
                        sftp.remove(self._shot_path(sftp, f"{name}-{kind}.png"))
                    except FileNotFoundError:
                        pass
        except OSError as e:  # ConnectionError included
            return False, f"Couldn't delete it: {e}"
        with self.lock:
            self.shots = [s for s in self.shots if s["name"] != name]
        return True, "Deleted from the headset."

    def _forget(self, path):
        """Take a file deleted after its download out of the lists: a video, or one of a
        screenshot's files (the screenshot goes with its last)."""
        name = path[len(SHOT_DIR):] if path.startswith(SHOT_DIR) else None
        with self.lock:
            self.listing = [v for v in self.listing if v["path"] != path]
            shots = []
            for s in self.shots:
                if name in s["sizes"]:
                    s = {**s, **{k: None for k in ("vr", "preview") if s[k] == name},
                         "sizes": {n: b for n, b in s["sizes"].items() if n != name}}
                if s["sizes"]:
                    shots.append(s)
            self.shots = shots

    def download(self, paths):
        with self.lock:
            for p in paths:
                if downloadable(p) and p not in self.queue and (not self.current or self.current["path"] != p):
                    self.queue.append(p)
        self.wake.set()
        return True, "Queued."

    # ---------------------------------------------------------------- the worker

    def _worker(self):
        last_auto = 0.0
        while True:
            self.wake.wait(timeout=5)
            self.wake.clear()
            nudged, self.nudged = self.nudged, False
            if self.recheck_at and time.time() >= self.recheck_at:
                nudged, self.recheck_at = True, None
            try:
                if self.settings["auto"] and self.frame.connected() and (nudged or time.time() - last_auto > AUTO_EVERY_S):
                    last_auto = time.time()
                    self._auto()  # lists them too
                elif nudged and self.frame.connected():
                    self.refresh()
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
        if s["auto_screenshots"]:
            with self.lock:
                shots = list(self.shots)  # refresh() above listed them
            for shot in shots:
                if time.time() - shot["mtime"] > SETTLED_S and not self.shot_downloaded(shot):
                    self.auto_paths.update(shot_files(shot))
                    self.download(shot_files(shot))

    def _mtime(self, path):
        """The listed time of a video or screenshot file, for its download; None if it isn't listed."""
        name = path[len(SHOT_DIR):]
        with self.lock:
            for v in self.listing:
                if v["path"] == path:
                    return v["mtime"]
            for s in self.shots:
                if path.startswith(SHOT_DIR) and name in s["sizes"]:
                    return s["mtime"]
        return None

    def _fetch(self, path):
        mtime = self._mtime(path)
        local = self.local_path(path)
        retry = False
        try:
            os.makedirs(os.path.dirname(local), exist_ok=True)
            with self.frame.sftp() as sftp:
                if path.startswith(SHOT_DIR):
                    remote = self._shot_path(sftp, path[len(SHOT_DIR):])
                else:
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
                if mtime:
                    os.utime(local, (mtime, mtime))
                print(f"videos: downloaded {path}", flush=True)
                if path in self.auto_paths and self.settings["delete_after"]:
                    sftp.remove(remote)
                    print(f"videos: deleted {path} from the headset after downloading it", flush=True)
                    self._forget(path)
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
            shots = [{**s, "files": shot_files(s), "downloaded": self.shot_downloaded(s)} for s in self.shots]
            return {"settings": self.settings, "videos": listing, "screenshots": shots,
                    "screenshot_folder": self.shot_folder, "listed_at": self.listed_at,
                    "queue": list(self.queue), "current": dict(self.current) if self.current else None,
                    "errors": list(self.errors)}
