"""How good the connections between this PC and the headset are, for the app's Connection page.

Two links can join them, and each is measured from both ends:

- **Directions:** on the headset, iw's "tx" is what the headset sends: to your router on
  wlan0, and to the dongle on wlanap (it's the access point there).
- **The dongle:** the Frame runs its own access point (wlanap, 10.35.78.0/24) and the PC's
  Valve dongle joins it as a Wi-Fi adapter.
  - From the PC: Windows' WLAN API (band, channel, signal, the adapter's rates) for the
    adapter on a "frame_" network, and its USB speed (usbdongle.py, via stream.py).
  - From the headset: `iw dev wlanap station dump` (the dongle as the AP sees it: signal of
    its acks, both bitrates, retries and failures) and `iw dev wlanap info` (channel, width).
- **Your network:** the headset's own Wi-Fi to your router (`iw dev wlan0 link`: network,
  band, signal, bitrates), reached from the PC over the LAN.
- **Round trip:** 4 ICMP echoes a second apart to each of the headset's addresses (Windows'
  ICMP API), for latency, jitter and loss.
- **Retries:** iw counts the dongle's transmissions and retries from when it joined, so the
  page shows the share retried between the last two samples.
- **The stream:** while SteamVR streams, the driver log's view (linklog.py, via stream.py):
  SteamVR's bandwidth estimate, held frames, stalls and bitrate step-downs a minute.

Nothing here reads a PC command's text output (netsh's and ping's labels follow the Windows
display language): only Windows' own APIs, through ctypes.

A sample starts every SAMPLE_S (the headset's radios only while frame.py is connected),
HISTORY samples kept. Each link gets a grade, good / fair / poor, from its weakest figure (GRADES).
"""
import ctypes
import re
import socket
import threading
import time
from ctypes import wintypes

import pairing
import stream

SAMPLE_S = 15.0  # from the start of one sample to the next (sooner when the page asks)
HISTORY = 40  # samples, 10 minutes
DONGLE_HOST = pairing.DONGLE_NET + "1"  # the headset's own address on its access point
PING_COUNT, PING_TIMEOUT_MS = 4, 1000
# (figure, good at or better, poor at or worse): signal in dBm (higher is better), ping in ms
# and loss in % (lower is better).
GRADES = {"signal": (-60, -72), "ping": (8, 30), "loss": (0, 5)}


def _num(text, pattern, cast=float):
    m = re.search(pattern, text)
    try:
        return cast(m.group(1)) if m else None
    except ValueError:
        return None


def _rate_detail(text):
    """iw's "160MHz HE-MCS 8 HE-NSS 2 HE-GI 0 HE-DCM 0" as "160 MHz · MCS 8 · 2 streams"."""
    if not text:
        return None
    width = re.search(r"(\d+)MHz", text)
    mcs = re.search(r"MCS[ -]?(\d+)", text)
    nss = re.search(r"NSS (\d+)", text)
    parts = [f"{width.group(1)} MHz" if width else None, f"MCS {mcs.group(1)}" if mcs else None,
             f"{nss.group(1)} stream{'s' if nss.group(1) != '1' else ''}" if nss else None]
    return " · ".join(p for p in parts if p) or None


def _band(freq_mhz):
    if freq_mhz is None:
        return None
    return "6 GHz" if freq_mhz >= 5925 else "5 GHz" if freq_mhz >= 4900 else "2.4 GHz"


# ---------------------------------------------------------------- the PC's Wi-Fi adapters
# Windows' WLAN API (wlanapi.h) and IP Helper (netioapi.h), as ctypes structures.

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class _WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [("InterfaceGuid", _GUID), ("strInterfaceDescription", ctypes.c_wchar * 256),
                ("isState", ctypes.c_uint)]


class _DOT11_SSID(ctypes.Structure):
    _fields_ = [("uSSIDLength", wintypes.ULONG), ("ucSSID", ctypes.c_ubyte * 32)]


