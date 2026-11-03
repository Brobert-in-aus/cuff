"""Typing on the Frame from the PC: text and keys typed in the app's Type page, or the
clipboard (the page's button, or Ctrl+Alt+V anywhere), go to whatever has focus in the
headset.

- The PC holds a short queue; the headset fetches it with a long poll,
  GET /typing/next?after=<seq>&run=<id> (signed by a paired headset, and answered signed:
  pairing.py), held up to POLL_S and answered as soon as there's something newer. It opens no
  port, and plain HTTP serves both ends.
- seq counts from 0 again when the app restarts, so each run has its own random id ("run" in
  every answer). A headset asking with another run's id, or after a seq this run hasn't
  reached, is starting over: it gets whatever no headset has fetched yet, as at -1.
- The headset polls only while the announcement says "typing": true (beacon.py), which is
  while the Type page is open (it says so every few seconds) or something was sent in the
  last ACTIVE_S. So the hotkey works with the window closed; it starts within a couple of
  seconds.
- Items: {"seq", "text"} or {"seq", "key": enter|backspace|tab|escape|delete|left|right|up|
  down|home|end}. Items older than STALE_S are dropped unsent, so text typed while the
  headset was away doesn't arrive later somewhere unexpected.
- Nothing typed is logged; the log says only how many characters went.
"""
import ctypes
import ctypes.wintypes  # noqa: F401 - MSG, for the hotkey's message loop
import secrets
import threading
import time

KEYS = ("enter", "backspace", "tab", "escape", "delete", "left", "right", "up", "down", "home", "end")
POLL_S = 25.0
ACTIVE_S = 15.0
STALE_S = 20.0
MAX_TEXT = 4000


class Typing:
    def __init__(self):
        self.cond = threading.Condition()
        self.items = []  # [(time, item)]
        self.seq = 0
        self.run = secrets.token_hex(4)  # this run's id: seq starts again from 0 in the next
        self.page_at = 0.0  # the Type page last said it's open
        self.sent_at = 0.0
        self.delivered = 0  # the highest seq a headset has fetched
        self.fetched_at = 0.0

    def active(self):
        now = time.monotonic()
        return now - self.page_at < ACTIVE_S or now - self.sent_at < ACTIVE_S

    def page_open(self):
        self.page_at = time.monotonic()

    def send(self, text=None, key=None):
        """(ok, message)."""
        if key is None and text is None:
            return False, "Nothing to type."
        if key is not None and key not in KEYS:
            return False, f"Unknown key {key}."
        if text is not None:
            text = str(text)[:MAX_TEXT]
            if not text:
                return False, "Nothing to type."
        item = {"key": key} if key else {"text": text}
        with self.cond:
            self.seq += 1
            item["seq"] = self.seq
            now = time.monotonic()
            self.items = [(t, i) for t, i in self.items if now - t < STALE_S] + [(now, item)]
            self.sent_at = now
            self.cond.notify_all()
        return True, "Sent."

    def next(self, after, run=None):
        """Items after seq `after` (-1, a headset starting to listen: whatever no headset has
        fetched yet, such as a clipboard sent before it heard the announcement; the same for a
        seq of another run, or one this run hasn't reached), waiting up to POLL_S for some.
        -> {"seq", "run", "items", "on"}"""
        deadline = time.monotonic() + POLL_S
        with self.cond:
            if after < 0 or after > self.seq or (run and run != self.run):
                after = self.delivered
            while True:
                now = time.monotonic()
                fresh = [i for t, i in self.items if i["seq"] > after and now - t < STALE_S]
                if fresh or now >= deadline or not self.active():
                    break
                self.cond.wait(timeout=min(1.0, deadline - now))
            if fresh:
                self.delivered, self.fetched_at = max(self.delivered, fresh[-1]["seq"]), now
                chars = sum(len(i.get("text", "")) for i in fresh)
                print(f"typing: {len(fresh)} item(s), {chars} character(s) to the headset", flush=True)
            elif after >= self.seq:
                self.fetched_at = now
            return {"seq": max(after, self.seq), "run": self.run, "items": fresh, "on": self.active()}

    def state(self):
        now = time.monotonic()
        return {"active": self.active(), "pending": max(0, self.seq - self.delivered),
                "headset_polling": now - self.fetched_at < POLL_S + 5}


def clipboard_text():
    """The Windows clipboard's text, or None."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    CF_UNICODETEXT = 13
    for _ in range(5):  # another program may hold it for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(ptr) if ptr else None
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def send_clipboard(typing):
    text = clipboard_text()
    if not text:
        return False, "The clipboard has no text."
    return typing.send(text=text.replace("\r\n", "\n"))


def hotkey(typing, notify=None):
    """Ctrl+Alt+V anywhere sends the clipboard (RegisterHotKey; its own thread and message loop)."""
    def loop():
        user32 = ctypes.windll.user32
        MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, VK_V, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x56, 0x0312
        if not user32.RegisterHotKey(None, 1, MOD_ALT | MOD_CONTROL | MOD_NOREPEAT, VK_V):
            print("typing: Ctrl+Alt+V is taken by another program; no hotkey", flush=True)
            return
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                _, message = send_clipboard(typing)
                print(f"typing: Ctrl+Alt+V: {message}", flush=True)
                if notify:
                    notify(message)

    threading.Thread(target=loop, daemon=True, name="typing-hotkey").start()
