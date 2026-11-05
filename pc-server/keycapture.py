"""Type live catches every key: while the Type page's Type live box has focus, a Windows
low-level keyboard hook takes each key before Windows and Edge act on it and sends it to the
headset instead (typing_link.py). The page sees the keys it can stop itself; this is for the
ones it can't: Alt+Tab, the Windows key and Win+anything, Alt+F4, Ctrl+Esc, and the browser's
own shortcuts such as Ctrl+W, Ctrl+N and Ctrl+T.

- **When:** it swallows a key only while both hold:
  - the page says the box has focus (POST /app/typing/capture {"on": true}, on focus and every
    second after; {"on": false} on blur). A page that stops saying so, crashed or closed, stops
    it within HEARTBEAT_S;
  - the window in front is Cuff for PC's own: a Chromium window (Edge's app mode, app.py) titled
    with the page's title. Clicking anywhere else, which the mouse can always do, ends it at once.
  Otherwise every key goes through as usual. Ctrl+Alt+Del and Win+L never reach a hook.
- **The hook** is installed only while the page says the box has focus, on its own thread with
  a message loop, and removed when that stops (and on quit; a process's hooks also go with it).
- **The callback** does as little as it can: decides, queues the key, and returns 1 to swallow
  it. Windows silently removes a low-level hook whose callback takes longer than
  LowLevelHooksTimeout (about a second, set in the registry) to answer, as when the GIL is
  busy. A key that then reaches the page while the hook should have taken it shows it's gone
  (page_key), and it's installed again.
- **What's sent** (a worker thread, in order): a key that types a character, with no Ctrl, Alt
  or Windows key held (Shift is fine; Ctrl+Alt counts as AltGr when the layout types something
  with it), goes as text, through ToUnicodeEx with the window's keyboard layout, so accents and
  dead keys come out as on the PC. Enter, Backspace, Tab, Esc, Delete, the arrows, Home and End
  on their own go as named keys. Anything else goes as a combination of HID codes from the key's
  scan code (the physical key, whatever the layout), after Steam's codes for the modifiers held.
  Ctrl+V (and Ctrl+Alt+V) sends the PC's clipboard, as in the page. A modifier on its own
  sends nothing; Caps Lock, Num Lock and Scroll Lock go through to the PC, and so do keys with
  no HID code (volume, media), so they keep working.
- **Exactly once:** a key the hook swallows never reaches the page, and a key it lets through
  isn't sent by it; the page sends what it gets. A key's release is swallowed exactly when its
  press was, so Windows never sees half a key.
"""
import ctypes
import ctypes.wintypes as w
import queue
import threading
import time

import typing_link

HEARTBEAT_S = 2.5  # the page says it's focused every second; this long without, it isn't
TITLE = "Cuff for PC"  # app.html's <title>: Edge's app window has it as its own title
WINDOW_CLASS = "Chrome_WidgetWin_1"  # Chromium's top-level windows (Edge, Chrome)
TICK_MS = 250  # how often the hook thread checks it's still wanted
TEXT_GAP_S = 0.04  # characters typed this close together go in one item

WH_KEYBOARD_LL, HC_ACTION, LLKHF_EXTENDED = 13, 0, 0x01
WM_KEYUP, WM_SYSKEYUP, WM_TIMER, WM_QUIT, WM_APP = 0x0101, 0x0105, 0x0113, 0x0012, 0x8000
WM_REHOOK = WM_APP + 1
VK_SHIFT, VK_CONTROL, VK_MENU, VK_CAPITAL, VK_NUMLOCK, VK_SCROLL = 0x10, 0x11, 0x12, 0x14, 0x90, 0x91
VK_LWIN, VK_RWIN, VK_SNAPSHOT, VK_PAUSE, VK_V = 0x5B, 0x5C, 0x2C, 0x13, 0x56
MODIFIERS = {0xA0: "shift", 0xA1: "shift", 0xA2: "ctrl", 0xA3: "ctrl", 0xA4: "alt", 0xA5: "alt",
             VK_LWIN: "super", VK_RWIN: "super",
             VK_SHIFT: "shift", VK_CONTROL: "ctrl", VK_MENU: "alt"}  # (the last three, injected)