class _WLAN_ASSOCIATION_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("dot11Ssid", _DOT11_SSID), ("dot11BssType", ctypes.c_uint), ("dot11Bssid", ctypes.c_ubyte * 6),
                ("dot11PhyType", ctypes.c_uint), ("uDot11PhyIndex", wintypes.ULONG),
                ("wlanSignalQuality", wintypes.ULONG), ("ulRxRate", wintypes.ULONG), ("ulTxRate", wintypes.ULONG)]


class _WLAN_CONNECTION_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("isState", ctypes.c_uint), ("wlanConnectionMode", ctypes.c_uint),
                ("strProfileName", ctypes.c_wchar * 256), ("wlanAssociationAttributes", _WLAN_ASSOCIATION_ATTRIBUTES),
                ("bSecurityEnabled", wintypes.BOOL), ("bOneXEnabled", wintypes.BOOL),
                ("dot11AuthAlgorithm", ctypes.c_uint), ("dot11CipherAlgorithm", ctypes.c_uint)]


class _WLAN_BSS_ENTRY(ctypes.Structure):
    _fields_ = [("dot11Ssid", _DOT11_SSID), ("uPhyId", wintypes.ULONG), ("dot11Bssid", ctypes.c_ubyte * 6),
                ("dot11BssType", ctypes.c_uint), ("dot11BssPhyType", ctypes.c_uint), ("lRssi", wintypes.LONG),
                ("uLinkQuality", wintypes.ULONG), ("bInRegDomain", ctypes.c_ubyte), ("usBeaconPeriod", wintypes.USHORT),
                ("ullTimestamp", ctypes.c_ulonglong), ("ullHostTimestamp", ctypes.c_ulonglong),
                ("usCapabilityInformation", wintypes.USHORT), ("ulChCenterFrequency", wintypes.ULONG),
                ("uRateSetLength", wintypes.ULONG), ("usRateSet", wintypes.USHORT * 126),
                ("ulIeOffset", wintypes.ULONG), ("ulIeSize", wintypes.ULONG)]


class _MIB_IF_ROW2_HEAD(ctypes.Structure):
    """The start of MIB_IF_ROW2, up to the adapter's MAC address. GetIfEntry2 is given a buffer
    the size of the whole row."""
    _fields_ = [("InterfaceLuid", ctypes.c_ulonglong), ("InterfaceIndex", wintypes.ULONG), ("InterfaceGuid", _GUID),
                ("Alias", ctypes.c_wchar * 257), ("Description", ctypes.c_wchar * 257),
                ("PhysicalAddressLength", wintypes.ULONG), ("PhysicalAddress", ctypes.c_ubyte * 32)]


_IF_ROW2_SIZE = 1352  # sizeof(MIB_IF_ROW2)
_CONNECTED = 1  # wlan_interface_state_connected
_OP_CURRENT_CONNECTION, _OP_CHANNEL, _OP_RSSI = 7, 8, 0x10000102  # WLAN_INTF_OPCODE
# DOT11_PHY_TYPE, as netsh names the radio type.
_PHY = {4: "802.11a", 5: "802.11b", 6: "802.11g", 7: "802.11n", 8: "802.11ac", 10: "802.11ax", 11: "802.11be"}


def _wlanapi():
    wlan = ctypes.WinDLL("wlanapi")
    wlan.WlanOpenHandle.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                                    ctypes.POINTER(wintypes.HANDLE)]
    wlan.WlanEnumInterfaces.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    wlan.WlanQueryInterface.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                                        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_void_p),
                                        ctypes.c_void_p]
    wlan.WlanGetNetworkBssList.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                           wintypes.BOOL, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    wlan.WlanFreeMemory.argtypes = [ctypes.c_void_p]
    wlan.WlanCloseHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    return wlan


def _mac(raw, n=6):
    return ":".join(f"{b:02x}" for b in bytes(raw)[:n])


