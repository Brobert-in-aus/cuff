"""The PC's live view of the stream's link, from the Steam Link driver's own log
(Steam/logs/driver_vrlink.txt), read as it grows, once a second:

  Link BW: Est <n> Mbit/s (auto: 0|1)        SteamVR's link estimate, about every 30 s
  Link BW hist: w <bucket> <c0>,<c1>,...     the capacity it measured, in <bucket>-Mbit/s bins
  THROTTLE EVENT: <us>                       a frame held back for the link (1 s or more: a stall)
  Bad link event: d=<ms> ms <a> mbit/s -> <b> mbit/s, ping <ms> ms, ...
                                             the link faltered; b < a is a bitrate step-down
  Connection active|inactive at <s>          the headset joined or left
  Reset video stream ...                     the stream restarted from a keyframe
  [Perf warning] <ms>ms - videonvenc::... /  the encoder ran late
  Perf warning: Getting from SubmitFrame to StartEncode took <ms>ms

The line formats were worked out by SteamFrame Advanced Options (MIT,
github.com/elliotttate/SteamFrame-Advanced-Options, StreamingHealth.cs) and checked against
this PC's logs on 2026-10-26. Steam rotates the log to driver_vrlink.previous.txt when SteamVR
starts; a shrunk or rewritten file is read again from the top.

  python linklog.py       print one answer from the current log
"""
import collections
import datetime
import json
import os
import re
import sys
import threading
import time

SAMPLE_S = 1.0
WINDOW_S = 60.0  # the per-minute counts
SEED_BYTES = 512 * 1024  # how far back the first read looks, for the last estimate
MAX_READ = 256 * 1024  # per poll; a burst beyond that is skipped, not queued
STALE_S = 120.0  # an estimate or bitrate older than this is from another stream
STALL_US = 1_000_000

LINE = re.compile(r"^(\w{3} \w{3} +\d+ \d{4} \d\d:\d\d:\d\d\.\d+) \[\w+\] - (.*)$")
ESTIMATE = re.compile(r"Link BW: Est ([\d.]+) Mbit/s \(auto: (\d)\)")
HIST = re.compile(r"Link BW hist: w ([\d.]+)\s+([\d,]+)")
THROTTLE = re.compile(r"THROTTLE EVENT: (\d+)")
BAD_LINK = re.compile(r"Bad link event: d=([\d.]+) ms ([\d.]+) mbit/s -> ([\d.]+) mbit/s, ping ([\d.]+) ms")
CONNECTION = re.compile(r"Connection (active|inactive) at")
RESET = re.compile(r"Reset video stream because")
ENCODER_LATE = re.compile(r"\[Perf warning\] [\d.]+ ?ms - (?:Encode Time|videonvenc::)|Perf warning: Getting from SubmitFrame")


def steam_dir():
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                return winreg.QueryValueEx(k, "SteamPath")[0]
        except OSError:
            pass
        return r"C:\Program Files (x86)\Steam"
    return os.path.expanduser("~/.steam/steam")


def log_time(stamp):
    try:
        return datetime.datetime.strptime(" ".join(stamp.split()), "%a %b %d %Y %H:%M:%S.%f").timestamp()
    except ValueError:
        return None


def capacity_p10(width, counts):
    """The capacity the link reached 90 % of the time, in Mbit/s (bins are `width` wide)."""
    total = sum(counts)
    if not total:
        return None
    seen = 0
    for i, c in enumerate(counts):
        seen += c
        if seen >= total * 0.1:
            return i * width
    return None


