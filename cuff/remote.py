"""Cuff's settings and recordings for Cuff for PC, which runs this on the headset over SSH:

  python -m cuff.remote dump          every setting, with its choices and value, as JSON
  python -m cuff.remote apply < JSON  {section id: {key: value}}: saves them, then tells Cuff
                                      (SIGHUP) to take up the live ones; prints {"errors",
                                      "restart": whether a change waits for a Cuff restart}
  python -m cuff.remote videos        the clips and recordings, and the screenshots, as JSON

Each prints a JSON object with "api": the protocol version (API), for Cuff for PC to compare.

Sections, and when their changes take effect:
  placement  the panel's size and place (placement.json)                  live
  quick      the quick row's six slots (quick.json)                       live
  alerts     how alerts get attention, charging, low battery (alerts.json) live
  typing     whether typing from Cuff for PC is accepted (alerts.json)    live
  <charm>    a charm's own settings: charm.toml's options = "<module>",   live, through the
             a module with schema(config_dir) and apply(config_dir,       charm's reload()
             values) (Record's options.py); or its [settings] tables,     at a restart
             kept in charms/<id>.json
  charms     which charms are on, and the tab order (charms.json)         at a restart

It reads charm.toml files and doesn't load charms, so it runs beside a running Cuff.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import tomllib

from . import charm as charms
from . import config, placement, quickrow
from .atomic import atomic_write_json
from .services import Prefs

# The dump's protocol version, for Cuff for PC to compare: bumped when a change would break a
# PC app that reads the old shape.
API = 1

CONFIG_DIR = os.path.dirname(config.CONFIG_FILE)
VIDEOS = os.path.expanduser("~/Videos/framecorder")
RECORDING_NOW_S = 10  # a video written to this recently is probably still being recorded
SCREENSHOTS = os.path.expanduser(config.PROFILE["screenshot_dir"])
SHOT_FILE = re.compile(r"^(.+)-(vr|preview)\.png$")  # app.py's pair: <time>-vr.png, <time>-preview.png


def _manifests():
    """[(folder, manifest)] for every charm folder found."""
    out = []
    for folder in charms._folders():
        try:
            with open(os.path.join(folder, "charm.toml"), "rb") as f:
                out.append((folder, tomllib.load(f)))
        except (OSError, tomllib.TOMLDecodeError):
            continue
    return out


def _options_module(folder, manifest):
    name = manifest.get("options")
    if not name:
        return None
    spec = importlib.util.spec_from_file_location(f"cuff_options_{manifest['id']}", os.path.join(folder, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundled(folder):
    return os.path.dirname(os.path.abspath(folder)) == os.path.abspath(charms.BUNDLED)


# ---------------------------------------------------------------- reading

def _placement():
    values = placement.Placement._load()
    fields = [{"key": "hand", "label": "Wrist", "group": "Place", "type": "choice",
               "options": [["left", "Left"], ["right", "Right"]], "value": values["hand"],
               "help": "The other controller touches it"}]
    for page, sliders in placement.PAGES.items():
        for s in sliders:
            fields.append({"key": s.key, "label": s.label, "group": page, "type": "number",
                           "min": s.lo, "max": s.hi, "step": s.step, "unit": s.unit, "scale": s.scale,
                           "value": values[s.key], "default": placement.defaults()[s.key]})
    return {"id": "placement", "title": "Wrist placement", "live": True, "fields": fields}


def _quick(manifests):
    offered = [[i, label, glyph] for i, (label, glyph) in quickrow.CORE.items()]
    for _, m in manifests:
        try:
            for button, (label, glyph) in quickrow.declared(m).items():
                offered.append([f"{m['id']}:{button}", f"{label} ({m.get('name', m['id'])})", glyph])
        except (ValueError, KeyError):
            continue
    return {"id": "quick", "title": "Quick row", "live": True, "fields": [
        {"key": "row", "label": "Buttons", "type": "slots", "slots": quickrow.SLOTS, "options": offered,
         "value": quickrow.row(), "default": quickrow.DEFAULT,
         "help": "Left to right. A charm that's off leaves its button greyed out."}]}


def _alerts():
    prefs = Prefs()
    labels = {"haptic": "Buzz the wrist", "sound": "Chime", "orb": "Glow on the hand"}
    fields = [{"key": k, "label": labels[k], "group": "How alerts get your attention", "type": "bool",
               "value": prefs.get(k), "default": True} for k in labels]
    fields.append({"key": "power", "label": "Charging stops", "group": "Charging", "type": "bool",
                   "value": prefs.get("power"), "default": True,
                   "help": "An alert when the headset stops charging: a cable pulled, or a pack that can't keep up"})
    for device, label in Prefs.DEVICE_LABELS:
        d, default = prefs.device(device), Prefs.DEVICES[device]
        group = f"Low battery: {label}"
        fields += [{"key": f"{device}.on", "label": "Alert", "group": group, "type": "bool", "value": d["on"],
                    "default": default["on"]},
                   {"key": f"{device}.0", "label": "First, at", "group": group, "type": "number", "unit": "%",
                    "min": 0, "max": Prefs.LEVEL_MAX, "step": Prefs.LEVEL_STEP, "scale": 1, "value": d["levels"][0],
                    "default": default["levels"][0], "help": "Orange; 0 is off"},
                   {"key": f"{device}.1", "label": "Then, at", "group": group, "type": "number", "unit": "%",
                    "min": 0, "max": Prefs.LEVEL_MAX, "step": Prefs.LEVEL_STEP, "scale": 1, "value": d["levels"][1],
                    "default": default["levels"][1], "help": "Red; 0 is off"}]
    return {"id": "alerts", "title": "Alerts", "live": True, "fields": fields}


def _typing():
    """Typing from Cuff for PC (cuff/pc.py), kept with the alert switches in alerts.json."""
    return {"id": "typing", "title": "Typing from the PC", "live": True, "fields": [
        {"key": "typing", "label": "Accept typing", "type": "bool", "value": Prefs().get("typing"), "default": True,
         "help": "Text typed in Cuff for PC's Type page, or its clipboard, goes to what has focus here"}]}


def _charm_list(manifests):
    choices = charms.load_choices()
    order = [i for i in choices.get("order") or config.CONFIG.get("tabs", [])]
    ids = [m["id"] for _, m in manifests if "id" in m]
    order = [i for i in order if i in ids] + [i for i in ids if i not in order]
    fields = [{"key": "order", "label": "Tab order", "type": "order", "value": order,
               "options": [[m["id"], m.get("name", m["id"])] for _, m in manifests if "id" in m],
               "help": "Settings is always last"}]
    for folder, m in manifests:
        if "id" not in m:
            continue
        perms = list(m.get("permissions", []))
        powerful = [p for p in perms if charms.PERMISSIONS.get(p) == "powerful"]
        needs = m.get("requires")
        missing = bool(needs and not os.path.exists(os.path.expanduser(needs)))
        help = ", ".join(perms) or "no permissions"
        if powerful and not _bundled(folder):
            help += " · turning it on approves " + ", ".join(powerful)
        if missing:
            help += " · not installed"
        fields.append({"key": m["id"], "label": m.get("name", m["id"]), "group": "Charms", "type": "bool",
                       "value": m["id"] not in choices["disabled"] and (not powerful or _bundled(folder)
                                                                         or m["id"] in choices["approved"]),
                       "help": help, "default": True})
    return {"id": "charms", "title": "Charms and tabs", "live": False, "fields": fields}


def _charm_settings(folder, m):
    module = _options_module(folder, m)
    if module is not None:
        return {"id": m["id"], "title": m.get("name", m["id"]), "live": True, "fields": module.schema(CONFIG_DIR)}
    spec = m.get("settings", {})
    if not spec:
        return None
    saved = _saved_charm_settings(m)
    fields = []
    for key, s in spec.items():
        kind = {"bool": "bool", "int": "number", "choice": "choice"}.get(s.get("type"), "text")
        field = {"key": key, "label": s.get("label", key), "type": kind, "value": saved.get(key, s.get("default")),
                 "default": s.get("default"), "help": s.get("help")}
        if kind == "choice":
            field["options"] = [[c, str(c)] for c in s.get("choices", [])]
        if kind == "number":
            field.update({"min": s.get("min"), "max": s.get("max"), "step": s.get("step", 1), "scale": 1, "unit": ""})
        fields.append(field)
    return {"id": m["id"], "title": m.get("name", m["id"]), "live": False, "fields": fields}


def _saved_charm_settings(m):
    """A charm's charms/<id>.json; {} if there's none, or (said on stderr) if it can't be read."""
    path = os.path.join(charms.SETTINGS_DIR, f"{m['id']}.json")
    try:
        with open(path) as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            raise ValueError("not an object")
        return saved
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        print(f"remote: {path}: {e!r}; using charm.toml's defaults", file=sys.stderr, flush=True)
        return {}