def _alias_and_mac(guid):
    """The adapter's name (as Windows' settings show it, "Wi-Fi 2") and MAC address."""
    try:
        iphlp = ctypes.WinDLL("iphlpapi")
        luid = ctypes.c_ulonglong()
        if iphlp.ConvertInterfaceGuidToLuid(ctypes.byref(guid), ctypes.byref(luid)) != 0:
            return None, ""
        buf = ctypes.create_string_buffer(_IF_ROW2_SIZE)
        row = _MIB_IF_ROW2_HEAD.from_buffer(buf)
        row.InterfaceLuid = luid.value
        if iphlp.GetIfEntry2(buf) != 0:
            return None, ""
        return row.Alias or None, _mac(row.PhysicalAddress, min(row.PhysicalAddressLength, 32))
    except (OSError, AttributeError, ValueError):
        return None, ""


def _query(wlan, handle, guid, opcode, ctype):
    """WlanQueryInterface: a copy of what it returns for opcode, or None."""
    size, data = wintypes.DWORD(), ctypes.c_void_p()
    if wlan.WlanQueryInterface(handle, ctypes.byref(guid), opcode, None, ctypes.byref(size), ctypes.byref(data), None):
        return None
    try:
        if size.value < ctypes.sizeof(ctype):
            return None
        value = ctype.from_buffer_copy(ctypes.string_at(data.value, ctypes.sizeof(ctype)))
        return value.value if isinstance(value, ctypes._SimpleCData) else value
    finally:
        wlan.WlanFreeMemory(data)


def _frequency(wlan, handle, guid, bssid):
    """The connected access point's centre frequency in MHz, from the adapter's scan list (all of
    it, matched by BSSID: filtered by SSID and security, the Frame's access point can be missing)."""
    bss_list = ctypes.c_void_p()
    if wlan.WlanGetNetworkBssList(handle, ctypes.byref(guid), None, 1, False, None,
                                  ctypes.byref(bss_list)):
        return None
    try:
        count = ctypes.c_uint32.from_address(bss_list.value + 4).value  # after dwTotalSize
        for e in (_WLAN_BSS_ENTRY * count).from_address(bss_list.value + 8):
            if bytes(e.dot11Bssid) == bytes(bssid):
                return e.ulChCenterFrequency / 1000
        return None
    finally:
        wlan.WlanFreeMemory(bss_list)


def _band_of_channel(channel):
    """Without a frequency: 2.4 GHz channels are 1-14 (5 and 6 GHz channel numbers overlap)."""
    return "2.4 GHz" if channel and channel <= 14 else None


def pc_adapters():
    """The PC's connected Wi-Fi adapters: [{"name", "description", "ssid", "mac", "band", "channel",
    "signal_pct", "rssi", "rx_mbps", "tx_mbps", "radio", "dongle"}]."""
    try:
        wlan = _wlanapi()
    except (OSError, AttributeError):  # not Windows, or no WLAN service
        return []
    handle, negotiated = wintypes.HANDLE(), wintypes.DWORD()
    if wlan.WlanOpenHandle(2, None, ctypes.byref(negotiated), ctypes.byref(handle)):
        return []
    out = []
    try:
        ifaces = ctypes.c_void_p()
        if wlan.WlanEnumInterfaces(handle, None, ctypes.byref(ifaces)):
            return []
        try:
            count = ctypes.c_uint32.from_address(ifaces.value).value  # then dwIndex, then the list
            infos = [_WLAN_INTERFACE_INFO.from_buffer_copy(i)
                     for i in (_WLAN_INTERFACE_INFO * count).from_address(ifaces.value + 8)]
        finally:
            wlan.WlanFreeMemory(ifaces)
        for info in infos:
            if info.isState != _CONNECTED:
                continue
            guid = info.InterfaceGuid
            conn = _query(wlan, handle, guid, _OP_CURRENT_CONNECTION, _WLAN_CONNECTION_ATTRIBUTES)
            if conn is None or conn.isState != _CONNECTED:
                continue
            assoc = conn.wlanAssociationAttributes
            ssid = bytes(assoc.dot11Ssid.ucSSID[:assoc.dot11Ssid.uSSIDLength]).decode("utf-8", "replace")
            channel = _query(wlan, handle, guid, _OP_CHANNEL, wintypes.ULONG)
            freq = _frequency(wlan, handle, guid, assoc.dot11Bssid)
            name, mac = _alias_and_mac(guid)
            description = info.strInterfaceDescription
            out.append({"name": name, "description": description, "ssid": ssid, "mac": mac,
                        "band": _band(freq) if freq else _band_of_channel(channel),
                        "channel": str(channel) if channel else None,
                        "signal_pct": assoc.wlanSignalQuality,
                        "rssi": _query(wlan, handle, guid, _OP_RSSI, wintypes.LONG),
                        "rx_mbps": assoc.ulRxRate / 1000, "tx_mbps": assoc.ulTxRate / 1000,
                        "radio": _PHY.get(assoc.dot11PhyType),
                        "dongle": ssid.startswith("frame_") or "Valve" in description})
    finally:
        wlan.WlanCloseHandle(handle, None)
    return out


