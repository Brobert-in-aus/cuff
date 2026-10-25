"""AirPods battery from LibrePods (https://github.com/librepods-org/librepods), for the status bar.

AirPods don't give BlueZ a battery level, so Cuff can't see one the usual way (Battery1).
LibrePods talks to them over Apple's own protocol and gets each bud's and the case's level,
but offers no API: its Linux app has no D-Bus service, and its local socket only takes
"reopen". It does log every report, as

    Received Battery Info: [BatteryInfo { component: Left, level: 97, status: NotCharging },
    BatteryInfo { component: Right, level: 98, ... }, BatteryInfo { component: Case, level: 0,
    status: Disconnected }]

so a thread follows its systemd user unit's journal (PROFILE["librepods_unit"]) and keeps the
latest report. With no such unit it finds nothing and stays quiet.
"""
import re
import subprocess
import sys
import threading

from .config import PROFILE

UNIT = PROFILE["librepods_unit"]
REPORT = "Received Battery Info:"
PART = re.compile(r"component: (\w+), level: (\d+), status: (\w+)")
BUDS = ("Left", "Right")


class LibrePods(threading.Thread):
    """report: {"Left": (level 0-100, status), "Right": ..., "Case": ...} from the latest log
    line, or {} before one. status is LibrePods' own: Charging, NotCharging or Disconnected."""

    def __init__(self):
        super().__init__(daemon=True, name="librepods")
        self.report = {}

    def run(self):
        # The last few lines first, so a report from before Cuff started counts.
        cmd = ["journalctl", "--user", "-u", UNIT, "-f", "-n", "200", "-o", "cat", "--no-pager"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                    errors="replace")
        except OSError as e:
            print(f"librepods: can't read its journal: {e!r}", file=sys.stderr, flush=True)
            return
        for line in proc.stdout:
            if REPORT in line:
                parts = {name: (int(level), status) for name, level, status in PART.findall(line)}
                if parts:
                    self.report = parts

    def battery(self):
        """(level 0-1 of the lower bud in use, charging) or None: a bud LibrePods reports as
        Disconnected (in the case, out of range) doesn't count."""
        buds = [(level, status) for name, (level, status) in self.report.items()
                if name in BUDS and status != "Disconnected"]
        if not buds:
            return None
        return min(level for level, _ in buds) / 100, any(status == "Charging" for _, status in buds)
