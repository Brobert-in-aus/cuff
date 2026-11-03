"""The quick row: which buttons show in its six slots, and what each looks like.

The core offers dictation, mic mute, passthrough, recenter and screenshot. A charm can offer
more in its charm.toml:

  [quick.record]          a button id, unique within the charm
  label = "Record"        its name in Settings > Quick
  glyph = "record"        a ui.glyph() name (else the label's first letters are drawn)

The charm then answers quick(button) with how the button looks now (else it shows plain):

  {"glyph": "stop", "style": "alert", "text": None}

  style   normal; on (teal, a toggle that's on); alert (red: recording, muted, listening);
          busy (amber: a countdown, transcribing); dim (normal, greyed ink); off (greyed
          out, and a press is ignored)
  text    a short label drawn in place of the glyph ("3", "2", "1")

A press arrives as press("quick:<button>"). quick() is asked on every redraw, so keep it
quick and call ctx.changed() when the look changes.

In the row, a core button is its name and a charm's is "<charm id>:<button>". Users choose
the row in Settings > Quick (saved in quick.json next to config.toml). A newly installed
charm's button is offered there, not added, so it never pushes one of yours out. A chosen
button whose charm isn't loaded shows greyed out, keeping its slot.
"""
import json
import os
import sys

from . import config
from .atomic import atomic_write_json

SLOTS = 6
CORE = {"dictation": ("Dictation", "dictation"), "mic": ("Mic mute", "mic"),
        "passthrough": ("Room View", "eye"), "recenter": ("Recenter", "recenter"),
        "screenshot": ("Screenshot", "screenshot")}  # name: (label, glyph)
DEFAULT = ["dictation", "mic", "passthrough", "recenter", "record:record", "screenshot"]
ROW_FILE = os.path.join(os.path.dirname(config.CONFIG_FILE), "quick.json")
STYLES = ("normal", "on", "alert", "busy", "dim", "off")

# "<charm id>:<button>": (label, glyph), for every loaded charm's buttons (charm.load fills it).
OFFERED = {}

_row = None  # the row as last read or saved


def declared(manifest):
    """{button: (label, glyph)} from a charm.toml's [quick.*] tables."""
    out = {}
    for button, spec in manifest.get("quick", {}).items():
        if ":" in button or not isinstance(spec, dict):
            raise ValueError(f"quick button {button!r}: needs an id without ':' and a table")
        out[button] = (str(spec.get("label", button)), spec.get("glyph"))
    return out


def offer(charm_id, manifest):
    for button, spec in declared(manifest).items():
        OFFERED[f"{charm_id}:{button}"] = spec


def offered():
    """[(id, label, glyph)]: the core's buttons, then the charms'."""
    return [(i, *spec) for i, spec in CORE.items()] + [(i, *spec) for i, spec in OFFERED.items()]


def row():
    """The six slots, left to right: a button id, or None for an empty slot."""
    global _row
    if _row is None:
        try:
            with open(ROW_FILE) as f:
                saved = json.load(f).get("row")
        except FileNotFoundError:
            saved = None
        except (OSError, ValueError, AttributeError) as e:
            print(f"quick row: {ROW_FILE}: {e!r}; using the default row", file=sys.stderr, flush=True)
            saved = None
        _row = _clean(saved) if isinstance(saved, list) else list(DEFAULT)
    return list(_row)


def reload():
    """quick.json changed from outside (Cuff for PC): read it again."""
    global _row
    _row = None


def _clean(slots):
    slots = [s if isinstance(s, str) and s else None for s in slots][:SLOTS]
    seen = set()
    for i, s in enumerate(slots):  # one of each
        if s in seen:
            slots[i] = None
        seen.add(s)
    return slots + [None] * (SLOTS - len(slots))


def save(slots):
    global _row
    _row = _clean(slots)
    try:
        atomic_write_json(ROW_FILE, {"row": _row}, indent=1)
    except OSError as e:
        print(f"quick row: saving: {e!r}", file=sys.stderr, flush=True)


def place(slot, button):
    """Put button (an id, or None to empty it) in slot; if it's already in another slot, the
    two swap."""
    slots = row()
    if button is not None and button in slots:
        slots[slots.index(button)] = slots[slot]
    slots[slot] = button
    save(slots)


def reset():
    save(DEFAULT)


def label(button):
    spec = CORE.get(button) or OFFERED.get(button)
    if spec:
        return spec[0]
    return button.partition(":")[2].replace("-", " ").capitalize() if button else ""


def glyph_of(button):
    spec = CORE.get(button) or OFFERED.get(button)
    return spec[1] if spec else None


def core_look(name, quick, stt):
    """How a core button looks, from Quick.state and Dictation.state."""
    if name == "dictation":
        phase = stt.get("phase", "idle")
        style = {"listening": "alert", "transcribing": "busy"}.get(phase, "normal")
        if style == "normal" and (not stt.get("configured", True) or quick.get("stt_up") is False):
            style = "dim"  # no push-to-talk command, or its server isn't answering
        return {"glyph": "dictation", "style": style}
    if name == "mic":
        muted = quick.get("mic_muted")
        return {"glyph": "mic-muted" if muted else "mic", "style": "alert" if muted else "normal"}
    if name == "passthrough":
        if quick.get("passthrough_ok") is False:
            return {"glyph": "eye", "style": "off"}  # SteamVR's mailbox isn't there or didn't work
        return {"glyph": "eye", "style": "on" if quick.get("passthrough") else "normal"}
    if name == "screenshot":
        count = quick.get("countdown")
        return {"glyph": "screenshot", "style": "busy" if count else "normal", "text": str(count) if count else None}
    return {"glyph": name, "style": "normal"}


def looks(slots, tabs, quick, stt, call):
    """{button id: {"glyph", "style", "text"}} for the buttons in slots. tabs: the loaded
    charms by id; call: charm.call, so a failing charm's button shows plain."""
    out = {}
    for button in slots:
        if button is None:
            continue
        if button in CORE:
            out[button] = core_look(button, quick, stt)
            continue
        charm_id, _, own = button.partition(":")
        charm = tabs.get(charm_id)
        base = {"glyph": glyph_of(button), "style": "normal", "text": None}
        if charm is None or button not in OFFERED:
            out[button] = {**base, "style": "off"}  # its charm is off or gone
            continue
        look = call(charm, "quick", own)
        if isinstance(look, dict):
            base.update({k: v for k, v in look.items() if k in base})
            if base["style"] not in STYLES:
                base["style"] = "normal"
        out[button] = base
    return out
