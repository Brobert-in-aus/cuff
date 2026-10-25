"""Panel layout, drawing and hit-testing.

Coordinates are logical pixels on a W x H canvas, drawn at SCALE for sharp text.
Top to bottom:
  status bar   time, batteries; never interactive          } fixed: the core draws these,
  quick row    dictation, mic mute, passthrough, recenter, } the same for everyone
               keyboard, screenshot                        }
  tab row      one tab per charm; tapping the open one collapses it (page None): the image
               then ends under the tabs, H_COLLAPSED tall instead of H_OPEN
  page         PAGE, drawn by the open charm
Charms draw with the helpers here (s, FONTS, glyph, ellipsize, wrap and the colours).
To add a quick action: an entry in QUICK, a glyph in glyph(), and its handler in quick.py.
"""
import math
import sys

from PIL import Image, ImageDraw, ImageFont

from . import charm as charms
from .config import PROFILE

W, SCALE = 240, 2

BG = (16, 18, 26, 235)
CARD = (32, 36, 48, 255)
BUTTON = (48, 54, 70, 255)
PRESSED = (30, 215, 96, 255)
FG = (240, 242, 248, 255)
DIM = (150, 156, 172, 255)
GREEN = (30, 215, 96, 255)
LOW = (226, 58, 58, 255)
PASSTHROUGH_ON = (52, 120, 128, 255)
KEYBOARD_ON = (66, 92, 160, 255)
DISABLED = (36, 40, 52, 255)
TAB_ACTIVE = (40, 110, 72, 255)
PHASE_COLOUR = {"idle": BUTTON, "listening": (226, 58, 58, 255), "transcribing": (240, 170, 30, 255),
                "done": BUTTON, "error": BUTTON}

QUICK_Y, QUICK_H = 32, 24  # the quick-actions row
QUICK = ["dictation", "mic", "passthrough", "recenter", "keyboard", "screenshot"]  # left to right
TABS_Y, TAB_H = 60, 22  # the tab row
PAGE_Y = 88  # top of the page card
PAGE = (8, PAGE_Y, W - 8, PAGE_Y + 134)  # the open charm's area
H_OPEN = PAGE[3] + 8  # with a tab open
H_COLLAPSED = TABS_Y + TAB_H + 6  # with every tab closed
H = H_OPEN  # the layout the coordinates belong to

# id -> (x0, y0, x1, y1): the quick row and tabs on every page, plus the open charm's buttons.
_qw = (W - 16 - 4 * (len(QUICK) - 1)) / len(QUICK)
QUICK_BUTTONS = {f"q:{name}": (8 + i * (_qw + 4), QUICK_Y, 8 + i * (_qw + 4) + _qw, QUICK_Y + QUICK_H)
                 for i, name in enumerate(QUICK)}


SETTINGS_TAB = "settings"  # the core's own tab, always last, drawn as a gear
MAX_TABS = 3  # other tabs shown at once without a pager
PAGED_TABS = 3  # with one: ‹ and › take room
PAGER_W, GEAR_W = 18, 30


def _split(tabs):
    """(the tabs that page, whether the settings gear is there): the gear is pinned at the end."""
    others = [t for t in tabs if t[0] != SETTINGS_TAB]
    return others, len(others) != len(tabs)


def _per_page(others):
    return MAX_TABS if len(others) <= MAX_TABS else PAGED_TABS


