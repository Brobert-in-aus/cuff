"""Steam's own client API, evaluated in its SharedJSContext over the CEF remote debugger
(127.0.0.1:8080). Used to type dictated text into the focused window: games, the desktop
and Steam itself all receive SteamClient.Input.ControllerKeyboardSendText.

Undocumented, so typing_ok() checks once that the call is there; dictation greys its button
out if it isn't.
"""
import base64
import json
import os
import socket
import struct
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


def _ws_recv(s):
    def exact(n):
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf
    _, b2 = exact(2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", exact(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", exact(8))[0]
    return exact(n)


def steam_eval(expression):
    """Evaluate JS in Steam's SharedJSContext (CEF debugger on 127.0.0.1:8080); returns the value."""
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
                return msg.get("result", {}).get("result", {}).get("value")
    finally:
        s.close()


_typing = None  # None until checked


def typing_ok():
    """Whether Steam's text-input call is there (checked once; False if Steam isn't reachable)."""
    global _typing
    if _typing is None:
        try:
            _typing = steam_eval("typeof SteamClient.Input.ControllerKeyboardSendText") == "function"
        except (OSError, ValueError, StopIteration, EOFError, ConnectionError):
            _typing = False
    return _typing


def type_text(text):
    """Type text into the focused window through Steam."""
    steam_eval(f"SteamClient.Input.ControllerKeyboardSendText({json.dumps(text)})")