# ---------------------------------------------------------------- the headset's radios

HEADSET_CMD = ("echo '## wlan0'; iw dev wlan0 link 2>/dev/null; echo '## station'; "
               "iw dev wlanap station dump 2>/dev/null; echo '## ap'; iw dev wlanap info 2>/dev/null; "
               "echo '## addr'; ip -4 -o addr show wlan0 2>/dev/null")


def parse_headset(text):
    """iw's output (HEADSET_CMD) as {"wifi": {...} or None, "dongle": [{...}], "ap": {...} or None,
    "address": the headset's own address on your network, or None}."""
    parts = dict(re.findall(r"## (\w+)\n(.*?)(?=\n## |\Z)", text, re.S))
    wifi, link = None, parts.get("wlan0", "")
    if link.startswith("Connected"):
        freq = _num(link, r"freq: ([\d.]+)")
        wifi = {"ssid": _num(link, r"SSID: (.+)", str), "band": _band(freq), "freq": freq,
                "signal": _num(link, r"signal: (-?\d+)", int),
                "rx_mbps": _num(link, r"rx bitrate: ([\d.]+)"), "tx_mbps": _num(link, r"tx bitrate: ([\d.]+)"),
                "tx_detail": _rate_detail(_num(link, r"tx bitrate: [\d.]+ MBit/s (.+)", str))}
    stations = []
    for block in re.split(r"(?=^Station )", parts.get("station", ""), flags=re.M):
        if not block.startswith("Station"):
            continue
        tx_packets = _num(block, r"tx packets:\s+(\d+)", int) or 0
        retries = _num(block, r"tx retries:\s+(\d+)", int) or 0
        stations.append({"mac": _num(block, r"Station (\S+)", str),
                         "signal": _num(block, r"avg ack signal:\s*(-?\d+)", int) or _num(block, r"signal avg:\s*(-?\d+)", int),
                         "tx_mbps": _num(block, r"tx bitrate:\s+([\d.]+)"), "rx_mbps": _num(block, r"rx bitrate:\s+([\d.]+)"),
                         "tx_detail": _rate_detail(_num(block, r"tx bitrate:\s+[\d.]+ MBit/s (.+)", str)),
                         "rx_detail": _rate_detail(_num(block, r"rx bitrate:\s+[\d.]+ MBit/s (.+)", str)),
                         "tx_packets": tx_packets, "retries": retries,
                         "failed": _num(block, r"tx failed:\s+(\d+)", int) or 0,
                         "drops": _num(block, r"rx drop misc:\s+(\d+)", int) or 0,
                         "connected_s": _num(block, r"connected time:\s+(\d+)", int)})
    ap = None
    info = parts.get("ap", "")
    if "type AP" in info:
        freq = _num(info, r"channel \d+ \(([\d.]+) MHz\)")
        ap = {"channel": _num(info, r"channel (\d+)", int), "band": _band(freq),
              "width": _num(info, r"width: (\d+ MHz)", str), "ssid": _num(info, r"ssid (\S+)", str)}
    return {"wifi": wifi, "dongle": stations, "ap": ap, "address": _num(parts.get("addr", ""), r"inet ([\d.]+)/", str)}


