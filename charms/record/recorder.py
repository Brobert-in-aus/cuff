"""Runs Cuff's recorder (recorder/ in Cuff's repo, from Framecorder's: see its README) for the
Record charm.

The recorder (~/.local/share/cuff/bin/cuff-recorder, built by recorder/install.sh) reads the
panels through the root-owned panel helper (cuff-grab, or Framecorder's), un-warps one eye or keeps both, encodes in
hardware and keeps a replay buffer for clips. With --control it takes
one command per line on stdin (record <file>, stop-record, clip, quit) and reports on stdout:

    recording <path>                          a recording started writing
    saved recording|clip <bytes> <secs> <path>
    busy recording                            a clip asked for mid-recording (it's all in there)
    failed recording|clip|command <why>       (a clip right after another is "failed clip skipped: ...")

Every clip asked for gets a saved, busy or failed reply. Started without --replay, it records
nothing until told to (record <file>).

It keeps running while clips are on (the buffer lives in it), and otherwise only for a
recording. It's restarted when its settings change, between recordings, and again 5 s after
it dies. If Cuff dies, the kernel sends it SIGINT, so it still finishes what it's writing.

Its stderr goes to record.log, which is set aside as record.log.1 when a start finds it over
LOG_MAX bytes, so the two hold the last few runs.
"""
import ctypes
import os
import signal
import subprocess
import sys
import threading
import time

RECORDER = os.path.expanduser("~/.local/share/cuff/bin/cuff-recorder")
# The panel helper: Cuff's own (install.sh --helper), else Framecorder's, as the recorder looks.
HELPERS = [os.path.expanduser("~/.local/lib/cuff/cuff-grab"),
           os.path.expanduser("~/.local/lib/framecorder/framecorder-grab")]
VIDEOS = os.path.expanduser("~/Videos/framecorder")
CLIPS = os.path.join(VIDEOS, "clips")
LOG = os.path.expanduser("~/.local/state/cuff/record.log")
LOG_MAX = 1_000_000
LOG_TAIL = 16_384  # how much of the log's end stuck_message reads
RETRY_S = 5.0
QUIT_GRACE_S = 10.0

from .options import DEFAULTS  # noqa: F401 - the settings, as Framecorder spells them
BITRATE = {"standard": 20, "high": 40, "max": 80}  # Mbit/s


def panels_unlocked():
    """Whether Framecorder's panel helper has its permission: then the recorder reads the panels
    (any shape, both eyes, no cost to the game), else SteamVR's headset view (16:9, left eye)."""
    for helper in HELPERS:
        try:
            if os.stat(helper).st_uid == 0 and os.getxattr(helper, "security.capability"):
                return True
        except OSError:
            pass
    return False


def recorder_args(s, panel):
    """The recorder's arguments for settings s, as Framecorder's tab builds them."""
    if not panel:
        args = ["--view", "eye", "--aspect", "16:9", "--source", "headset"]
    elif s["shape"] == "both":
        args = ["--view", "raw"]
    else:
        aspect = {"square": "1:1", "tall": "9:16"}.get(s["shape"], "16:9")
        args = ["--view", "eye", "--aspect", aspect, "--eye", "right" if s["eye"] == "right" else "left"]
    args += ["--bitrate", str(BITRATE.get(s["quality"], 40))]
    if s["fps"] in ("60", "30"):
        args += ["--fps", s["fps"]]
    if s["game_audio"] != "true":
        args.append("--no-audio")
    if s["mic"] == "true":
        args.append("--mic")
    # Steadying the view (the recorder's stabilise.rs) needs the panels' own picture of one eye.
    if panel and s["shape"] != "both" and s.get("stabilise", "off") != "off":
        args += ["--stabilise", s["stabilise"]]
    return args


def _rotate_log():
    """Sets record.log aside (as record.log.1, replacing the one before) once it's over LOG_MAX."""
    try:
        if os.path.getsize(LOG) > LOG_MAX:
            os.replace(LOG, LOG + ".1")
    except OSError:
        pass


def _die_with_parent():
    """In the child: SIGINT when Cuff goes, so the recorder still finishes its files."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(1, signal.SIGINT)  # PR_SET_PDEATHSIG


class Recorder:
    """One recorder process. on_event(word, rest) gets each line it reports."""

    def __init__(self, settings, on_event):
        self.replay = int(settings["clip"]) if settings["clips"] == "true" else None
        self.args = recorder_args(settings, panels_unlocked())
        cmd = [RECORDER] + (["--replay", str(self.replay)] if self.replay else []) + self.args + ["--control"]
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        _rotate_log()
        with open(LOG, "a") as log:
            log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)}\n")
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                         text=True, bufsize=1, env={**os.environ, "RUST_LOG": "info"},
                                         preexec_fn=_die_with_parent)
        self.quitting = None  # when quit was sent
        print(f"record: recorder started (pid {self.proc.pid}, clips {self.replay})", flush=True)
        threading.Thread(target=self._read, args=(on_event,), daemon=True, name="recorder-events").start()

    def _read(self, on_event):
        for line in self.proc.stdout:
            word, _, rest = line.strip().partition(" ")
            if word:
                on_event(word, rest)

    def send(self, line):
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            print(f"record: couldn't tell the recorder to {line}", file=sys.stderr, flush=True)
            return False

    def quit(self):
        if self.quitting is None:
            self.send("quit")
            self.quitting = time.monotonic()

    def exited(self):
        """None while it runs; else its exit code (killed if it ignored quit for too long)."""
        if self.quitting is not None and time.monotonic() - self.quitting > QUIT_GRACE_S:
            self.proc.kill()
        return self.proc.poll()

    def stuck_message(self):
        """The recorder's last complaint, for the page."""
        try:
            with open(LOG, "rb") as f:
                f.seek(max(0, os.fstat(f.fileno()).st_size - LOG_TAIL))
                lines = f.read().decode(errors="replace").splitlines()[-40:]
        except OSError:
            return "The recorder stopped"
        last = next((l for l in reversed(lines) if "ERROR" in l), lines[-1] if lines else "The recorder stopped")
        return last.split("] ", 1)[-1].strip()