def tab_pages(tabs):
    """How many pages the tab row has (the gear is on every one)."""
    others, _ = _split(tabs)
    return max(1, -(-len(others) // _per_page(others)))


def tab_page_of(tabs, page):
    """The tab page showing charm `page` (0 if it isn't there, or for the gear)."""
    others, _ = _split(tabs)
    ids = [i for i, _ in others]
    return ids.index(page) // _per_page(others) if page in ids else 0


def tab_buttons(tabs, tab_page=0):
    """"tab:<charm id>" -> box for the tabs on tab page `tab_page`, sharing the row equally.
    The settings gear is pinned at the right end; with more than MAX_TABS other tabs,
    "tabs:prev" and "tabs:next" page through them, PAGED_TABS at a time."""
    if not tabs:
        return {}
    others, gear = _split(tabs)
    x0, x1, out = 8, W - 8, {}
    if gear:
        out[f"tab:{SETTINGS_TAB}"] = (x1 - GEAR_W, TABS_Y, x1, TABS_Y + TAB_H)
        x1 -= GEAR_W + 4
    per = _per_page(others)
    if len(others) > MAX_TABS:
        out["tabs:prev"] = (x0, TABS_Y, x0 + PAGER_W, TABS_Y + TAB_H)
        out["tabs:next"] = (x1 - PAGER_W, TABS_Y, x1, TABS_Y + TAB_H)
        x0, x1 = x0 + PAGER_W + 4, x1 - PAGER_W - 4
    shown = others[tab_page * per:(tab_page + 1) * per]
    if shown:
        slots = per if len(others) > MAX_TABS else len(shown)  # a short last page keeps tab widths
        tw = (x1 - x0 - 4 * (slots - 1)) / slots
        out.update({f"tab:{page}": (x0 + i * (tw + 4), TABS_Y, x0 + i * (tw + 4) + tw, TABS_Y + TAB_H)
                    for i, (page, _) in enumerate(shown)})
    return out


def buttons(tabs, charm=None, tab_page=0):
    """Every touchable box; the open charm's buttons are prefixed "c:"."""
    page_buttons = {}
    if charm is not None:
        own = charms.call(charm, "buttons")
        if own is not charms.FAILED:
            page_buttons = {f"c:{k}": box for k, box in own.items()}
    return {**QUICK_BUTTONS, **tab_buttons(tabs, tab_page), **page_buttons}


def height(page):
    """Logical height of the panel image for this page (None: every tab closed)."""
    return H_OPEN if page else H_COLLAPSED


def _font(name, size):
    return ImageFont.truetype(f"{PROFILE['font_dir']}/{name}", size * SCALE)


FONTS = {"title": _font("NotoSans-Bold.ttf", 14), "body": _font("NotoSans-Regular.ttf", 12),
         "small": _font("NotoSans-Medium.ttf", 10), "clock": _font("NotoSans-Bold.ttf", 12),
         "clock_suffix": _font("NotoSans-Medium.ttf", 9), "battery": _font("NotoSans-Medium.ttf", 9),
         "clip": _font("NotoSans-Bold.ttf", 9), "clip_small": _font("NotoSans-Bold.ttf", 8),
         "clip_tiny": _font("NotoSans-Bold.ttf", 7), "big": _font("NotoSans-Bold.ttf", 30),
         "medium": _font("NotoSans-Bold.ttf", 18), "tab_two": _font("NotoSans-Medium.ttf", 9)}


def hit(x, y, tabs, charm=None, tab_page=0):
    for name, (x0, y0, x1, y1) in buttons(tabs, charm, tab_page).items():
        if x0 <= x < x1 and y0 <= y < y1:
            return name
    return None


def s(*v):
    """Logical coordinates to image pixels."""
    return [int(round(c * SCALE)) for c in v]


def ellipsize(text, font, width):
    if font.getlength(text) <= width * SCALE:
        return text
    while text and font.getlength(text + "…") > width * SCALE:
        text = text[:-1]
    return text + "…"


def wrap(text, font, width, lines):
    out, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if font.getlength(trial) <= width * SCALE:
            line = trial
            continue
        out.append(line)
        line = word
        if len(out) == lines:
            break
    if len(out) < lines and line:
        out.append(line)
    if len(out) == lines and " ".join(out) != " ".join(text.split()):
        out[-1] = ellipsize(out[-1] + " …", font, width)
    return [ln for ln in out if ln]


def glyph(d, name, box, colour, bg=BG):
    """Transport and quick-action icons as plain shapes, centred in box. bg: what's behind
    it, for the holes some glyphs cut (the gear's centre)."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

    def tri(left, right, top, bottom, pointing_right=True):
        pts = ([(left, top), (right, (top + bottom) / 2), (left, bottom)] if pointing_right
               else [(right, top), (left, (top + bottom) / 2), (right, bottom)])
        d.polygon([tuple(s(*p)) for p in pts], fill=colour)

    if name == "play":
        tri(cx - 5, cx + 7, cy - 8, cy + 8)
    elif name == "pause":
        d.rectangle(s(cx - 7, cy - 8, cx - 2, cy + 8), fill=colour)
        d.rectangle(s(cx + 2, cy - 8, cx + 7, cy + 8), fill=colour)
    elif name == "next":
        tri(cx - 7, cx + 4, cy - 7, cy + 7)
        d.rectangle(s(cx + 4, cy - 7, cx + 7, cy + 7), fill=colour)
    elif name == "previous":
        tri(cx - 4, cx + 7, cy - 7, cy + 7, pointing_right=False)
        d.rectangle(s(cx - 7, cy - 7, cx - 4, cy + 7), fill=colour)
    elif name in ("volume-down", "volume-up"):
        d.rectangle(s(cx - 9, cy - 3, cx - 6, cy + 3), fill=colour)
        tri(cx - 9, cx - 1, cy - 7, cy + 7, pointing_right=False)
        d.rectangle(s(cx + 2, cy - 1, cx + 9, cy + 1), fill=colour)
        if name == "volume-up":
            d.rectangle(s(cx + 4.5, cy - 3.5, cx + 6.5, cy + 3.5), fill=colour)
    elif name == "dictation":  # speech bubble with three dots
        d.rounded_rectangle(s(cx - 9, cy - 7, cx + 9, cy + 4), radius=4 * SCALE, fill=colour)
        d.polygon([tuple(s(cx - 5, cy + 3)), tuple(s(cx - 6, cy + 8)), tuple(s(cx, cy + 3))], fill=colour)
        for dx in (-4.5, 0, 4.5):
            d.ellipse(s(cx + dx - 1.4, cy - 2.9, cx + dx + 1.4, cy - 0.1), fill=BG)
    elif name == "eye":
        d.ellipse(s(cx - 9, cy - 5.5, cx + 9, cy + 5.5), outline=colour, width=2 * SCALE)
        d.ellipse(s(cx - 3, cy - 3, cx + 3, cy + 3), fill=colour)
    elif name == "recenter":  # target with ticks
        d.ellipse(s(cx - 6.5, cy - 6.5, cx + 6.5, cy + 6.5), outline=colour, width=2 * SCALE)
        d.ellipse(s(cx - 1.8, cy - 1.8, cx + 1.8, cy + 1.8), fill=colour)
        for a, b, c, e in ((cx - 10, cy - 1, cx - 7, cy + 1), (cx + 7, cy - 1, cx + 10, cy + 1),
                           (cx - 1, cy - 10, cx + 1, cy - 7), (cx - 1, cy + 7, cx + 1, cy + 10)):
            d.rectangle(s(a, b, c, e), fill=colour)
    elif name == "keyboard":
        d.rounded_rectangle(s(cx - 10, cy - 6, cx + 10, cy + 6), radius=2 * SCALE, outline=colour, width=SCALE)
        for y in (cy - 3, cy):
            for k in range(5):
                x = cx - 7.5 + k * 3.75
                d.rectangle(s(x - 1, y - 0.8, x + 1, y + 0.8), fill=colour)
        d.rectangle(s(cx - 5, cy + 2.5, cx + 5, cy + 3.8), fill=colour)
    elif name == "screenshot":  # camera
        d.rounded_rectangle(s(cx - 9, cy - 5, cx + 9, cy + 7), radius=2 * SCALE, fill=colour)
        d.rectangle(s(cx - 4, cy - 8, cx + 4, cy - 4), fill=colour)
        d.ellipse(s(cx - 4, cy - 3, cx + 4, cy + 5), fill=BG)
        d.ellipse(s(cx - 2, cy - 1, cx + 2, cy + 3), fill=colour)
    elif name == "gear":  # eight teeth round a ring
        teeth, r_out, r_in = 8, 7.5, 5.6
        pts = []
        for k in range(teeth * 4):
            a = math.pi * 2 * (k + 0.5) / (teeth * 4)
            r = r_out if k % 4 in (1, 2) else r_in
            pts.append(tuple(s(cx + r * math.cos(a), cy + r * math.sin(a))))
        d.polygon(pts, fill=colour)
        d.ellipse(s(cx - 2.4, cy - 2.4, cx + 2.4, cy + 2.4), fill=bg)
    elif name in ("mic", "mic-muted"):
        k = (y1 - y0) / 52  # drawn for a 52 px tall box, scaled to the box
        d.rounded_rectangle(s(cx - 6 * k, cy - 16 * k, cx + 6 * k, cy + 4 * k), radius=6 * k * SCALE, fill=colour)
        d.arc(s(cx - 11 * k, cy - 8 * k, cx + 11 * k, cy + 10 * k), 0, 180, fill=colour,
              width=max(1, round(3 * k * SCALE)))
        d.rectangle(s(cx - 1.5 * k, cy + 10 * k, cx + 1.5 * k, cy + 15 * k), fill=colour)
        d.rectangle(s(cx - 7 * k, cy + 14 * k, cx + 7 * k, cy + 17 * k), fill=colour)
        if name == "mic-muted":  # a slash, with a gap cut in the background colour
            d.line(s(cx - 12 * k, cy - 16 * k, cx + 12 * k, cy + 17 * k), fill=BG, width=round(5 * k * SCALE))
            d.line(s(cx - 12 * k, cy - 16 * k, cx + 12 * k, cy + 17 * k), fill=colour, width=round(2.5 * k * SCALE))


# Status bar sizes: compact enough for "12:52 pm" plus four batteries (headset, controllers,
# Bluetooth earbuds when connected) in the 208 px between the margins.
BATTERY_GAP = 3  # between battery groups
BAT_LABEL_GAP, BAT_ICON_W, BAT_TEXT_GAP = 1.5, 10, 2.5  # label | icon (+ nub) | number


def _battery_text(level):
    return "–" if level is None else f"{round(level * 100)}"


HEADPHONES_W = 7.5  # the earbuds' label, drawn as a glyph


def _label_width(label):
    return HEADPHONES_W if label == "E" else FONTS["battery"].getlength(label) / SCALE


def _battery_width(label, level):
    return _label_width(label) + FONTS["battery"].getlength(_battery_text(level)) / SCALE + \
        BAT_LABEL_GAP + BAT_ICON_W + 1.2 + BAT_TEXT_GAP


def _headphones(d, x, y, colour):
    """A headphones glyph in a HEADPHONES_W box: headband arc and two ear cups.
    Snapped to a whole pixel, with every x offset a whole pixel too, so both sides round the
    same way (at fractional positions one cup came out a pixel narrower)."""
    x = round(x * SCALE) / SCALE
    w = HEADPHONES_W
    d.arc(s(x + 0.5, y + 3.2, x + w - 0.5, y + 11.5), 180, 360, fill=colour, width=round(1.2 * SCALE))
    d.rounded_rectangle(s(x, y + 7.2, x + 2.5, y + 11.6), radius=0.8 * SCALE, fill=colour)
    d.rounded_rectangle(s(x + w - 2.5, y + 7.2, x + w, y + 11.6), radius=0.8 * SCALE, fill=colour)


POWER_COLOUR = {"charging": GREEN, "plugged": (240, 170, 30, 255)}  # else white, red when low


def _battery(d, x, y, label, level, state):
    """`label [■■□] 85` at x, y (top-left); returns the x after it. level is 0-1 or None.
    state: "charging" (green, with a bolt), "plugged" (amber, with a bolt: powered but still
    draining), or "battery"."""
    font = FONTS["battery"]
    if label == "E":
        _headphones(d, x, y, DIM)
    else:
        d.text(s(x, y + 0.5), label, font=font, fill=DIM)
    x += _label_width(label) + BAT_LABEL_GAP
    w = BAT_ICON_W
    d.rounded_rectangle(s(x, y + 3.5, x + w, y + 11.5), radius=2 * SCALE, outline=DIM, width=SCALE)
    d.rectangle(s(x + w, y + 5.7, x + w + 1.2, y + 9.3), fill=DIM)
    colour = POWER_COLOUR.get(state) or (LOW if level is not None and level < 0.2 else FG)
    if level is not None:
        d.rectangle(s(x + 1.8, y + 5.3, x + 1.8 + (w - 3.6) * level, y + 9.7), fill=colour)
    if state in POWER_COLOUR:  # a bolt across the icon, cut out of the fill
        k = w / 12  # the bolt is drawn for a 12 px icon
        bolt = [(x + 6.9 * k, y + 3.8), (x + 3.9 * k, y + 7.9), (x + 6 * k, y + 7.9), (x + 5.1 * k, y + 11.2),
                (x + 8.2 * k, y + 7), (x + 6 * k, y + 7)]
        d.polygon([tuple(s(*pt)) for pt in bolt], fill=BG, outline=colour)
    x += w + 1.2 + BAT_TEXT_GAP
    text = _battery_text(level)
    d.text(s(x, y + 0.5), text, font=font, fill=colour if state in POWER_COLOUR else FG)
    return x + font.getlength(text) / SCALE


def _clock(d, x, y, time_str):
    """"12:52 pm" as bold digits with a smaller am/pm; returns the x after it."""
    digits, _, suffix = time_str.partition(" ")
    d.text(s(x, y), digits, font=FONTS["clock"], fill=FG)
    x += FONTS["clock"].getlength(digits) / SCALE + 1.5
    if suffix:
        d.text(s(x, y + 3), suffix, font=FONTS["clock_suffix"], fill=DIM)
        x += FONTS["clock_suffix"].getlength(suffix) / SCALE
    return x


class Marquee:
    """One line of text in box, scrolling when it doesn't fit (a charm's marquee()).

    It is drawn as its own small overlay over the panel, so scrolling re-uploads only this
    strip. Overflowing text pauses, scrolls left, and wraps round with a gap.
    """

    GAP, SPEED, PAUSE = 30, 30, 2.0  # logical px, px/s, s

    def __init__(self, title, box):
        self.title, self.box = title, box
        x0, y0, x1, y1 = box
        self.w, self.h = int((x1 - x0) * SCALE), int((y1 - y0) * SCALE)
        font = FONTS["title"]
        text_w = int(font.getlength(title)) + 1
        self.scrolls = text_w > self.w
        self.period = (text_w + self.GAP * SCALE) if self.scrolls else 0
        self.strip = Image.new("RGBA", (max(self.w, self.period + self.w), self.h), CARD)
        d = ImageDraw.Draw(self.strip)
        d.text((0, 2 * SCALE), title, font=font, fill=FG)
        if self.scrolls:
            d.text((self.period, 2 * SCALE), title, font=font, fill=FG)
        self.start = None

    def offset(self, t):
        """Scroll position in strip pixels at time t (seconds)."""
        if not self.scrolls:
            return 0
        if self.start is None:
            self.start = t
        cycle = self.PAUSE + self.period / (self.SPEED * SCALE)
        into = (t - self.start) % cycle
        return 0 if into < self.PAUSE else int((into - self.PAUSE) * self.SPEED * SCALE) % self.period

    def frame(self, offset):
        return self.strip.crop((offset, 0, offset + self.w, self.h))


TOAST_H = 24  # the toast row's height, logical px; it sits just above the panel


def toast_image(text):
    """A toast: one line on a rounded strip as wide as the panel, for the row above it."""
    img = Image.new("RGBA", (W * SCALE, TOAST_H * SCALE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(s(0, 0, W, TOAST_H), radius=11 * SCALE, fill=TOAST)
    font = FONTS["small"]
    text = ellipsize(text, font, W - 24)
    d.text(s(W / 2 - font.getlength(text) / SCALE / 2, 5), text, font=font, fill=FG)
    return img


def silhouette(fill):
    """The open panel's outline filled with one colour (the settings placement preview)."""
    img = Image.new("RGBA", (W * SCALE, H_OPEN * SCALE), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle(s(0, 0, W, H_OPEN), radius=18 * SCALE, fill=fill)
    return img


TOAST = (66, 92, 160, 255)


def _badge_width(badge):
    return 5 if badge == "dot" else FONTS["clip_tiny"].getlength(str(badge)[:4]) / SCALE + 5


def _badge(d, x, cy, badge):
    """A charm's tab badge, starting at x beside its label: a dot, or a short text pill."""
    if badge == "dot":
        d.ellipse(s(x, cy - 2.5, x + 5, cy + 2.5), fill=GREEN)
        return
    text, font = str(badge)[:4], FONTS["clip_tiny"]
    d.rounded_rectangle(s(x, cy - 5, x + _badge_width(badge), cy + 5), radius=5 * SCALE, fill=GREEN)
    d.text(s(x + 2.5, cy - 4.5), text, font=font, fill=(16, 18, 26, 255))


def render(status, quick, stt, pressed, tabs, page, charm, title=True, tab_page=0, toast=None, badges=None):
    """status: {"time": str, "batteries": [(label, level or None, "charging"|"plugged"|"battery")]}
    quick: {"mic_muted", "passthrough", "keyboard", "keyboard_target", "stt_up"}; stt: Dictation.state.
    tabs: [(charm id, label)]; page: the open charm's id, or None with every tab closed (a
    shorter image); charm: that charm. title: let the charm draw its marquee text (False when
    a Marquee layer draws it). tab_page: which MAX_TABS tabs the tab row shows.
    toast: unused (toasts are their own layer, toast_image()); badges: {charm id: badge}."""
    quick = quick or {}
    h = height(page)
    img = Image.new("RGBA", (W * SCALE, h * SCALE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(s(0, 0, W, h), radius=18 * SCALE, fill=BG)

    # Status bar: time on the left, batteries right-aligned. Never interactive.
    if status:
        _clock(d, 16, 9, status["time"])
        groups = status["batteries"]
        width = sum(_battery_width(lb, lv) for lb, lv, _ in groups) + BATTERY_GAP * (len(groups) - 1)
        x = W - 16 - width
        for label, level, state in groups:
            x = _battery(d, x, 12, label, level, state) + BATTERY_GAP

    # Quick actions. Toggles show their state as a fill; actions only flash when pressed.
    for name in QUICK:
        key = f"q:{name}"
        box = QUICK_BUTTONS[key]
        fill, colour = BUTTON, FG
        if name == "dictation":
            fill = PHASE_COLOUR.get(stt["phase"], BUTTON)
            if not stt.get("configured", True) or (quick.get("stt_up") is False and stt["phase"] == "idle"):
                colour = DIM  # no push-to-talk command, or its server isn't answering
        elif name == "mic" and quick.get("mic_muted"):
            fill = LOW
        elif name == "passthrough" and quick.get("passthrough_ok") is False:
            fill, colour = DISABLED, DIM  # SteamVR's mailbox isn't there or didn't work
        elif name == "passthrough" and quick.get("passthrough"):
            fill = PASSTHROUGH_ON
        elif name == "keyboard" and quick.get("keyboard"):
            fill = KEYBOARD_ON
        elif name == "keyboard" and quick.get("keyboard_target") is False:
            fill, colour = DISABLED, DIM  # no window to type into
        count = quick.get("countdown") if name == "screenshot" else None
        if count:
            fill = PHASE_COLOUR["transcribing"]  # amber while the screenshot counts down
        if pressed == key:
            fill = PRESSED
        d.rounded_rectangle(s(*box), radius=8 * SCALE, fill=fill)
        if count:  # 3, 2, 1 in place of the camera
            font, text = FONTS["medium"], str(count)
            x0, y0, x1, y1 = box
            d.text(s((x0 + x1) / 2, (y0 + y1) / 2), text, font=font, fill=FG, anchor="mm")
            continue
        icon = {"mic": "mic-muted" if quick.get("mic_muted") else "mic", "passthrough": "eye"}.get(name, name)
        glyph(d, icon, box, colour)

    # Tabs: the open one filled; tapping it again closes it. ‹ › page through more than
    # MAX_TABS; dimmed at the first and last page, with a dot while the open tab is elsewhere.
    pages, labels = tab_pages(tabs), dict(tabs)
    for key, (x0, y0, x1, y1) in tab_buttons(tabs, tab_page).items():
        if key.startswith("tabs:"):
            forward = key == "tabs:next"
            usable = tab_page < pages - 1 if forward else tab_page > 0
            d.rounded_rectangle(s(x0, y0, x1, y1), radius=8 * SCALE,
                                fill=PRESSED if pressed == key and usable else BUTTON if usable else DISABLED)
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            tip, back = (cx + 3, cx - 2.5) if forward else (cx - 3, cx + 2.5)
            d.line([tuple(s(back, cy - 5)), tuple(s(tip, cy)), tuple(s(back, cy + 5))],
                   fill=FG if usable else DIM, width=2 * SCALE, joint="curve")
            open_page = tab_page_of(tabs, page) if page and page != SETTINGS_TAB else tab_page
            if (open_page > tab_page) if forward else (open_page < tab_page):
                d.ellipse(s(cx - 2, y1 - 6, cx + 2, y1 - 2), fill=GREEN)
            continue
        page_id, label = key[4:], labels[key[4:]]
        active = page == page_id
        fill = PRESSED if pressed == key else TAB_ACTIVE if active else BUTTON
        d.rounded_rectangle(s(x0, y0, x1, y1), radius=8 * SCALE, fill=fill)
        if page_id == SETTINGS_TAB:
            glyph(d, "gear", (x0, y0, x1, y1), FG if active else DIM, bg=fill)
            continue
        # The label, with the charm's badge (if any, and not while it's open) beside it.
        badge = None if active else (badges or {}).get(page_id)
        extra = _badge_width(badge) + 2 if badge else 0
        font, room = FONTS["small"], x1 - x0 - 6 - extra
        colour = FG if active else DIM
        two = label.split(" ", 1)
        if (font.getlength(label) > room * SCALE and len(two) == 2 and not badge
                and all(FONTS["tab_two"].getlength(t) <= room * SCALE for t in two)):
            # A two-word label too long for one line ("Now Playing"): one word a line.
            for i, t in enumerate(two):
                d.text(s((x0 + x1) / 2, y0 + 6 + i * 10), t, font=FONTS["tab_two"], fill=colour, anchor="mm")
            continue
        text = ellipsize(label, font, room)
        tx = (x0 + x1) / 2 - (font.getlength(text) / SCALE + extra) / 2
        d.text(s(tx, y0 + 4), text, font=font, fill=colour)
        if badge:
            _badge(d, tx + font.getlength(text) / SCALE + 2, (y0 + y1) / 2, badge)

    if page and charm is not None:
        d.rounded_rectangle(s(*PAGE), radius=12 * SCALE, fill=CARD)
        own = pressed[2:] if pressed and pressed.startswith("c:") else None
        tree = charms.call(charm, "page")
        if tree is not charms.FAILED and tree is not None:
            from . import widgets  # it imports this module
            try:
                charm._layout = widgets.Layout(tree)
                widgets.draw(img, d, charm._layout, own, title)
                tree = True
            except Exception as e:  # noqa: BLE001 - a bad tree is the charm's fault, not ours
                print(f"charm {charm.ctx.id}: page layout: {e!r}", file=sys.stderr, flush=True)
                charm._layout, tree = None, charms.FAILED
        if tree is True:
            pass
        elif tree is charms.FAILED or charms.call(charm, "draw", img, d, own, title) is charms.FAILED:
            d.text(s(18, PAGE_Y + 14), f"{charm.label} stopped (see the log)", font=FONTS["body"], fill=DIM)
    return img
