"""Charms: one tab each, loaded from folders.

A charm is a Python package folder with a charm.toml:

  id = "now-playing"
  name = "Now playing"
  api = 1
  entry = "now_playing:NowPlaying"    module in the folder : Charm subclass
  permissions = ["pc"]                 what it uses (below)
  [tab]
  label = "Spotify"
  [settings.speaker]                   settings, read as ctx.settings["speaker"]
  type = "text"                        text, bool, int or choice (with choices = [...])
  default = "Steam Frame"
  label = "Spotify Connect speaker"

Bundled charms live in charms/ next to this package; more folders can be added with
CUFF_CHARM_PATH (os.pathsep-separated), which is how private charms are loaded without
being part of Cuff.

The page: return a widget tree from page() (widgets.py) and the core lays it out, draws it,
hit-tests it and scrolls its scrolling line; or, for a page widgets can't express, draw it
yourself with draw() and give buttons() and marquee().

Permissions say what a charm uses. Charms are ordinary Python in Cuff's process, so this is
a statement and a gate, not a sandbox:
  basic      toasts, frame-stats                 granted when declared
  standard   pc, network                         granted when declared, shown in Settings
  powerful   audio (changes the audio routing),  the charm stays off until approved in
             commands (runs programs)            Settings > Charms (or approved = [...] in
                                                 config.toml); Cuff's own bundled charms
                                                 count as approved
A Context call a charm didn't declare (notify without "toasts", stats without
"frame-stats", pc_url without "pc") raises PermissionError.
"""
import importlib
import importlib.util
import json
import os
import re
import sys
import time
import tomllib

from . import config

API = 1
BUNDLED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "charms")
FAILED = object()  # call()'s result when the charm raised
PERMISSIONS = {"toasts": "basic", "frame-stats": "basic", "pc": "standard", "network": "standard",
               "audio": "powerful", "commands": "powerful"}
CHOICES_FILE = os.path.join(os.path.dirname(config.CONFIG_FILE), "charms.json")  # Settings > Charms
SETTINGS_DIR = os.path.join(os.path.dirname(config.CONFIG_FILE), "charms")


def load_choices():
    """{"disabled": [ids], "approved": [ids]}, with config.toml's approved added."""
    try:
        with open(CHOICES_FILE) as f:
            choices = json.load(f)
    except (OSError, ValueError):
        choices = {}
    return {"disabled": list(choices.get("disabled", [])),
            "approved": sorted(set(choices.get("approved", [])) | set(config.CONFIG.get("approved", [])))}


def save_choices(choices):
    try:
        os.makedirs(os.path.dirname(CHOICES_FILE), exist_ok=True)
        with open(CHOICES_FILE, "w") as f:
            json.dump(choices, f, indent=1)
    except OSError as e:
        print(f"charms: saving choices: {e!r}", file=sys.stderr, flush=True)


