"""The app page's API (app.html), answered only for the PC itself (docs/PC-APP.md).

Every /app route but the page itself, thumbnails and screenshots needs the header X-Cuff-App:
1, and every one needs a Host of 127.0.0.1 or localhost. A web page elsewhere can't add that
header without the browser asking first (CORS preflight, which this server never allows), and a
rebound DNS name fails the Host check, so only the app page can drive the headset from here.
Thumbnails and screenshots, for <img>, carry this run's random thumb_key (from /app/state,
which only the page can read) instead of the header. server.py puts its other routes from the PC itself behind allowed() too.

  GET  /app                    app.html
  GET  /app/state              the PC: headsets, pairing, dictation, the log; and the headset link
  POST /app/unpair             {"id"}
  POST /app/frame/connect      {"host", "user", "password"?}
  POST /app/frame/forget       disconnect, and stop reconnecting
  POST /app/frame/run          {"kind": status|install|uninstall, "parts": [...], "pair": bool}
  POST /app/frame/answer       {"text"}: the running job's question
  POST /app/frame/cancel
  POST /app/frame/restart      restart Cuff on the headset
  GET  /app/settings           Cuff's settings on the headset (cuff.remote dump)
  POST /app/settings           {section: {key: value}}
  GET  /app/videos             the downloads' state and the last lists (videos and screenshots)
  POST /app/videos/refresh     list the headset's videos and screenshots again
  POST /app/videos/download    {"paths": [...]}: videos, and screenshots' files (screenshots/<file>)
  POST /app/videos/delete      {"path"}: a video; or {"shot"}: a screenshot's two files
  POST /app/videos/settings    {"folder", "auto", "auto_clips", "auto_recordings", "auto_screenshots",
                               "delete_after"}
  POST /app/videos/open        {"path"} plays (or shows) a downloaded one; {} opens the folder
  POST /app/videos/choose      a folder picker; -> {"folder"}
  GET  /app/videos/thumb?key=&path=  a JPEG (key: /app/state's thumb_key)
  GET  /app/videos/screenshot?key=&name=&thumb=1  a screenshot file: its PNG, or with thumb a
                               small JPEG (name: <time>-vr.png or <time>-preview.png)
  POST /app/typing             {"text"} or {"key"}: type on the headset (typing_link.py)
  POST /app/typing/clipboard   type the clipboard's text
  POST /app/typing/open        the Type page is open (it says so every few seconds)
  POST /app/update/check       look for a newer release now (updates.py)
  POST /app/update/install     download it and run its installer
  GET  /app/link               connection quality: the dongle, your network, the stream (linkquality.py)
  POST /app/link/refresh       take a sample now
  POST /app/quit               quit Cuff for PC (the installer's first step, to replace its files)
"""
import hmac
import os
import secrets
import threading
import urllib.parse

import typing_link

HOSTS = ("127.0.0.1", "localhost")
THUMB = "/app/videos/thumb"  # an <img> source: thumb_key instead of the header
SHOT = "/app/videos/screenshot"  # the same


