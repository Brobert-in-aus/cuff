"""Performance: how the headset is keeping up, modelled on Virtual Desktop's stats view.

Streaming a PC game with Steam Link (the scene app is Steam Link's client, vrlink):
  game     from Cuff for PC (GET /stream, live from the PC's compositor): the game's
           framerate against the target, reprojection and motion smoothing (SteamVR's
           counterpart of SSW: on, forced on, in use) and why, GPU and CPU milliseconds per
           frame, and dropped frames. Without the PC app these say "Install Cuff for PC" (or
           "Cuff for PC not connected"); they never stand in headset figures.
  links    measured on the headset, for both of the Frame's links (it streams over either or
           both): home Wi-Fi, and the dongle (the Frame's own 6 GHz access point). Each with
           its band, live bitrate, ping and jitter to the PC, signal and link rate. The PC's
           address on each comes from Steam Link's command line.
  graph    the game's framerate over the last minute, against the target
  headset  its display rate and compose time, CPU / GPU / battery temperature, CPU load
Anything else running on the headset: its framerate, reprojection, an App CPU / App GPU /
Compositor stack, the graph and the headset line, all from the headset's compositor.
The tab's badge is a dot while frames drop or the framerate is under 90 % of the target.
"""
import collections
import json
import os
import re
import subprocess
import threading
import time
import urllib.request

from cuff import Charm, ui
from cuff.pc import needs_pc

Y = ui.PAGE_Y
s, FONTS = ui.s, ui.FONTS
AMBER, RED = ui.POWER_COLOUR["plugged"], ui.LOW
THERMAL = "/sys/class/thermal"
STAGE_COLOURS = [(120, 72, 150, 255), (66, 92, 160, 255), (52, 120, 128, 255)]
PC_TIMEOUT_S = 0.6
LINKS_EVERY_S = 2.0


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def run(cmd, timeout=3):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def thermals():
    """{"cpu", "gpu", "battery"}: the hottest zone of each kind, in °C (None if absent)."""
    out = {"cpu": None, "gpu": None, "battery": None}
    try:
        zones = os.listdir(THERMAL)
    except OSError:
        return out
    for z in zones:
        kind, temp = read(f"{THERMAL}/{z}/type"), read(f"{THERMAL}/{z}/temp")
        if not kind or not temp or not temp.lstrip("-").isdigit():
            continue
        c = int(temp) / 1000
        key = ("cpu" if kind.startswith(("cpu", "cpuss")) else "gpu" if kind.startswith("gpu")
               else "battery" if "bat" in kind else None)
        if key and (out[key] is None or c > out[key]):
            out[key] = c
    return out


class CpuLoad:
    def __init__(self):
        self.prev = None

    def read(self):
        """Busy share of all CPUs since the last call, 0-1."""
        line = read("/proc/stat")
        if not line:
            return None
        fields = [int(v) for v in line.splitlines()[0].split()[1:]]
        idle, total = fields[3] + fields[4], sum(fields)
        prev, self.prev = self.prev, (idle, total)
        if not prev or total == prev[1]:
            return None
        return 1 - (idle - prev[0]) / (total - prev[1])


# ---------------------------------------------------------------- the two links

def stream_addresses():
    """The PC's addresses Steam Link's client was started with (-c <ip>, one per link)."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        cmd = read(f"/proc/{pid}/cmdline")
        if cmd and cmd.split("\0")[0].endswith("vrlink"):
            args = cmd.split("\0")
            return [args[i + 1] for i, a in enumerate(args[:-1]) if a == "-c"]
    return []


def interfaces():
    """{interface: {"kind": "Wi-Fi" | "Dongle", "freq": MHz}} from `iw dev`: the managed
    interface is home Wi-Fi, the AP one is the Frame's own access point for the dongle."""
    out, name = {}, None
    for line in run(["iw", "dev"]).splitlines():
        line = line.strip()
        if line.startswith("Interface "):
            name = line.split()[1]
            out[name] = {"kind": None, "freq": None}
        elif line.startswith(("Unnamed", "phy#")):  # a block that isn't a network interface
            name = None
        elif name and line.startswith("type "):
            out[name]["kind"] = {"managed": "Wi-Fi", "AP": "Dongle"}.get(line.split()[1])
        elif name and line.startswith("channel ") and "MHz" in line:
            m = re.search(r"\((\d+) MHz\)", line)
            out[name]["freq"] = int(m.group(1)) if m else None
    return {k: v for k, v in out.items() if v["kind"]}


