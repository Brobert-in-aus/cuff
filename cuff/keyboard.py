"""A dictation key on Steam's own on-screen keyboard.

Selecting a text field opens Steam's keyboard, and while it's open Cuff's panel is hidden, so
the quick row's dictation button is out of reach just when it's wanted. This puts a mic key on
the keyboard itself, over the left end of the space bar, that does the same as that button.

How: Steam's keyboard is a CEF page ("SteamVR - Keyboard") on the remote debugger
(127.0.0.1:8080), the one steam.py types through. One thread keeps a WebSocket to that page:
  - it adds a binding, __cuffDictate, that keyboard.js calls when the key is pressed, and
    turns each call into the dictation button's toggle;
  - it injects keyboard.js whenever the page gets a new JS context (a reload, Steam
    restarting), and keeps the key's look in step with dictation (listening, transcribing,
    dimmed when it can't type);
  - when Steam or its debugger goes away it waits and connects again.
Nothing in Steam's files changes, so a Steam update doesn't undo it; if Steam's layout changes
so the space bar can't be found, the key doesn't show.

[dictation] keyboard_key = false in config.toml leaves Steam's keyboard alone.
"""
import json
import os
import select
import sys
import threading
import time

from . import steam

PAGE = "SteamVR - Keyboard"
BINDING = "__cuffDictate"
JS = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "keyboard.js"), encoding="utf-8").read()
RETRY_S = 5.0  # between attempts while Steam's keyboard page isn't there
INJECT_DELAY_S = 1.0  # after a new JS context, so Steam has drawn its keys


class KeyboardMic(threading.Thread):
    def __init__(self, toggle, state):
        """toggle(): the dictation button's tap. state(): Dictation.state ({"phase",
        "configured"})."""
        super().__init__(daemon=True, name="keyboard-mic")
        self.toggle, self.state = toggle, state
        self.changed = threading.Event()  # dictation's state changed: update the key
        self.next_id = 0

    def notify(self):
        self.changed.set()

    def run(self):
        logged = False
        while True:
            try:
                s = steam.page_socket(PAGE)
            except (OSError, ValueError, StopIteration, EOFError, ConnectionError):
                time.sleep(RETRY_S)
                continue
            try:
                print("keyboard: attached to Steam's keyboard", flush=True)
                logged = False
                self._serve(s)
            except (OSError, ValueError, EOFError, ConnectionError) as e:
                if not logged:
                    print(f"keyboard: lost Steam's keyboard ({e!r}); waiting for it", file=sys.stderr, flush=True)
                    logged = True
            finally:
                s.close()
            time.sleep(RETRY_S)

    def _send(self, s, method, **params):
        self.next_id += 1
        steam._ws_send(s, json.dumps({"id": self.next_id, "method": method, "params": params}))

    def _look(self):
        st = self.state()
        return {"phase": st.get("phase", "idle"), "ready": bool(st.get("configured"))}

    def _serve(self, s):
        s.settimeout(5)
        self._send(s, "Runtime.addBinding", name=BINDING)
        self._send(s, "Runtime.enable")  # reports the current context too, which injects
        inject_at = None
        while True:
            if inject_at is not None and time.monotonic() >= inject_at:
                inject_at = None
                self._send(s, "Runtime.evaluate", expression=JS)
                self.changed.set()  # and show the current state on the new key
            if self.changed.is_set():
                self.changed.clear()
                self._send(s, "Runtime.evaluate",
                           expression=f"window.__cuffMic && window.__cuffMic.set({json.dumps(self._look())})")
            if not select.select([s], [], [], 0.2)[0]:
                continue
            opcode, payload = steam._ws_frame(s)
            if opcode == 8:
                raise ConnectionError("the debugger closed the connection")
            if opcode != 1:
                continue
            msg = json.loads(payload)
            method = msg.get("method")
            if method == "Runtime.executionContextCreated":
                if (msg["params"]["context"].get("auxData") or {}).get("isDefault"):
                    inject_at = time.monotonic() + INJECT_DELAY_S
            elif method == "Runtime.bindingCalled" and msg["params"].get("name") == BINDING:
                print("keyboard: dictation key", flush=True)
                threading.Thread(target=self.toggle, daemon=True, name="keyboard-dictate").start()
            elif "result" in msg and (msg["result"].get("exceptionDetails")):
                d = msg["result"]["exceptionDetails"]
                print(f"keyboard: script error: {(d.get('exception') or {}).get('description') or d.get('text')}",
                      file=sys.stderr, flush=True)