def retry_delta(now, before):
    """The headset's retries to the dongle between two samples (iw counts from when the dongle
    joined): {"retry_pct", "failed", "packets"}, or None for the first sample, another station,
    a new association (the counters went back) or nothing sent."""
    if not now or not before or now.get("mac") != before.get("mac"):
        return None
    packets = now["tx_packets"] - before["tx_packets"]
    retries, failed = now["retries"] - before["retries"], now["failed"] - before["failed"]
    if packets <= 0 or retries < 0 or failed < 0:
        return None
    return {"retry_pct": round(100 * retries / packets, 1), "failed": failed, "packets": packets}


# ---------------------------------------------------------------- round trips

class _IP_OPTION_INFORMATION(ctypes.Structure):
    _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]


class _ICMP_ECHO_REPLY(ctypes.Structure):
    _fields_ = [("Address", wintypes.ULONG), ("Status", wintypes.ULONG), ("RoundTripTime", wintypes.ULONG),
                ("DataSize", wintypes.USHORT), ("Reserved", wintypes.USHORT), ("Data", ctypes.c_void_p),
                ("Options", _IP_OPTION_INFORMATION)]


def echoes(host, count=PING_COUNT, timeout_ms=PING_TIMEOUT_MS):
    """Round trips in ms (None for each one lost) of count ICMP echoes a second apart, through
    Windows' ICMP API. Timed here, to a fraction of a millisecond (the API's own figure is in
    whole milliseconds). None if host doesn't resolve, or without the API."""
    try:
        addr = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
        iphlp = ctypes.WinDLL("iphlpapi")
    except (OSError, AttributeError, IndexError):
        return None
    iphlp.IcmpCreateFile.restype = wintypes.HANDLE
    iphlp.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
    iphlp.IcmpSendEcho.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.WORD,
                                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    iphlp.IcmpSendEcho.restype = wintypes.DWORD
    icmp = iphlp.IcmpCreateFile()
    if not icmp or icmp == ctypes.c_void_p(-1).value:
        return None
    dest = int.from_bytes(socket.inet_aton(addr), "little")  # IPAddr: the address's bytes in order
    payload = b"cuff-pc link check".ljust(32, b".")
    reply = ctypes.create_string_buffer(ctypes.sizeof(_ICMP_ECHO_REPLY) + len(payload) + 8 + 64)
    times = []
    try:
        for i in range(count):
            started = time.perf_counter()
            n = iphlp.IcmpSendEcho(icmp, dest, payload, len(payload), None, reply, len(reply), timeout_ms)
            took = time.perf_counter() - started
            ok = n > 0 and _ICMP_ECHO_REPLY.from_buffer(reply).Status == 0  # IP_SUCCESS
            times.append(round(took * 1000, 2) if ok else None)
            if i < count - 1:
                time.sleep(max(0.0, 1.0 - took))
    finally:
        iphlp.IcmpCloseHandle(icmp)
    return times


def summarise(times, count=PING_COUNT):
    """{"avg_ms", "min_ms", "max_ms", "jitter_ms", "loss_pct"} from round trips (None: lost)."""
    got = [t for t in times if t is not None]
    loss = round(100 * (count - len(got)) / count)
    if not got:
        return {"avg_ms": None, "min_ms": None, "max_ms": None, "jitter_ms": None, "loss_pct": loss}
    jitter = sum(abs(a - b) for a, b in zip(got, got[1:])) / max(1, len(got) - 1)
    return {"avg_ms": round(sum(got) / len(got), 1), "min_ms": round(min(got), 1), "max_ms": round(max(got), 1),
            "jitter_ms": round(jitter, 1), "loss_pct": loss}


def ping(host, count=PING_COUNT):
    """{"avg_ms", "min_ms", "max_ms", "jitter_ms", "loss_pct"}, or None (no host, or it doesn't
    resolve)."""
    if not host:
        return None
    times = echoes(host, count)
    return None if times is None else summarise(times, count)