def station(iface):
    """The busiest station on iface (the router for Wi-Fi, the PC's dongle for the AP):
    {"rx_bytes", "signal", "rate"} (dBm and the receive link rate in Mbit/s)."""
    stations = []
    for line in run(["iw", "dev", iface, "station", "dump"]).splitlines():
        line = line.strip()
        if line.startswith("Station "):
            stations.append({"rx_bytes": 0, "signal": None, "rate": None})
            continue
        if not stations:
            continue
        cur = stations[-1]
        key, _, value = line.partition(":")
        value = value.strip()
        first = value.split()[0] if value else ""
        if key == "rx bytes" and first.isdigit():
            cur["rx_bytes"] = int(first)
        elif key in ("signal avg", "avg ack signal") and first.lstrip("-").isdigit() and int(first):
            if cur["signal"] is None or key == "avg ack signal":
                cur["signal"] = int(first)  # the dongle's own signal reads 0; its acks are the real one
        elif key == "rx bitrate":
            cur["rate"] = float(first)
    return max(stations, key=lambda st: st["rx_bytes"]) if stations else None


def iface_for(addr):
    m = re.search(r"\bdev (\S+)", run(["ip", "-o", "route", "get", addr]))
    return m.group(1) if m else None


def ping(addr):
    """(average ms, jitter ms, loss 0-1) over four pings 0.2 s apart, or None."""
    out = run(["ping", "-n", "-q", "-c", "4", "-i", "0.2", "-W", "1", addr], timeout=4)
    m = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", out)
    loss = re.search(r"([\d.]+)% packet loss", out)
    if not m:
        return None
    return float(m.group(2)), float(m.group(4)), float(loss.group(1)) / 100 if loss else 0.0


class Links(threading.Thread):
    """Every LINKS_EVERY_S while active: each link's bitrate, signal, link rate and ping.
    latest: [{"kind", "freq", "mbps", "signal", "rate", "ping", "jitter", "loss"}]."""

    def __init__(self):
        super().__init__(daemon=True, name="performance-links")
        self.active = threading.Event()
        self.latest = []
        self.prev = {}  # iface -> (monotonic, rx_bytes)

    def run(self):
        while True:
            self.active.wait()
            started = time.monotonic()
            ifaces = interfaces()
            targets = {}
            for addr in stream_addresses():
                dev = iface_for(addr)
                if dev in ifaces:
                    targets[dev] = addr
            rows = []
            for dev, info in sorted(ifaces.items(), key=lambda kv: kv[1]["kind"] != "Wi-Fi"):
                st = station(dev) or {}
                now = time.monotonic()
                prev = self.prev.get(dev)
                mbps = None
                if prev and st.get("rx_bytes") is not None and st["rx_bytes"] >= prev[1]:
                    mbps = (st["rx_bytes"] - prev[1]) * 8 / (now - prev[0]) / 1e6
                if st.get("rx_bytes") is not None:
                    self.prev[dev] = (now, st["rx_bytes"])
                p = ping(targets[dev]) if dev in targets else None
                rows.append({"kind": info["kind"], "freq": info["freq"], "mbps": mbps, "signal": st.get("signal"),
                             "rate": st.get("rate"), "ping": p[0] if p else None, "jitter": p[1] if p else None,
                             "loss": p[2] if p else None, "pc": dev in targets})
            self.latest = rows
            time.sleep(max(0.2, LINKS_EVERY_S - (time.monotonic() - started)))


# ---------------------------------------------------------------- the tab

