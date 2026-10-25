"""Announce the PC app on every network the PC is on, so the headset finds it on its own:
home Wi-Fi, and the Steam Frame's dongle link (the Frame's own access point) alike.

Every EVERY_S, one UDP broadcast per local IPv4 address, from that address, to port
PORT: {"app": "cuff-pc", "version": 1, "port": <HTTP port>, "name": <host name>,
"pairing": <pairing open>}. The headset takes the sender's address and port as the PC to use
(cuff/pc.py), and asks to pair while "pairing" is true.
"""
import json
import socket
import sys
import threading
import time

PORT = 8766
EVERY_S = 2.0


def local_addresses():
    """The PC's IPv4 addresses, without loopback."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return sorted({i[4][0] for i in infos if not i[4][0].startswith("127.")})


class Beacon(threading.Thread):
    def __init__(self, http_port, pairing=None):
        super().__init__(daemon=True, name="beacon")
        self.port, self.pairing = http_port, pairing
        self.failed = set()

    def message(self):
        return json.dumps({"app": "cuff-pc", "version": 1, "port": self.port, "name": socket.gethostname(),
                           "pairing": bool(self.pairing and self.pairing.is_open())}).encode()

    def run(self):
        while True:
            message = self.message()
            for addr in local_addresses():
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
