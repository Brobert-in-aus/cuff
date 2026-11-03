"""Now playing: what's playing in any MPRIS player and its controls (media.py picks the
player: whatever is playing first, then Spotify).

- The page: album art, title and artist; where it's up to, with a scrub bar to tap
  (a Scrub widget of 16 steps; MPRIS SetPosition) and the time left; back 15 s, previous, play/pause, next,
  forward 15 s; and the player's own volume, with − and + either side.
- The source under the title ("Spotify · 70%", "Chromium") is a button: it opens the player
  page, to pick which player to show while more than one is open ("Auto" goes back to
  whatever's playing), and the playback speed (MPRIS Rate) where the player allows it.
- The tab is "Now Playing", or "Spotify" while it shows this headset's Spotify Connect
  speaker (Soloist while it's Spotify's active device, or spotifyd).

The volume buttons step the player's own Volume by 5%, separate from the system sink; for a
Connect speaker that's the Spotify app's slider. Players without a writable Volume, Seek or
Rate leave those greyed out.

Its quick-row button, play/pause (charm.toml's [quick.play-pause]), can be put on the quick row
in Settings > Quick; it's greyed out with no player.

With nothing playing, the page says so, and if Soloist is set up here, how to play Spotify on
it ("Pick <its name> in the Spotify app").
"""
from cuff import Charm
from cuff import widgets as w

from .media import Media

LABELS = {False: "Now Playing", True: "Spotify"}
SCRUB_STEPS = 16  # the scrub bar's touchable steps
RATES = [1.0, 1.25, 1.5, 2.0]
MAX_PLAYERS = 3  # listed on the player page


def clock(seconds):
    m, s = divmod(int(max(0, seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class NowPlaying(Charm):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.media = Media(self._changed, shown=lambda: ctx.shown)
        self.sub = "main"  # or "players"

    def _changed(self):
        self.label = LABELS[self.media.state["spotify"]]
        self.ctx.changed()

    def start(self):
        self.media.start()

    def quick(self, button):
        m = self.media.state
        if not m.get("player"):
            return {"glyph": "play", "style": "off"}
        return {"glyph": "pause" if m["playing"] else "play", "style": "normal"}

    def press(self, button_id):
        if button_id == "quick:play-pause":
            self.media.command("play-pause")
        elif button_id == "players":
            self.sub = "players"
        elif button_id == "back":
            self.sub = "main"
        elif button_id.startswith("goto:"):
            # A step of the scrub bar: the middle of that step.
            self.media.command(f"goto:{(int(button_id[5:]) + 0.5) / SCRUB_STEPS:.4f}")
        else:
            if button_id.startswith("player:"):
                self.sub = "main"
            self.media.command(button_id)

    def sample(self):
        self.media.state = {"title": "Everything In Its Right Place", "artist": "Radiohead", "playing": True,
                            "player": "soloist", "source": "Spotify", "spotify": True, "art": None, "volume": 0.7,
                            "position": 83, "length": 251.0, "can_seek": True, "rate": 1.0, "rates": (0.5, 2.0),
                            "players": [("soloist", "Spotify", True), ("mpv", "mpv", False)], "chosen": False}
        self.label = LABELS[True]

    def page(self):
        return self._players() if self.sub == "players" else self._main()

    def _main(self):
        m = self.media.state
        source = m["source"] or ""
        if m.get("volume") is not None:
            source += f" · {round(m['volume'] * 100)}%"
        info = w.Column([w.Text(m["title"], size="title", scroll=True, height=20),
                         w.Text(m["artist"], size="small", colour="dim"),
                         w.Spacer(),
                         w.Button("players", f"{source}  ›" if source else "Players  ›", height=16)], gap=2, grow=1)
        top = w.Row([w.Image(m["art"], (48, 48), glyph="play"), info], height=48, gap=8)

        seek = m.get("can_seek") and m.get("length")
        position, length = m.get("position"), m.get("length")
        done = position / length if seek and position is not None else 0.0
        scrub = w.Row([w.Text(clock(position) if position is not None else "", size="tiny", colour="dim", width=30),
                       w.Scrub("goto", done, steps=SCRUB_STEPS, enabled=bool(seek), grow=1),
                       w.Text(f"-{clock(length - (position or 0))}" if length else "", size="tiny", colour="dim",
                              width=34, align="right")], height=12, gap=4)

        skip = "normal" if m.get("can_seek") else "off"
        controls = w.Row([w.Button("seek-back", "-15", style=skip),
                          w.Button("previous", glyph="previous"),
                          w.Button("play-pause", glyph="pause" if m["playing"] else "play"),
                          w.Button("next", glyph="next"),
                          w.Button("seek-forward", "+15", style=skip)], height=30, gap=5)
        has_volume = m.get("volume") is not None
        volume = w.Row([w.Button("volume-down", glyph="volume-down", width=40, style="normal" if has_volume else "off"),
                        w.Bar(m["volume"] if has_volume else 0.0, height=4, grow=1),
                        w.Button("volume-up", glyph="volume-up", width=40, style="normal" if has_volume else "off")],
                       height=20, gap=6)
        return w.Column([top, scrub, controls, volume], gap=5)

    def _players(self):
        m = self.media.state
        rows = [w.Button("player:auto", "Auto: whatever's playing", height=18,
                         style="normal" if m.get("chosen") else "active")]
        for name, identity, playing in m.get("players", [])[:MAX_PLAYERS]:
            current = m.get("chosen") and m.get("player") == name
            rows.append(w.Button(f"player:{name}", identity + (" · playing" if playing else ""), height=18,
                                 style="active" if current else "normal"))
        if not m.get("players"):
            rows.append(w.Text("No media players are open", size="small", colour="dim", align="center"))
        rows.append(w.Spacer())
        low, high = m.get("rates") or (1.0, 1.0)
        rate = m.get("rate")
        rows.append(w.Row([w.Button("back", "‹ Back", width=50)] + [
            w.Button(f"rate:{r}", f"{r:g}×", style=("active" if rate is not None and abs(rate - r) < 0.01 else "normal")
                     if rate is not None and low <= r <= high else "off")
            for r in RATES], height=20, gap=4))
        return w.Column(rows, gap=4)
