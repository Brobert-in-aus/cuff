"""What the core offers every charm through its Context: toasts and frame statistics.

Toasts: a one-line banner in its own row above the panel for SHOW_S. An alert (haptic=True)
also buzzes the left controller (the wrist the panel is on), plays a chime, and, while the
panel isn't on show, lights a glow beyond the controller: for news worth a glance, a timer
going off, a break reminder, the headset's power going. Each charm gets at most one per MIN_GAP_S, and only MAX_QUEUE
wait their turn; the rest are dropped (and logged).

Frame statistics: the overlay loop samples SteamVR's compositor once a second (charm code
never touches OpenVR); stats() is the latest sample, history() the last HISTORY of them.
"""
import collections
import json
import os
import sys
import threading
import time

SHOW_S = 4.0
MIN_GAP_S = 3.0
MAX_QUEUE = 3
BUZZ = (0.0, 0.25, 0.5)  # seconds after the toast appears: three short pulses
HISTORY = 60


class Prefs:
    """How alerts get your attention, switched in Settings > Alerts and kept in
    ~/.config/cuff/alerts.json: haptic (buzz the wrist), sound (the chime), orb (on the hand
    while the panel is turned away), power (alert when the headset stops charging); and low
    battery alerts per device (devices). Defaults on; [alerts] in config.toml can change the
    defaults."""

    DEFAULTS = {"haptic": True, "sound": True, "orb": True, "power": True}
    # Low battery alerts per device: on or off, and two levels (percent; 0 is off). Steam warns
    # about the headset and controllers itself near the end (see app.BatteryWatch), so those
    # get one earlier level by default; nothing else watches the headphones.
    DEVICES = {"headset": {"on": True, "levels": [20, 0]}, "controllers": {"on": True, "levels": [20, 0]},
               "headphones": {"on": True, "levels": [20, 5]}}
    LEVEL_STEP, LEVEL_MAX = 5, 50

    def __init__(self):
        from .config import CONFIG, CONFIG_FILE
        self.file = os.path.join(os.path.dirname(CONFIG_FILE), "alerts.json")
        conf = CONFIG.get("alerts", {})
        self.values = {**self.DEFAULTS, **{k: bool(v) for k, v in conf.items() if k in self.DEFAULTS}}
        self.devices = {d: {"on": v["on"], "levels": list(v["levels"])} for d, v in self.DEVICES.items()}
        for d in self.devices:  # [alerts] headset = [20, 5] in config.toml changes a default
            if isinstance(conf.get(d), list):
                self.devices[d]["levels"] = (sorted((int(p) for p in conf[d]), reverse=True) + [0, 0])[:2]
        try:
            with open(self.file) as f:
                saved = json.load(f)
            self.values.update({k: bool(v) for k, v in saved.items() if k in self.DEFAULTS})
            for d, v in saved.get("devices", {}).items():
                if d in self.devices:
                    self.devices[d] = {"on": bool(v.get("on", True)),
                                       "levels": [int(x) for x in (list(v.get("levels", [])) + [0, 0])[:2]]}
        except (OSError, ValueError, TypeError):
            pass

    def get(self, key):
        return self.values.get(key, True)

    def toggle(self, key):
        self.values[key] = not self.get(key)
        self._save()

    def device(self, name):
        """{"on", "levels": [higher, lower]} for a device's low battery alerts."""
        return self.devices[name]

    def toggle_device(self, name):
        self.devices[name]["on"] = not self.devices[name]["on"]
        self._save()

    def step_level(self, name, index, steps):
        levels = self.devices[name]["levels"]
        levels[index] = max(0, min(self.LEVEL_MAX, levels[index] + steps * self.LEVEL_STEP))
        self._save()

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.file), exist_ok=True)
            with open(self.file, "w") as f:
                json.dump({**self.values, "devices": self.devices}, f)
        except OSError as e:
            print(f"alerts: saving: {e!r}", file=sys.stderr, flush=True)


class Toasts:
    def __init__(self, changed, prefs=None):
        self.changed = changed
        self.prefs = prefs or Prefs()
        self.lock = threading.Lock()
        self.queue = collections.deque()  # (source, text, haptic, glow, icon)
        self.showing = None  # (source, text, until)
        self.last = {}  # source -> when it last pushed
        self.buzz = collections.deque()  # monotonic times the overlay loop should pulse
        self.alerts = collections.deque(maxlen=8)  # (glow, icon) per alert shown, for the orbs and chime
        self.alert_count = 0  # alerts shown so far (alerts keeps only the last 8)
        self.pinned = {}  # source -> text that shows whenever no other toast does

    def pin(self, source, text):
        """Keep text up until unpin(source): for something still waiting on the user, such
        as a pairing code. Other toasts show over it, then it comes back."""
        with self.lock:
            self.pinned[source] = text
        self.changed()

    def unpin(self, source):
        with self.lock:
            self.pinned.pop(source, None)
        self.changed()

    def push(self, source, text, haptic=False, glow="white", icon=None):
        """glow ("white", "orange", "red") and icon (an RGBA image, or None): the alert's orb."""
        now = time.monotonic()
        with self.lock:
            if now - self.last.get(source, -MIN_GAP_S) < MIN_GAP_S or len(self.queue) >= MAX_QUEUE:
                print(f"toast from {source} dropped: {text!r}", file=sys.stderr, flush=True)
                return
            self.last[source] = now
            self.queue.append((source, text, haptic, glow, icon))
            if self.showing is None:
                self._next(now)
        self.changed()

    def glow(self, glow="white", icon=None):
        """An extra glow for the row on the hand, with no toast of its own (the settings test
        shows three at once; the buzz and chime come with the toast that goes with it)."""
        with self.lock:
            self.alerts.append((glow, icon))
            self.alert_count += 1

    def _next(self, now):
        """Show the next queued toast (lock held)."""
        if not self.queue:
            self.showing = None
            return
        source, text, haptic, glow, icon = self.queue.popleft()
        self.showing = (source, text, now + SHOW_S)
        if haptic:
            if self.prefs.get("haptic"):
                self.buzz.extend(now + t for t in BUZZ)
            self.alerts.append((glow, icon))
            self.alert_count += 1
        timer = threading.Timer(SHOW_S + 0.05, self._expire)
        timer.daemon = True
        timer.start()

    def _expire(self):
        with self.lock:
            if self.showing and time.monotonic() >= self.showing[2]:
                self._next(time.monotonic())
        self.changed()

    def current(self):
        """(source, text) on show, else a pinned one, or None."""
        with self.lock:
            if self.showing:
                return self.showing[:2]
            return next(reversed(self.pinned.items()), None)

    def due_buzz(self, now):
        """How many pulses are due now (the overlay loop calls this each frame)."""
        n = 0
        while self.buzz and self.buzz[0] <= now:
            self.buzz.popleft()
            n += 1
        return n


