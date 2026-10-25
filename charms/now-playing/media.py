"""Now playing and transport over MPRIS: whatever is playing first, then Spotify.

All D-Bus work runs on one worker thread: it polls the best player once a second and runs
queued commands, so the overlay loop never blocks.

On the Steam Frame, Spotify is best run as a headless Spotify Connect speaker: Spotify's own
Soloist (with soloist-mpris for MPRIS), or spotifyd where Spotify still serves librespot. Pick
it in the Spotify app on a phone or PC, and this controls it over MPRIS from then on.
"""
import io
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

import dbus
from PIL import Image

MPRIS = "org.mpris.MediaPlayer2."
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
ART_PX = 128
IDLE = {"title": "Nothing playing", "artist": "", "playing": False,
        "player": None, "source": None, "spotify": False, "art": None, "volume": None}
VOLUME_STEP = 0.05


# ---------------------------------------------------------------- D-Bus / MPRIS

def nested_bus_address():
    """Session bus of the nested Plasma desktop, if it is running."""
    try:
        pid = subprocess.run(["pgrep", "-u", str(os.getuid()), "-x", "plasmashell"],
                             capture_output=True, text=True).stdout.split()[0]
        with open(f"/proc/{pid}/environ", "rb") as f:
            for kv in f.read().split(b"\0"):
                if kv.startswith(b"DBUS_SESSION_BUS_ADDRESS="):
                    return kv.split(b"=", 1)[1].decode()
    except (IndexError, OSError):
        pass
    return None


_buses = {}


def buses():
    addrs = [os.environ.get("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")]
    nested = nested_bus_address()
    if nested and nested not in addrs:
        addrs.append(nested)
    out = []
    for a in addrs:
        bus = _buses.get(a)
        if bus is None:
            try:
                bus = _buses[a] = dbus.bus.BusConnection(a)
            except dbus.DBusException:
                continue
        out.append(bus)
    return out


SOLOIST = os.path.expanduser("~/.local/bin/soloist")
ACTIVE_EVERY_S = 3.0
_soloist = [0.0, False]  # when last asked, whether this is Spotify's active device


