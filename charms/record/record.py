"""Record: video of what the headset shows, from the wrist. Save a clip of the last few
seconds, start and stop a recording, and change how it records.

Settings and the Cuff Player shortcut's id are in ~/.config/cuff/record/.

Pages: Record (status, Clip, Record/Stop), Video (shape, eye, quality, frame rate), Audio
(game audio, mic, clips and their length) and Watch (the clips and recordings, newest first,
to play in the headset with player.py; Now Playing controls the player). Its quick-row
button (charm.toml's [quick.record]) calls quick_press, which does what the Record page's
"Quick action" setting says: save a clip (the default), start or stop a recording, or both (save the clip, then
start recording). While a recording runs, it stops it.

The recording itself is Cuff's recorder (recorder/, from Framecorder), run by recorder.py.
Settings are spelled as Framecorder spells them (options.py, which Cuff for PC's settings page
uses too; reload() takes up its changes); the first time, they're read from Framecorder's
~/.config/framecorder/ui.conf. Clips and recordings go to ~/Videos/framecorder (clips in
clips/), with a toast and a buzz when saved.
"""
import os
import subprocess
import threading
import time

from cuff import Charm
from cuff import config
from cuff import widgets as w

from . import options
from . import recorder as rec
from .player import Player
from .recorder import Recorder

TICK_S = 0.5
# How long "both" waits for its clip's reply before giving up on recording after it: a clip is
# usually saved in a second or two, and the recorder answers every clip it's asked for.
CLIP_REPLY_S = 30.0
PAGES = ["Record", "Video", "Audio", "Watch"]
WATCH_ROWS = 3  # videos per Watch page
LABEL_W = 38  # the settings rows' labels
# Each setting's choices, as (value, label), in Framecorder's spelling (options.py, shared
# with cuff.remote for Cuff for PC's settings page).
from .options import BUTTON, CLIP_LENGTHS, EYES, ON_OFF, QUALITIES, RATES, SHAPES, STEADY, VALID  # noqa: E402


