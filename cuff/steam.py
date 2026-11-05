"""Steam's own client API, evaluated in its SharedJSContext over the CEF remote debugger
(127.0.0.1:8080). Used to type dictated text, and text typed on the PC (cuff/pc.py), into the
focused window: games, the desktop and Steam itself all receive
SteamClient.Input.ControllerKeyboardSendText. Keys such as Enter, and key combinations such as
Ctrl+A, go through ControllerKeyboardSetKeyState, as Steam's on-screen keyboard does.

Undocumented, so typing_ok() checks that the call is there; dictation greys its button out if
it isn't. Any call that fails (Steam's debugger unreachable, or the expression throwing) forgets
that check, so typing_ok() asks again, and on_typing_lost() callbacks run so the button greys
out until it answers.
"""
import base64
import json
import os
import socket
import struct
import time
import urllib.request


# ---------------------------------------------------------------- minimal WebSocket client

def _ws_connect(host, port, path, timeout=5):
    s = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            raise ConnectionError("closed during handshake")
        resp += chunk
    if b" 101 " not in resp.split(b"\r\n")[0]:
        raise ConnectionError(resp.split(b"\r\n")[0].decode(errors="replace"))
    return s


def _ws_send(s, text):
    data = text.encode()
    hdr = bytearray([0x81])
    n = len(data)
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
    mask = os.urandom(4)
    s.sendall(bytes(hdr) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _ws_frame(s):
    """(opcode, payload) of the next frame: 1 text, 8 close, 9 ping (the debugger doesn't
    mask or fragment what it sends)."""
    def exact(n):
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf
    b1, b2 = exact(2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", exact(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", exact(8))[0]
    return b1 & 0x0F, exact(n)


def _ws_recv(s):
    return _ws_frame(s)[1]


def page_socket(title):
    """A WebSocket to the debugger's page with this title (keyboard.py keeps one open)."""
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:8080/json", timeout=5))
    url = next(t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page" and t["title"] == title)
    host_port, path = url[len("ws://"):].split("/", 1)
    host, port = host_port.split(":")
    return _ws_connect(host, int(port), "/" + path)


class SteamError(ValueError):
    """The expression reached Steam and threw (or the debugger refused it)."""


def steam_eval(expression):
    """Evaluate JS in Steam's SharedJSContext (CEF debugger on 127.0.0.1:8080); returns the value.
    Raises SteamError when it throws there (the debugger's exceptionDetails, which used to come back
    as a plain None), else OSError, ValueError, StopIteration, EOFError or ConnectionError when the
    debugger can't be reached. Either way the typing check is forgotten (typing_ok)."""
    try:
        targets = json.load(urllib.request.urlopen("http://127.0.0.1:8080/json", timeout=5))
        url = next(t["webSocketDebuggerUrl"] for t in targets if t["title"] == "SharedJSContext")
        host_port, path = url[len("ws://"):].split("/", 1)
        host, port = host_port.split(":")
        s = _ws_connect(host, int(port), "/" + path)
        try:
            _ws_send(s, json.dumps({"id": 1, "method": "Runtime.evaluate",
                                    "params": {"expression": expression, "returnByValue": True,
                                               "awaitPromise": True}}))
            while True:
                msg = json.loads(_ws_recv(s))
                if msg.get("id") == 1:
                    return _value(msg)
        finally:
            s.close()
    except (OSError, ValueError, StopIteration, EOFError, ConnectionError):
        _lost()
        raise


def _value(msg):
    """The value out of the debugger's reply to Runtime.evaluate, or SteamError."""
    if "error" in msg:
        raise SteamError(msg["error"].get("message") or "the debugger refused it")
    result = msg.get("result") or {}
    details = result.get("exceptionDetails")
    if details is not None:
        raise SteamError((details.get("exception") or {}).get("description") or details.get("text") or "it threw")
    return (result.get("result") or {}).get("value")


_typing = False  # True once Steam's text-input call has been found, until a call fails
_checked_at = None
_lost_callbacks = []
RECHECK_S = 10.0  # while it isn't there (Steam starting, or gone), ask again this often at most


def on_typing_lost(callback):
    """callback() runs, on the failing call's thread, when typing that worked stops working."""
    _lost_callbacks.append(callback)


def _lost():
    """A call failed: forget that typing works, and say so if it did."""
    global _typing, _checked_at
    was, _typing, _checked_at = _typing, False, time.monotonic()
    if was:
        for callback in list(_lost_callbacks):
            callback()


def typing_ok():
    """Whether Steam's text-input call is there. Once found it's remembered until a call fails;
    until then it's asked again (at most every RECHECK_S), since Cuff can start before Steam's
    interface does, as after a reboot."""
    global _typing, _checked_at
    if _typing:
        return True
    now = time.monotonic()
    if _checked_at is not None and now - _checked_at < RECHECK_S:
        return False
    _checked_at = now
    try:
        _typing = steam_eval("typeof SteamClient.Input.ControllerKeyboardSendText") == "function"
    except (OSError, ValueError, StopIteration, EOFError, ConnectionError):
        _typing = False
    return _typing


def type_text(text):
    """Type text into the focused window through Steam."""
    steam_eval(f"SteamClient.Input.ControllerKeyboardSendText({json.dumps(text)})")


# Keys Steam's own on-screen keyboard presses with ControllerKeyboardSetKeyState: USB HID
# keyboard usage codes.
KEYS = {"enter": 40, "escape": 41, "backspace": 42, "tab": 43, "home": 74, "delete": 76, "end": 77,
        "right": 79, "left": 80, "down": 81, "up": 82}


def press_key(name):
    """Press and release a key (enter, backspace, the arrows...) in the focused window."""
    code = KEYS[name]
    steam_eval(f"SteamClient.Input.ControllerKeyboardSetKeyState({code}, true);"
               f"SteamClient.Input.ControllerKeyboardSetKeyState({code}, false)")


# ControllerKeyboardSetKeyState takes HID usage codes for keys, but its own codes for the
# modifiers (not HID's 224-231): so Ctrl+A is [103, 4].
MODIFIERS = {"ctrl": 103, "alt": 100, "shift": 101, "super": 102}
MAX_COMBO = 6


def valid_combo(codes):
    """Whether codes is a combination press_combo takes: a list of 1 to MAX_COMBO whole
    numbers from 4 to 231 (modifiers first, then the key)."""
    return (isinstance(codes, list) and 1 <= len(codes) <= MAX_COMBO
            and all(type(c) is int and 4 <= c <= 231 for c in codes))


def press_combo(codes):
    """Press a key combination (Ctrl+A is [103, 4]) in the focused window: each code down in
    order, a moment, then up in reverse, with short gaps so the desktop sees the modifiers held
    before the key. One evaluate call, which waits for the whole press."""
    if not valid_combo(codes):
        raise ValueError(f"not a key combination: {codes!r}")
    steam_eval("(async () => { const I = SteamClient.Input, w = (ms) => new Promise((r) => setTimeout(r, ms));"
               f" const c = {json.dumps(codes)};"
               " for (const k of c) { I.ControllerKeyboardSetKeyState(k, true); await w(25); }"
               " await w(40);"
               " for (const k of [...c].reverse()) { I.ControllerKeyboardSetKeyState(k, false); await w(25); }"
               " })()")