def _comm(pid):
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return None


class FrameStats:
    """Per-second rates from the compositor's cumulative counters, plus the latest frame's
    own timing. A sample is
      time, display_hz, fps            frames per second against the display's refresh
      gpu_ms, cpu_ms, compositor_gpu_ms  milliseconds per frame (the scene app, or the
                                         compositor itself with none running)
      dropped, reprojected             frames per second
      scene                            the scene app: "vrlink" when streaming from a PC with
                                       Steam Link, else its app key or process name; or None
      reprojection                     {"async", "motion", "motion_enabled", "motion_forced",
                                        "cpu", "gpu", "throttled"}: the latest frame's flags
      frame                            {"interval_ms", "transfer_ms", "present_ms",
                                        "compositor_cpu_ms", "compositor_gpu_ms"} for it"""

    def __init__(self):
        self.prev = None
        self.samples = collections.deque(maxlen=HISTORY)

    @staticmethod
    def _flags(openvr, flags):
        get = lambda name: bool(flags & getattr(openvr, name, 0))  # noqa: E731
        return {"async": get("VRCompositor_ReprojectionAsync"), "motion": get("VRCompositor_ReprojectionMotion"),
                "motion_enabled": get("VRCompositor_ReprojectionMotion_Enabled"),
                "motion_forced": get("VRCompositor_ReprojectionMotion_ForcedOn"),
                "cpu": get("VRCompositor_ReprojectionReason_Cpu"), "gpu": get("VRCompositor_ReprojectionReason_Gpu"),
                "throttled": get("VRCompositor_ReprojectionMotion_AppThrottled")}

    def sample(self, comp, system, openvr, apps=None):
        """Called by the overlay loop about once a second."""
        now = time.monotonic()
        scene = None
        if apps is not None:
            pid = apps.getCurrentSceneProcessId()
            if pid:
                # The app key is reliable ("vrlink.client" for Steam Link's client, whose process
                # name is a thread's, "vrlinkrunthread"); the process name is the fallback.
                try:
                    key = apps.getApplicationKeyByProcessId(pid) or ""
                except openvr.error_code.OpenVRError:
                    key = ""
                scene = "vrlink" if key.startswith("vrlink") else key or _comm(pid)
        ok, f = comp.getFrameTiming(0)
        frame = reprojection = None
        if ok:
            reprojection = self._flags(openvr, f.m_nReprojectionFlags)
            frame = {"interval_ms": f.m_flClientFrameIntervalMs, "transfer_ms": f.m_flTransferLatencyMs,
                     "present_ms": f.m_flPresentCallCpuMs, "compositor_cpu_ms": f.m_flCompositorRenderCpuMs,
                     "compositor_gpu_ms": f.m_flCompositorRenderGpuMs}
        c = comp.getCumulativeStats(openvr.sizeof(openvr.Compositor_CumulativeStats))
        c = c[-1] if isinstance(c, tuple) else c
        try:
            hz = system.getFloatTrackedDeviceProperty(openvr.k_unTrackedDeviceIndex_Hmd,
                                                      openvr.Prop_DisplayFrequency_Float)
        except openvr.error_code.OpenVRError:
            hz = None
        cur = (now, c.m_nNumFrameSubmits, c.m_nNumDroppedFrames, c.m_nNumReprojectedFrames,
               c.m_flSumApplicationGPUTimeMS, c.m_flSumApplicationCPUTimeMS, c.m_flSumCompositorGPUTimeMS)
        prev, self.prev = self.prev, cur
        if prev is None or cur[1] < prev[1]:  # first sample, or a new scene app reset the counters
            return
        dt, frames = cur[0] - prev[0], cur[1] - prev[1]
        if dt <= 0:
            return
        per = (lambda i: (cur[i] - prev[i]) / frames) if frames else (lambda i: None)
        self.samples.append({"time": now, "display_hz": hz, "fps": frames / dt,
                             "gpu_ms": per(4), "cpu_ms": per(5) or None, "compositor_gpu_ms": per(6),
                             "dropped": max(0, cur[2] - prev[2]) / dt, "reprojected": max(0, cur[3] - prev[3]) / dt,
                             "scene": scene, "reprojection": reprojection, "frame": frame})

    def latest(self):
        return self.samples[-1] if self.samples else None

    def history(self):
        return list(self.samples)


class Services:
    def __init__(self, changed, pc=None):
        self.prefs = Prefs()
        self.toasts = Toasts(changed, self.prefs)
        self.frames = FrameStats()
        self.pc = pc
