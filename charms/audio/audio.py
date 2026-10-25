"""Audio: the system volume and where sound goes.

  volume   - and + (5 % steps) on the default output, its level, and mute
  outputs  the real outputs (built-in speakers, a USB-C adapter, Bluetooth headphones),
           the current one highlighted; tap one to send sound there
Through pactl (PipeWire's PulseAudio interface). Internal sinks (filter chains, a virtual
mic's mix) have no device.api, so they aren't listed. It reads the state every 2 s while
its page is open, and right after each tap.
"""
import json
import re
import subprocess
import threading
import time

from cuff import Charm
from cuff import widgets as w

STEP = 5  # percent
MAX_OUTPUTS = 3


def pactl(*args, timeout=3):
    try:
        return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def read_state():
    """{"volume": 0-1.5 or None, "muted", "default", "outputs": [(name, description)]}"""
    default = pactl("get-default-sink").strip()
    m = re.search(r"(\d+)%", pactl("get-sink-volume", "@DEFAULT_SINK@"))
    try:
        sinks = json.loads(pactl("-f", "json", "list", "sinks") or "[]")
    except ValueError:
        sinks = []
    outputs = [(s["name"], s.get("description") or s["name"]) for s in sinks
               if s.get("properties", {}).get("device.api")]
    return {"volume": int(m.group(1)) / 100 if m else None, "muted": "yes" in pactl("get-sink-mute", "@DEFAULT_SINK@"),
            "default": default, "outputs": outputs}


class Audio(Charm):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.state = {"volume": None, "muted": False, "default": None, "outputs": []}
        self.refresh = threading.Event()

    def start(self):
        threading.Thread(target=self._poll, daemon=True, name="audio").start()

    def _poll(self):
        while True:
            self.refresh.wait(2.0)
            self.refresh.clear()
            if not self.ctx.shown and self.state["volume"] is not None:
                continue
            state = read_state()
            if state != self.state:
                self.state = state
                self.ctx.changed()

    def press(self, button_id):
        kind, _, arg = button_id.partition(":")
        if kind == "down" or kind == "up":
            pactl("set-sink-volume", "@DEFAULT_SINK@", f"{'-' if kind == 'down' else '+'}{STEP}%")
        elif kind == "mute":
            pactl("set-sink-mute", "@DEFAULT_SINK@", "toggle")
        elif kind == "out":
            name = self.state["outputs"][int(arg)][0]
            pactl("set-default-sink", name)
        self.refresh.set()

    def sample(self):
        self.state = {"volume": 0.52, "muted": False, "default": "speaker",
                      "outputs": [("speaker", "Built-in Audio Playback"), ("usb", "USB-C Audio Adapter"),
                                  ("bt", "WF-1000XM3")]}

    def page(self):
        st = self.state
        if st["volume"] is None:
            return w.Column([w.Text("Reading the audio state…", colour="dim")])
        level = f"{round(st['volume'] * 100)}%" + (" · muted" if st["muted"] else "")
        volume = w.Row([w.Button("down", glyph="volume-down", width=40),
                        w.Column([w.Text(level, size="title", colour="dim" if st["muted"] else "fg", align="center"),
                                  w.Bar(min(1.0, st["volume"]), colour="dim" if st["muted"] else "green")],
                                 gap=3, grow=1),
                        w.Button("up", glyph="volume-up", width=40),
                        w.Button("mute", "Unmute" if st["muted"] else "Mute", width=52,
                                 style="warn" if st["muted"] else "normal")], height=32)
        outputs = [w.Button(f"out:{i}", desc, style="active" if name == st["default"] else "normal", height=20)
                   for i, (name, desc) in enumerate(st["outputs"][:MAX_OUTPUTS])]
        return w.Column([volume, w.Spacer(height=2), w.Text("Output", size="tiny", colour="dim"),
                         *(outputs or [w.Text("No outputs found", size="small", colour="dim")])], gap=4)
