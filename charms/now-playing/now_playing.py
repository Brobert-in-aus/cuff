"""Now playing: album art, title, artist, and buttons for previous, play/pause, next, volume
down and volume up. Any MPRIS player works: whatever is playing first, then Spotify (media.py).

The tab is "Now Playing", or "Spotify" while it shows this headset's Spotify Connect speaker
(Soloist while it's Spotify's active device, or spotifyd). Under the title, the source names
the player ("Spotify", "Chromium", "VLC").

The volume buttons step the player's own Volume by 5%, separate from the system sink. For a
Connect speaker that's the Spotify app's slider. A bar under the buttons and a "· 70%" next to
the source show the level. Players without a writable Volume ignore them.

With nothing playing, the page says so, and if Soloist is set up here, how to play Spotify on
it ("Pick <its name> in the Spotify app").
"""
from cuff import Charm
from cuff import widgets as w

from .media import Media

BUTTONS = [("previous", "previous"), ("play-pause", None), ("next", "next"), ("volume-down", "volume-down"),
           ("volume-up", "volume-up")]
LABELS = {False: "Now Playing", True: "Spotify"}


class NowPlaying(Charm):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.media = Media(self._changed)

    def _changed(self):
        self.label = LABELS[self.media.state["spotify"]]
        self.ctx.changed()

    def start(self):
        self.media.start()

    def press(self, button_id):
        self.media.command(button_id)

    def sample(self):
        self.media.state = {"title": "Everything In Its Right Place", "artist": "Radiohead", "playing": True,
                            "player": "soloist", "source": "Spotify", "spotify": True, "art": None, "volume": 0.7}
        self.label = LABELS[True]

    def page(self):
        m = self.media.state
        source = m["source"] or ""
        if m.get("volume") is not None:
            source += f"  ·  {round(m['volume'] * 100)}%"
        art = w.Image(m["art"], (64, 64), glyph="play")
        info = w.Column([w.Text(m["title"], size="title", scroll=True, height=22),
                         w.Text(m["artist"], size="body", colour="dim", lines=2),
                         w.Spacer(),
                         w.Text(source, size="small", colour="green")], gap=2, grow=1)
        controls = w.Row([w.Button(key, glyph=glyph or ("pause" if m["playing"] else "play"))
                          for key, glyph in BUTTONS], height=36, gap=6)
        volume = (w.Row([w.Spacer(grow=3), w.Bar(m["volume"], height=3, grow=2)], height=3)
                  if m.get("volume") is not None else w.Spacer(height=3))
        return w.Column([w.Row([art, info], height=64, gap=10), w.Spacer(), controls, volume], gap=3)
