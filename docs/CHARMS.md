# Writing charms

A charm adds one tab to Cuff. It's a folder holding a `charm.toml` and a Python package:

```
my-charm/
  charm.toml
  __init__.py      (empty)
  my_charm.py      the Charm subclass
  icon.png         anything else it needs, for example an alert icon
```

Cuff loads its bundled charms from `charms/`. It also loads any folder listed in
`CUFF_CHARM_PATH` (separated by `:`), for example from a `cuff.service.d` drop-in:

```ini
[Service]
Environment=CUFF_CHARM_PATH=%h/my-charms
```

## charm.toml

```toml
id = "hello"                 # unique; the folder name doesn't matter
name = "Hello"
version = "1.0.0"
api = 1                      # the charm API this was written for
entry = "hello:Hello"        # module in the folder : Charm subclass
permissions = ["toasts"]     # what it uses (below)

[tab]
label = "Hello"

[settings.greeting]          # read as ctx.settings["greeting"]
type = "text"                # text, bool, int or choice (with choices = [...])
default = "Hi"
label = "What to say"
```

A user's own setting values go in `~/.config/cuff/charms/<id>.json`.

## The charm

```python
from cuff import Charm
from cuff import widgets as w


class Hello(Charm):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.count = 0

    def page(self):
        return w.Column([
            w.Text(self.ctx.settings["greeting"], size="title", align="center"),
            w.Text(f"Pressed {self.count} times", colour="dim", align="center", grow=1),
            w.Row([w.Button("press", "Press me", style="active"), w.Button("reset", "Reset")], height=32),
        ])

    def press(self, button_id):
        if button_id == "press":
            self.count += 1
            if self.count == 10:
                self.ctx.notify("Ten presses!", haptic=True)
        else:
            self.count = 0
```

The core redraws a charm's page after each press. To redraw for any other reason, call
`self.ctx.changed()`.

### Methods

| Method | When it's called |
| --- | --- |
| `start()` | Once, after SteamVR is up: start any worker threads here. |
| `page()` | On each redraw: return a widget tree. |
| `press(button_id)` | When a button is touched. Its id comes from the tree. |
| `badge()` | On each redraw: `None`, `"dot"`, or a short text (`"5m"`) shown beside the tab's label while another tab is open. |
| `sample()` | Fill in sample state, for `python -m cuff render out.png <id>`. |
| `draw(img, d, pressed, title)`, `buttons()`, `marquee()` | The escape hatch, for a page widgets can't express: draw it with Pillow yourself (see `charms/performance`). |

Every method runs on Cuff's threads, so keep them quick, and do slow work on a thread of
your own. If a method raises, the error is logged and the tab shows "stopped"; the rest of
the panel carries on.

### Widgets

`cuff/widgets.py` lays the page out in a 208 × 126 area and draws it in Cuff's style.

| Widget | Use |
| --- | --- |
| `Column(children, gap=4)`, `Row(children, gap=4)` | Stack children down or across. In a row they share the width, unless one has `width=` set. |
| `Grid(children, cols)` | Equal cells, in rows. |
| `Text(text, size, colour, align, lines=1, scroll=False)` | Sizes: tiny, small, body, title, medium, big. `scroll=True` scrolls the line when it doesn't fit (one per page). |
| `Button(id, label=None, glyph=None, style="normal")` | Styles: normal, active (green), accent (blue), warn (amber), off (greyed out). Glyphs include play, pause, next, previous, volume-up, volume-down, mic, gear. |
| `Bar(value, colour="green", marker=None)` | A level from 0 to 1, with an optional tick (for example, the previous value). |
| `Image(pil_image, (w, h), glyph=None)` | A picture. Pass `None` for a blank tile, with a glyph on it if given. |
| `Tabs(items, selected, id)` | Sub-tabs: a press arrives as `"<id>:<item id>"`. |
| `Spacer(height=None)` | A fixed gap, or with no height, a share of the space that's left. |

Every widget takes `height=`, `width=` and `grow=`, in logical pixels (the panel is 240
wide). Colours are fg, dim, green, amber, red, blue, or an `(r, g, b, a)` tuple.

### ctx

| Call | Needs | What it does |
| --- | --- | --- |
| `ctx.changed()` | | Redraw the page. |
| `ctx.shown` | | `True` while the page is open, so a ticking charm can redraw only then. |
| `ctx.settings` | | The settings dict: defaults from `charm.toml`, plus the user's own values. |
| `ctx.data_dir`, `ctx.cache_dir` | | Files an installer put there, and state to remember. |
| `ctx.notify(text, haptic=False, glow="white", icon=None)` | toasts | A toast above the panel. `haptic=True` makes it an alert: a buzz, a chime, and a glow on the hand while the panel is turned away. `glow` is white, orange or red; `icon` is a PIL image or a PNG in the charm's folder, drawn in the glow. |
| `ctx.stats()`, `ctx.stats_history()` | frame-stats | SteamVR's frame timing, sampled once a second (see `cuff/services.py`). |
| `ctx.pc_url()`, `ctx.pc_headers()` | pc | Reach Cuff for PC: send `pc_headers()` with every request. |
| `ctx.pc_state()` | | `"connected"`, `"unpaired"`, `"away"` or `"missing"`. Show `cuff.pc.needs_pc(state)` in a PC-only field rather than a guess. |

## Permissions

Charms are ordinary Python inside Cuff's process, so permissions are a statement and a
gate, not a sandbox.

| Tier | Permissions | Granted |
| --- | --- | --- |
| basic | `toasts`, `frame-stats` | When declared. |
| standard | `pc`, `network` | When declared, and shown in Settings > Charms. |
| powerful | `audio` (changes audio routing), `commands` (runs programs) | Only once the user approves the charm in Settings > Charms. Cuff's bundled charms count as approved. |

A `ctx` call whose permission the charm didn't declare raises `PermissionError`.