class Performance(Charm):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.temps, self.load = {"cpu": None, "gpu": None, "battery": None}, None
        self.cpu = CpuLoad()
        self.links = Links()
        self.pc = None  # Cuff for PC's last /stream answer ({"pc": {...} or None})
        self.fps_history = collections.deque(maxlen=60)  # (fps, target, dropped) per second
        self.sample_st = None  # `cuff render`'s stand-in for ctx.stats()
        self.sample_pc_state = None

    def start(self):
        self.links.start()
        threading.Thread(target=self._tick, daemon=True, name="performance").start()

    def pc_state(self):
        return self.sample_pc_state or self.ctx.pc_state()

    def _fetch_pc(self):
        url = self.ctx.pc_url()
        if not url:
            return None
        try:
            req = urllib.request.Request(url + "/stream", headers=self.ctx.pc_headers())
            with urllib.request.urlopen(req, timeout=PC_TIMEOUT_S) as r:
                return json.load(r)
        except (OSError, ValueError):
            return None

    def _tick(self):
        last_badge = None
        while True:
            time.sleep(1.0)
            self.temps, self.load = thermals(), self.cpu.read()
            st = self.ctx.stats()
            streaming = bool(st and st.get("scene") == "vrlink")
            self.pc = self._fetch_pc() if streaming else None
            if streaming and self.ctx.shown:
                self.links.active.set()
            else:
                self.links.active.clear()
            game = (self.pc or {}).get("pc")
            if streaming and game:
                self.fps_history.append((game["fps"], game.get("target_hz"), game.get("dropped", 0)))
            elif st and not streaming:
                self.fps_history.append((st["fps"], st.get("display_hz"), st["dropped"]))
            elif streaming:
                self.fps_history.clear()  # the game's framerate is the PC's to tell
            badge = self.badge()
            if self.ctx.shown or badge != last_badge:
                self.ctx.changed()
            last_badge = badge

    def badge(self):
        recent = list(self.fps_history)[-5:]
        if not recent:
            return None
        if any(d for _, _, d in recent) or any(t and f < 0.9 * t for f, t, _ in recent):
            return "dot"
        return None

    def sample(self):
        """A Steam Link stream with Cuff for PC connected, as captured on 2026-10-25."""
        self.sample_st = {"display_hz": 144.0, "fps": 144.0, "gpu_ms": 1.76, "cpu_ms": 0.03,
                          "compositor_gpu_ms": 1.76, "dropped": 0.0, "reprojected": 144.0, "scene": "vrlink",
                          "reprojection": {"async": True}, "frame": {"compositor_gpu_ms": 1.76}}
        self.pc = {"pc": {"app": "steam.app.2981220", "fps": 143.9, "target_hz": 144.0, "gpu_ms": 3.96,
                          "cpu_ms": 1.53, "dropped": 0.0, "reprojected": 0.0, "async": True, "motion_active": False,
                          "motion_enabled": False, "motion_forced": False}}
        self.sample_pc_state = "connected"
        self.links.latest = [
            {"kind": "Wi-Fi", "freq": 5785, "mbps": 6.4, "signal": -25, "rate": 720.6, "ping": 3.4, "jitter": 2.3,
             "loss": 0.0, "pc": True},
            {"kind": "Dongle", "freq": 6135, "mbps": 0.4, "signal": -46, "rate": 1921.5, "ping": 1.9, "jitter": 0.4,
             "loss": 0.0, "pc": True}]
        self.fps_history.extend([(143.9 - (i % 7) * 0.4, 144.0, 0) for i in range(60)])
        self.temps, self.load = {"cpu": 52.8, "gpu": 44.4, "battery": 25.4}, 0.23
        if os.environ.get("CUFF_SAMPLE_NO_PC"):  # the same, without the PC app
            self.pc, self.sample_pc_state = None, "missing"
            self.fps_history.clear()

    # ---------------------------------------------------------------- drawing

    def _framerate(self, d, fps, hz, source):
        big, small = FONTS["medium"], FONTS["small"]
        if fps is None:
            d.text(s(16, Y + 2), "–", font=big, fill=ui.DIM)
            x = 16 + big.getlength("–") / ui.SCALE + 3
        else:
            ratio = fps / hz if hz else 1
            colour = ui.GREEN if ratio >= 0.95 else AMBER if ratio >= 0.8 else RED
            text = f"{fps:.0f}"
            d.text(s(16, Y + 2), text, font=big, fill=colour)
            x = 16 + big.getlength(text) / ui.SCALE + 3
        d.text(s(x, Y + 9), f"/ {hz:.0f} fps" if hz else "fps", font=small, fill=ui.DIM)
        d.text(s(224 - small.getlength(source) / ui.SCALE, Y + 9), source, font=small, fill=ui.DIM)

    @staticmethod
    def _reprojection_line(rp):
        mode = ("Motion smoothing" if rp.get("motion") else "Async reprojection" if rp.get("async") else "No reprojection")
        why = [w for w, k in (("GPU late", "gpu"), ("CPU late", "cpu"), ("throttled", "throttled")) if rp.get(k)]
        smoothing = "forced on" if rp.get("motion_forced") else "on" if rp.get("motion_enabled") else "off"
        return f"{mode} · smoothing {smoothing}" + (f" · {', '.join(why)}" if why else ""), bool(rp.get("motion") or why)

    def _line(self, d, y, text, fill=ui.DIM, font=None):
        font = font or FONTS["battery"]
        d.text(s(16, y), ui.ellipsize(text, font, 208), font=font, fill=fill)

    def _graph(self, d, box, hz, empty_text=None):
        gx0, gy0, gx1, gy1 = box
        d.rounded_rectangle(s(*box), radius=3 * ui.SCALE, fill=ui.BUTTON)
        hist = list(self.fps_history)
        if not hist and empty_text:
            tiny = FONTS["clip_small"]
            d.text(s((gx0 + gx1) / 2 - tiny.getlength(empty_text) / ui.SCALE / 2, (gy0 + gy1) / 2 - 5), empty_text,
                   font=tiny, fill=ui.DIM)
            return
        top = max([hz or 0] + [f for f, _, _ in hist] + [1]) * 1.1
        if hz:
            ty = gy1 - (gy1 - gy0) * hz / top
            d.line([tuple(s(gx0, ty)), tuple(s(gx1, ty))], fill=ui.DIM, width=1)
        if len(hist) > 1:
            pts = [(gx1 - (gx1 - gx0) * (len(hist) - 1 - i) / 59, gy1 - (gy1 - gy0) * f / top)
                   for i, (f, _, _) in enumerate(hist)]
            d.line([tuple(s(*p)) for p in pts], fill=ui.GREEN, width=int(1.5 * ui.SCALE))

    def _system(self, d, y, st):
        def deg(v):
            return f"{v:.0f}°" if v is not None else "–"
        t = self.temps
        self._line(d, y, f"CPU {deg(t['cpu'])}" + (f" {self.load * 100:.0f}%" if self.load is not None else "")
                   + f" · GPU {deg(t['gpu'])} · Battery {deg(t['battery'])}", font=FONTS["clip_small"])

    def draw(self, img, d, pressed, title=True):
        st = self.sample_st or self.ctx.stats()
        if not st:
            d.text(s(18, Y + 14), "Waiting for SteamVR's frame timing…", font=FONTS["body"], fill=ui.DIM)
            return
        if st.get("scene") == "vrlink":
            self._draw_stream(d, st)
        else:
            self._draw_local(d, st)

    def _draw_stream(self, d, st):
        tiny = FONTS["clip_small"]
        game = (self.pc or {}).get("pc")
        missing = needs_pc(self.pc_state()) if not game else None
        if game is None and not missing:
            missing = "Cuff for PC: SteamVR not running on the PC"
        # The game: the PC's to tell.
        self._framerate(d, game["fps"] if game else None, game.get("target_hz") if game else st.get("display_hz"),
                        "Steam Link")
        if game:
            line, warn = self._reprojection_line({"async": game.get("async"), "motion": game.get("motion_active"),
                                                  "motion_enabled": game.get("motion_enabled"),
                                                  "motion_forced": game.get("motion_forced")})
            self._line(d, Y + 26, line, AMBER if warn else ui.DIM)
            self._line(d, Y + 37, f"Game GPU {game['gpu_ms']:.1f} ms · CPU {game['cpu_ms']:.1f} ms · "
                                  f"Dropped {game.get('dropped', 0):.0f}/s",
                       AMBER if game.get("dropped") else ui.DIM)
        else:
            self._line(d, Y + 26, f"Game stats: {missing}", AMBER)
        # The links, measured here.
        cols = [16, 80, 110, 154, 196]
        for x, head in zip(cols, ("Link", "Mbps", "Ping ±", "Signal", "Rate")):
            d.text(s(x, Y + 49), head, font=tiny, fill=ui.DIM)
        rows = self.links.latest or [{"kind": "Wi-Fi"}, {"kind": "Dongle"}]
        for r_i, r in enumerate(rows[:2]):
            y = Y + 59 + r_i * 10
            band = (" 2.4 GHz" if r["freq"] < 3000 else " 5 GHz" if r["freq"] < 5925 else " 6 GHz") if r.get("freq") else ""
            ping = (f"{r['ping']:.1f}±{r['jitter']:.1f}" if r.get("ping") is not None
                    else "–" if r.get("pc") or not self.links.latest else "no PC")
            cells = [f"{r['kind']}{band}", f"{r['mbps']:.1f}" if r.get("mbps") is not None else "–", ping,
                     f"{r['signal']} dBm" if r.get("signal") is not None else "–",
                     f"{r['rate']:.0f}" if r.get("rate") else "–"]
            busy = r.get("mbps") is not None and r["mbps"] >= max((x.get("mbps") or 0) for x in rows)
            for i, (x, cell) in enumerate(zip(cols, cells)):
                d.text(s(x, y), cell, font=tiny, fill=ui.FG if busy or i else ui.DIM)
        # The game's framerate over the last minute.
        self._graph(d, (16, Y + 81, 224, Y + 101), game.get("target_hz") if game else None,
                    empty_text=missing)
        frame = st.get("frame") or {}
        self._line(d, Y + 104, f"Headset {st.get('display_hz', 0):.0f} Hz · compose "
                               f"{(frame.get('compositor_gpu_ms') or st.get('gpu_ms') or 0):.1f} ms",
                   font=tiny)
        self._system(d, Y + 114, st)

    def _draw_local(self, d, st):
        hz = st.get("display_hz") or 0
        self._framerate(d, st["fps"], hz, st.get("scene") or "No app")
        line, warn = self._reprojection_line(st.get("reprojection") or {})
        self._line(d, Y + 26, line, AMBER if warn else ui.DIM)
        # Where each frame's time goes, with a tick at one frame's budget.
        parts = [("App CPU", st.get("cpu_ms")), ("App GPU", st.get("gpu_ms") if st.get("scene") else None),
                 ("Compositor", st.get("compositor_gpu_ms"))]
        total = sum(ms for _, ms in parts if ms)
        budget = 1000 / hz if hz else 8.3
        scale = max(total, 2 * budget)
        bx0, bx1, by0, by1 = 16, 224, Y + 40, Y + 50
        d.rounded_rectangle(s(bx0, by0, bx1, by1), radius=3 * ui.SCALE, fill=ui.BUTTON)
        x = bx0
        for i, (_, ms) in enumerate(parts):
            if ms:
                w = (bx1 - bx0) * ms / scale
                d.rectangle(s(x, by0, x + w, by1), fill=STAGE_COLOURS[i])
                x += w
        bud = bx0 + (bx1 - bx0) * budget / scale
        d.rectangle(s(bud - 0.5, by0 - 2, bud + 0.5, by1 + 2), fill=ui.FG)
        tiny = FONTS["clip_small"]
        cols = parts + [("Total", total or None)]
        cw = (bx1 - bx0) / len(cols)
        for i, (name, ms) in enumerate(cols):
            cx = bx0 + cw * i + cw / 2
            c = STAGE_COLOURS[i] if i < len(parts) else ui.FG
            for row, (txt, fill) in enumerate(((name, c if ms else ui.DIM),
                                               (f"{ms:.1f}" if ms else "–", ui.FG if ms else ui.DIM))):
                d.text(s(cx - tiny.getlength(txt) / ui.SCALE / 2, Y + 53 + row * 10), txt, font=tiny, fill=fill)
        self._graph(d, (16, Y + 76, 224, Y + 100), hz)
        self._line(d, Y + 104, f"Dropped {st['dropped']:.0f}/s · Reprojected {st['reprojected']:.0f}/s",
                   AMBER if st["dropped"] else ui.DIM, font=tiny)
        self._system(d, Y + 114, st)