LOCKS = (VK_CAPITAL, VK_NUMLOCK, VK_SCROLL)
NAMED = {0x0D: "enter", 0x08: "backspace", 0x09: "tab", 0x1B: "escape", 0x2E: "delete",
         0x25: "left", 0x27: "right", 0x26: "up", 0x28: "down", 0x24: "home", 0x23: "end"}
MOD_ORDER = ("ctrl", "alt", "shift", "super")
MOD_LABEL = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Win"}

# HID usage codes by (scan code, extended), scan codes as Windows reports them (set 1): the
# physical key. Codes 100-103 are keys on HID (the key left of Z on European keyboards, the menu
# key, keypad =), but Steam reads them as its modifiers, so those keys never go in a combination.
_LETTERS = {0x10: "q", 0x11: "w", 0x12: "e", 0x13: "r", 0x14: "t", 0x15: "y", 0x16: "u", 0x17: "i",
            0x18: "o", 0x19: "p", 0x1E: "a", 0x1F: "s", 0x20: "d", 0x21: "f", 0x22: "g", 0x23: "h",
            0x24: "j", 0x25: "k", 0x26: "l", 0x2C: "z", 0x2D: "x", 0x2E: "c", 0x2F: "v", 0x30: "b",
            0x31: "n", 0x32: "m"}
HID = {(sc, False): 4 + ord(ch) - ord("a") for sc, ch in _LETTERS.items()}
HID.update({(0x02 + i, False): 30 + i for i in range(10)})  # 1-9, 0
HID.update({(0x3B + i, False): 58 + i for i in range(10)})  # F1-F10
HID.update({
    (0x01, False): 41, (0x0C, False): 45, (0x0D, False): 46, (0x0E, False): 42, (0x0F, False): 43,
    (0x1A, False): 47, (0x1B, False): 48, (0x1C, False): 40, (0x1C, True): 88, (0x27, False): 51,
    (0x28, False): 52, (0x29, False): 53, (0x2B, False): 49, (0x33, False): 54, (0x34, False): 55,
    (0x35, False): 56, (0x35, True): 84, (0x37, False): 85, (0x37, True): 70, (0x39, False): 44,
    (0x46, False): 71, (0x47, False): 95, (0x48, False): 96, (0x49, False): 97, (0x4A, False): 86,
    (0x4B, False): 92, (0x4C, False): 93, (0x4D, False): 94, (0x4E, False): 87, (0x4F, False): 89,
    (0x50, False): 90, (0x51, False): 91, (0x52, False): 98, (0x53, False): 99, (0x56, False): 100,
    (0x57, False): 68, (0x58, False): 69, (0x59, False): 103, (0x47, True): 74, (0x48, True): 82,
    (0x49, True): 75, (0x4B, True): 80, (0x4D, True): 79, (0x4F, True): 77, (0x50, True): 81,
    (0x51, True): 78, (0x52, True): 73, (0x53, True): 76, (0x5D, True): 101, (0x70, False): 136,
    (0x73, False): 135, (0x79, False): 138, (0x7B, False): 139, (0x7D, False): 137, (0x7E, False): 133,
})
BY_VK = {VK_SNAPSHOT: 70, VK_PAUSE: 72}  # keys whose scan codes Windows reports oddly
STEAM_MODIFIER_CODES = set(typing_link.MOD_CODES.values())

NAMES = {4 + i: chr(ord("A") + i) for i in range(26)}
NAMES.update({30 + i: str((i + 1) % 10) for i in range(10)})
NAMES.update({58 + i: f"F{i + 1}" for i in range(12)})
NAMES.update({40: "Enter", 41: "Esc", 42: "Backspace", 43: "Tab", 44: "Space", 45: "-", 46: "=", 47: "[",
              48: "]", 49: "\\", 51: ";", 52: "'", 53: "`", 54: ",", 55: ".", 56: "/", 70: "PrintScreen",
              71: "ScrollLock", 72: "Pause", 73: "Insert", 74: "Home", 75: "PageUp", 76: "Delete", 77: "End",
              78: "PageDown", 79: "Right", 80: "Left", 81: "Down", 82: "Up", 84: "Num /", 85: "Num *",
              86: "Num -", 87: "Num +", 88: "Num Enter"})
