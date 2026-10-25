"""Settings from ~/.config/cuff/config.toml (every key optional), the headset profile, and
where Cuff keeps its files.

  tabs = ["now-playing", "timer"]        tab order by charm id; unlisted charms follow
  [dictation]                            the dictation button (see dictation.py)
  servers = ["http://pc.lan:8765"]       PC servers tried before the headset's own model
  [wrist]                                placement and fade overrides (see wrist.py)
  [profile]                              overrides for the headset defaults below
"""
import os
import sys
import tomllib

CONFIG_FILE = os.path.expanduser(os.environ.get("CUFF_CONFIG", "~/.config/cuff/config.toml"))
RUN_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "cuff")  # layer PNGs
CACHE_DIR = os.path.expanduser("~/.cache/cuff")  # remembered state
DATA_DIR = os.path.expanduser("~/.local/share/cuff/data")  # charms' installed data

# The Steam Frame. M1 moves these to one profile file per headset.
FRAME = {
    "openvr_lib": "/opt/steamvr/bin/linuxarm64/libopenvr_api.so",
    "vrcmd": "/opt/steamvr/bin/linuxarm64/vrcmd",
    "real_mic": "alsa_loopback_device.alsa_input.platform-sound.HiFi__Mic__source",
    "screenshot_dir": "~/Pictures/Frame screenshots",
    "compositor_log": "~/.local/share/Steam/logs/vrcompositor.txt",  # Room View's state in a game
    "font_dir": "/usr/share/fonts/noto",
}


def _load():
    try:
        with open(CONFIG_FILE, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"config: {CONFIG_FILE}: {e!r}; using defaults", file=sys.stderr, flush=True)
        return {}


CONFIG = _load()
PROFILE = {**FRAME, **CONFIG.get("profile", {})}
if os.environ.get("OPENVR_API_LIB"):
    PROFILE["openvr_lib"] = os.environ["OPENVR_API_LIB"]
