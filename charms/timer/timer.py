"""Timer: a countdown, a stopwatch, and a reminder to take a break from VR.

  Timer      presets (1-30 min), -1 / +1 minute (also while running), start/pause, reset. When
             it runs out: a toast and a buzz on the wrist, and a dot on the tab until it's seen.
  Stopwatch  start/pause and reset.
  Break      how long you've been in VR (since Cuff started with SteamVR), and a nudge every
             30 / 45 / 60 / 90 minutes, or never (the default).
Its alerts glow white with a clock (clock.png) on the hand while the panel is turned away.
The tab's badge shows the countdown's time left ("5m", then seconds in the last minute).
Choices are remembered in the charm's cache dir.
"""
import json
import os
import sys
import threading
import time

from cuff import Charm
from cuff import widgets as w

PAGES = ["Timer", "Stopwatch", "Break"]
PRESETS = [1, 3, 5, 10, 15, 30]  # minutes
BREAK_EVERY = [0, 30, 45, 60, 90]  # minutes; 0 is off


def clock(seconds, tenths=False):
    seconds = max(0, seconds)
    h, rem = divmod(int(seconds), 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


class Timer(Charm):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.sub = "Timer"
        self.lock = threading.Lock()
        self.duration = 5 * 60  # the countdown's set length, seconds
        self.remaining = self.duration  # while paused
        self.ends = None  # monotonic time it runs out, while running
        self.rang = False  # ran out and not yet seen
        self.watch_total, self.watch_since = 0.0, None  # stopwatch
        self.session_start = time.monotonic()
        self.break_every = 0  # break reminders are off until chosen
        self.next_break = None
        self.state_file = os.path.join(ctx.cache_dir, "timer.json")
        self._load()
        self._schedule_break()

    # ---------------------------------------------------------------- state

    def _load(self):
        try:
            with open(self.state_file) as f:
                saved = json.load(f)
            self.duration = self.remaining = int(saved.get("duration", self.duration))
            if saved.get("break_every") in BREAK_EVERY:
                self.break_every = saved["break_every"]
        except (OSError, ValueError, TypeError):
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump({"duration": self.duration, "break_every": self.break_every}, f)
        except OSError as e:
            print(f"timer: saving: {e!r}", file=sys.stderr, flush=True)

    def _schedule_break(self):
        self.next_break = (time.monotonic() + self.break_every * 60) if self.break_every else None

    def left(self):
        return self.ends - time.monotonic() if self.ends else self.remaining

    def watch(self):
        return self.watch_total + (time.monotonic() - self.watch_since if self.watch_since else 0)

    # ---------------------------------------------------------------- the tick

    def start(self):
        threading.Thread(target=self._tick, daemon=True, name="timer").start()

    def _tick(self):
        last_badge = None
        while True:
            # Wake on the next whole second of whichever clock is shown, so it ticks evenly.
            time.sleep(1.0 - (time.monotonic() % 1.0) + 0.01)
            now = time.monotonic()
            with self.lock:
                if self.ends and now >= self.ends:
                    self.ends, self.remaining, self.rang = None, 0, True
                    self.ctx.notify("Timer done", haptic=True, icon="clock.png")
                if self.next_break and now >= self.next_break:
                    self.ctx.notify(f"In VR for {clock(now - self.session_start)}: time for a break?", haptic=True,
                                    icon="clock.png")
                    self._schedule_break()
                if self.rang and self.ctx.shown and self.sub == "Timer":
                    self.rang = False  # seen
            badge = self.badge()
            ticking = self.ends or self.watch_since or self.sub == "Break"
            if (self.ctx.shown and ticking) or badge != last_badge:
                self.ctx.changed()
            last_badge = badge

    def badge(self):
        if self.ends:
            left = self.left()
            return f"{int(left) + 1}s" if left < 59 else f"{int(left // 60) + 1}m"
        return "dot" if self.rang else None

    # ---------------------------------------------------------------- touch

    def press(self, button_id):
        kind, _, arg = button_id.partition(":")
        now = time.monotonic()
        with self.lock:
            if kind == "sub":
                self.sub = arg
                if arg == "Timer":
                    self.rang = False
            elif self.sub == "Timer":
                self.rang = False
                if kind == "preset":
                    self.duration = self.remaining = int(arg) * 60
                    self.ends = None
                    self._save()
                elif kind in ("minus", "plus"):
                    step = 60 if kind == "plus" else -60
                    if self.ends:
                        self.ends = max(now + 1, self.ends + step)
                    else:
                        self.remaining = max(60, self.remaining + step)
                elif kind == "go":
                    if self.ends:  # pause
                        self.remaining, self.ends = max(0, self.ends - now), None
                    else:
                        if self.remaining <= 0:
                            self.remaining = self.duration
                        self.ends = now + self.remaining
                elif kind == "reset":
                    self.ends, self.remaining = None, self.duration
            elif self.sub == "Stopwatch":
                if kind == "go":
                    if self.watch_since:
                        self.watch_total, self.watch_since = self.watch(), None
                    else:
                        self.watch_since = now
                elif kind == "reset":
                    self.watch_total, self.watch_since = 0.0, None
            elif kind == "every":
                self.break_every = int(arg)
                self._schedule_break()
                self._save()

    # ---------------------------------------------------------------- the page

    def sample(self):
        self.ends = time.monotonic() + 4 * 60 + 32

    def page(self):
        tabs = w.Tabs([(p, p) for p in PAGES], selected=self.sub, id="sub")
        if self.sub == "Timer":
            colour = "green" if self.ends else "amber" if self.rang else "fg"
            presets = w.Row([w.Button(f"preset:{m}", f"{m}m", style="active" if self.duration == m * 60 else "normal")
                             for m in PRESETS], height=20)
            controls = w.Row([w.Button("minus", "−1m"), w.Button("go", "Pause" if self.ends else "Start",
                                                                 style="normal" if self.ends else "active"),
                              w.Button("plus", "+1m"), w.Button("reset", "Reset")], height=32)
            return w.Column([tabs, w.Text(clock(self.left() + (0.999 if self.ends else 0)), size="big",
                                          colour=colour, align="center", grow=1), presets, controls])
        if self.sub == "Stopwatch":
            return w.Column([tabs, w.Text(clock(self.watch()), size="big", colour="green" if self.watch_since else "fg",
                                          align="center", grow=1),
                             w.Row([w.Button("go", "Pause" if self.watch_since else "Start", size="title",
                                             style="normal" if self.watch_since else "active"),
                                    w.Button("reset", "Reset", size="title")], height=32)])
        now = time.monotonic()
        note = (f"Nudge every {self.break_every} min; next in {clock(self.next_break - now)}"
                if self.next_break else "No break reminders")
        return w.Column([tabs, w.Text("In VR", size="small", colour="dim"),
                         w.Text(clock(now - self.session_start), size="big", align="center", grow=1),
                         w.Row([w.Button(f"every:{m}", f"{m}m" if m else "Off",
                                         style="active" if self.break_every == m else "normal")
                                for m in BREAK_EVERY], height=20),
                         w.Text(note, size="small", colour="dim", align="center")])