def dump():
    manifests = _manifests()
    sections = [_placement(), _quick(manifests), _alerts(), _typing()]
    for folder, m in manifests:
        if "id" in m:
            try:
                section = _charm_settings(folder, m)
            except Exception as e:  # noqa: BLE001 - one charm's options mustn't hide the rest
                section = {"id": m["id"], "title": m.get("name", m["id"]), "live": False, "fields": [],
                           "error": repr(e)}
            if section:
                sections.append(section)
    sections.append(_charm_list(manifests))
    return {"api": API, "sections": sections, "recording": recording_now()}


# ---------------------------------------------------------------- writing

def _apply_placement(values):
    current = placement.Placement._load()
    for key, value in values.items():
        if key == "hand" and value in placement.HANDS:
            current["hand"] = value
        elif key in placement.SLIDERS:
            current[key] = placement.SLIDERS[key].clamp(float(value))
    defaults = placement.defaults()
    out = {k: v for k, v in current.items() if v != defaults.get(k)}
    atomic_write_json(placement.FILE, out, indent=1)


def _apply_alerts(values):
    prefs = Prefs()
    for key, value in values.items():
        device, _, part = key.partition(".")
        if key in Prefs.DEFAULTS:
            prefs.values[key] = bool(value)
        elif device in prefs.devices and part == "on":
            prefs.devices[device]["on"] = bool(value)
        elif device in prefs.devices and part in ("0", "1"):
            prefs.devices[device]["levels"][int(part)] = max(0, min(Prefs.LEVEL_MAX, int(value) // Prefs.LEVEL_STEP
                                                                    * Prefs.LEVEL_STEP))
    prefs._save()