NAMES.update({89 + i: f"Num {(i + 1) % 10}" for i in range(10)})
NAMES[99] = "Num ."


def label(mods, hid):
    """'Ctrl+A' for a combination's modifiers and key."""
    return "+".join([MOD_LABEL[m] for m in MOD_ORDER if m in mods] + [NAMES.get(hid, f"key {hid}")])


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", w.DWORD), ("scanCode", w.DWORD), ("flags", w.DWORD), ("time", w.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, w.LPARAM)


def _user32():
    """user32 with the prototypes used here (its own instance, so other modules' are untouched)."""
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.SetWindowsHookExW.restype = ctypes.c_void_p
    u.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, w.DWORD]
    u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    u.CallNextHookEx.restype = ctypes.c_ssize_t
    u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, w.WPARAM, w.LPARAM]
    u.GetForegroundWindow.restype = ctypes.c_void_p
    u.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    u.InternalGetWindowText.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u.GetKeyboardLayout.restype = ctypes.c_void_p
    u.GetKeyboardLayout.argtypes = [w.DWORD]
    u.ToUnicodeEx.argtypes = [w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int, w.UINT,
                              ctypes.c_void_p]
    u.GetAsyncKeyState.restype = ctypes.c_short
    u.GetKeyState.restype = ctypes.c_short
    u.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, w.UINT, w.UINT]
    u.PeekMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, w.UINT, w.UINT, w.UINT]
    u.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]
    u.SetTimer.restype = ctypes.c_size_t
    u.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, w.UINT, ctypes.c_void_p]
    u.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    return u


