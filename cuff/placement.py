"""Where the panel sits: the values the settings page's Position sliders change.

Each slider maps to one of wrist.py's constants; "hand" is the wrist it's worn on ("left" or
"right": the right is the left mirrored, and the other controller touches it). The defaults are wrist.py's own, with
[wrist] in config.toml on top; what the user applies on the settings page is saved to
~/.config/cuff/placement.json and wins over both. Reset goes back to the defaults.

There are two sets of values: `applied` places the panel; `pending` is what the sliders
show. While they differ the settings page shows a translucent preview at `pending`, so the
panel being adjusted doesn't move under the finger until Apply.
"""
import json
import os
import sys
import threading

from . import wrist
from .config import CONFIG

FILE = os.path.expanduser("~/.config/cuff/placement.json")


class Slider:
    def __init__(self, key, label, lo, hi, step, unit, scale, constant):
        """scale: shown value per stored unit (100 for metres shown in cm)."""
        self.key, self.label, self.lo, self.hi, self.step = key, label, lo, hi, step
        self.unit, self.scale, self.constant = unit, scale, constant

    def clamp(self, value):
        steps = round((min(self.hi, max(self.lo, value)) - self.lo) / self.step)
        return round(self.lo + steps * self.step, 6)

    def text(self, value):
        shown = value * self.scale
        return f"{shown:.1f} {self.unit}" if self.unit != "°" else f"{shown:.0f}°"


# Sub-page -> its sliders, top to bottom.
PAGES = {
    "Place": [
        Slider("size", "Size", 0.07, 0.16, 0.005, "cm", 100, "PANEL_WIDTH"),
        Slider("along", "Along arm", 0.06, 0.20, 0.005, "cm", 100, "WRIST_CENTRE"),
        Slider("height", "Height", 0.0, 0.04, 0.0025, "mm", 1000, "SKIN_GAP"),
    ],
    "Angle": [
        Slider("around", "Around", 0, 150, 5, "°", 1, "WRIST_ROLL_DEG"),
        Slider("tilt", "Tilt", -60, 60, 5, "°", 1, "PANEL_TILT_DEG"),
        Slider("twist", "Twist", -45, 45, 5, "°", 1, "PANEL_YAW_DEG"),
    ],
}
SLIDERS = {s.key: s for page in PAGES.values() for s in page}

# config.toml's [wrist] overrides wrist.py's constants, for everything (sliders included).
for _name, _value in CONFIG.get("wrist", {}).items():
    if _name.isupper() and hasattr(wrist, _name):
        setattr(wrist, _name, tuple(_value) if isinstance(_value, list) else _value)


HANDS = ("left", "right")


def defaults():
    return {"hand": CONFIG.get("hand", "left") if CONFIG.get("hand") in HANDS else "left", "size": wrist.PANEL_WIDTH, "along": wrist.WRIST_CENTRE[2], "height": wrist.SKIN_GAP,
            "around": wrist.WRIST_ROLL_DEG, "tilt": wrist.PANEL_TILT_DEG, "twist": wrist.PANEL_YAW_DEG}


def overrides(values):
    """wrist.panel_offset() overrides for these slider values."""
    centre = wrist.WRIST_CENTRE
    return {"PANEL_WIDTH": values["size"], "WRIST_CENTRE": (centre[0], centre[1], values["along"]),
            "SKIN_GAP": values["height"], "WRIST_ROLL_DEG": values["around"],
            "PANEL_TILT_DEG": values["tilt"], "PANEL_YAW_DEG": values["twist"]}


class Placement:
    """applied and pending are replaced whole (never edited in place), so the overlay loop
    can read them from another thread; version goes up on every Apply."""

    def __init__(self):
        self.lock = threading.Lock()
        self.applied = self._load()
        self.pending = dict(self.applied)
        self.version = 0

    @staticmethod
    def _load():
        values = defaults()
        try:
            with open(FILE) as f:
                saved = json.load(f)
            values.update({k: SLIDERS[k].clamp(float(v)) for k, v in saved.items() if k in SLIDERS})
            if saved.get("hand") in HANDS:
                values["hand"] = saved["hand"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as e:
            print(f"placement: {FILE}: {e!r}; using the defaults", file=sys.stderr, flush=True)
        return values

    def offset(self, values):
        """The panel's pose in the wearing hand's tip frame. On the right wrist it's the left
        one mirrored across the tip frame's YZ plane (x to -x): M R M for the rotation, M t
        for the position, so it stays a proper rotation and the text reads the right way
        (from the hand toward the elbow, as on a watch worn on the right)."""
        pose = wrist.panel_offset(overrides(values))
        if values.get("hand") != "right":
            return pose
        m = (-1.0, 1.0, 1.0)
        return [[m[r] * pose[r][c] * m[c] for c in range(3)] + [m[r] * pose[r][3]] for r in range(3)]

    @property
    def changed(self):
        return self.pending != self.applied

    def set(self, key, value):
        with self.lock:
            self.pending = {**self.pending, key: SLIDERS[key].clamp(value)}

    def switch_hand(self):
        """The other wrist, pending until Apply like the sliders."""
        with self.lock:
            self.pending = {**self.pending, "hand": "right" if self.pending.get("hand") == "left" else "left"}

    def nudge(self, key, steps):
        self.set(key, self.pending[key] + steps * SLIDERS[key].step)

    def apply(self):
        with self.lock:
            self.applied = dict(self.pending)
            self.version += 1
            saved = {k: v for k, v in self.applied.items() if v != defaults()[k]}
        try:
            os.makedirs(os.path.dirname(FILE), exist_ok=True)
            with open(FILE, "w") as f:
                json.dump(saved, f, indent=1)
        except OSError as e:
            print(f"placement: saving: {e!r}", file=sys.stderr, flush=True)

    def cancel(self):
        with self.lock:
            self.pending = dict(self.applied)

    def reset(self):
        """The defaults (but the same wrist), as pending: they take effect with Apply."""
        with self.lock:
            self.pending = {**defaults(), "hand": self.pending.get("hand", "left")}  # the wrist stays


PLACEMENT = Placement()
