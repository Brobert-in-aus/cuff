"""Widgets: what a charm's page() returns. The core lays them out in the page, draws them in
Cuff's style, works out the touch targets, and runs a scrolling line as its own layer.

  from cuff import widgets as w
  def page(self):
      return w.Column([
          w.Tabs([("timer", "Timer"), ("watch", "Stopwatch")], selected=self.sub, id="sub"),
          w.Text("04:32", size="big", colour="green", align="center"),
          w.Row([w.Button("go", "Start", style="active"), w.Button("reset", "Reset")], height=32),
      ])

A Button's id comes back to press(); a Tabs' press is "<its id>:<item id>".
Sizes: "tiny", "small", "body", "title", "medium", "big". Colours: "fg", "dim", "green",
"amber", "red", "blue", or an (r, g, b, a) tuple. Button styles: "normal", "active" (green,
selected), "accent" (blue), "warn" (amber), "off" (greyed out), or fill=(r, g, b, a).
Heights are logical pixels; grow shares out what's left, in proportion.
"""
from PIL import Image as PILImage
from PIL import ImageDraw

from . import ui

COLOURS = {"fg": ui.FG, "dim": ui.DIM, "green": ui.GREEN, "amber": ui.POWER_COLOUR["plugged"],
           "red": ui.LOW, "blue": ui.TOAST}
FONT = {"tiny": "clip_small", "small": "small", "body": "body", "title": "title", "medium": "medium", "big": "big"}
STYLE_FILL = {"normal": ui.BUTTON, "active": ui.TAB_ACTIVE, "accent": ui.KEYBOARD_ON,
              "warn": ui.POWER_COLOUR["plugged"], "off": ui.DISABLED}
GAP = 4
BOX = (16, ui.PAGE_Y + 4, ui.W - 16, ui.PAGE_Y + 130)  # the page's inner area


def colour(c):
    return COLOURS.get(c, ui.FG) if isinstance(c, str) else c


def font(size):
    return ui.FONTS[FONT.get(size, "body")]


class Node:
    height = None  # natural height; None to share what's left (with grow)

    def __init__(self, grow=0, height=None, width=None):
        self.grow, self.width = grow, width
        if height is not None:
            self.height = height
        self.box = None

    def natural(self):
        return self.height


class Spacer(Node):
    def __init__(self, height=None, grow=0, width=None):
        """A fixed gap (height), or with neither height nor grow, a share of what's left."""
        super().__init__(grow=grow or (1 if height is None else 0), height=height or 0, width=width)


class Text(Node):
    def __init__(self, text, size="body", colour="fg", align="left", lines=1, scroll=False, **kw):
        super().__init__(**kw)
        self.text, self.size, self.colour, self.align, self.lines, self.scroll = str(text), size, colour, align, lines, scroll
        if self.height is None:
            line = font(size).getbbox("Ag")[3] / ui.SCALE + 2
            self.height = round(line * lines)


class Button(Node):
    height = 22

    def __init__(self, id, label=None, glyph=None, style="normal", fill=None, size="small", **kw):
        super().__init__(**kw)
        self.id, self.label, self.glyph, self.style, self.fill, self.size = id, label, glyph, style, fill, size


class Bar(Node):
    height = 4

    def __init__(self, value, colour="green", marker=None, vertical=False, **kw):
        """value and marker 0-1; marker draws a tick (say, the value before a change)."""
        super().__init__(**kw)
        self.value, self.colour, self.marker, self.vertical = max(0.0, min(1.0, value)), colour, marker, vertical


class Image(Node):
    def __init__(self, image, size, radius=8, glyph=None, **kw):
        """image: a PIL image (drawn scaled to size, logical px), or None for a tile, with
        glyph drawn on it if given."""
        super().__init__(height=size[1], width=size[0], **kw)
        self.image, self.radius, self.glyph = image, radius, glyph


class Tabs(Node):
    height = 18

    def __init__(self, items, selected, id="tab", **kw):
        """items: [(item id, label)]; selected: an item id. Pressing one gives "<id>:<item id>"."""
        super().__init__(**kw)
        self.items, self.selected, self.id = items, selected, id


class Container(Node):
    def __init__(self, children, gap=GAP, **kw):
        super().__init__(**kw)
        self.children, self.gap = [c for c in children if c is not None], gap


class Column(Container):
    def natural(self):
        if self.height is not None:
            return self.height
        if any(c.natural() is None or c.grow for c in self.children):
            return None
        return sum(c.natural() for c in self.children) + self.gap * max(0, len(self.children) - 1)


class Row(Container):
    def natural(self):
        if self.height is not None:
            return self.height
        heights = [c.natural() for c in self.children]
        return max([h for h in heights if h is not None] or [Button.height])


def Grid(children, cols, gap=GAP, row_height=None, **kw):
    """Children in rows of cols, the last row padded so every cell is the same width."""
    rows = []
    for i in range(0, len(children), cols):
        cells = list(children[i:i + cols])
        cells += [Spacer(height=0) for _ in range(cols - len(cells))]
        rows.append(Row(cells, gap=gap, height=row_height, grow=0 if row_height else 1))
    return Column(rows, gap=gap, **kw)


# ---------------------------------------------------------------- layout

