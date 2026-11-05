# Cuff internals

How Cuff works, and why: the findings behind its choices on the Steam Frame.

## Bundled charms

- **Timer** (`charms/timer`). Three sub-tabs.
  - **Timer:** presets from 1 to 30 minutes, −1 / +1 minute (also while running),
    start/pause and reset. When it runs out it raises a toast and buzzes the wrist, and its
    tab keeps a dot until you look. While it runs, the tab shows the time left ("5m", then
    seconds in the last minute).
  - **Stopwatch:** start/pause and reset.
  - **Break:** time in VR since Cuff started with SteamVR, and a toast with a buzz every 30,
    45, 60 or 90 minutes (or never).
  - The duration and reminder interval are remembered.
- **Performance** (`charms/performance`). Modelled on Virtual Desktop's stats view.
  - **Streaming a PC game with Steam Link** (the scene app's key is `vrlink.client`):
    - **The game, from Cuff for PC** (`GET /stream`, once a second, live from the PC's
      compositor): its framerate against the target, green, amber (under 95 %) or red (under
      80 %); reprojection and motion smoothing (SteamVR's counterpart of SSW: on, forced on,
      or in use) and why (GPU or CPU late); GPU and CPU milliseconds per frame; dropped
      frames; and a minute's graph of the framerate. Without the PC app, these say so.
    - **Both links, measured on the headset**, since the Frame streams over either or both:
      home Wi-Fi and the dongle (the Frame's own 6 GHz access point). Each row: band, live
      bitrate (from its station's byte counter, `iw station dump`), ping and jitter to the
      PC (four pings every 2 s, while the tab is open), signal (the dongle's own reads 0, so
      its ack signal) and link rate. The PC's address on each link comes from Steam Link's
      command line (`-c <ip>`).
    - **The link as the PC's driver logs it**, also from Cuff for PC (`link` and
      `dongle_usb` in `/stream`): the video bitrate, the capacity SteamVR measured
      (`link 225+`: what it carried 90 % of the time), frames held back for the link per
      minute, and bitrate step-downs. It also warns when the dongle is on USB 2. Amber on
      any of those, or when the bitrate is above that capacity.
    - **The headset:** its display rate and compose time, packets Steam Link's client
      re-requested in the last minute (`retries`) and times its decoder got stuck or hit an
      unrecoverable error (`stuck`, amber). Those come from its own log,
      `~/.steam/steam/logs/vrlink.txt`, read as it grows. Then CPU / GPU / battery temperature
      (the hottest `/sys/class/thermal` zone of each kind) and CPU load.
  - **Anything else on the headset:** its framerate, reprojection, an App CPU / App GPU /
    Compositor stack with a tick at one frame's budget, the minute's graph, dropped and
    reprojected frames, and the headset line.
  - **Badge:** a dot while frames drop or the framerate is under 90 % of the target.
  - **What the headset can't see of a stream** (captured 2026-10-25, a game at 144 Hz):
    Steam Link's client presents at the display's rate whatever the game manages (144 fps at
    1.8 ms GPU), SteamVR counts every frame as reprojected because the client warps each one
    itself, and transfer latency stays 0. Steam Link's own encode / network / decode
    breakdown is only in its client's debug window and in timing snapshots it writes on the
    PC after a hitch (`Steam/logs/vrlink_aftm_*.csv`), never live, so Cuff doesn't show it.
    The driver's text log (`Steam/logs/driver_vrlink.txt`) is live, and that's where the
    link line comes from.
- **Audio** (`charms/audio`). The default output's volume (− and +, 5 % steps) and mute, and
  the real outputs to send sound to (those with a `device.api`: the built-in speakers, a USB-C
  adapter, Bluetooth headphones; filter chains and virtual mics are left out), through pactl.
- **Settings** (`cuff/settings.py`). Part of the core and pinned to the end of the tab row,
  drawn as a gear. Five sub-tabs:
  - **Place** and **Angle** set where the panel sits: Size, Along arm (from the controller tip
    toward the elbow) and Height (off the wrist); Around (the wrist), Tilt (the top edge away
    from you) and Twist (in its own plane). Each row has − and + (one step), and a bar whose
    eight segments jump straight to that part of the range; a value turns amber while it
    differs from the applied one, which a tick on the bar marks.
    - **Preview.** Changes wait for **Apply**. Meanwhile a flat blue panel shape (20 %) shows
      on your wrist where it will go, so the panel you're touching doesn't move under your
      finger. It always shows, at its true depth: nearer than the panel it tints it; farther,
      the panel fades to 50 % so the preview shows through (see "Overlay depth").
    - **Wrist** (a Left | Right row under the sliders, the current wrist green, a pending
      one amber) moves the panel to the other wrist, also pending until Apply, with the
      preview on that wrist. The right-wrist pose is the left one mirrored across the
      tip frame's YZ plane (M R M, M t with M = diag(−1, 1, 1)), so it stays a proper rotation
      and the text runs from the hand toward the elbow. The other controller then touches it
      (the poke sphere, its vibration), and alerts buzz and glow at the wearing hand.
    - **Cancel** drops the changes; **Reset** sets the defaults (pending until Apply), but asks
      first: one tap turns it into "Reset?" for 3 s, and a second tap resets.
    - **Saved** to `~/.config/cuff/placement.json`, only the values that differ from the
      defaults (wrist.py's, with `[wrist]` in `config.toml` on top).
  - **Charms** lists every charm found with its permissions and state: turn one on or off,
    approve one that needs a powerful permission (`~/.config/cuff/charms.json`), three per
    page; **Restart to apply** restarts Cuff (charms load at startup; systemd starts it again).
  - **Alerts** switches the buzz, chime, glow on the hand and charging-stopped alert, sends a
    test alert, and opens **Low battery**: each device (headset, controllers, headphones) on or
    off with two levels in 5 % steps (0 is off). Kept in `~/.config/cuff/alerts.json`.
  - **Quick** chooses the quick row: its six slots, then every button on offer (the core's
    and the loaded charms'), with Empty and Defaults (two taps). Tap a slot, then a button
    for it; one that's already in the row swaps places. Saved at once to
    `~/.config/cuff/quick.json`.
  - With five sub-tabs, each is as wide as its label plus a share of the rest
    (`ui.spans`); sub-tabs whose labels all fit stay equal widths.
- **Record** (`charms/record`): video of what the headset shows (setup and credits:
  `docs/RECORDING.md`). Its `charm.toml` has `requires = "~/.local/share/cuff/bin/cuff-recorder"`, so it
  stays off ("not installed" in Settings > Charms) without the recorder.
  - **The recorder** is `recorder/` (Rust), from [Framecorder](https://github.com/coah80/framecorder)
    (MIT, credits in `recorder/README.md`), built for SteamOS 0.6's FFmpeg 8 by
    `recorder/install.sh`. `recorder.py` runs it as Framecorder's own tab did:
    `cuff-recorder [--replay N] <args> --control`. Commands go one per line on stdin
    (`record <file>`, `stop-record`, `clip`, `quit`), and events come back on stdout
    (`recording <path>`, `saved recording|clip <bytes> <secs> <path>`, `busy recording`,
    `failed <kind> <why>`). Every clip asked for gets one of those: one asked for within a
    second of the last gets `failed clip skipped: ...`, which the page doesn't show as an
    error. Started with `--control` and no `--replay`, it records nothing until `record`.
  - **Lifecycle:** it stays up while clips are on (the buffer lives in it), and otherwise
    only for a recording: with clips off, the one started for a recording is told to quit
    once that's saved. It restarts when its settings change, between recordings, and 5 s
    after it dies. `PR_SET_PDEATHSIG` sends it SIGINT if Cuff dies, so it finishes its file.
    It never pauses, so recording works with no game, and Cuff's panel is in the video
    while you look at it.
  - **Clip, then record** (the quick action's "both") waits for the clip's reply. The wait
    is dropped when the recorder exits or restarts, after 30 s, and by a plain Clip, so a
    stale one can't start a recording later.
  - **The replay buffer** (`replay.rs`) keeps its time by the clock, for video and audio
    alike, so with the panels dark (headset off) the mic's audio still ages out. A gap in
    the video (over 1 s without a frame) is a discontinuity: the next frame is forced to be
    a keyframe, and a clip never reaches back across a gap that began before its start (it
    starts at the first keyframe after it). Its memory is also capped at twice what its time
    should take at the chosen bitrates. Silence is padded into the audio a second at a time;
    with no file being written, a gap longer than the buffer keeps is skipped, not encoded.
  - **Logs:** the recorder's stderr goes to `~/.local/state/cuff/record.log`, set aside as
    `record.log.1` when a start finds it over 1 MB. Its performance CSV (a line a second) goes
    to `~/.local/state/framecorder/<time>.perf.csv` when there's no recording file to put it
    next to: the newest 5 are kept, each capped at 2 MB (it starts over past that).
  - **SteamVR's interfaces** for the headset view source: `IVRCompositor` (_029, _028 or
    _027, each with its own slots for the Vulkan extension calls, measured on SteamVR 2.18.4),
    `IVROverlay` (_028, _029 or _027: `FindOverlay` is slot 0 in all), `IVROverlayView_003` and
    `IVRHeadsetView_001` (the only versions with a known layout). Teardown always goes headset
    view, then SteamVR, then the GPU device, early errors included.
  - **Panel access:** it reads the panels through the root-owned panel helper
    (`~/.local/lib/cuff/cuff-grab`, put there by `install.sh --helper`, with
    `cap_sys_admin`; Framecorder's `framecorder-grab` works too). Without a helper it
    records SteamVR's view: 16:9, left eye only.
  - **Settings** are kept in Framecorder's spelling in `~/.config/cuff/record/settings.json`,
    with the Cuff Player shortcut's id in `player.json`. They're under config, not cache,
    so they survive an uninstall.
    The first time, they're read from Framecorder's `ui.conf`.
  - **Pages:**
    - **Record:** the status and its time, Clip, Record/Stop, why something failed, and
      the newest file.
    - **Video** and **Audio:** the settings. Video's **Steady** passes `--stabilise
      light|strong` (the recorder's `stabilise.rs`, a GPU pass that turns the crop against
      head jitter each frame). Only for one eye read from the panels.
    - **Watch:** clips and recordings, newest first, with their lengths (`ffprobe`, run on
      the charm's thread).
  - **Toasts:** each saved clip or recording gets one, with a buzz. The tab's badge shows
    the recording time. Its quick-row button (`[quick.record]`) calls `quick_press`.
  - **Playing (`player.py`):** mpv from Flathub (`io.mpv.Mpv`), whose MPRIS plugin hands it
    to Now Playing. A window shows in VR only when Steam launched it, so mpv runs from a
    "Cuff Player" non-Steam shortcut. Cuff adds it once through Steam's client
    (`SteamClient.Apps.AddShortcut`, then `SetShortcutLaunchOptions` and `RunGame`), and
    keeps its id in `player.json`. Its launch options play `playlist.m3u`, rewritten before
    each Play: that video, then the older ones.
- **Now playing** (`charms/now-playing`).
  - **The page:** album art, title and artist; where it's up to, with a scrub bar to tap and
    the time left; −15 s, previous, play/pause, next, +15 s; and volume.
  - **The source** under the title ("Spotify · 70%") is a button that opens the player
    page: pick which player to show, or Auto, and the playback speed.
  - **The MPRIS calls:** the scrub bar is a `Scrub` widget, 16 steps, using `SetPosition`
    with the track's `mpris:trackid`; skipping is `Seek`; speed is `Rate`, within
    `MinimumRate`/`MaximumRate`.
  - **Greyed out:** skipping and the bar while the player can't seek, and the volume
    buttons without a writable `Volume`.
  - **Position** is read once a second, but only goes into the state while the page is
    open, so a playing song doesn't redraw the panel every second behind another tab.
  - **Which player:** any MPRIS player works. Whatever is playing comes first, then paused,
    then the rest. Within each, Spotify comes first: a headless Connect speaker on the
    headset, meaning Spotify's own
    [Soloist](https://developer.spotify.com/documentation/soloist) through
    [soloist-mpris](https://github.com/TobiasVeiga00/soloist-mpris), or
    [spotifyd](https://github.com/Spotifyd/spotifyd) (which Spotify now refuses song keys
    for some accounts). Then Chromium's web player.
  - **Soloist:** soloist-mpris keeps Soloist's MPRIS name, and its last track, while another
    device plays. So Soloist counts only while `soloist ctl status` says it's the active
    device (asked at most every 3 s).
  - **The tab** reads "Spotify" while it shows that speaker, and "Now Playing" otherwise. A
    two-word label too long for its tab goes on two lines. The source under the title is
    "Spotify", or the player's MPRIS `Identity` ("Chromium", "VLC").
  - **Nothing playing:** the page says so. If Soloist is installed, it adds "Pick "<name>" in
    Spotify", with the name from `~/.config/soloist/env`.
  - **A picked player** stays shown until it closes, then Auto takes over again.
  - **Volume:** − and + step the player's MPRIS `Volume` by 5%, separate from the system
    sink. For a Connect speaker that's the Spotify app's slider. The bar between them and
    the "· 70%" by the source show the level.

## The panel

The panel has four rows, top to bottom:

1. **Status bar**: time and batteries. It's never interactive.
2. **Quick actions**: always shown; six slots, chosen in Settings > Quick.
3. **Tabs**: one per charm. Three fit; with more, ‹ and › at the ends of the row page through
   them, three at a time (a short last page keeps the same tab width) (dimmed at the first and last page, with a green dot pointing to the open tab when
   it's on another page).
4. **The open tab's page**.

Tapping the open tab closes it, and the panel shrinks to the first three rows (88 px
instead of 230). The top edge stays put, so the status and quick rows don't move: the
panel's frame is the open layout's centre, and a shorter image is shifted down by half the
difference (`Layer(anchor_top=True)`). Touch maps against the height being shown. The open
tab, or none, is remembered across restarts (`~/.cache/cuff/state.json`).

- **Status bar.** Shows the time ("7:52 pm": bold digits with a smaller am/pm) and the
  battery for the headset (H), the left (L) and right (R) controllers, and Bluetooth
  earbuds (a headphones glyph) while connected ones report a level. Each battery turns red below 20 %.
  - **Fitting four batteries.** The bar is compact enough for "12:52 pm" plus four
    batteries reading 100: 12 px clock with a 9 px suffix, 9 px battery text, 10 px icons
    and 3 px gaps, leaving 5.2 px spare with the headphones glyph.
  - **The earbuds:** a connected audio `org.bluez.Device1`, polled every 3 s by
    `quick.py`. It's one figure for the pair, from one of two sources:
    - **BlueZ's `Battery1`**, where the device gives one. The WF-1000XM3 does, over HFP, but
      only now and then: it was still at 50 % when it said "battery low" itself, then
      jumped to 20 %. So a new level refreshes the status bar at once.
    - **LibrePods**, for AirPods, which give BlueZ no `Battery1`. Its log has every per-bud
      and case report (`librepods.py` follows the `librepods` user unit's journal, since it
      has no API). The figure is the lower bud in use (one LibrePods reports as
      `Disconnected` doesn't count), shown as charging while either is.
  - **When batteries are read:** every 10 s (every 2 s while one hasn't reported yet), and
    the panel is redrawn only when the minute or a battery value changes.
  - **The headset** comes from the Frame's own power supplies (`/sys/class/power_supply`):
    the battery's level, current and status, and whether the USB-C input is live. That
    gives three states, so a pack running flat is easy to spot:
    - **Green with a bolt: charging.** Current is flowing into the battery.
    - **Amber with a bolt: plugged in but still draining.** USB-C power is present, but the
      battery's current is negative, so the pack can't keep up or is nearly flat.
    - **White: on battery.** Nothing is plugged in; a flat pack cuts out, so it's time to
      swap.

    If those files are missing, it falls back to SteamVR's `Prop_DeviceIsCharging_Bool`.
  - **The controllers** use SteamVR's `Prop_DeviceBatteryPercentage_Float` and
    `Prop_DeviceIsCharging_Bool`: green with a bolt while charging.
- **Quick actions** (`cuff/quickrow.py`). Six slots, 34x24 px each, filled from the
  buttons the core and charms offer (Settings > Quick; the default is the six below). An
  empty slot is blank and untouchable. Toggles show their state as a fill; actions only
  flash when pressed.
  - **A charm's button** is declared in its `charm.toml` (`[quick.<id>]`, a label and a
    glyph). Its look comes from the charm's `quick(button)` on each redraw (a glyph, a style
    of normal, on, alert, busy, dim or off, and an optional short text), and a press goes to
    `press("quick:<id>")` on the charm worker. In the row it's `"<charm id>:<id>"`.
  - **A new charm's button is offered, not added**, so it never pushes one of yours out. A
    chosen button whose charm is off keeps its slot, greyed out.
  - **A core button:** an entry in `quickrow.CORE` and `core_look`, a glyph in `ui.glyph`,
    and a handler in `quick.py` or `app.py`.

  | Button | What it does | How |
  | --- | --- | --- |
  | Dictation (speech bubble) | Push-to-talk: tap, talk, tap. The text is typed into the focused window: a game, the desktop or Steam. Red while listening, amber while transcribing; dimmed when Steam's text input isn't reachable. | `pw-record`, then a PC server if configured, else the headset's own model; typed with Steam's `ControllerKeyboardSendText` (see "Dictation" below) |
  | Mic | Mutes the physical mic (the profile's `real_mic`). Red with a slash while muted. Anything mixed into a virtual mic downstream of it still gets through. | `pactl set-source-mute <real_mic> toggle`, state polled every 2 s |
  | Passthrough (eye) | SteamVR Room View on/off. Teal while on, following SteamVR's real state; greyed out if SteamVR's mailbox isn't there or didn't respond. | SteamVR's mailbox; see "Passthrough" below |
  | Recenter (target) | Resets the forward direction and makes a new chaperone bubble where you stand, as holding the Steam button does, so a popped bubble's forced passthrough ends. Room View blinks on for 0.3 s to do it (skipped while Room View is on by choice). | `seated_and_standing_position_reset` to SteamVR's `vrcompositor_systemlayer` mailbox (`vrcmd --recenter` if that fails), then Room View on and off: turning it on clears the bubble ("Clearing chaperone bubble because room view for scene apps was enabled") and turning it off creates one ("Creating chaperone bubble because room view was turned off…"). The reset alone, by either route, left the old bubble popped. |
  | Record (dot, a stop square while recording) | The Record charm's button: saves a clip, or starts or stops a recording, as its Quick action setting says. Red while recording; greyed out without the recorder. It replaced the SteamVR keyboard button. | `[quick.record]` in the Record charm; `quick_press` on the charm worker |
  | Screenshot (camera) | Saves a stereo screenshot to `~/Pictures/Frame screenshots/` (`*-vr.png` and `*-preview.png`) 3.4 s after the tap (the button counts down 3, 2, 1 in amber, then shows the camera again 0.4 s before the shot, so a picture of the panel has the camera on it, not a "1"), so you can turn the panel out of view; the wrist buzzes as it's taken. | OpenVR `TakeStereoScreenshot` on the overlay thread; `vrcmd --screenshot` did nothing on the Frame |

- **Scrolling line.** A charm can mark one line to scroll (`marquee()`; Now playing's
  song title). Too long for its box, it scrolls: it pauses 2 s, moves
  left at 30 px/s, and wraps round with a gap (`ui.Marquee`). The line is its own
  small overlay laid 0.5 mm over the panel, so scrolling re-uploads only that strip, at
  up to 20 fps and only while the panel is visible.
- **Touch point.** A 12 mm sphere 60 mm ahead of the right controller's tip, along the
  direction it points, so the hand holding it isn't in the way. It shows and fades with
  the panel. It is white normally, blue when near the panel, and green while pressing.
  The blue has hysteresis: it starts within 40 mm in front of the panel and inside its
  edges, and it stays until the sphere is beyond 55 mm, or 5 mm past an edge
  (`HOVER_EXIT`, `EDGE_EXIT`).

## How it works

| Piece | Role |
| --- | --- |
| `cuff/app.py` | The OpenVR loop: placement, show-on-look, touch, the sphere, haptics, and the quick actions that need OpenVR (passthrough, screenshot); charms' quick buttons go to their charm. The render thread and the charm-press worker. `--debug` prints poses and touch depth. |
| `cuff/wrist.py` | Where the panel sits on the wrist and how it fades in (clean-room, see below) |
| `cuff/vr.py` | The arm64 `libopenvr_api` shim, pose maths, hand tip poses (`Hands`), double-buffered layers (`Layer`) and SteamVR's mailbox (`Mailbox`) |
| `cuff/ui.py` | Layout (240x230 logical px open, 240x88 collapsed, drawn at 2x with Pillow and Noto Sans), the fixed rows, the tab pager, icons as shapes, hit-testing, and the drawing helpers charms use |
| `cuff/charm.py` | The `Charm` base class, the loader, and the guard around every charm call |
| `cuff/settings.py` | The Settings tab (a built-in charm) |
| `cuff/quickrow.py` | The quick row: the chosen slots (`quick.json`), the buttons on offer, and how each looks |
| `cuff/services.py` | Toasts and frame timing, for every charm |
| `cuff/pc.py` | Finds Cuff for PC from its announcements, remembers it, and says whether it's there |
| `cuff/placement.py` | The panel's size and placement: slider definitions, pending and applied values, `placement.json` |
| `cuff/quick.py` | Mic mute and recenter on a worker thread, plus state polling (mic mute, the dictation server, Bluetooth earbuds) |
| `cuff/librepods.py` | AirPods battery from LibrePods' journal (it has no API) |
| `cuff/dictation.py` | The dictation button: recording, transcription (PC servers, then the local model), typing |
| `cuff/stt_local.py` | The on-headset model, as a helper process started on the first tap and gone after 5 idle minutes |
| `cuff/steam.py` | Steam's client API over its CEF debugger, for typing |
| `cuff/keyboard.py`, `cuff/keyboard.js` | The dictation key on Steam's on-screen keyboard (see "Dictation") |
| `cuff/status.py` | Battery readings for the status bar |
| `cuff/config.py` | `config.toml`, the headset profile (the Frame's paths and mic node by default) and Cuff's directories |
| `pc-server/` | Cuff for PC: the desktop app (`app.py`, `app.html`, `appapi.py`; see docs/PC-APP.md), dictation (`server.py`), the stream's PC side (`stream.py`), the announcement (`beacon.py`), the headset over SSH (`frame.py`, `videos.py`), its updates (`updates.py`), and its Windows install (`installer/`, or `install.ps1` from a git checkout; `firewall.ps1`) |
| `cuff/remote.py` | Settings, videos and screenshots for Cuff for PC: `python -m cuff.remote dump / apply / videos`, run over SSH; `apply` sends Cuff SIGHUP to reload |
| `cuff/atomic.py` | Saves the JSON settings and state files atomically (temporary file, fsync, rename), for Cuff and `cuff.remote` alike |
| `cuff.service` | The systemd user service |

- **Placement** (`wrist.py`). The panel sits on the back of the left wrist, relative to
  the left hand's **tip** pose: the wrist's centre about 13 cm behind the tip and 3 cm
  below it, the face 3.3 cm out from there, turned 60° about the forearm from straight up
  toward the outside of the wrist, with text running along the forearm toward the hand.
  It's then tipped 30° so its top edge leans away from you (`PANEL_TILT_DEG`, set in the
  headset), moving round the wrist to stay off the skin. It's 0.11 m wide. Each constant
  can be overridden under `[wrist]` in `config.toml` (`PANEL_TILT_DEG` and
  `WRIST_ROLL_DEG` first, then `WRIST_CENTRE`).
- **Show on look** (`wrist.py`). Alpha is a smoothstep of how squarely the panel faces the
  HMD (the cosine between its normal and the direction to the eyes), from 0 at 0.30
  (about 72° off) to 1 at 0.75 (about 41°), so a natural glance shows it fully. When alpha is
  0 the panel is hidden, not just transparent.
- **Clean room.** `wrist.py` was written from a behaviour spec alone, without other wrist
  overlays' code or constants, so Cuff can be MIT licensed.
- **Touch.** The right tip is moved into panel space each frame.
  - A press fires on entry: in bounds, less than 4 mm in front of the panel, and no
    more than 3 cm behind it.
  - It releases after moving more than 12 mm away or leaving the panel's edge.
  - Only a panel at alpha 0.6 or more takes touches, so a glance doesn't press anything.
  - The touch point is the right tip moved 60 mm along its aim (-Z, `POKE_REACH`).
  - A press gives a 20 ms vibration on the right controller, through the legacy
    `TriggerHapticPulse`.
- **Hand poses.** Each controller's raw pose is multiplied by its render model's
  `tip` component, the same point OpenVR's `/user/hand/*/pose/tip` input resolves to.
  - **IVRInput doesn't work here.** On the Frame the `tip` component is 12.7 mm to the
    side, 25 mm down and 21 mm back, tilted 40°. IVRInput pose actions were tried first.
    Their bindings loaded, but SteamVR kept the actions inactive for an overlay that
    isn't a Steam app: ours is `system.generated.python3.14`, while WayVR is
    `steam.overlay.3860181570`.
  - **Priority didn't help.** Setting `k_nActionSetOverlayGlobalPriorityMin` changed
    nothing, because that priority also needs an experimental developer setting.
- **Layers ride the controller.** The panel, the placement preview, the toast and the
  scrolling line are placed with `SetOverlayTransformTrackedDeviceRelative`: `tip ×
  offset` relative to the wearing controller (`Hands.attach`). The compositor then moves
  them with its own latest pose every frame.
  - **Why:** they used to be placed in standing space from a pose the loop read each frame.
    On a frame the loop ran late (writing a PNG, for example), the panel stayed where the
    hand had just been, and visibly jumped back while the hand moved.
  - **What stays in standing space:** the fade and touch still work from that pose. So do
    the billboarded layers (the touch sphere and the alert glows), which face the head.
- **Threads.** OpenVR calls stay on the main thread, and no charm code runs there.
  - The render thread draws the panel, and with each frame works out the open charm's
    hit boxes and scrolling line, which the main loop reads.
  - Charm presses run one at a time on their own worker (`Presses`).
  - **Budget.** A redraw over 50 ms counts against the charm on show and is logged (at most
    once a minute). After three in a row, redraws are spaced at least 1 s apart until one
    comes in under budget.
- **Images go in as PNG files, not raw.** Each layer image is written to
  `$XDG_RUNTIME_DIR/cuff/` (about 6 ms for the full panel) and loaded with
  `SetOverlayFromFile`, under a new file name every time (SteamVR caches by path; the
  last 6 per layer are kept).
  - **Why not raw.** **SteamVR accepts only ~200 `SetOverlayRaw` calls per process.**
    After that every call fails with `RequestFailed` for good. It doesn't matter whether
    the overlay is visible, and pausing doesn't help. In a test, 201 of 600 succeeded.
  - **What it broke.** The scrolling title hit that limit after about 2.5 minutes, and
    the panel exited.
  - **File uploads have no such limit.** 600 of 600 succeeded.
  - **But every file costs SteamVR memory for good** (measured 2026-11-05, SteamVR 2.18).
    - **The cost:** vrcompositor keeps each loaded path's image, about 1.15 MB for a
      480x460 panel with mipmaps, until SteamVR restarts.
    - **Nothing frees it:** not ClearOverlayTexture, not destroying the overlay, not
      deleting or overwriting the file. Reusing a name shows the old image.
    - **Raw uploads aren't cached,** but they hit the cap above.
    - **What it cost before v1.2.1:** redrawing the Performance tab every second, panel in
      view or not, was about 4 GB an hour, and 12.9 GB built up over 66 hours on
      2026-11-03.
    - **Since v1.2.1, `Layer` loads as little as it can:**
      - nothing while the layer isn't on show;
      - nothing while SteamVR's activity level says the headset isn't in use (Standby,
        or a timeout). Its pose freezes on a desk, so the panel can seem to be in view.
      - nothing identical, pixel for pixel, to what it last loaded.
      The latest image waits and loads when the layer is shown. With the panel on
      Performance and the headset idle, vrcompositor grew by 0 kB in 2 minutes, against
      11.5 MB every 10 s before.
    - **The real fix,** a texture instead of a file (`SetOverlayTexture`), is still to do.
- **Double-buffered layers (`Layer`).** The panel, the title and the sphere each use
  two overlays, each with its own PNG file.
  - **Why.** Replacing an overlay's image made it blink on every redraw. That showed
    on every touch: one redraw on press, one on release.
  - **How.** A new image goes to the hidden twin, which is shown two frame syncs later.
    The old one is hidden the frame after that. Images that arrive mid-swap wait, and
    only the latest is kept.
  - **Errors.** A failed load is retried 30 frames later. Any OpenVR error inside a
    frame is logged (at most once a minute) and that frame is skipped.
- **Library.** Uses pyopenvr `2.12.1401` and Pillow `13.0.0` in a venv with system
  site-packages (for `dbus-python`). pyopenvr bundles only x86 libraries, so its
  `LoadLibrary` is redirected to `/opt/steamvr/bin/linuxarm64/libopenvr_api.so`
  (`OPENVR_API_LIB` overrides this). The runtime provides the FnTables it asks for:
  `IVRSystem_023`, `IVROverlay_028`, `IVRRenderModels` and `IVRInput_010`. IVRInput is
  used only to get the hand's input-source handle.

**WayVR's watch sits in the same spot.** While both are running they overlap. Turn
WayVR's watch off in its settings.

## Passthrough

SteamVR calls passthrough "Room View".
- **What owns it.** SteamVR's systemlayer, in the compositor, owns the toggle. Its own
  dashboard flips it with an internal JS binding
  (`VRHTML.VROverlayInternal.SetCameraRoomViewEnabled`).
- **What works: SteamVR's own mailbox.** vrclient exposes `IVRMailbox_001` through
  `VR_GetGenericInterface`, the route the dashboard's systemlayer client uses. Cuff
  registers a mailbox (`cuff_<pid>`) and sends to `vrcompositor_systemlayer`:
  `{"type": "set_camera_roomview_enabled", "enabled": …}` (Room View itself) and the same
  with `_forsceneapps` (over a running game); the `_forsceneapps` one alone did nothing in
  the Steam menu. The compositor log answers `[systemlayer] Clearing chaperone bubble
  because room view for scene apps was enabled`.
  - The vtable layout (0 register, 1 unregister, 2 send, 3 read) was found by probing
    SteamVR 2.18.4 on the Frame. `vr.Mailbox` records that version. Read is
    `(handle, buf, size, &needed)` and returns a reply sent to `returnAddress`; sending
    `{"type": "get_debug_commands", "returnAddress": …, "message_id": 1}` to a mailbox lists
    the commands it takes.
- **It's checked, since it's undocumented.** At startup Cuff opens the mailbox once and logs
  the runtime version, noting when it isn't the tested one. If the interface is missing,
  registering fails or a send is refused, the button is greyed out. After each press, Room
  View's state (below) should follow within 4 s. If it doesn't, the log says so and the
  button shows what SteamVR reports. (It used to grey out for the rest of the run, which on
  2026-10-31 took out a turn-off that had worked.)
- **The button's state** (`vr.RoomView`) comes from two places, depending on whether a game
  (a scene app, Steam Link's `vrlink` included) has focus.
  - **No game:** SteamVR's `camera/enableConstructRoomView` setting. It's written as `true`
    when Room View is on and removed when it's off, whether the panel or the dashboard
    toggled it.
  - **In a game:** the setting stays as it was while Room View goes on and off over the
    game. So the state comes from the compositor's log
    (`~/.local/share/Steam/logs/vrcompositor.txt`), whose system layer logs "room view for
    scene apps was enabled" and "room view was turned off". Cuff reads the log through on
    start, and again when a new SteamVR run starts a new one, then follows it. The
    turned-off line appears only when the compositor is forcing Room View (a popped
    chaperone bubble), not after a plain turn-off. So a press SteamVR accepted counts as the
    state until the log says otherwise.
  - Before this, a press in a game always asked for "off" and was greyed out.
  - **When it's read:** every 2 s, every 0.25 s while a press awaits confirmation, and on
    `CameraSettingsHaveChanged`. `VREvent_RoomViewShown`/`Hidden` never arrived.
  - **Recenter's blink** logs "enabled" too. So when the blink turns Room View off, the log
    is read through before that's noted, and an "enabled" line within 1 s after is ignored;
    otherwise the next read flipped the button back to on with Room View off. A press during
    the blink turns Room View on (it was off before the blink) and cancels the blink's
    turn-off. Both messages are always sent, and a press or blink counts if SteamVR accepted
    either.
- **Routes that don't work:**
  - Writing that setting: it changes, but the compositor only reports it, so Room View
    doesn't follow.
  - `vrcmd --compositorcmd toggle_camera_roomview_forsceneapps` reaches the compositor
    mailbox, which has no handler for it.
  - Steam's own UI only knows the Room View device properties.
  - The same mailbox over vrserver's web socket (`ws://127.0.0.1:27062/?secret=…`) works,
    but it needs the web secret from Steam's CEF debugger. Cuff used that before.

## Overlay depth

The Frame's compositor depth-tests overlays against each other. Measured on SteamVR 2.18.4
with two squares in front of the headset and `TakeStereoScreenshot` (one capture per
process, about 10 s apart, since back-to-back captures come back blank):

| A 20 % blue square is… | Drawn | Where it overlaps an opaque red one |
| --- | --- | --- |
| nearer | last | red shows through, tinted |
| farther | last | pure red: blue hidden |
| nearer | first | red blanked |
| farther | first | pure red: blue hidden |
| farther, at 50 %, red at 60 % | first | purple: blue shows through |

So a translucent overlay still hides what's behind it, and the draw order only decides
blending when the see-through one is nearer and drawn later. `SortWithNonSceneOverlays`
changes nothing, and no overlay flag turns depth testing off. The placement preview uses the
last row: it switches sides by distance, and the panel fades while the preview is behind it.

**The panel hides while a SteamVR keyboard is open.** While it's up, SteamVR blanks
other apps' controller poses: `bPoseIsValid` is 0 and the matrix is identity, though
`eTrackingResult` stays `Running_OK`. That's apparently so typing can't be read from hand
motion. The panel then can neither follow the wrist nor be touched.

What was tried first:
- **Holding the panel** at the last wrist pose: visible, but it couldn't be touched.
- **Accepting the blanked poses:** they're identity, so the panel jumped and flickered.
- **Taking SteamVR laser clicks.**

The user chose hiding.

The layers still set `VROverlayFlags_VisibleInDashboard`, so the panel stays up over the
Steam menu.

**A keyboard button** was on the quick row until the record button replaced it. It opened
SteamVR's keyboard for the visible window, and was greyed out with no window to attach to.
Making the panel the keyboard's target flickered, because the panel swaps twins and hides
while a keyboard is open. Dictation types through `ControllerKeyboardSendText`; backspace,
enter and tab go through as `ControllerKeyboardSetKeyState` presses.

## Dictation

- **Recording.** `pw-record` at 16 kHz mono from the default mic, until the second tap
  (`SIGINT`, so the WAV header is finished) or `max_seconds` (60). Under 0.3 s counts as an
  accidental tap.
- **Transcription.** Any `[dictation] servers`, then Cuff for PC (`POST /transcribe`, 10 s
  timeout), whichever answered a `/health` probe sent while you were talking; then the
  headset's own model unless `local = false` (away from home, or with the PC off).
  - **The local model** is `cuff/stt_local.py`, a helper process: Parakeet TDT 0.6B v2 int8
    through onnx-asr on the CPU. Measured on the Frame: 2.1 s of speech in 0.3 s (0.65 s the
    first time), about 2 s to load from the cache, 1.4 GB resident.
  - It starts when you tap, so the load overlaps your speech, and exits after 5 idle
    minutes, giving the memory back.
- **Typing.** `SteamClient.Input.ControllerKeyboardSendText` in Steam's SharedJSContext,
  over its CEF remote debugger (127.0.0.1:8080). It reaches games, the nested desktop and
  Steam itself. It's undocumented, so Cuff checks at startup that the call exists, and
  dims the button if it doesn't. A Steam call that fails later (the debugger unreachable, or
  the expression throwing, which the debugger reports as `exceptionDetails`) forgets that
  check: the button dims again until the call answers (asked every 10 s).
- **Your own tool instead.** `[dictation] command` runs an external push-to-talk command:
  it's stopped with `SIGUSR1`, types the text itself, and reports
  `{"phase": "listening" | "transcribing" | "done" | "error" | "idle", "text"}` JSON lines on
  stdout.
- **A key on Steam's keyboard** (`cuff/keyboard.py`, `cuff/keyboard.js`). Selecting a text
  field opens Steam's keyboard, which hides the panel (above), so the keyboard gets a mic key
  of its own that does what the quick row's button does.
  - **Where:** over the left end of the space bar, as wide as Steam's switch key beside it,
    in the space bar's own colours. It's placed from the space bar's position every 0.5 s,
    so it follows anything that resizes Steam's rows. Blue under the laser, red while
    listening, amber while transcribing, dimmed when dictation can't type.
  - **How:** one thread keeps a WebSocket to the debugger's "SteamVR - Keyboard" page. It
    adds a `__cuffDictate` binding, which the key calls on a press. It injects
    `keyboard.js` on every new JS context (a reload, Steam restarting), and calls
    `__cuffMic.set({phase, ready})` when dictation's state changes. The press is kept from
    Steam, so it doesn't type a space. It relies only on the page title and `data-key`
    attributes, not Steam's generated class names. If the space bar can't be found, the key
    doesn't show.
  - **Off:** `[dictation] keyboard_key = false`.
  - **Checked on the Frame (2026-11-03):** injected beside another add-on's extra row; a
    press over the debugger started and stopped dictation; with the laser, the user dictated
    into a text field and the space bar still typed spaces.


## Notifications and alerts

- **Toasts** (`services.Toasts`) are a one-line banner in their own row, an overlay just above
  the panel's top edge, for 4 s. One per source per 3 s, at most three waiting; the rest are
  dropped and logged.
- **Alerts** are toasts raised with `haptic=True`. Each also gets three 20 ms buzzes on the
  controller of the wrist the panel is on, a chime (`cuff/chime.py`: two soft notes made
  into a WAV once and played with `pw-play` under a `*_filter_chain` node name, so SteamOS's
  spatial audio leaves it on the default output), and, while the panel is turned away, a
  glow on the hand.
- **Glows.** One per waiting alert, up to four in a row 36 mm apart, on the far side of that
  controller from the head: 7 cm past the controller's middle along the line from the eyes
  (`ORB_BEHIND`), and 7 cm up from that line as seen from the head (`ORB_ABOVE`), running
  sideways across the view. So they show just beyond and above the controller as you look at
  it; straight behind it, the controller hid them. Each is a billboarded soft glow whose
  bright core is twice the touch sphere's size, white by default, orange or red for a
  battery's first and second levels, with the alert's icon in the middle
  (`vr.glow_texture`). They go out when the panel is looked at, or after 8 s. A burst of
  alerts plays one chime.
- **Test alert** (Settings > Alerts) waits 3 s, so the panel can be turned away, then lights
  three: white, orange with a PNG (`cuff/assets/bell.png`) and red with an image drawn in code.
- **The headset's power.** `PowerWatch` alerts when the headset goes from charging to
  plugged-in-but-draining ("Headset not charging": a battery pack running flat) or from
  either to on battery ("External power lost"). The power state is read every 2 s on its own
  and a change must hold for 5 s, so an alert lands about 6 s after a cable is pulled. At 95 %
  or more, charging stopping is the battery topping up, so it doesn't alert.
- **Low battery.** `BatteryWatch` alerts when a device drops to one of its levels while not
  charging, and re-arms once it's 5 % above, or charging. Steam warns about the headset itself
  (its quick access menu from 10 %, "really low" under 4 %, `CSteamEngine::CheckForLowBattery`)
  and about controllers (their own low-battery flag), so by default the headset and
  controllers get one earlier level (20 %) and the headphones, which nothing else watches,
  20 % and 5 %.

## Cuff for PC

- **Finding it.** It broadcasts `{"app": "cuff-pc", "id", "port", "name", "pairing", "typing"}`
  every 2 s on UDP 8766 from each of the PC's addresses on networks Windows calls Private or
  Domain, and on the dongle's 10.35.78.0/24, which Windows calls Public (`pc-server/beacon.py`;
  the categories come from `Get-NetConnectionProfile` once a minute, and if that fails it
  announces everywhere, as before). `id` is the PC's own, random and kept with its tokens. The
  addresses come from `getaddrinfo(b"..localmachine")`, which works for a computer name that
  isn't ASCII. The headset listens (`cuff/pc.py`) and keeps addresses per PC id, but uses one
  only once the PC there has proved it holds this headset's token (a signed `GET /health`
  whose answer is signed and names the id; redone once a minute), then the newest such address
  heard in the last 10 s, so it's found over home Wi-Fi and over the dongle link alike. A
  malformed announcement is ignored.
- **The desktop app** (`pc-server/app.py`, its page `app.html` and API `appapi.py`): see
  docs/PC-APP.md. The page lists the paired headsets by a short hash of their token, each
  with when it was last heard from (in memory: a paired headset's once-a-minute check of each
  address is its hello, `cuff/pc.py`). `--no-beacon` runs a second copy that doesn't announce
  itself, for testing.
- **Settings from the PC** (`cuff/remote.py`). Cuff takes `SIGHUP` as "settings changed": the
  overlay loop reloads placement (`Placement.reload`), alerts (`Prefs.reload`) and the quick
  row (`quickrow.reload`), and calls each charm's `reload()` on a thread of its own. Charms on
  or off and the tab order (`charms.json`'s `order`, which wins over `config.toml`'s `tabs`)
  wait for a restart. Its output carries `"api": 1`, a protocol version for Cuff for PC to
  compare. `videos` lists the clips and recordings (`"folder"`, `"videos"`, `"recording"`),
  and, since v1.1.0, the screenshots too: `"screenshot_folder"` (the profile's
  `screenshot_dir`, expanded) and `"screenshots"`, one entry per pair with its `"name"`
  (`<time>`), `"vr"` and `"preview"` file names (null when that file's missing), `"sizes"`,
  `"size"` and `"mtime"`. New keys keep `"api": 1`; Cuff for PC lists the default folder over
  SFTP when they're absent.
- **New recordings** (since v1.2.0). When a screenshot is taken (2 s later, once its files are
  written) or the Record charm saves a clip or recording (`ctx.recordings_changed()`),
  `cuff/pc.py` sends Cuff for PC a signed `POST /recordings/changed` in the background, so its
  Recordings page lists it without a Refresh. Nothing about the file is sent, and an
  unreachable or older PC is ignored.
- **Saving settings.** Cuff and `cuff.remote` both write the JSON files under
  `~/.config/cuff` (and Cuff's cache) through `cuff/atomic.py`: a temporary file in the same
  folder, flushed to disk, then renamed over the old one, so a crash mid-write can't leave
  half a file. A file that's there but can't be read is logged, and the defaults are used.
- **Pairing** (`pc-server/pairing.py`). `pair.ps1` (or the app page) opens pairing for 5 minutes (from the PC
  itself only; running it again while open keeps the request) and asks for the code in a
  "Pair with Cuff" window. The headset makes up a 4-digit code, shows it as an alert that
  stays pinned above the panel until pairing ends (`Toasts.pin`), then alerts again with
  "Paired with <PC>" or "Pairing closed: run pair.ps1 on <PC> again".
  - **The code never crosses the network.** The headset asks with a random request id and its
    half of a SPAKE2 exchange keyed by the code (RFC 3526's 2048-bit group; both ends carry the
    same few lines). When the code is typed on the PC (from the PC itself only), the PC answers
    each waiting request with its half and a new token sealed with the key, and waits up to
    10 s. The headset collects that from the address it asked from (`/pair/result`), opens it
    only if it was made with its code, and proves so (`/pair/finish`); only then is the token
    paired and the PC window says so. A device that doesn't know the code can't pose as the PC
    (the headset gives up after 3 answers made with another code) or pair in its place.
  - **Requests:** each address may ask once a second and hold 2 requests; with 8 waiting, a
    new one pushes out the oldest rather than being refused.
  - **Tokens** are kept in `~/.config/cuff/pc-pairing.json`, mode 600, under the PC's name,
    with `"@ids": {<PC id>: <PC name>}` saying which PC each belongs to. Pairing over SSH
    writes the binding with the token (`frame.py`), so a reinstalled Cuff for PC, which has a
    new id, takes the name over. One under the name only (an older Cuff's) is tried with the
    PC of that name and bound to its id the first time that PC proves it holds it; once bound,
    the name is no use to another id.
  - **Signed, not sent.** The token never crosses the network after pairing. Every request but
    `/health` and pairing is signed with it: `Authorization: Cuff v=1, id=<headset id>,
    ts=<unix time>, nonce=<random>, mac=<hex HMAC-SHA256(token, method
path?query
ts
nonce

    sha256(body))>`, the headset id being the short hash the app page shows. The PC refuses a
    request more than 60 s off its clock, or a nonce it has seen. It signs its answer to a
    signed request (`X-Cuff-Mac`: HMAC-SHA256(token, `response
nonce
status
sha256(body)`)),
    and the headset acts on no answer that isn't signed, so a device copying the announcement
    gets neither the token, nor dictation audio, nor a way to type on the headset. Charms and
    dictation get a marker from `pc_headers()` instead of the token; `cuff/pc.py` signs marked
    requests in urllib as they leave, only to a checked address. `STT_TOKEN`, set by hand on
    both ends, also works as a bearer token. A signed upload is read before the 401, so the
    client sees the answer.
  - **From the PC itself**, routes that trust 127.0.0.1 (`/pair/open`, `/pair/confirm`, and
    `/stream`, `/transcribe`, `/typing/next` without a signature, `/recordings/changed`) need
    the app page's guard too:
    a `Host` of 127.0.0.1 or localhost and `X-Cuff-App: 1`; JSON bodies must be sent as
    `application/json`. So a web page in a browser on the PC can't use them.
  - **Clocks:** the headset's and PC's clocks must agree to within a minute (both normally set
    themselves); otherwise the PC answers 401 and says so.
- **Firewall** (`pc-server/firewall.ps1`): TCP 8765 on Private networks, and on Public ones
  only from 10.35.78.0/24, the Frame's dongle subnet, which Windows classes as Public. It isn't
  tied to the dongle's adapter: its name varies, and it may not be plugged in when the rule is
  made. A public network using that subnet could reach the port, but past `/health` and
  pairing (which needs pair.ps1 open and the code) everything needs a signature.
- **Dictation's pins.** `onnxruntime-gpu` 1.24 with cuDNN 9.10: ORT 1.31 targets CUDA 13, which
  has no Windows wheels, and cuDNN 9.27 fails with `CUDNN_BACKEND_API_FAILED`, after which ORT
  silently runs on the CPU. Without an NVIDIA GPU it runs on the CPU too, slower but working.
- **Stream stats** (`pc-server/stream.py`): OpenVR as a background app on the PC (it never
  starts SteamVR), sampled once a second: the game's app key, framerate and target, GPU and
  CPU milliseconds per frame, dropped and reprojected frames, and reprojection flags.
- **Link log** (`pc-server/linklog.py`): `<Steam>/logs/driver_vrlink.txt` read as it grows,
  once a second. On start it reads the last 512 KB for the latest estimate. A shrunk or
  rewritten file (SteamVR rotates it at start) is read from the top. Lines used:
  `Link BW: Est`, `Link BW hist` (5 Mbit/s bins; Cuff reports the 10th percentile),
  `THROTTLE EVENT: <µs>` (1 s or more counts as a stall), `Bad link event: ... <a> mbit/s ->
  <b> mbit/s, ping <ms>`, `Connection active|inactive`, `Reset video stream` and the encoder's
  perf warnings. The estimate and bitrate are dropped after 2 minutes without a new line.
  The formats came from SteamFrame Advanced Options (MIT) and were checked against this PC's
  logs.
- **Dongle USB speed** (`pc-server/usbdongle.py`): the USB hub IOCTLs (no admin) for Valve
  `28DE:2432`. The V2 query's operating flags tell USB 3 from USB 2, because the EX query
  alone can call a SuperSpeed device High-Speed. Checked at most every 30 s. Ported from
  SteamFrame Advanced Options (MIT).
- `uv run server.py --selftest clip.wav` times one transcription; `--stats-only` skips the
  speech model; `uv run stream.py` prints one stream answer; `python linklog.py [log]`
  replays a driver log (counting back from its last line); `python usbdongle.py` prints the
  dongle's speed.