def grade(signal=None, ping_ms=None, loss=None):
    """good / fair / poor from the weakest of the figures given, or None with none."""
    marks = []
    for value, (good, poor), higher_better in ((signal, GRADES["signal"], True), (ping_ms, GRADES["ping"], False),
                                               (loss, GRADES["loss"], False)):
        if value is None:
            continue
        if higher_better:
            marks.append(0 if value >= good else 2 if value <= poor else 1)
        else:
            marks.append(0 if value <= good else 2 if value >= poor else 1)
    return ("good", "fair", "poor")[max(marks)] if marks else None


class LinkQuality:
    def __init__(self, frame, pairing_state):
        self.frame, self.pairing = frame, pairing_state
        self.lock = threading.Lock()
        self.latest = None
        self.history = []  # [{"t", "dongle_ping", "lan_ping", "dongle_signal", "wifi_signal"}]
        self.last_station = None  # the last sample's view of the dongle, for retries between samples
        self.wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="link-quality").start()

    def refresh(self):
        self.wake.set()

    def lan_host(self, headset=None):
        """The headset's address on your network: what it reports for its Wi-Fi (a name such
        as frame.local can resolve to its dongle address instead), else the last a paired
        headset was heard from, if not the dongle's."""
        if headset and headset.get("address"):
            return headset["address"]
        for h in self.pairing.headsets():
            if h.get("address") and not h["address"].startswith(pairing.DONGLE_NET):
                return h["address"]
        return None

    def _loop(self):
        """A sample every SAMPLE_S from the start of the one before (a sample takes a few
        seconds itself: the pings, the headset's figures), or at once when the page asks."""
        while True:
            started = time.monotonic()
            try:
                self._sample()
            except Exception as e:  # noqa: BLE001 - a bad sample mustn't end the sampling
                print(f"link quality: {e!r}", flush=True)
            self.wake.wait(max(0.0, started + SAMPLE_S - time.monotonic()))
            self.wake.clear()

    def _sample(self):
        adapters = pc_adapters()
        headset = None
        if self.frame.connected():
            try:
                _, out, _ = self.frame.run(HEADSET_CMD, timeout=10)
                headset = parse_headset(out)
            except OSError:  # the SSH link dropped mid-sample (ConnectionError), or timed out
                headset = None
        dongle_pc = next((a for a in adapters if a["dongle"]), None)
        results = {}
        threads = [threading.Thread(target=lambda k=k, h=h: results.__setitem__(k, ping(h)))
                   for k, h in (("dongle", DONGLE_HOST if dongle_pc else None), ("lan", self.lan_host(headset)))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snap = stream.snapshot()
        # The headset's view of this PC's dongle: its station entry, by the adapter's MAC address.
        stations = (headset or {}).get("dongle", [])
        station = next((s for s in stations if dongle_pc and s["mac"] == dongle_pc["mac"]),
                       stations[0] if stations else None)
        if station:
            station = {**station, "delivery": retry_delta(station, self.last_station)}
        self.last_station = station
        wifi = (headset or {}).get("wifi")
        dp, lp = results.get("dongle"), results.get("lan")
        latest = {
            "t": time.time(),
            "dongle": None if not dongle_pc and not station else {
                "pc": dongle_pc, "headset": station, "ap": (headset or {}).get("ap"), "ping": dp,
                "usb": (snap.get("dongle_usb") or [None])[0],
                "grade": grade(station and station["signal"], dp and dp["avg_ms"], dp and dp["loss_pct"]),
            },
            "lan": None if not wifi and not lp else {
                "headset": wifi, "host": self.lan_host(headset), "ping": lp,
                "grade": grade(wifi and wifi["signal"], lp and lp["avg_ms"], lp and lp["loss_pct"]),
            },
            "stream": snap.get("link"),
            "headset_reachable": headset is not None,
        }
        with self.lock:
            self.latest = latest
            self.history = (self.history + [{
                "t": latest["t"],
                "dongle_ping": dp and dp["avg_ms"], "lan_ping": lp and lp["avg_ms"],
                "dongle_signal": station and station["signal"], "wifi_signal": wifi and wifi["signal"],
            }])[-HISTORY:]

    def state(self):
        with self.lock:
            return {"latest": self.latest, "history": list(self.history), "every_s": SAMPLE_S}
