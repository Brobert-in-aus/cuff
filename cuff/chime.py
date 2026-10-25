"""The alert chime: two soft notes, rising, made once into a WAV and played with pw-play.

The player's node name contains "filter" and "chain", so SteamOS's spatial-audio script
leaves it on the default output instead of re-routing it to the built-in speakers.
Settings > Alerts switches it off; [toasts] volume = 0.0-1.0 in config.toml (default 0.4).
"""
import math
import os
import struct
import subprocess
import sys
import wave

from .config import CONFIG, RUN_DIR

SETTINGS = CONFIG.get("toasts", {})
RATE = 48000
NOTES = ((880.0, 0.11), (1318.5, 0.18))  # A5 then E6, seconds


def _make(path):
    frames = bytearray()
    for freq, length in NOTES:
        n = int(RATE * length)
        for i in range(n):
            t = i / RATE
            env = min(1.0, t / 0.008) * math.exp(-t * 9)  # a short attack, a soft decay
            v = env * (math.sin(2 * math.pi * freq * t) + 0.3 * math.sin(4 * math.pi * freq * t)) / 1.3
            frames += struct.pack("<hh", *([int(v * 0.6 * 32767)] * 2))
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))


def play():
    path = os.path.join(RUN_DIR, "chime.wav")
    try:
        if not os.path.exists(path):
            os.makedirs(RUN_DIR, exist_ok=True)
            _make(path)
        subprocess.Popen(["pw-play", "--volume", str(float(SETTINGS.get("volume", 0.4))),
                          "-P", "{ node.name = cuff_chime_filter_chain }", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        print(f"chime: {e!r}", file=sys.stderr, flush=True)
