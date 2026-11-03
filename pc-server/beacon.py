"""Announce the PC app on the PC's home networks, so the headset finds it on its own: home
Wi-Fi, and the Steam Frame's dongle link (the Frame's own access point) alike.

Every EVERY_S, one UDP broadcast per local IPv4 address, from that address, to port
PORT: {"app": "cuff-pc", "version": 1, "id": <this PC's id>, "port": <HTTP port>,
"name": <host name>, "pairing": <pairing open>, "typing": <typing waiting>}. The headset
takes it only from a PC it's paired with (by id) that proves it by answering a signed request
(cuff/pc.py, pairing.py), asks to pair while "pairing" is true, and fetches typed text while
"typing" is (typing_link.py).

Only on networks Windows calls Private or Domain, and on the dongle's (DONGLE_NET, which
Windows calls Public), not on a café's Wi-Fi. The categories come from Get-NetConnectionProfile
every CATEGORIES_S, or sooner for a network it hasn't seen (until the first answer, a second
or so, it announces on the dongle's only); if Windows can't say, it announces on every network,
as before.
"""
import json
import socket
import subprocess
import sys
import threading
import time

from pairing import DONGLE_NET

PORT = 8766
EVERY_S = 2.0
CATEGORIES_S = 60.0
ANNOUNCE_ON = ("Private", "DomainAuthenticated")
PROFILES = ("Get-NetConnectionProfile | ForEach-Object { $c = $_.NetworkCategory; "
            "Get-NetIPAddress -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue "
            "| ForEach-Object { \"$($_.IPAddress) $c\" } }")


def local_addresses():
    """The PC's IPv4 addresses, without loopback. "..localmachine" is Windows' name for all of
    them; as bytes it skips the IDNA encoding that fails for a computer name that isn't ASCII."""
    for host in (b"..localmachine", socket.gethostname()):
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET)
        except (OSError, UnicodeError):
            continue
        found = {i[4][0] for i in infos if not i[4][0].startswith("127.")}
        if found:
            return sorted(found)
    return []


def network_categories():
    """{IPv4 address: "Public" | "Private" | "DomainAuthenticated"}, Windows' network profiles,
    or None when it can't say (not Windows, or PowerShell failed)."""
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", PROFILES],
                             capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode:
        return None
    return dict(line.split(None, 1) for line in out.stdout.splitlines() if len(line.split()) == 2)


class Beacon(threading.Thread):
    def __init__(self, http_port, pairing=None, typing=None):
        super().__init__(daemon=True, name="beacon")
        self.port, self.pairing, self.typing = http_port, pairing, typing
        self.failed = set()
        self.categories, self.categories_at, self.asking = None, None, False
        self.skipped = set()  # addresses not announced on, logged once

    def message(self):
        return json.dumps({"app": "cuff-pc", "version": 1, "id": self.pairing.pc_id if self.pairing else None,
                           "port": self.port, "name": socket.gethostname(),
                           "pairing": bool(self.pairing and self.pairing.is_open()),
                           "typing": bool(self.typing and self.typing.active())}).encode()

    def _ask(self):
        categories = network_categories()
        if categories is None and self.categories_at is None:
            print("beacon: can't tell home networks from public ones; announcing on all", file=sys.stderr, flush=True)
        self.categories = categories if categories is not None else self.categories
        self.categories_at, self.asking = time.monotonic(), False

    def _refresh(self, addresses):
        """Ask Windows for the networks' categories now and then (on a thread: it takes a second),
        and soon after a network turns up that the last answer didn't have."""
        now = time.monotonic()
        unknown = self.categories is not None and any(a not in self.categories for a in addresses)
        due = self.categories_at is None or now - self.categories_at > (10.0 if unknown else CATEGORIES_S)
        if due and not self.asking:
            self.asking = True
            threading.Thread(target=self._ask, daemon=True, name="beacon-networks").start()

    def announce_on(self, addr):
        if addr.startswith(DONGLE_NET):
            return True
        if self.categories is None:  # not asked yet (a second or two), or Windows can't say
            return self.categories_at is not None
        return self.categories.get(addr) in ANNOUNCE_ON

    def run(self):
        while True:
            message = self.message()
            addresses = local_addresses()
            self._refresh(addresses)
            for addr in addresses:
                if not self.announce_on(addr):
                    if self.categories is not None and addr not in self.skipped:
                        self.skipped.add(addr)
                        kind = self.categories.get(addr, "an unknown")
                        print(f"beacon: not announcing on {addr} ({kind} network)", flush=True)
                    continue
                self.skipped.discard(addr)
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                        s.bind((addr, 0))  # so it leaves through that address's network
                        s.sendto(message, ("255.255.255.255", PORT))
                    self.failed.discard(addr)
                except OSError as e:
                    if addr not in self.failed:  # log each address's failure once
                        self.failed.add(addr)
                        print(f"beacon: {addr}: {e!r}", file=sys.stderr, flush=True)
            time.sleep(EVERY_S)
