"""Cuff for PC as a desktop app: the server (server.py) with a tray icon and a window.

  uv run app.py [server.py's options]   the server and the tray icon; opens the window
                                        the first time (or with --open)
  uv run app.py --open                  with Cuff for PC already running: just opens its window

- The window is the app page (http://127.0.0.1:<port>/app) in Microsoft Edge's app mode: its
  own window, with no tabs or address bar, and its own profile in %LOCALAPPDATA%\\cuff-pc\\window.
  Edge comes with Windows 11; without it, the default browser opens the page.
- The tray icon is a cuff, with a green dot while a headset has been heard from in the
  last minute. Its menu: Open Cuff for PC (also a double-click), Pair a headset, Recordings,
  Quit. Closing the window leaves the app running.
- Ctrl+Alt+V anywhere types the PC's clipboard on the headset (typing_link.py), with a
  notification from the tray icon. The tray menu's Type on the Frame opens the Type page,
  whose Type live box takes every key while it has focus (keycapture.py).
- Only one copy runs (per port, per Windows session): a named mutex, held while it runs. A
  second start (the logon task, the Start menu shortcut, or by hand) opens the running copy's
  window, waiting for it to answer if it's still starting (the speech model takes a while),
  and exits. If the first copy goes away meanwhile, the second carries on as the one.
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

import server
import typing_link

ICON = None  # the tray icon, for notifications
DATA = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share"), "cuff-pc")
FIRST_RUN = os.path.join(DATA, "opened")
ALREADY_EXISTS = 183  # ERROR_ALREADY_EXISTS
WAIT_FOR_FIRST_S = 15 * 60  # a second copy waits this long for the first to answer
_instance = None  # the mutex (or lock file) this copy holds while it runs
EDGE = [os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")]


def port_of(argv):
    try:
        for i, a in enumerate(argv):
            if a == "--port" and i + 1 < len(argv):
                return int(argv[i + 1])
            if a.startswith("--port="):
                return int(a.split("=", 1)[1])
    except ValueError:
        pass  # server.py's own argument parsing says what's wrong
    return 8765


def running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 - nothing there, or something that isn't this app
        return False


def claim(port):
    """Become the one running copy for this port: True if this is it (and stays it until the
    process ends), False if another copy holds it. A named mutex on Windows (in this Windows
    session's namespace), so it goes with the process however that ends; elsewhere, a lock file."""
    global _instance
    if _instance is not None:
        return True
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.CreateMutexW(None, False, f"Local\\CuffForPC-{port}")
        if not handle:
            return True  # no mutex to be had: better two copies than none
        if ctypes.get_last_error() == ALREADY_EXISTS:
            kernel32.CloseHandle(handle)  # (holding it would keep the name alive for the other copy)
            return False
        _instance = handle
        return True
    import fcntl
    os.makedirs(DATA, exist_ok=True)
    f = open(os.path.join(DATA, f"app-{port}.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _instance = f
    return True


def second_copy(port):
    """Another copy runs: open its window once it answers. True when done; False if the other
    copy went away first (then this one should carry on as the one)."""
    deadline = time.monotonic() + WAIT_FOR_FIRST_S
    while time.monotonic() < deadline:
        if running(port):
            open_window(port)
            return True
        if claim(port):
            return False
        time.sleep(1)
    print("app: another copy of Cuff for PC is running but hasn't answered; not starting a second", flush=True)
    return True


def open_window(port, page=""):
    url = f"http://127.0.0.1:{port}/app" + (f"#{page}" if page else "")
    edge = next((e for e in EDGE if os.path.exists(e)), None)
    if edge:
        subprocess.Popen([edge, f"--app={url}", f"--user-data-dir={os.path.join(DATA, 'window')}",
                          "--window-size=1100,820", "--no-first-run"],
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        webbrowser.open(url)


def icon_image(on):
    """A wrist cuff: a rounded band with a panel on it, and a dot (green while a headset's about)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((6, 22, 58, 46), radius=12, fill=(70, 78, 100, 255))
    d.rounded_rectangle((18, 14, 46, 52), radius=7, fill=(28, 32, 44, 255), outline=(230, 234, 244, 255), width=3)
    d.ellipse((40, 4, 62, 26), fill=(30, 215, 96, 255) if on else (130, 136, 150, 255),
              outline=(16, 18, 26, 255), width=3)
    return img


def headset_about():
    p = server.PAIRING
    return bool(p) and any(h["seen_s"] is not None and h["seen_s"] < 60 for h in p.headsets())


def tray(port, stop):
    import pystray

    def quit_app(icon, _):
        if server.CAPTURE:
            server.CAPTURE.close()  # (the process going would take the hook with it anyway)
        icon.stop()
        stop.set()

    menu = pystray.Menu(
        pystray.MenuItem("Open Cuff for PC", lambda: open_window(port), default=True),
        pystray.MenuItem("Pair a headset", lambda: open_window(port, "overview")),
        pystray.MenuItem("Recordings", lambda: open_window(port, "recordings")),
        pystray.MenuItem("Type on the Frame", lambda: open_window(port, "type")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app))
    icon = pystray.Icon("cuff-pc", icon_image(False), "Cuff for PC", menu)
    global ICON
    ICON = icon

    def follow():
        shown = None
        while not stop.is_set():
            on = headset_about()
            if on != shown:
                shown = on
                icon.icon = icon_image(on)
                icon.title = "Cuff for PC: headset connected" if on else "Cuff for PC"
            time.sleep(5)

    threading.Thread(target=follow, daemon=True).start()
    icon.run()


def main():
    argv = sys.argv[1:]
    port = port_of(argv)
    want_window, no_tray = "--open" in argv, "--no-tray" in argv or os.name != "nt"
    if not claim(port) and second_copy(port):
        return 0
    stop = threading.Event()
    sys.argv = [sys.argv[0]] + [a for a in argv if a not in ("--open", "--no-tray")]

    def serve():
        try:
            server.main()
        finally:
            stop.set()

    def first_window():
        # The speech model can take a minute to load, and much longer the first time, when
        # it's downloaded: the window waits for the server to answer, however long that is.
        while not (running(port) or stop.is_set()):
            time.sleep(0.5)
        if server.TYPING and os.name == "nt":
            typing_link.hotkey(server.TYPING, notify=lambda text: ICON and ICON.notify(text, "Cuff for PC"))
        if not stop.is_set() and (want_window or not os.path.exists(FIRST_RUN)):
            os.makedirs(DATA, exist_ok=True)
            open(FIRST_RUN, "w").close()
            open_window(port)

    threading.Thread(target=serve, daemon=True, name="server").start()
    threading.Thread(target=first_window, daemon=True).start()
    if no_tray:
        stop.wait()
    else:
        threading.Thread(target=lambda: (stop.wait(), os._exit(0)), daemon=True).start()
        try:
            tray(port, stop)
        except Exception as e:  # noqa: BLE001 - no tray (no desktop session): the server carries on
            print(f"app: no tray icon ({e!r}); serving without it", flush=True)
            stop.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
