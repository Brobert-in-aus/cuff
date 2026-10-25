"""The Settings tab: built into Cuff, always the last tab, shown as a gear.

It's a charm like any other, but part of the core, since it changes the core's own state
(where the panel sits, which charms run). Its page has sub-tabs:
  Place   Size, Along arm (from the controller tip toward the elbow), Height (off the wrist);
          and on Place and Angle, Wrist: Left or Right (pending until Apply too)
  Angle   Around (the wrist), Tilt (the top edge away from you), Twist (in its own plane)
  Charms  each charm with what it's allowed to do, turned on or off, or approved when it
          asks for a powerful permission; Restart Cuff applies the changes (charms load at
          startup), as systemd starts it again
Each slider row is a label with its value, - and + buttons, and a bar whose segments set
the value directly. Changes are pending until Apply: meanwhile the core shows a translucent
preview of the panel where it will go, so the panel doesn't move under the finger. Cancel
drops them; Reset sets the defaults (also pending until Apply), but only on a second tap: the
first turns it into "Reset?" for RESET_CONFIRM_S.
"""
import os
import signal
import threading
import time

from PIL import Image, ImageDraw

from . import charm as charms
from . import ui
from . import widgets as w
from .charm import Charm, Context
from .placement import PAGES, PLACEMENT, SLIDERS

ID = "settings"
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
Y = ui.PAGE_Y
SUBTAB_Y = (Y + 4, Y + 22)
ROW_Y = [Y + 24, Y + 46, Y + 68]  # each row 20 tall
ROW_H = 20
WRIST_Y = Y + 90  # the Wrist row, under the sliders on Place and Angle
WRIST_X = {"left": (100, 160), "right": (164, 224)}
LABEL_X = (16, 70)
MINUS_X = (72, 96)
BAR_X = (100, 196)
PLUS_X = (200, 224)
SEGMENTS = 8  # the bar's tap targets
ACTION_Y = (Y + 112, Y + 130)
RESET_CONFIRM_S = 3.0
CHARMS_PER_PAGE = 3
TEST_DELAY_S = 3.0  # the test alert waits this long, so the panel can be turned away
ACTIONS = {"reset": ((16, 76), "Reset"), "cancel": ((84, 150), "Cancel"), "apply": ((158, 224), "Apply")}
s, FONTS = ui.s, ui.FONTS


SUBPAGES = list(PAGES) + ["Charms", "Alerts"]
ALERT_SWITCHES = [("haptic", "Buzz"), ("sound", "Chime"), ("orb", "Glow"), ("power", "Charging stops")]
BATTERY_DEVICES = [("headset", "Headset"), ("controllers", "Controllers"), ("headphones", "Headphones")]


def subtab_buttons():
    names = SUBPAGES
    x0, x1, gap = 16, 224, 4
    w = (x1 - x0 - gap * (len(names) - 1)) / len(names)
    return {f"sub:{n}": (x0 + i * (w + gap), SUBTAB_Y[0], x0 + i * (w + gap) + w, SUBTAB_Y[1])
            for i, n in enumerate(names)}


def slider_buttons(page):
    out = {}
    for row, slider in enumerate(PAGES[page]):
        y0, y1 = ROW_Y[row], ROW_Y[row] + ROW_H
        out[f"dec:{slider.key}"] = (MINUS_X[0], y0, MINUS_X[1], y1)
        out[f"inc:{slider.key}"] = (PLUS_X[0], y0, PLUS_X[1], y1)
        seg = (BAR_X[1] - BAR_X[0]) / SEGMENTS
        for n in range(SEGMENTS):
            out[f"bar:{slider.key}:{n}"] = (BAR_X[0] + n * seg, y0, BAR_X[0] + (n + 1) * seg, y1)
    return out