def soloist_active():
    """Whether Soloist is the device Spotify plays on now (soloist-mpris keeps its MPRIS name,
    and the last track, while another device plays). Asked at most every ACTIVE_EVERY_S."""
    now = time.monotonic()
    if now - _soloist[0] >= ACTIVE_EVERY_S:
        try:
            out = subprocess.run([SOLOIST, "ctl", "status"], capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        _soloist[:] = [now, "active device: yes" in out]
    return _soloist[1]


def soloist_name():
    """The name Soloist shows in Spotify's device list, or None if Soloist isn't set up here
    (soloist-mpris keeps it in ~/.config/soloist/env, defaulting to the hostname)."""
    if not os.path.exists(SOLOIST):
        return None
    try:
        with open(os.path.expanduser("~/.config/soloist/env")) as f:
            names = [line.split("=", 1)[1].strip() for line in f if line.startswith("SOLOIST_DEVICE_NAME=")]
    except OSError:
        names = []
    return (names[-1] if names else "") or socket.gethostname()


def is_spotify(name):
    """A Spotify Connect speaker on this headset: Soloist (via soloist-mpris) or spotifyd."""
    return ".soloist" in name or ".spotify" in name


def player_rank(name, status):
    # Whatever is playing first, then paused, then the rest; within that, Spotify (a Connect
    # speaker here), then Chromium (the web player), then anything else.
    kind = 0 if is_spotify(name) else 1 if ".chromium" in name else 2
    return ({"Playing": 0, "Paused": 1}.get(status, 2), kind)


def find_player():
    """Return (bus, name, props) of the best MPRIS player, or None. Soloist counts only while
    it's Spotify's active device."""
    best = None
    for bus in buses():
        try:
            names = [n for n in bus.list_names() if n.startswith(MPRIS)]
        except dbus.DBusException:
            _buses.clear()
            continue
        for name in names:
            if ".soloist" in name and not soloist_active():
                continue
            try:
                obj = bus.get_object(name, "/org/mpris/MediaPlayer2")
                props = obj.GetAll(PLAYER_IFACE, dbus_interface="org.freedesktop.DBus.Properties")
            except dbus.DBusException:
                continue
            rank = player_rank(name, str(props.get("PlaybackStatus", "")))
            if best is None or rank < best[0]:
                best = (rank, bus, name, props)
    return best[1:] if best else None


# ---------------------------------------------------------------- album art

def sandboxed_path(path, owner_pid):
    """Flatpak Chromium writes art to its private /tmp. The bus name's owner is the
    flatpak D-Bus proxy, outside the sandbox, so look through the browser processes' roots."""
    pids = [owner_pid] if owner_pid else []
    for p in os.listdir("/proc"):
        if p.isdigit():
            try:
                with open(f"/proc/{p}/comm") as f:
                    if f.read().strip() in ("chrome", "chromium", "chromium-browse"):
                        pids.append(int(p))
            except OSError:
                pass
    for pid in pids:
        candidate = f"/proc/{pid}/root{path}"
        if os.path.exists(candidate):
            return candidate
    return path


def load_art(url, owner_pid):
    """Square RGBA art at ART_PX, or None."""
    if url.startswith("file://"):
        path = urllib.parse.unquote(url[7:])
        if not os.path.exists(path):
            path = sandboxed_path(path, owner_pid)
        with open(path, "rb") as f:
            data = f.read()
    else:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = r.read()
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    return img.crop((left, top, left + side, top + side)).resize((ART_PX, ART_PX), Image.LANCZOS)


# ---------------------------------------------------------------- worker

class Media(threading.Thread):
    """state: {"title", "artist", "playing", "player", "source", "spotify", "art", "volume"}
    (source: "Spotify" or the player's own name; spotify: a Spotify Connect speaker here is the
    player; art is a PIL image or None; volume is the player's own 0-1, or None if it has none)."""

    def __init__(self, on_change):
        super().__init__(daemon=True, name="media")
        self.on_change = on_change
        self.commands = queue.Queue()
        self.state = self.idle()
        self._art_url, self._art = None, None
        self._identities = {}  # bus name -> its MPRIS Identity

    def command(self, cmd):
        """play-pause | next | previous | volume-up | volume-down"""
        self.commands.put(cmd)

    def run(self):
        while True:
            try:
                cmd = self.commands.get(timeout=1.0)
            except queue.Empty:
                cmd = None
            try:
                if cmd:
                    self._run(cmd)
                self._poll()
            except dbus.DBusException as e:
                print(f"media: dbus: {e}", file=sys.stderr, flush=True)
                _buses.clear()
            except Exception as e:  # noqa: BLE001 - never let the worker die
                print(f"media: {e!r}", file=sys.stderr, flush=True)

    def _run(self, cmd):
        found = find_player()
        if found is None:
            return
        bus, name, props = found
        obj = bus.get_object(name, "/org/mpris/MediaPlayer2")
        if cmd in ("volume-up", "volume-down"):
            # The player's own volume (spotifyd: the same one as the Spotify app's slider),
            # separate from the system sink. Players without a writable Volume ignore this.
            if "Volume" in props:
                step = VOLUME_STEP if cmd == "volume-up" else -VOLUME_STEP
                volume = min(1.0, max(0.0, round(float(props["Volume"]) + step, 2)))
                obj.Set(PLAYER_IFACE, "Volume", dbus.Double(volume),
                        dbus_interface="org.freedesktop.DBus.Properties")
            return
        method = {"play-pause": "PlayPause", "next": "Next", "previous": "Previous"}[cmd]
        getattr(obj, method)(dbus_interface=PLAYER_IFACE)

    @staticmethod
    def idle():
        """The state with nothing playing: a hint to pick Soloist, only if it's set up here."""
        name = soloist_name()
        return {**IDLE, **({"artist": f"Pick “{name}” in the Spotify app"} if name else {})}

    def _identity(self, bus, name):
        """The player's own name ("Chromium", "VLC"), from MPRIS Identity; else its bus name."""
        if name not in self._identities:
            try:
                obj = bus.get_object(name, "/org/mpris/MediaPlayer2")
                self._identities[name] = str(obj.Get("org.mpris.MediaPlayer2", "Identity",
                                                     dbus_interface="org.freedesktop.DBus.Properties"))
            except dbus.DBusException:
                self._identities[name] = name[len(MPRIS):].split(".")[0].capitalize()
        return self._identities[name]

    def _poll(self):
        found = find_player()
        if not found:
            state = self.idle()
        else:
            bus, name, props = found
            md = props.get("Metadata", {})
            artists = md.get("xesam:artist", [])
            artist = ", ".join(str(a) for a in artists) if isinstance(artists, list) else str(artists)
            url = str(md.get("mpris:artUrl", ""))
            if url != self._art_url:
                self._art_url, self._art = url, None
                if url:
                    try:
                        owner = int(bus.call_blocking(
                            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                            "GetConnectionUnixProcessID", "s", (name,)))
                    except dbus.DBusException:
                        owner = None
                    try:
                        self._art = load_art(url, owner)
                    except Exception as e:  # noqa: BLE001 - art is cosmetic
                        print(f"media: art: {e}", file=sys.stderr, flush=True)
            spotify = is_spotify(name)
            source = "Spotify" if spotify else self._identity(bus, name)
            state = {"title": " ".join(str(md.get("xesam:title", "")).split()) or source,
                     "artist": " ".join((artist or str(md.get("xesam:album", ""))).split()),
                     "playing": str(props.get("PlaybackStatus", "")) == "Playing",
                     "player": name[len(MPRIS):], "source": source, "spotify": spotify, "art": self._art,
                     "volume": float(props["Volume"]) if "Volume" in props else None}
        if state != self.state:
            self.state = state
            self.on_change()