class Layout:
    """A page laid out: every node has .box; buttons maps id -> box; marquee is (text, box)
    of the first scrolling Text, or None."""

    def __init__(self, root, box=BOX):
        self.root, self.buttons, self.marquee = root, {}, None
        self._place(root, box)

    def _place(self, node, box):
        node.box = box
        x0, y0, x1, y1 = box
        if isinstance(node, Column):
            free = (y1 - y0) - node.gap * max(0, len(node.children) - 1)
            fixed = sum(c.natural() or 0 for c in node.children if c.natural() is not None and not c.grow)
            growers = [c for c in node.children if c.natural() is None or c.grow]
            weight = sum(c.grow or 1 for c in growers) or 1
            spare = max(0, free - fixed)
            y = y0
            for c in node.children:
                h = (spare * (c.grow or 1) / weight) if c in growers else c.natural()
                self._place(c, (x0, y, x1, y + h))
                y += h + node.gap
        elif isinstance(node, Row):
            free = (x1 - x0) - node.gap * max(0, len(node.children) - 1)
            fixed = sum(c.width for c in node.children if c.width)
            sharers = [c for c in node.children if not c.width]
            weight = sum(c.grow or 1 for c in sharers) or 1
            x = x0
            for c in node.children:
                w = c.width if c.width else max(0, free - fixed) * (c.grow or 1) / weight
                h = c.natural() if c.natural() is not None and not isinstance(c, Container) else (y1 - y0)
                cy = y0 + ((y1 - y0) - h) / 2 if h < (y1 - y0) else y0
                self._place(c, (x, cy, x + w, cy + min(h, y1 - y0)))
                x += w + node.gap
        elif isinstance(node, Button):
            self.buttons[node.id] = box
        elif isinstance(node, Tabs):
            n = len(node.items)
            w = ((x1 - x0) - 4 * (n - 1)) / max(1, n)
            for i, (item, _) in enumerate(node.items):
                self.buttons[f"{node.id}:{item}"] = (x0 + i * (w + 4), y0, x0 + i * (w + 4) + w, y1)
        elif isinstance(node, Text) and node.scroll and self.marquee is None:
            self.marquee = (node.text, tuple(round(v) for v in box))


# ---------------------------------------------------------------- drawing

def _centred_text(d, text, f, box, fill):
    x0, y0, x1, y1 = box
    text = ui.ellipsize(text, f, x1 - x0 - 4)
    w = f.getlength(text) / ui.SCALE
    top, bottom = f.getbbox(text)[1] / ui.SCALE, f.getbbox(text)[3] / ui.SCALE
    d.text(ui.s((x0 + x1) / 2 - w / 2, (y0 + y1) / 2 - (top + bottom) / 2), text, font=f, fill=fill)


def draw(img, d, layout, pressed, title=True):
    """Draw a laid-out page. pressed: the button id being touched. title: draw scrolling text
    too (False when the core's own layer shows it)."""
    _draw(img, d, layout.root, pressed, title, layout)


def _draw(img, d, node, pressed, title, layout):
    s = ui.s
    x0, y0, x1, y1 = node.box
    if isinstance(node, Container):
        for c in node.children:
            _draw(img, d, c, pressed, title, layout)
    elif isinstance(node, Text):
        if node.scroll and not title and layout.marquee and layout.marquee[1] == tuple(round(v) for v in node.box):
            return  # the core's scrolling layer draws it
        f = font(node.size)
        lines = ui.wrap(node.text, f, x1 - x0, node.lines) if node.lines > 1 else [ui.ellipsize(node.text, f, x1 - x0)]
        line_h = (y1 - y0) / max(1, node.lines)
        for i, ln in enumerate(lines):
            w = f.getlength(ln) / ui.SCALE
            x = {"center": (x0 + x1) / 2 - w / 2, "right": x1 - w}.get(node.align, x0)
            d.text(s(x, y0 + i * line_h), ln, font=f, fill=colour(node.colour))
    elif isinstance(node, Button):
        fill = ui.PRESSED if pressed == node.id and node.style != "off" else node.fill or STYLE_FILL.get(node.style, ui.BUTTON)
        d.rounded_rectangle(s(*node.box), radius=min(8, (y1 - y0) / 3) * ui.SCALE, fill=fill)
        ink = ui.DIM if node.style == "off" else ui.FG
        if node.glyph:
            ui.glyph(d, node.glyph, node.box, ink, bg=fill)
        if node.label:
            _centred_text(d, node.label, font(node.size), node.box, ink)
    elif isinstance(node, Bar):
        d.rounded_rectangle(s(*node.box), radius=min(x1 - x0, y1 - y0) / 2 * ui.SCALE, fill=ui.BUTTON)
        if node.value > 0:
            fb = ((x0, y1 - (y1 - y0) * node.value, x1, y1) if node.vertical
                  else (x0, y0, x0 + (x1 - x0) * node.value, y1))
            d.rounded_rectangle(s(*fb), radius=min(x1 - x0, y1 - y0) / 2 * ui.SCALE, fill=colour(node.colour))
        if node.marker is not None:
            mx = x0 + (x1 - x0) * node.marker
            d.rectangle(s(mx - 0.75, y0 - 3, mx + 0.75, y1 + 3), fill=ui.DIM)
    elif isinstance(node, Image):
        w, h = round((x1 - x0) * ui.SCALE), round((y1 - y0) * ui.SCALE)
        if node.image is None:
            d.rounded_rectangle(s(*node.box), radius=node.radius * ui.SCALE, fill=ui.BUTTON)
            if node.glyph:
                ui.glyph(d, node.glyph, node.box, ui.GREEN, bg=ui.BUTTON)
            return
        pic = node.image.convert("RGBA").resize((w, h))
        mask = PILImage.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=node.radius * ui.SCALE, fill=255)
        img.paste(pic, tuple(s(x0, y0)), mask)
    elif isinstance(node, Tabs):
        for item, label in node.items:
            key = f"{node.id}:{item}"
            box = layout.buttons[key]
            shown = item == node.selected
            d.rounded_rectangle(s(*box), radius=6 * ui.SCALE,
                                fill=ui.PRESSED if pressed == key else ui.TAB_ACTIVE if shown else ui.BUTTON)
            _centred_text(d, label, font("small"), box, ui.FG if shown else ui.DIM)