class Context:
    """What the core hands a charm: changed() asks for a redraw; data_dir holds what its
    installer put there; cache_dir is for state it remembers. shown is True while its page is
    open (so a charm that ticks can redraw only then). notify() raises a toast; stats() and
    stats_history() are the compositor's frame statistics (services.FrameStats); pc_url() and
    pc_headers() reach Cuff for PC, and pc_state() says whether it's there (pc.py). settings
    is the charm's settings: charm.toml's defaults, overridden by
    ~/.config/cuff/charms/<id>.json."""

    def __init__(self, charm_id, root, changed, services=None, permissions=(), settings=None):
        self.id, self.root, self.changed = charm_id, root, changed
        self.data_dir = os.path.join(config.DATA_DIR, charm_id)
        self.cache_dir = os.path.join(config.CACHE_DIR, charm_id)
        self.services = services
        self.permissions = set(permissions)
        self.shown = False
        self.settings = dict(settings or {})
        try:
            with open(os.path.join(SETTINGS_DIR, f"{charm_id}.json")) as f:
                self.settings.update(json.load(f))
        except (OSError, ValueError):
            pass

    def _need(self, permission):
        if permission not in self.permissions and self.id != "settings":
            raise PermissionError(f"charm {self.id} didn't declare the {permission!r} permission")

    def notify(self, text, haptic=False, glow="white", icon=None):
        """A one-line toast above the panel. haptic makes it an alert: a buzz on the wrist, a
        chime, and, while the panel is turned away, a glow on the hand (glow: "white",
        "orange" or "red"), with icon in its middle if given: a PIL image, or a PNG's path in
        the charm's folder."""
        self._need("toasts")
        if isinstance(icon, str):
            icon = self._icon(icon)
        if self.services:
            self.services.toasts.push(self.id, text, haptic, glow if glow in ("white", "orange", "red") else "white",
                                      icon)

    def _icon(self, name):
        cache = self.__dict__.setdefault("_icons", {})
        if name not in cache:
            try:
                from PIL import Image
                cache[name] = Image.open(os.path.join(self.root, name)).convert("RGBA")
            except (OSError, TypeError) as e:
                print(f"charm {self.id}: icon {name}: {e!r}", file=sys.stderr, flush=True)
                cache[name] = None
        return cache[name]

    def stats(self):
        self._need("frame-stats")
        return self.services.frames.latest() if self.services else None

    def stats_history(self):
        self._need("frame-stats")
        return self.services.frames.history() if self.services else []

    def pc_url(self):
        """Cuff for PC's base URL ("http://host:8765"), or None while it isn't reachable."""
        self._need("pc")
        pc = self.services and self.services.pc
        return pc.url() if pc else None

    def pc_headers(self):
        """The headers a request to Cuff for PC needs (its pairing token)."""
        self._need("pc")
        pc = self.services and self.services.pc
        return pc.headers() if pc else {}

    def pc_state(self):
        """"connected", "unpaired" (there, not paired yet), "away" (known but not reachable
        now) or "missing" (never seen). Needs no permission."""
        pc = self.services and self.services.pc
        return pc.state() if pc else "missing"


class Charm:
    """Subclass this. Every method runs on the core's threads: keep them quick and do slow
    work on a worker thread of your own, calling self.ctx.changed() when the page should redraw."""

    label = "?"  # from charm.toml's [tab] label

    def __init__(self, ctx):
        self.ctx = ctx
        self._layout = None  # the last page() laid out, kept by the core

    def start(self):
        """Once, after SteamVR is up: start worker threads here."""

    def page(self):
        """The page as a widget tree (widgets.py), or None to draw() it instead."""
        return None

    def press(self, button_id):
        """A button on the page was touched."""

    def badge(self):
        """A mark on this charm's tab, seen while another tab is open: None, "dot", or a
        short text (a count, "5m"). Asked on every redraw, so call changed() when it changes."""
        return None

    def sample(self):
        """Fill in sample state for `cuff render`, without any services running."""

    # For draw()-charms; a page() charm gets these from its layout.

    def draw(self, img, d, pressed, title=True):
        """Draw the page into img (an RGBA image at ui.SCALE) with the ImageDraw d.
        pressed: the button id being touched, or None. title: draw the marquee() text too
        (False when the core shows it as its own scrolling layer)."""

    def buttons(self):
        """{button id: (x0, y0, x1, y1)} inside ui.PAGE."""
        return self._layout.buttons if self._layout else {}

    def marquee(self):
        """(text, box) for one line that scrolls when it doesn't fit, or None."""
        return self._layout.marquee if self._layout else None


_last_error = {}


def call(charm, method, *args):
    """charm.method(*args), or FAILED if it raised (logged at most once a minute per charm)."""
    try:
        return getattr(charm, method)(*args)
    except Exception as e:  # noqa: BLE001 - a charm must never take the overlay down
        now = time.monotonic()
        if now - _last_error.get(charm.ctx.id, 0) > 60:
            _last_error[charm.ctx.id] = now
            print(f"charm {charm.ctx.id}: {method}: {e!r}", file=sys.stderr, flush=True)
        return FAILED