class Capture:
    """The keyboard hook for Type live. page(on) from the page's capture route; page_key() when
    the page itself sent a key; close() on quit."""

    def __init__(self, typing, ours=None):
        self.typing = typing
        self.ours = ours or self._ours  # whether a foreground window is Cuff for PC's (tests swap it)
        self.until = 0.0  # the page's focus counts until then (monotonic)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.queue = queue.SimpleQueue()
        self.thread_id = 0  # the hook thread's, for PostThreadMessageW
        self.started = False
        self.closed = False
        self.hook = None
        self.failed = False  # the last try to hook failed (said once)
        self.hooked_at = 0.0  # while hooked: when; else 0
        self.called_at = 0.0  # the callback last ran
        self.down = set()  # keys whose press was swallowed: their release is too
        self.held = set()  # modifiers among them, by name (left and right alike)
        self.keys = 0  # keys sent from here, for the page's count
        self.last = ""  # the last combination sent from here ("Ctrl+A")
        self.last_n = 0  # counts combinations, so the page knows a new one from the same one again
        self.user32 = _user32()
        self._proc = HOOKPROC(self._callback)  # kept: Windows calls it for as long as it's hooked
        self._class = ctypes.create_unicode_buffer(64)
        self._title = ctypes.create_unicode_buffer(256)

    # ---- from the page

    def page(self, on):
        """The page says the Type live box has (on) or hasn't focus. -> the page's view."""
        with self.lock:
            self.until = time.monotonic() + HEARTBEAT_S if on else 0.0
            if on and not self.started and not self.closed:
                self.started = True
                threading.Thread(target=self._hook_loop, daemon=True, name="typing-capture").start()
                threading.Thread(target=self._work, daemon=True, name="typing-capture-send").start()
        if on:
            self.wake.set()
        return self.state()

    def state(self):
        return {"capturing": self.wanted() and bool(self.hook), "keys": self.keys,
                "last": self.last, "last_n": self.last_n}

    def wanted(self):
        return not self.closed and time.monotonic() < self.until

    def page_key(self):
        """The page sent a key itself, so the hook didn't take it. Normal before the hook is in,
        or with another window in front; but with the hook in a while and not called at all
        lately, Windows has probably removed it (LowLevelHooksTimeout): install it again. (Keys
        a test sends straight to the page, not through Windows, look the same: harmless.)"""
        now = time.monotonic()
        if (self.hook and self.wanted() and now - self.hooked_at > 1.0 and now - self.called_at > 1.5
                and self.thread_id):
            print("typing: a key reached the page without passing the keyboard hook (Windows removes "
                  "one that answers too slowly); installing it again", flush=True)
            self.user32.PostThreadMessageW(self.thread_id, WM_REHOOK, 0, 0)

    def close(self):
        """Unhook and stop (quitting)."""
        self.closed, self.until = True, 0.0
        self.wake.set()
        self.queue.put(None)
        if self.thread_id:
            self.user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)

    # ---- the hook thread

    def _hook_loop(self):
        u = self.user32
        msg = w.MSG()
        u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # gives this thread its message queue
        self.thread_id = threading.get_native_id()
        while not self.closed:
            self.wake.wait()
            self.wake.clear()
            if not self.wanted():
                continue
            self._hook()
            timer = u.SetTimer(None, 0, TICK_MS, None)
            try:
                while self.hook and u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    if msg.message == WM_TIMER and not self.wanted():
                        break
                    if msg.message == WM_REHOOK:
                        self._unhook()
                        self._hook()
            finally:
                u.KillTimer(None, timer)
                self._unhook()

    def _hook(self):
        self.hook = self.user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not self.hook:
            if not self.failed:  # (it's tried again with each of the page's heartbeats)
                print(f"typing: no keyboard hook (error {ctypes.get_last_error()}); "
                      "shortcuts the page can't stop still reach the PC", flush=True)
            self.failed = True
            return
        self.failed = False
        self.hooked_at = self.called_at = time.monotonic()

    def _unhook(self):
        if self.hook:
            self.user32.UnhookWindowsHookEx(self.hook)
        self.hook, self.hooked_at = None, 0.0
        self.down.clear()  # (their releases go to Windows, which hasn't seen them pressed: harmless)
        self.held.clear()

    def _callback(self, code, wparam, lparam):
        """Windows' call for each key. Small and quick: see the module's docstring."""
        try:
            self.called_at = time.monotonic()
            if code == HC_ACTION and self._swallow(wparam, KBDLLHOOKSTRUCT.from_address(lparam)):
                return 1
        except Exception:  # noqa: BLE001 - a key must never be lost to a bug here
            pass
        return self.user32.CallNextHookEx(None, code, wparam, lparam)

    def _swallow(self, wparam, k):
        """Whether to swallow this key event; queues the presses to send."""
        vk = k.vkCode
        if wparam in (WM_KEYUP, WM_SYSKEYUP):
            if vk in self.down:
                self.down.discard(vk)
                if vk in MODIFIERS:
                    self.held.discard(MODIFIERS[vk])
                return True
            return False
        if vk in LOCKS or not self.wanted():
            return False
        fg = self.user32.GetForegroundWindow()
        if not self.ours(fg):
            return False
        if vk in MODIFIERS:
            self.down.add(vk)
            self.held.add(MODIFIERS[vk])
            return True
        hid = BY_VK.get(vk) or HID.get((k.scanCode & 0xFF, bool(k.flags & LLKHF_EXTENDED)))
        if hid is None:
            return False
        self.down.add(vk)
        layout = self.user32.GetKeyboardLayout(self.user32.GetWindowThreadProcessId(fg, None))
        self.queue.put((vk, k.scanCode, hid, self._mods(), layout))
        return True

    def _mods(self):
        """The modifiers held: those swallowed, and any Windows saw pressed before capture began."""
        u, held = self.user32, set(self.held)
        for vk, name in ((VK_SHIFT, "shift"), (VK_CONTROL, "ctrl"), (VK_MENU, "alt"),
                         (VK_LWIN, "super"), (VK_RWIN, "super")):
            if u.GetAsyncKeyState(vk) & 0x8000:
                held.add(name)
        return frozenset(held)

    def _ours(self, hwnd):
        """Whether hwnd is Cuff for PC's window: Chromium's, titled with the page's title (Edge's
        app window; in a browser tab, "Cuff for PC - <browser>"). Reads the title without sending
        the window a message, so a hung window can't hold the callback up."""
        if not hwnd:
            return False
        u = self.user32
        u.GetClassNameW(hwnd, self._class, len(self._class))
        if self._class.value != WINDOW_CLASS:
            return False
        u.InternalGetWindowText(hwnd, self._title, len(self._title))
        title = self._title.value
        return title == TITLE or title.startswith(TITLE + " - ")

    # ---- the worker: what to send for each key, in order

    def translate(self, vk, scan, hid, mods, layout):
        """("text", s), ("key", name), ("combo", codes, label), ("clipboard",) or None."""
        ctrl, alt, win = "ctrl" in mods, "alt" in mods, "super" in mods
        if vk == VK_V and ctrl and not win and "shift" not in mods:
            return ("clipboard",)  # Ctrl+V, as the page's paste; Ctrl+Alt+V, as the hotkey
        if not win and ctrl == alt:  # none, or Ctrl+Alt: AltGr if the layout types with it
            text = self._unicode(vk, scan, mods, layout)
            if text is None:
                return None  # a dead key: it goes with the next one
            if text and all(ord(c) >= 0x20 and ord(c) != 0x7F for c in text):
                return ("text", text)
        if not mods and vk in NAMED:
            return ("key", NAMED[vk])
        if hid in STEAM_MODIFIER_CODES:
            return None  # Steam would read it as a modifier
        return ("combo", [typing_link.MOD_CODES[m] for m in MOD_ORDER if m in mods] + [hid], label(mods, hid))

    def _unicode(self, vk, scan, mods, layout):
        """What the key types with these modifiers in this layout: "" for nothing, None for a dead
        key (Windows keeps it, on this thread, for the next key, as when typing in a window)."""
        state = (ctypes.c_ubyte * 256)()
        for name, vks in (("shift", (VK_SHIFT, 0xA0)), ("ctrl", (VK_CONTROL, 0xA2)), ("alt", (VK_MENU, 0xA5))):
            if name in mods:
                for v in vks:
                    state[v] = 0x80
        state[VK_CAPITAL] = self.user32.GetKeyState(VK_CAPITAL) & 1
        buf = ctypes.create_unicode_buffer(8)
        n = self.user32.ToUnicodeEx(vk, scan, state, buf, len(buf), 0, layout)
        if n < 0:
            return None
        return buf.value[:n]

    def _work(self):
        """Send each queued key in order. Characters typed close together go as one text item,
        TEXT_GAP_S after the first, as the page does."""
        text, deadline = "", 0.0
        while True:
            try:
                item = self.queue.get(timeout=max(0.0, deadline - time.monotonic())) if text else self.queue.get()
            except queue.Empty:
                text = self._send_text(text)
                continue
            if item is None:
                self._send_text(text)
                return
            try:
                out = self.translate(*item)
                if out and out[0] == "text":
                    text, deadline = text + out[1], deadline if text else time.monotonic() + TEXT_GAP_S
                elif out:
                    text = self._send_text(text)
                    self._send(out)
            except Exception as e:  # noqa: BLE001 - one bad key mustn't stop the rest
                print(f"typing: a captured key wasn't sent: {e!r}", flush=True)

    def _send_text(self, text):
        """Send text, if any; -> "" (what's left to send)."""
        if text and self.typing.send(text=text)[0]:
            self.keys += len(text)
        return ""

    def _send(self, out):
        kind = out[0]
        if kind == "key":
            ok, _ = self.typing.send(key=out[1])
        elif kind == "combo":
            ok, _ = self.typing.send(combo=out[1])
            if ok:
                self.last, self.last_n = out[2], self.last_n + 1
        else:  # the clipboard
            ok, message = typing_link.send_clipboard(self.typing)
            self.last, self.last_n = ("the clipboard" if ok else message), self.last_n + 1
        if ok:
            self.keys += 1