def clock(seconds):
    m, s = divmod(int(max(0, seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def videos():
    """Every clip and recording, newest first: [(path, mtime, "clip" or "recording")]."""
    found = []
    for folder, kind in ((rec.CLIPS, "clip"), (rec.VIDEOS, "recording")):
        try:
            for name in os.listdir(folder):
                if name.endswith((".mp4", ".mkv")):
                    path = os.path.join(folder, name)
                    found.append((path, os.stat(path).st_mtime, kind))
        except OSError:
            pass
    return sorted(found, key=lambda v: -v[1])


def duration(path):
    """A video's length in seconds (ffprobe), or None."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=5).stdout
        return float(out)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def newest(folder):
    """(mtime, size) of the newest video in folder, or None."""
    best = None
    try:
        for name in os.listdir(folder):
            if name.endswith((".mp4", ".mkv")):
                st = os.stat(os.path.join(folder, name))
                if best is None or st.st_mtime > best[0]:
                    best = (st.st_mtime, st.st_size)
    except OSError:
        pass
    return best


class Record(Charm):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.sub = PAGES[0]
        self.lock = threading.RLock()
        # Settings and the Cuff Player shortcut's id are kept with Cuff's config, not its cache:
        # an uninstall clears the cache but keeps settings.
        self.config_dir = os.path.join(os.path.dirname(config.CONFIG_FILE), "record")
        self.settings_file = os.path.join(self.config_dir, "settings.json")
        self.settings = self._load()
        self.proc = None  # the Recorder, while one runs
        self.recording = None  # {"started": monotonic or None until it's writing, "stopping": bool}
        self.retry_at = 0.0
        self.error = None  # why something didn't work, for the page
        self.note = None  # the last thing saved
        self.files = {"clip": None, "recording": None}
        self.record_after_clip = None  # the button's "both": until when to record once the clip's answered
        self.player = Player(self.config_dir, ctx.cache_dir)
        self.watch_page = 0
        self.delete_armed = None  # the video whose Delete was tapped once
        self.lengths = {}  # path -> (mtime, seconds), from ffprobe

    # ---------------------------------------------------------------- settings

    def _load(self):
        """Saved settings over the defaults; the first time, Framecorder's own (options.py)."""
        return options.load(os.path.dirname(config.CONFIG_FILE))

    def reload(self):
        """Settings changed from outside (Cuff for PC): a running recorder with other settings
        restarts once nothing's being recorded (_tick)."""
        with self.lock:
            self.settings = self._load()
        self.ctx.changed()

    def _set(self, key, value):
        if key not in VALID or value not in dict(VALID[key]):
            return
        with self.lock:
            self.settings[key] = value
            if key == "clip":
                self.settings["clips"] = "true"  # picking a length turns clips on
            try:
                options.save(os.path.dirname(config.CONFIG_FILE), self.settings)
            except OSError as e:
                self.error = f"Couldn't save the settings ({e.strerror})"
        # A running recorder with other settings restarts once nothing's being recorded (_tick).

    # ---------------------------------------------------------------- the recorder

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="record").start()

    def _run(self):
        while True:
            files = {"clip": newest(rec.CLIPS), "recording": newest(rec.VIDEOS)}
            with self.lock:
                changed = self._tick() or files != self.files
                self.files = files
            if self.sub == "Watch" and self.ctx.shown:
                changed = self._measure() or changed
            if changed or (self.recording and self.ctx.shown):  # the clock ticks while it shows
                self.ctx.changed()
            time.sleep(TICK_S)

    def _tick(self):
        """Keeps a recorder running while clips are on, restarts one whose settings changed
        (between recordings), lets one go that nothing needs, and notices one that died.
        Returns whether anything shown changed."""
        p, clips = self.proc, self.settings["clips"] == "true"
        if self.record_after_clip is not None and time.monotonic() > self.record_after_clip:
            print("record: no reply to the clip; not recording after it", flush=True)
            self.record_after_clip = None
        if p is not None:
            code = p.exited()
            if code is None:
                if not self.recording and p.quitting is None:
                    wanted = int(self.settings["clip"]) if clips else None
                    if p.replay != wanted or p.args != rec.recorder_args(self.settings, rec.panels_unlocked()):
                        p.quit()
                    elif not clips and self.record_after_clip is None:
                        # Started for a recording that's over: with clips off nothing needs it, and
                        # it would keep the encoder (and SteamVR's headset view) busy for nothing.
                        p.quit()
                return False
            self.proc = None
            self.record_after_clip = None  # its clip's reply isn't coming
            if p.quitting is None:  # it wasn't asked to go
                self.error = p.stuck_message()
                self.recording = None
                self.retry_at = time.monotonic() + rec.RETRY_S
                print(f"record: recorder stopped ({code}): {self.error}", flush=True)
            return True
        if clips and time.monotonic() >= self.retry_at:
            self._start()
            return True
        return False

    def _start(self):
        self.record_after_clip = None  # a new recorder has no clip on the way
        try:
            self.proc = Recorder(self.settings, self._event)
        except OSError as e:
            self.proc, self.error = None, f"Couldn't start the recorder ({e.strerror})"
            self.retry_at = time.monotonic() + rec.RETRY_S

    def _event(self, word, rest):
        """A line from the recorder (recorder.py lists them)."""
        with self.lock:
            if word == "recording" and self.recording:
                self.recording["started"] = time.monotonic()
            elif word == "saved":
                kind, size, secs, path = (rest.split(" ", 3) + ["", "", "", ""])[:4]
                size_mb, secs = int(size or 0) / 1e6, float(secs or 0)
                if kind == "recording":
                    self.recording = None
                    self.note = f"Saved {os.path.basename(path)} · {clock(secs)} · {size_mb:.1f} MB"
                    self.ctx.notify(f"Recording saved · {clock(secs)}", haptic=True)
                else:
                    self.note = f"Clipped {os.path.basename(path)} · {secs:.0f} s · {size_mb:.1f} MB"
                    self.ctx.notify(f"Clipped the last {secs:.0f} s", haptic=True)
                    self._record_after_clip()
                self.error = None
            elif word == "busy":
                self.note = "Recording, so the clip's in it already"
                self.ctx.notify("Recording: it's all in there")
                self.record_after_clip = None  # already recording
            elif word == "failed":
                kind, _, why = rest.partition(" ")
                if kind == "recording":
                    self.recording = None
                if why.startswith("skipped"):
                    pass  # pressed again right after a clip: the one just taken stands
                elif "nothing to clip yet" in why:
                    self.error = "Nothing to clip yet: give it a few seconds"
                else:
                    self.error = why or f"The {kind} failed"
                if kind == "clip":
                    self._record_after_clip()  # record anyway
        self.ctx.changed()

    def _record_after_clip(self):
        if self.record_after_clip is not None:
            self.record_after_clip = None
            self._begin()

    # ---------------------------------------------------------------- actions

    def quick(self, button):
        """The quick-row record button: a stop square, red, while recording; greyed out
        without the recorder."""
        if not os.path.exists(rec.RECORDER):
            return {"glyph": "record", "style": "off"}
        if self.recording is not None:
            return {"glyph": "stop", "style": "alert"}
        return {"glyph": "record", "style": "normal"}

    def quick_press(self):
        """The quick row's record button: stops a recording that's going; otherwise does what
        the Quick action setting says (clip, record, or both: the clip, then record)."""
        with self.lock:
            action = self.settings["button"]
            if self.recording or action == "record":
                self.toggle_record()
            elif action == "clip":
                self.clip()
            elif self.clip_ready():  # both
                self.clip(then_record=True)
            else:  # both, with nothing to clip
                self.toggle_record()
        self.ctx.changed()

    def toggle_record(self):
        """Start a recording, or stop the one going."""
        with self.lock:
            self.error = None
            if self.recording:
                if not self.recording["stopping"] and self.proc and self.proc.send("stop-record"):
                    self.recording["stopping"] = True
            else:
                self._begin()
        self.ctx.changed()

    def _begin(self):
        """Starts a recording (lock held)."""
        if self.proc is not None and self.proc.quitting is not None:
            self.error = "The recorder's restarting with new settings: try again in a moment"
            return
        if self.proc is None:
            self._start()
            if self.proc is None:
                return
        os.makedirs(rec.VIDEOS, exist_ok=True)
        path = os.path.join(rec.VIDEOS, time.strftime("%Y-%m-%d_%H-%M-%S") + ".mp4")
        if self.proc.send(f"record {path}"):
            self.recording = {"started": None, "stopping": False}
            self.note = None

    def clip_ready(self):
        p = self.proc
        return p is not None and p.replay is not None and p.quitting is None

    def clip(self, then_record=False):
        """Asks for a clip; then_record (the button's "both") starts a recording once the
        recorder has answered it. A plain clip drops any "both" still waiting, so an old one
        can't start a recording nobody asked for."""
        with self.lock:
            self.record_after_clip = None
            if not self.clip_ready():
                self.error = "Clips are off" if self.settings["clips"] != "true" else "Clips are starting up"
                self.ctx.notify(self.error)
            elif self.proc.send("clip"):
                self.error = None
                if then_record:
                    self.record_after_clip = time.monotonic() + CLIP_REPLY_S

    def press(self, button_id):
        # Delete takes two taps on the same row; anything else in between disarms it.
        armed, self.delete_armed = self.delete_armed, None
        if button_id.startswith("delete:"):
            path = button_id[7:]
            if armed == path:
                self._delete(path)
            else:
                self.delete_armed = path
            return
        if button_id == "quick:record":  # its quick-row button
            self.quick_press()
        elif button_id in ("record", "stop"):
            self.toggle_record()
        elif button_id == "clip":
            self.clip()
        elif button_id.startswith("sub:"):
            self.sub, self.error = button_id[4:], None
        elif button_id.startswith("set:"):
            self._set(*button_id[4:].split("=", 1))
        elif button_id.startswith("play:"):
            self._play(button_id[5:])
        elif button_id == "close-player":
            self.player.close()
        elif button_id in ("watch:prev", "watch:next"):
            self.watch_page += 1 if button_id == "watch:next" else -1

    def _delete(self, path):
        """Deletes a clip or recording from the Watch page, if it's one of ours."""
        if os.path.dirname(path) not in (rec.VIDEOS, rec.CLIPS):
            return
        try:
            os.remove(path)
            self.lengths.pop(path, None)
            self.error = None
        except OSError as e:
            self.error = f"Couldn't delete it ({e.strerror})"

    def _play(self, path):
        """Plays path, then the older ones after it (next and previous move through them)."""
        paths = [v[0] for v in videos()]
        start = paths.index(path) if path in paths else 0
        self.error = self.player.play(paths[start:] or [path])

    def badge(self):
        r = self.recording
        return clock(time.monotonic() - r["started"]) if r and r["started"] else None

    def sample(self):
        self.files = {"clip": (time.time() - 120, 41_200_000), "recording": None}

    # ---------------------------------------------------------------- pages

    def page(self):
        body = {"Record": self._record, "Video": self._video, "Audio": self._audio, "Watch": self._watch}[self.sub]()
        return w.Column([w.Tabs([(p, p) for p in PAGES], selected=self.sub, id="sub"), *body],
                        gap=2 if self.sub == "Video" else 4)

    def _record(self):
        r, s = self.recording, self.settings
        clips = s["clips"] == "true"
        if r and r["stopping"]:
            head = w.Text("Saving the recording", size="title", colour="amber", align="center")
        elif r and r["started"]:
            head = w.Text(f"Recording  {clock(time.monotonic() - r['started'])}", size="title", colour="red",
                          align="center")
        elif r:
            head = w.Text("Starting the recording", size="title", colour="amber", align="center")
        elif clips and self.proc is None:
            head = w.Text("Clips starting up", size="title", colour="amber", align="center")
        else:
            head = w.Text(f"Ready · clips {s['clip']} s" if clips else "Ready · clips off", size="title",
                          colour="green", align="center")
        if self.error:
            detail, colour = self.error, "amber"
        elif not rec.panels_unlocked():
            detail, colour = "Panel helper not unlocked: 16:9 of the left eye only", "amber"
        else:
            detail, colour = self.note or self._latest(), "dim"
        can_clip = clips and self.proc is not None and not r
        buttons = w.Row([
            w.Button("clip", "Clip", style="accent" if can_clip else "off"),
            w.Button("stop", "Stop", style="off" if r["stopping"] else "warn") if r else
            w.Button("record", "Record"),
        ], height=34, gap=6)
        return [head, w.Text(detail, size="small", colour=colour, align="center"),
                self._choice("Quick action", "button", BUTTON, label_w=66), w.Spacer(), buttons]

    def _choice(self, label, key, choices, enabled=True, label_w=LABEL_W, height=22):
        """A labelled row of buttons, one per choice, the current one lit."""
        value = self.settings.get(key)
        return w.Row([w.Text(label, size="small", colour="dim", width=label_w)] + [
            w.Button(f"set:{key}={v}", text, style="active" if v == value else "normal" if enabled else "off")
            for v, text in choices], height=height, gap=4)

    def _video(self):
        # Five rows, so a little shorter than the other pages' (19 px + 2 px gaps).
        panel, one_eye = rec.panels_unlocked(), self.settings["shape"] != "both"
        return [self._choice("Shape", "shape", SHAPES, enabled=panel, height=19),
                self._choice("Eye", "eye", EYES, enabled=panel and one_eye, height=19),
                self._choice("Steady", "stabilise", STEADY, enabled=panel and one_eye, height=19),
                self._choice("Quality", "quality", QUALITIES, height=19),
                self._choice("FPS", "fps", RATES, height=19)]

    def _audio(self):
        clips_on = self.settings["clips"] == "true"
        clip_row = w.Row([w.Text("Clips", size="small", colour="dim", width=LABEL_W),
                          w.Button("set:clips=false", "Off", style="normal" if clips_on else "active")] + [
            w.Button(f"set:clip={val}", text, style="active" if clips_on and val == self.settings["clip"] else "normal")
            for val, text in CLIP_LENGTHS], height=22, gap=4)
        return [self._choice("Game", "game_audio", ON_OFF),
                self._choice("Mic", "mic", ON_OFF),
                clip_row,
                w.Text(self.error or "Clips keep that much in memory, ready to save at any time.",
                       size="tiny", colour="amber" if self.error else "dim", align="center", lines=2)]

    def _watch(self):
        found = videos()
        pages = max(1, -(-len(found) // WATCH_ROWS))
        self.watch_page = min(max(self.watch_page, 0), pages - 1)
        rows = []
        for path, mtime, kind in found[self.watch_page * WATCH_ROWS:(self.watch_page + 1) * WATCH_ROWS]:
            same_day = time.localtime(mtime)[:3] == time.localtime()[:3]
            when = (time.strftime("%-I:%M %p", time.localtime(mtime)).lower() if same_day
                    else time.strftime("%-d %b", time.localtime(mtime)))
            length = self._length(path, mtime)
            armed = self.delete_armed == path
            rows.append(w.Row([w.Text(f"{kind.capitalize()} {when}" + (f" · {clock(length)}" if length else ""),
                                      size="small", grow=1),
                               w.Button(f"delete:{path}", "Sure?" if armed else "Delete",
                                        style="warn" if armed else "normal", width=46),
                               w.Button(f"play:{path}", "Play", style="accent", width=40)], height=22, gap=4))
        if not found:
            rows.append(w.Text("No clips or recordings yet", size="small", colour="dim", align="center"))
        rows.append(w.Spacer())
        rows.append(w.Row([
            w.Button("watch:prev", "‹", width=30, style="normal" if self.watch_page else "off"),
            w.Text(self.error or f"{self.watch_page + 1} / {pages}", size="tiny",
                   colour="amber" if self.error else "dim", align="center", grow=1),
            w.Button("watch:next", "›", width=30, style="normal" if self.watch_page < pages - 1 else "off"),
            w.Button("close-player", "Close player", width=78),
        ], height=22, gap=4))
        return rows

    def _length(self, path, mtime):
        """From the cache only: _measure fills it on the charm's own thread."""
        cached = self.lengths.get(path)
        return cached[1] if cached and cached[0] == mtime else None

    def _measure(self):
        """ffprobe the lengths the Watch page shows that aren't known yet. Returns whether any were."""
        start = self.watch_page * WATCH_ROWS
        new = False
        for path, mtime, _ in videos()[start:start + WATCH_ROWS]:
            if self.lengths.get(path, (None,))[0] != mtime:
                self.lengths[path] = (mtime, duration(path))
                new = True
        return new

    def _latest(self):
        """The newest clip or recording: "Last clip 2 min ago · 41.2 MB"."""
        found = [(f[0], kind, f[1]) for kind, f in self.files.items() if f]
        if not found:
            return "Nothing saved yet"
        mtime, kind, size = max(found)
        ago = time.time() - mtime
        when = "just now" if ago < 60 else f"{int(ago // 60)} min ago" if ago < 3600 else time.strftime(
            "%-I:%M %p", time.localtime(mtime)).lower()
        return f"Last {kind} {when} · {size / 1e6:.1f} MB"