def _apply_charms(values, manifests):
    choices = charms.load_choices()
    folders = {m["id"]: f for f, m in manifests if "id" in m}
    approved = set(choices["approved"])  # save_choices leaves config.toml's out
    disabled = set(choices["disabled"])
    order = choices.get("order")
    for key, value in values.items():
        if key == "order":
            order = [i for i in value if i in folders]
        elif key in folders:
            if value:
                disabled.discard(key)
                if not _bundled(folders[key]):
                    approved.add(key)
            else:
                disabled.add(key)
    out = {"disabled": sorted(disabled), "approved": sorted(approved)}
    if order:
        out["order"] = order
    charms.save_choices(out)


def _apply_charm_settings(folder, m, values):
    module = _options_module(folder, m)
    if module is not None:
        return module.apply(CONFIG_DIR, values)
    saved = _saved_charm_settings(m)
    spec = m.get("settings", {})
    saved.update({k: v for k, v in values.items() if k in spec})
    atomic_write_json(os.path.join(charms.SETTINGS_DIR, f"{m['id']}.json"), saved, indent=1)
    return []


def apply(changes):
    manifests = _manifests()
    by_id = {m["id"]: (f, m) for f, m in manifests if "id" in m}
    errors, restart = [], False
    for section, values in changes.items():
        try:
            if section == "placement":
                _apply_placement(values)
            elif section == "quick":
                quickrow.save(list(values.get("row", quickrow.row())))
            elif section in ("alerts", "typing"):
                _apply_alerts(values)
            elif section == "charms":
                _apply_charms(values, manifests)
                restart = True
            elif section in by_id:
                folder, m = by_id[section]
                errors += _apply_charm_settings(folder, m, values)
                restart = restart or not m.get("options")
            else:
                errors.append(f"unknown section {section}")
        except (ValueError, TypeError, OSError) as e:
            errors.append(f"{section}: {e}")
    reload_cuff()
    return {"errors": errors, "restart": restart}


def reload_cuff():
    """Ask the running Cuff to take up the live settings (app.py's SIGHUP handler)."""
    subprocess.run(["systemctl", "--user", "kill", "--kill-whom=main", "--signal=HUP", "cuff"],
                   capture_output=True, timeout=10)


# ---------------------------------------------------------------- recordings and screenshots

def recording_now():
    """Whether a video was written to in the last few seconds (a recording going)."""
    newest = max((v["mtime"] for v in videos()), default=0)
    return time.time() - newest < RECORDING_NOW_S


def videos():
    """[{"path" (under ~/Videos/framecorder), "kind": clip|recording, "size", "mtime"}], newest first."""
    out = []
    for sub, kind in (("", "recording"), ("clips", "clip")):
        folder = os.path.join(VIDEOS, sub)
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".mp4"):
                continue
            try:
                st = os.stat(os.path.join(folder, name))
            except OSError:
                continue
            out.append({"path": f"{sub}/{name}" if sub else name, "kind": kind, "size": st.st_size,
                        "mtime": st.st_mtime})
    return sorted(out, key=lambda v: v["mtime"], reverse=True)


def screenshots():
    """[{"name" (the pair's <time>), "vr", "preview" (file names, or None when missing),
    "sizes" ({file name: bytes}), "size" (both), "mtime" (the newer)}], newest first."""
    shots = {}
    try:
        names = os.listdir(SCREENSHOTS)
    except OSError:
        return []
    for name in names:
        m = SHOT_FILE.match(name)
        if not m:
            continue
        try:
            st = os.stat(os.path.join(SCREENSHOTS, name))
        except OSError:
            continue
        shot = shots.setdefault(m[1], {"name": m[1], "vr": None, "preview": None, "sizes": {}, "size": 0, "mtime": 0})
        shot[m[2]] = name
        shot["sizes"][name] = st.st_size
        shot["size"] += st.st_size
        shot["mtime"] = max(shot["mtime"], st.st_mtime)
    return sorted(shots.values(), key=lambda s: s["mtime"], reverse=True)


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "dump":
        out = dump()
    elif cmd == "apply":
        out = {"api": API, **apply(json.load(sys.stdin))}
    elif cmd == "videos":
        out = {"api": API, "folder": VIDEOS, "videos": videos(), "recording": recording_now(),
               "screenshot_folder": SCREENSHOTS, "screenshots": screenshots()}
    else:
        print(__doc__, file=sys.stderr)
        return 2
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