class Settings(Charm):
    label = "Settings"
    icon = "gear"  # drawn on its tab instead of the label

    def __init__(self, ctx):
        super().__init__(ctx)
        self.sub = next(iter(PAGES))
        self.reset_armed = 0.0  # until when a second tap on Reset resets
        self.loaded_choices = charms.load_choices()  # as the charms were loaded
        self.charm_page = 0
        self.alerts_view = None  # "battery" for the low battery levels

    def page(self):
        """Charms and Alerts are widget trees; the sliders are drawn by hand (draw())."""
        if self.sub == "Alerts":
            return self._alerts_page()
        if self.sub != "Charms":
            return None
        choices = charms.load_choices()
        listed = [e for e in charms.CATALOG if e["id"]]
        pages = max(1, -(-len(listed) // CHARMS_PER_PAGE))
        self.charm_page = min(self.charm_page, pages - 1)
        rows = []
        for entry in listed[self.charm_page * CHARMS_PER_PAGE:(self.charm_page + 1) * CHARMS_PER_PAGE]:
            cid, perms = entry["id"], entry["permissions"]
            disabled, approved = cid in choices["disabled"], cid in choices["approved"]
            needs = entry["powerful"] and not approved and entry["status"] == "needs approval"
            detail = ", ".join(perms) if perms else "no permissions"
            status = ("failed: see the log" if entry["status"] == "failed" else
                      "off" if disabled else "needs approval" if needs else "on")
            button = (w.Button(f"approve:{cid}", "Approve", style="warn", width=56) if needs and not disabled else
                      w.Button(f"toggle:{cid}", "Turn on" if disabled else "Turn off", width=56,
                               style="active" if disabled else "normal"))
            rows.append(w.Row([w.Column([w.Text(entry["name"], size="small"),
                                         w.Text(f"{status} · {detail}", size="tiny", colour="amber" if needs else "dim")],
                                        gap=0, grow=1), button], height=22))
        rows.append(w.Spacer())
        nav = ([w.Button("charms:prev", "‹", width=30, style="normal" if self.charm_page else "off"),
                w.Text(f"{self.charm_page + 1} / {pages}", size="tiny", colour="dim", align="center", width=30),
                w.Button("charms:next", "›", width=30, style="normal" if self.charm_page < pages - 1 else "off")]
               if pages > 1 else [])
        restart = ([w.Spacer(), w.Button("restart", "Restart to apply", style="accent", width=100)]
                   if choices != self.loaded_choices else [w.Spacer()])
        rows.append(w.Row(nav + restart, height=20))
        return w.Column([w.Tabs([(n, n) for n in SUBPAGES], selected=self.sub, id="sub"), *rows], gap=4)

    @staticmethod
    def _test_alert(toasts):
        """Three glows at once: white, orange with a PNG (assets/bell.png), red with an image
        drawn in code (Pillow), the two ways a charm can give one an icon."""
        toasts.push("settings-test", "Test alert", True)
        try:
            bell = Image.open(os.path.join(ASSETS, "bell.png")).convert("RGBA")
        except OSError:
            bell = None
        mark = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
        d = ImageDraw.Draw(mark)
        d.rounded_rectangle((40, 12, 56, 62), radius=7, fill=(40, 10, 10, 240))
        d.ellipse((39, 70, 57, 88), fill=(40, 10, 10, 240))
        toasts.glow("orange", bell)
        toasts.glow("red", mark)

    def _battery_page(self, prefs):
        rows = [w.Row([w.Button("alerts:back", "‹ Alerts", width=64), w.Text("Low battery: alert at", size="small",
                                                                            align="center", grow=1)], height=18)]
        for key, label in BATTERY_DEVICES:
            dev = prefs.device(key) if prefs else {"on": True, "levels": [20, 0]}
            cells = [w.Text(label, size="small", width=62),
                     w.Button(f"bat-on:{key}", "On" if dev["on"] else "Off", width=30,
                              style="active" if dev["on"] else "normal")]
            for n, level in enumerate(dev["levels"]):
                off = not dev["on"]
                cells += [w.Button(f"bat-dn:{key}:{n}", "−", width=16, style="off" if off else "normal"),
                          w.Text(f"{level}%" if level else "off", size="tiny", align="center", width=24,
                                 colour="dim" if off or not level else ("amber" if n == 0 else "red")),
                          w.Button(f"bat-up:{key}:{n}", "+", width=16, style="off" if off else "normal")]
            rows.append(w.Row(cells, height=22, gap=2))
        rows.append(w.Text("The glow is orange at the first level, red at the second", size="tiny",
                           colour="dim", align="center"))
        return w.Column([w.Tabs([(n, n) for n in SUBPAGES], selected=self.sub, id="sub"), *rows], gap=4)

    def _alerts_page(self):
        prefs = self.ctx.services.prefs if self.ctx.services else None
        if self.alerts_view == "battery":
            return self._battery_page(prefs)
        switches = []
        for key, label in ALERT_SWITCHES:
            on = prefs.get(key) if prefs else True
            switches.append(w.Button(f"alert:{key}", f"{label}: {'on' if on else 'off'}",
                                     style="active" if on else "normal"))
        switches.append(w.Button("alerts:battery", "Low battery ›"))
        switches.append(w.Button("alert-test", "Test alert", style="accent"))
        return w.Column([w.Tabs([(n, n) for n in SUBPAGES], selected=self.sub, id="sub"),
                         w.Grid(switches, cols=2, row_height=24),
                         w.Text("Timers, break reminders, charging and low battery", size="tiny",
                                colour="dim", align="center")], gap=5)

    def buttons(self):
        if self.sub in ("Charms", "Alerts"):
            return super().buttons()  # from the widget layout
        actions = {f"act:{k}": (x0, ACTION_Y[0], x1, ACTION_Y[1]) for k, ((x0, x1), _) in ACTIONS.items()}
        wrist = {f"wrist:{h}": (x0, WRIST_Y, x1, WRIST_Y + ROW_H) for h, (x0, x1) in WRIST_X.items()}
        return {**subtab_buttons(), **slider_buttons(self.sub), **wrist, **actions}

    def press(self, button_id):
        kind, _, rest = button_id.partition(":")
        armed, self.reset_armed = time.monotonic() < self.reset_armed, 0.0  # any other tap disarms
        if button_id == "act:reset":
            if armed:
                PLACEMENT.reset()
            else:
                self.reset_armed = time.monotonic() + RESET_CONFIRM_S
                timer = threading.Timer(RESET_CONFIRM_S + 0.05, self.ctx.changed)  # back to "Reset"
                timer.daemon = True
                timer.start()
        elif kind == "sub":
            self.sub, self.alerts_view = rest, None
        elif kind == "alert" and self.ctx.services:
            self.ctx.services.prefs.toggle(rest)
        elif kind == "alerts":
            self.alerts_view = None if rest == "back" else rest
        elif kind in ("bat-on", "bat-dn", "bat-up") and self.ctx.services:
            prefs = self.ctx.services.prefs
            if kind == "bat-on":
                prefs.toggle_device(rest)
            elif prefs.device(rest.split(":")[0])["on"]:
                device, n = rest.split(":")
                prefs.step_level(device, int(n), 1 if kind == "bat-up" else -1)
        elif button_id == "alert-test" and self.ctx.services:
            # Delayed, so the wrist can turn away first: the glow only shows while the panel isn't.
            toasts = self.ctx.services.toasts
            toasts.push("settings", f"Turn your wrist away: test alert in {TEST_DELAY_S:.0f} s")
            timer = threading.Timer(TEST_DELAY_S, self._test_alert, (toasts,))
            timer.daemon = True
            timer.start()
        elif button_id in ("charms:prev", "charms:next"):
            self.charm_page = max(0, self.charm_page + (1 if button_id == "charms:next" else -1))
        elif kind in ("toggle", "approve"):
            choices = charms.load_choices()
            if kind == "approve":
                choices["approved"] = sorted(set(choices["approved"]) | {rest})
            elif rest in choices["disabled"]:
                choices["disabled"].remove(rest)
            else:
                choices["disabled"].append(rest)
            charms.save_choices({"disabled": choices["disabled"],
                                 "approved": [a for a in choices["approved"] if a not in charms.config.CONFIG.get("approved", [])]})
        elif button_id == "restart":
            print("settings: restarting Cuff to apply charm changes", flush=True)
            threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM)).start()  # systemd restarts it
        elif kind in ("dec", "inc"):
            PLACEMENT.nudge(rest, -1 if kind == "dec" else 1)
        elif kind == "bar":
            key, n = rest.rsplit(":", 1)
            slider = SLIDERS[key]
            PLACEMENT.set(key, slider.lo + (int(n) + 0.5) / SEGMENTS * (slider.hi - slider.lo))
        elif kind == "wrist":
            if PLACEMENT.pending.get("hand") != rest:
                PLACEMENT.switch_hand()
        elif button_id == "act:apply":
            PLACEMENT.apply()
        elif button_id == "act:cancel":
            PLACEMENT.cancel()

    def sample(self):
        PLACEMENT.set("size", PLACEMENT.applied["size"] + 0.02)
        PLACEMENT.set("tilt", PLACEMENT.applied["tilt"] - 10)

    def draw(self, img, d, pressed, title=True):
        for key, (x0, y0, x1, y1) in subtab_buttons().items():
            shown = key[4:] == self.sub
            d.rounded_rectangle(s(x0, y0, x1, y1), radius=6 * ui.SCALE,
                                fill=ui.PRESSED if pressed == key else ui.TAB_ACTIVE if shown else ui.BUTTON)
            font = FONTS["small"]
            d.text(s((x0 + x1) / 2 - font.getlength(key[4:]) / ui.SCALE / 2, y0 + 3), key[4:], font=font,
                   fill=ui.FG if shown else ui.DIM)
        pending, applied = PLACEMENT.pending, PLACEMENT.applied
        for row, slider in enumerate(PAGES[self.sub]):
            y0 = ROW_Y[row]
            value = pending[slider.key]
            moved = value != applied[slider.key]
            d.text(s(LABEL_X[0], y0), slider.label, font=FONTS["small"], fill=ui.FG)
            d.text(s(LABEL_X[0], y0 + 11), slider.text(value), font=FONTS["battery"],
                   fill=ui.POWER_COLOUR["plugged"] if moved else ui.DIM)
            for key, (bx0, bx1), sign in ((f"dec:{slider.key}", MINUS_X, -1), (f"inc:{slider.key}", PLUS_X, 1)):
                d.rounded_rectangle(s(bx0, y0, bx1, y0 + ROW_H), radius=6 * ui.SCALE,
                                    fill=ui.PRESSED if pressed == key else ui.BUTTON)
                cx, cy = (bx0 + bx1) / 2, y0 + ROW_H / 2
                d.rectangle(s(cx - 4.5, cy - 1, cx + 4.5, cy + 1), fill=ui.FG)
                if sign > 0:
                    d.rectangle(s(cx - 1, cy - 4.5, cx + 1, cy + 4.5), fill=ui.FG)
            # The bar: a track, filled to the pending value, with a tick at the applied one.
            cy = y0 + ROW_H / 2
            frac = (value - slider.lo) / (slider.hi - slider.lo)
            was = (applied[slider.key] - slider.lo) / (slider.hi - slider.lo)
            d.rounded_rectangle(s(BAR_X[0], cy - 2, BAR_X[1], cy + 2), radius=2 * ui.SCALE, fill=ui.BUTTON)
            x = BAR_X[0] + (BAR_X[1] - BAR_X[0]) * frac
            if frac > 0:
                d.rounded_rectangle(s(BAR_X[0], cy - 2, x, cy + 2), radius=2 * ui.SCALE, fill=ui.GREEN)
            if moved:
                wx = BAR_X[0] + (BAR_X[1] - BAR_X[0]) * was
                d.rectangle(s(wx - 0.75, cy - 6, wx + 0.75, cy + 6), fill=ui.DIM)
            d.ellipse(s(x - 5, cy - 5, x + 5, cy + 5), fill=ui.FG)
        # Wrist: which one it's worn on. The current one green; a pending switch amber.
        now_on, will_be = applied.get("hand", "left"), pending.get("hand", "left")
        d.text(s(LABEL_X[0], WRIST_Y), "Wrist", font=FONTS["small"], fill=ui.FG)
        d.text(s(LABEL_X[0], WRIST_Y + 11), "switching" if will_be != now_on else f"{now_on} hand",
               font=FONTS["battery"], fill=ui.POWER_COLOUR["plugged"] if will_be != now_on else ui.DIM)
        for h, (x0, x1) in WRIST_X.items():
            key = f"wrist:{h}"
            fill = (ui.PRESSED if pressed == key else ui.POWER_COLOUR["plugged"] if h == will_be != now_on
                    else ui.TAB_ACTIVE if h == now_on and will_be == now_on else ui.BUTTON)
            d.rounded_rectangle(s(x0, WRIST_Y, x1, WRIST_Y + ROW_H), radius=6 * ui.SCALE, fill=fill)
            label = h.capitalize()
            f = FONTS["small"]
            d.text(s((x0 + x1) / 2 - f.getlength(label) / ui.SCALE / 2, WRIST_Y + 3), label, font=f,
                   fill=ui.FG if h in (now_on, will_be) else ui.DIM)
        changed = PLACEMENT.changed
        armed = time.monotonic() < self.reset_armed
        for key, ((x0, x1), text) in ACTIONS.items():
            bid = f"act:{key}"
            live = changed or key == "reset"
            fill = ui.PRESSED if pressed == bid else (ui.TAB_ACTIVE if key == "apply" and changed else
                                                      ui.BUTTON if live else ui.DISABLED)
            if key == "reset" and armed:
                text, fill = "Reset?", ui.POWER_COLOUR["plugged"]
            d.rounded_rectangle(s(x0, ACTION_Y[0], x1, ACTION_Y[1]), radius=7 * ui.SCALE, fill=fill)
            font = FONTS["small"]
            d.text(s((x0 + x1) / 2 - font.getlength(text) / ui.SCALE / 2, ACTION_Y[0] + 2), text, font=font,
                   fill=ui.FG if live else ui.DIM)


def make(changed, services=None):
    return Settings(Context(ID, None, changed, services))
