"""Record's settings: their choices, defaults and file. The charm uses them, and so does
cuff.remote (charm.toml's options = "options"), which loads this file on its own for Cuff
for PC's settings page, so it imports nothing from the charm, and from Cuff only its file
writer (cuff.atomic, which cuff.remote has too).

Values are strings in Framecorder's spelling, kept in ~/.config/cuff/record/settings.json.
"""
import json
import os
import sys

from cuff.atomic import atomic_write_json

DEFAULTS = {"shape": "wide", "eye": "left", "quality": "high", "fps": "auto",
            "game_audio": "true", "mic": "true", "clips": "true", "clip": "30", "stabilise": "off",
            "button": "clip"}
# Each setting's choices, as (value, label).
SHAPES = [("wide", "16:9"), ("square", "1:1"), ("tall", "9:16"), ("both", "Both")]
EYES = [("left", "Left eye"), ("right", "Right eye")]
QUALITIES = [("standard", "Standard"), ("high", "High"), ("max", "Max")]
RATES = [("auto", "Auto"), ("60", "60"), ("30", "30")]
CLIP_LENGTHS = [("15", "15s"), ("30", "30s"), ("60", "1m"), ("120", "2m")]
STEADY = [("off", "Off"), ("light", "Light"), ("strong", "Strong")]  # stabilisation; more is narrower
ON_OFF = [("true", "On"), ("false", "Off")]
BUTTON = [("clip", "Clip"), ("record", "Record"), ("both", "Both")]  # what the quick row's button does
VALID = {"shape": SHAPES, "eye": EYES, "quality": QUALITIES, "fps": RATES, "clip": CLIP_LENGTHS,
         "game_audio": ON_OFF, "mic": ON_OFF, "clips": ON_OFF, "button": BUTTON, "stabilise": STEADY}
# For cuff.remote: (key, label, group, help).
FIELDS = [
    ("button", "Quick action", "Recording", "What the quick row's record button does"),
    ("clips", "Clips", "Recording", "Keep the last few seconds ready to save as a clip"),
    ("clip", "Clip length", "Recording", None),
    ("shape", "Shape", "Video", "Other shapes than 16:9 need the panel helper"),
    ("eye", "Eye", "Video", "With the panel helper, one eye's view"),
    ("stabilise", "Steady", "Video", "Smooths head movement; stronger keeps a narrower view"),
    ("quality", "Quality", "Video", "Standard 20, High 40, Max 80 Mbit/s"),
    ("fps", "Frame rate", "Video", None),
    ("game_audio", "Game audio", "Audio", None),
    ("mic", "Microphone", "Audio", None),
]


def settings_file(config_dir):
    return os.path.join(config_dir, "record", "settings.json")


def load(config_dir):
    """The settings: saved ones over the defaults. The first time, Framecorder's own."""
    values = dict(DEFAULTS)
    path = settings_file(config_dir)
    try:
        with open(path) as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        try:
            with open(os.path.expanduser("~/.config/framecorder/ui.conf")) as f:
                saved = dict(line.strip().split("=", 1) for line in f if "=" in line)
        except OSError:
            saved = {}
    except (OSError, ValueError) as e:
        print(f"record: {path}: {e!r}; using the defaults", file=sys.stderr, flush=True)
        saved = {}
    values.update({k: str(v) for k, v in saved.items() if k in VALID and str(v) in dict(VALID[k])})
    return values


def save(config_dir, values):
    atomic_write_json(settings_file(config_dir), values, indent=1)


def schema(config_dir):
    """cuff.remote: the fields, with their values."""
    values = load(config_dir)
    return [{"key": key, "label": label, "group": group, "help": help, "type": "choice",
             "options": [list(o) for o in VALID[key]], "value": values[key], "default": DEFAULTS[key]}
            for key, label, group, help in FIELDS]


def apply(config_dir, changes):
    """cuff.remote: save changed values; returns a list of problems."""
    values, errors = load(config_dir), []
    for key, value in changes.items():
        if key in VALID and str(value) in dict(VALID[key]):
            values[key] = str(value)
        else:
            errors.append(f"record: {key}={value!r} isn't a choice")
    save(config_dir, values)
    return errors