class AppApi:
    def __init__(self, port, state, pairing, frame, videos, typing, updates, link):
        self.port, self.state, self.pairing, self.frame, self.videos = port, state, pairing, frame, videos
        self.typing, self.updates, self.link = typing, updates, link
        self.thumb_key = secrets.token_urlsafe(16)  # new each run

    def allowed(self, handler, path):
        host = (handler.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in HOSTS:
            return False
        if path == "/app":
            return True
        if path in (THUMB, SHOT):
            key = dict(urllib.parse.parse_qsl(handler.path.partition("?")[2])).get("key", "")
            return hmac.compare_digest(key, self.thumb_key)
        return handler.headers.get("X-Cuff-App") == "1"

    # Each returns (code, obj) for JSON, or (code, bytes, content type).

    def get(self, path, query):
        q = dict(urllib.parse.parse_qsl(query))
        if path == "/app/state":
            return 200, {**self.state(), "frame": self.frame.state(), "typing": self.typing.state(),
                         "updates": self.updates.state(), "thumb_key": self.thumb_key}
        if path == "/app/settings":
            return self._try(self.frame.settings)
        if path == "/app/link":
            return 200, self.link.state()
        if path == "/app/videos":
            return 200, self.videos.state()
        if path == THUMB:
            try:
                return 200, self.videos.thumbnail(q.get("path", "")), "image/jpeg"
            except Exception as e:  # noqa: BLE001 - no thumbnail is no thumbnail
                return 404, {"error": str(e)}
        if path == SHOT:
            try:
                name = q.get("name", "")
                if q.get("thumb"):
                    return 200, self.videos.shot_thumbnail(name), "image/jpeg"
                return 200, self.videos.shot_image(name), "image/png"
            except Exception as e:  # noqa: BLE001 - a missing screenshot is a missing image
                return 404, {"error": str(e)}
        return 404, {"error": "not found"}

    def post(self, path, body):
        f, v = self.frame, self.videos
        routes = {
            "/app/unpair": lambda: self.pairing.unpair(str(body.get("id", ""))),
            "/app/frame/connect": lambda: f.connect(body.get("host"), body.get("user") or "steamos", body.get("password"),
                                                   trust=body.get("trust")),
            "/app/frame/forget": lambda: (f.forget(), (True, "Disconnected."))[1],
            "/app/frame/run": lambda: f.start(str(body.get("kind")), body.get("parts") or [], bool(body.get("pair"))),
            "/app/frame/answer": lambda: f.answer(body.get("text", "")),
            "/app/frame/cancel": f.cancel,
            "/app/frame/restart": f.restart_cuff,
            "/app/settings": lambda: f.apply_settings(body),
            "/app/videos/refresh": lambda: {"videos": v.refresh()},
            "/app/videos/download": lambda: v.download([str(p) for p in body.get("paths", [])]),
            "/app/videos/delete": lambda: (v.delete_shot(str(body["shot"])) if "shot" in body
                                           else v.delete(str(body.get("path", "")))),
            "/app/videos/settings": lambda: (v.save_settings(body), (True, "Saved."))[1],
            "/app/videos/open": lambda: self._open(body.get("path")),
            "/app/videos/choose": self._choose,
            "/app/typing": lambda: self.typing.send(text=body.get("text"), key=body.get("key")),
            "/app/typing/clipboard": lambda: typing_link.send_clipboard(self.typing),
            "/app/typing/open": lambda: (self.typing.page_open(), self.typing.state())[1],
            "/app/update/check": self.updates.check,
            "/app/update/install": self.updates.install,
            "/app/quit": self._quit,
            "/app/link/refresh": lambda: (self.link.refresh(), (True, "Measuring…"))[1],
        }
        if path not in routes:
            return 404, {"error": "not found"}
        if path == "/app/unpair":
            return routes[path]()  # already (code, obj)
        return self._try(routes[path])

    @staticmethod
    def _try(fn):
        try:
            out = fn()
        except Exception as e:  # noqa: BLE001 - the page shows what went wrong
            return 500, {"ok": False, "message": str(e) or repr(e)}
        if isinstance(out, tuple):
            ok, message = out
            return (200 if ok else 400), {"ok": ok, "message": message}
        return 200, out

    @staticmethod
    def _quit():
        print("app: asked to quit (the installer, replacing this copy)", flush=True)
        threading.Timer(0.5, os._exit, (0,)).start()  # after the reply goes out
        return True, "Quitting."

    def _open(self, path):
        if path:
            local = self.videos.local_path(str(path))
            if not os.path.exists(local):
                return False, "It isn't downloaded."
            os.startfile(local)  # noqa: S606 - the PC's own player, for the PC's own file
            return True, "Opened."
        folder = self.videos.settings["folder"]
        os.makedirs(folder, exist_ok=True)
        os.startfile(folder)  # noqa: S606
        return True, "Opened."

    def _choose(self):
        """Windows' folder picker, through Tk (on its own thread, its own root)."""
        out = {}

        def ask():
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            out["folder"] = filedialog.askdirectory(initialdir=self.videos.settings["folder"],
                                                    title="Where downloaded videos go", parent=root)
            root.destroy()

        t = threading.Thread(target=ask, daemon=True)
        t.start()
        t.join()
        folder = out.get("folder")
        if folder:
            self.videos.save_settings({"folder": os.path.normpath(folder)})
        return {"ok": True, "folder": self.videos.settings["folder"]}
