"""Quick actions for the panel's fixed second row. Dictation lives in dictation.py and the
OpenVR ones in app.py; mic mute and recenter run here, on one worker thread so a slow call
never stalls the overlay loop.

- Mic mute: pactl on the physical mic (the profile's real_mic), so anything mixed into a
  virtual mic downstream of it still gets through.
- Passthrough runs in app.py: SteamVR "Room View" through SteamVR's own mailbox
  (vr.Mailbox), with its real state read back from SteamVR's settings.
- Recenter: vrcmd --recenter. (Screenshot runs in app.py: OpenVR's
  TakeStereoScreenshot, as vrcmd --screenshot does nothing on the Frame.)
- Keyboard runs in app.py: ShowKeyboardForOverlay on the visible window's overlay.
- Dictation server: the [dictation] /health URLs, so the button can show it's down.
"""
import queue
import subprocess
import sys
import threading
import time
import urllib.request

from . import dictation
from .config import PROFILE
from .librepods import LibrePods

REAL_MIC = PROFILE["real_mic"]
VRCMD = PROFILE["vrcmd"]


class Quick(threading.Thread):
    """state: {"mic_muted", "passthrough", "passthrough_ok", "keyboard", "keyboard_target", "stt_up"}
    (bool, or None while unknown), plus "earbuds": a Bluetooth audio device's battery 0-1, or None,
    and "earbuds_charging".
    passthrough_ok is False once the passthrough route has failed: the button is greyed out."""

    POLL = 2.0  # mic mute state
    STT_POLL = 15.0
    BT_POLL = 3.0  # Bluetooth audio battery (earbuds send it only now and then; follow it quickly)

    def __init__(self, on_change):
        super().__init__(daemon=True, name="quick")
        self.on_change = on_change
        self.actions = queue.Queue()
        self.state = {"mic_muted": None, "passthrough": False, "passthrough_ok": None, "keyboard": False, "keyboard_target": None,
                      "stt_up": None, "earbuds": None, "earbuds_charging": False}
        self._stt_at = self._bt_at = 0.0
        self.librepods = LibrePods()  # AirPods' battery, which BlueZ doesn't have

    def _set(self, **changes):
        new = {**self.state, **changes}
        if new != self.state:
            self.state = new
            self.on_change()

    def act(self, name):
        """mic | recenter (passthrough, keyboard and screenshot run in app.py)"""
        self.actions.put(name)

    def set_passthrough(self, shown, ok=None):
        """The Room View state (from SteamVR's settings, or the press about to change it), and
        ok=False once the route has failed."""
        self._set(passthrough=shown, **({} if ok is None else {"passthrough_ok": ok}))

    def set_keyboard(self, shown):
        """From SteamVR's global keyboard opened/closed events."""
        self._set(keyboard=shown)

    def set_countdown(self, seconds):
        """The screenshot's countdown (3, 2, 1) on its button, or None."""
        self._set(countdown=seconds)

    def set_keyboard_target(self, available):
        """Whether a window is visible for the keyboard to type into (else it's greyed out)."""
        self._set(keyboard_target=available)

    def run(self):
        self.librepods.start()
        while True:
            try:
                name = self.actions.get(timeout=self.POLL)
            except queue.Empty:
                name = None
            try:
                if name:
                    self._run(name)
                self._poll()
            except Exception as e:  # noqa: BLE001 - never let the worker die
                print(f"quick: {name or 'poll'}: {e!r}", file=sys.stderr, flush=True)

    def _run(self, name):
        if name == "mic":
            subprocess.run(["pactl", "set-source-mute", REAL_MIC, "toggle"], check=True)
        elif name == "recenter":
            subprocess.run([VRCMD, "--recenter"], capture_output=True, timeout=10)

    def _poll(self):
        out = subprocess.run(["pactl", "get-source-mute", REAL_MIC], capture_output=True, text=True).stdout
        self._set(mic_muted="yes" in out if out else None)
        now = time.monotonic()
        if now - self._stt_at > self.STT_POLL:
            self._stt_at = now
            self._set(stt_up=self._stt_reachable())
        if now - self._bt_at > self.BT_POLL:
            self._bt_at = now
            level, charging = self._earbud_battery() or (None, False)
            self._set(earbuds=level, earbuds_charging=charging)

    def _earbud_battery(self):
        """(battery 0-1, charging) of a connected Bluetooth audio device, or None. One figure for
        the pair: BlueZ's Battery1 where the device gives one (e.g. WF-1000XM3 over HFP), else
        LibrePods' report for AirPods (the lower bud in use)."""
        try:
            import dbus
            bus = dbus.SystemBus()
            objects = dbus.Interface(bus.get_object("org.bluez", "/"),
                                     "org.freedesktop.DBus.ObjectManager").GetManagedObjects()
        except Exception:  # noqa: BLE001 - no BlueZ, no earbuds
            return None
        audio = [ifaces for ifaces in objects.values()
                 if ifaces.get("org.bluez.Device1", {}).get("Connected")
                 and str(ifaces["org.bluez.Device1"].get("Icon", "audio")).startswith("audio")]
        for ifaces in audio:
            bat = ifaces.get("org.bluez.Battery1")
            if bat:
                return int(bat["Percentage"]) / 100, False
        if any("AirPods" in str(ifaces["org.bluez.Device1"].get("Name", "")) for ifaces in audio):
            return self.librepods.battery()
        return None

    @staticmethod
    def _stt_reachable():
        """None with no health URLs to check."""
        urls = dictation.health_urls()
        if not urls:
            return None
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=2):
                    return True
            except OSError:
                continue
        return False