class LinkLog(threading.Thread):
    """latest(): the link as the driver logs it (see snapshot's keys), or None without a log."""

    def __init__(self, path=None):
        super().__init__(daemon=True, name="link-log")
        self.path = path or os.path.join(steam_dir(), "logs", "driver_vrlink.txt")
        self.lock = threading.Lock()
        self.offset, self.anchor = None, b""
        self.reset_state()

    def reset_state(self):
        self.estimate = None  # (t, mbps, auto)
        self.capacity = None  # (t, p10 Mbit/s)
        self.bitrate = None  # (t, mbps after the last bad-link event, ping ms)
        self.connected = None
        self.events = collections.deque()  # (t, kind, value)
        self.newest = None  # the last line's time

    def run(self):
        while True:
            try:
                self.poll()
            except OSError:
                pass
            time.sleep(SAMPLE_S)

    def poll(self):
        with open(self.path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            head = f.read(64)
            if self.offset is None or size < self.offset or head != self.anchor:
                with self.lock:
                    self.reset_state()
                self.anchor = head
                self.offset = max(0, size - SEED_BYTES)
            if size - self.offset > MAX_READ:
                self.offset = size - MAX_READ
            f.seek(self.offset)
            data = f.read(size - self.offset)
        end = data.rfind(b"\n") + 1  # a line still being written waits for the next poll
        self.offset += end
        lines = data[:end].decode("utf-8", "replace").splitlines()  # a partial first line won't match LINE
        with self.lock:
            for line in lines:
                self.parse(line)

    def parse(self, line):
        m = LINE.match(line)
        if not m:
            return
        t, body = log_time(m.group(1)), m.group(2)
        if t is None:
            return
        self.newest = t
        if m2 := THROTTLE.search(body):
            us = int(m2.group(1))
            self.events.append((t, "stall" if us >= STALL_US else "held", us / 1000))
        elif m2 := BAD_LINK.search(body):
            before, after, ping = float(m2.group(2)), float(m2.group(3)), float(m2.group(4))
            self.events.append((t, "step_down" if after < before else "bad_link", before - after))
            self.bitrate = (t, after, ping)
        elif m2 := ESTIMATE.search(body):
            self.estimate = (t, float(m2.group(1)), m2.group(2) == "1")
        elif m2 := HIST.search(body):
            counts = [int(c) for c in m2.group(2).split(",") if c]
            self.capacity = (t, capacity_p10(float(m2.group(1)), counts))
        elif m2 := CONNECTION.search(body):
            self.connected = m2.group(1) == "active"
        elif RESET.search(body):
            self.events.append((t, "reset", 0))
        elif ENCODER_LATE.search(body):
            self.events.append((t, "encoder_late", 0))

    def latest(self, now=None):
        """now: the time to count back from (default the clock; the log's last line for an old log)."""
        if self.offset is None:
            return None
        now = now or time.time()
        with self.lock:
            while self.events and self.events[0][0] < now - WINDOW_S:
                self.events.popleft()
            count = collections.Counter(kind for _, kind, _ in self.events)
            held = [v for _, kind, v in self.events if kind == "held"]

            def fresh(v):
                return v if v and v[0] > now - STALE_S else None
            est, cap, rate = fresh(self.estimate), fresh(self.capacity), fresh(self.bitrate)
            return {
                "connected": self.connected,
                "estimate": est[1] if est else None,  # SteamVR's link estimate, Mbit/s
                "auto": est[2] if est else None,  # automatic bitrate
                "capacity_p10": cap[1] if cap else None,  # what the link carried 90 % of the time
                "bitrate": rate[1] if rate else None,  # the video bitrate at the last bad-link event
                "ping": rate[2] if rate else None,
                # per minute:
                "held": count["held"], "held_worst_ms": round(max(held), 1) if held else None,
                "stalls": count["stall"], "bad_link": count["bad_link"], "step_downs": count["step_down"],
                "resets": count["reset"], "encoder_late": count["encoder_late"],
            }


MONITOR = LinkLog()


if __name__ == "__main__":
    log = LinkLog(sys.argv[1] if len(sys.argv) > 1 else None)
    log.poll()
    print(json.dumps(log.latest(log.newest if len(sys.argv) > 1 else None), indent=1))