def _folders():
    dirs = [BUNDLED] + [p for p in os.environ.get("CUFF_CHARM_PATH", "").split(os.pathsep) if p]
    for d in dirs:
        d = os.path.expanduser(d)
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            if os.path.isfile(os.path.join(d, name, "charm.toml")):
                yield os.path.join(d, name)


def _import(folder, charm_id, entry):
    """The charm folder imported as a package, so its modules can import each other."""
    module_name, _, class_name = entry.partition(":")
    pkg = "cuff_charm_" + re.sub(r"\W", "_", charm_id)
    spec = importlib.util.spec_from_file_location(pkg, os.path.join(folder, "__init__.py"),
                                                  submodule_search_locations=[folder])
    package = importlib.util.module_from_spec(spec)
    sys.modules[pkg] = package
    spec.loader.exec_module(package)
    return getattr(importlib.import_module(f"{pkg}.{module_name}"), class_name)


# Every charm found on the last load(), loaded or not, for Settings > Charms:
# [{"id", "name", "folder", "permissions", "powerful", "status", "error"}], status one of
# "on", "off" (turned off), "needs approval", "failed".
CATALOG = []


def load(changed, services=None):
    """Every charm that's on and loads, as {id: Charm} in tab order (config's tabs first)."""
    choices = load_choices()
    found = {}
    CATALOG.clear()
    for folder in _folders():
        entry = {"id": None, "name": os.path.basename(folder), "folder": folder, "permissions": [],
                 "powerful": [], "status": "failed", "error": None}
        CATALOG.append(entry)
        try:
            with open(os.path.join(folder, "charm.toml"), "rb") as f:
                manifest = tomllib.load(f)
            charm_id = entry["id"] = manifest["id"]
            entry["name"] = manifest.get("name", charm_id)
            if manifest.get("api") != API:
                raise ValueError(f"needs charm API {manifest.get('api')}, this is {API}")
            if charm_id in found or charm_id == "settings":
                raise ValueError(f"id {charm_id} is already taken")
            perms = list(manifest.get("permissions", []))
            unknown = [p for p in perms if p not in PERMISSIONS]
            if unknown:
                raise ValueError(f"unknown permissions {unknown}")
            entry["permissions"] = perms
            entry["powerful"] = [p for p in perms if PERMISSIONS[p] == "powerful"]
            if charm_id in choices["disabled"]:
                entry["status"] = "off"
                continue
            bundled = os.path.dirname(os.path.abspath(folder)) == os.path.abspath(BUNDLED)
            if entry["powerful"] and not bundled and charm_id not in choices["approved"]:
                entry["status"] = "needs approval"
                print(f"charm {charm_id}: off until approved in Settings > Charms ({', '.join(entry['powerful'])})",
                      flush=True)
                continue
            defaults = {k: v.get("default") for k, v in manifest.get("settings", {}).items()}
            cls = _import(folder, charm_id, manifest["entry"])
            charm = cls(Context(charm_id, folder, changed, services, perms, defaults))
            charm.label = manifest.get("tab", {}).get("label", entry["name"])
            found[charm_id] = charm
            entry["status"] = "on"
            print(f"charm {charm_id}: loaded from {folder}", flush=True)
        except Exception as e:  # noqa: BLE001 - one bad charm mustn't stop the rest
            entry["error"] = repr(e)
            print(f"charm {folder}: not loaded: {e!r}", file=sys.stderr, flush=True)
    order = [i for i in config.CONFIG.get("tabs", []) if i in found]
    order += [i for i in found if i not in order]
    tabs = {i: found[i] for i in order}
    from . import settings  # the core's own tab, always last
    tabs[settings.ID] = settings.make(changed, services)
    return tabs
