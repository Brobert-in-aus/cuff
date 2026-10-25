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
    - **The headset:** its display rate and compose time, CPU / GPU / battery temperature
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
- **Audio** (`charms/audio`). The default output's volume (− and +, 5 % steps) and mute, and
  the real outputs to send sound to (those with a `device.api`: the built-in speakers, a USB-C
  adapter, Bluetooth headphones; filter chains and virtual mics are left out), through pactl.
- **Settings** (`cuff/settings.py`). Part of the core and pinned to the end of the tab row,
  drawn as a gear. Four sub-tabs:
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
- **Now playing** (`charms/now-playing`). Album art, title, artist, and buttons for previous,
  play/pause, next, volume down and volume up.
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
    the Spotify app", with the name from `~/.config/soloist/env`.
  - **Volume:** the buttons step the player's MPRIS `Volume` by 5%, separate from the system
    sink. For a Connect speaker that's the Spotify app's slider. A bar under the buttons and
    a "· 70%" next to the source show the level. Players without a writable `Volume` ignore
    them.

## The panel

The panel has four rows, top to bottom:

1. **Status bar**: time and batteries. It's never interactive.
2. **Quick actions**: always shown.
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
  - **The earbuds** come from BlueZ: a connected audio `org.bluez.Device1` with a
    `Battery1` percentage, polled every 30 s by `quick.py`. It's one figure for the pair;
    the WF-1000XM3 reported 100 over HFP. They're read
  every 10 s (every 2 s while one hasn't reported yet), and the panel is redrawn only when
  the minute or a battery value changes.
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
- **Quick actions.** Six buttons, 34x24 px each. Toggles show their state as a fill;
  actions only flash when pressed. They're fixed: the same six for everyone, and charms
  can't add to them. To add one to Cuff itself, add an entry to `ui.QUICK`, a glyph in
  `ui.glyph`, and a handler in `quick.py` or `app.py`.

  | Button | What it does | How |
  | --- | --- | --- |
  | Dictation (speech bubble) | Push-to-talk: tap, talk, tap. The text is typed into the focused window: a game, the desktop or Steam. Red while listening, amber while transcribing; dimmed when Steam's text input isn't reachable. | `pw-record`, then a PC server if configured, else the headset's own model; typed with Steam's `ControllerKeyboardSendText` (see "Dictation" below) |
  | Mic | Mutes the physical mic (the profile's `real_mic`). Red with a slash while muted. Anything mixed into a virtual mic downstream of it still gets through. | `pactl set-source-mute <real_mic> toggle`, state polled every 2 s |
  | Passthrough (eye) | SteamVR Room View on/off. Teal while on, following SteamVR's real state; greyed out if SteamVR's mailbox isn't there or didn't respond. | SteamVR's mailbox; see "Passthrough" below |
  | Recenter (target) | Resets the forward direction. | `vrcmd --recenter` |
  | Keyboard | Opens SteamVR's keyboard for the visible window: a game or desktop (gamescope) or the Steam menu. That window gets the typing, like the "show keyboard" button SteamVR draws under it. With no window visible the button is greyed out and does nothing (checked every 0.5 s). The panel hides while any keyboard is open (see below). | `ShowKeyboardForOverlay` on `valve.steam.desktopgame.N` (if visible) or `valve.steam.gamepadui.main`; state from `VREvent_KeyboardOpened_Global` / `KeyboardClosed_Global` |
  | Screenshot (camera) | Saves a stereo screenshot to `~/Pictures/Frame screenshots/` (`*-vr.png` and `*-preview.png`) 3 s after the tap (the button counts down 3, 2, 1 in amber), so you can turn the panel out of view; the wrist buzzes as it's taken. | OpenVR `TakeStereoScreenshot` on the overlay thread; `vrcmd --screenshot` did nothing on the Frame |

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
| `cuff/app.py` | The OpenVR loop: placement, show-on-look, touch, the sphere, haptics, and the quick actions that need OpenVR (passthrough, keyboard, screenshot). The render thread and the charm-press worker. `--debug` prints poses and touch depth. |
| `cuff/wrist.py` | Where the panel sits on the wrist and how it fades in (clean-room, see below) |
| `cuff/vr.py` | The arm64 `libopenvr_api` shim, pose maths, hand tip poses (`Hands`), double-buffered layers (`Layer`) and SteamVR's mailbox (`Mailbox`) |
| `cuff/ui.py` | Layout (240x230 logical px open, 240x88 collapsed, drawn at 2x with Pillow and Noto Sans), the fixed rows, the tab pager, icons as shapes, hit-testing, and the drawing helpers charms use |
| `cuff/charm.py` | The `Charm` base class, the loader, and the guard around every charm call |
| `cuff/settings.py` | The Settings tab (a built-in charm) |
| `cuff/services.py` | Toasts and frame timing, for every charm |
| `cuff/pc.py` | Finds Cuff for PC from its announcements, remembers it, and says whether it's there |
| `cuff/placement.py` | The panel's size and placement: slider definitions, pending and applied values, `placement.json` |
| `cuff/quick.py` | Mic mute and recenter on a worker thread, plus state polling (mic mute, the dictation server, Bluetooth earbuds) |
| `cuff/dictation.py` | The dictation button: recording, transcription (PC servers, then the local model), typing |
| `cuff/stt_local.py` | The on-headset model, as a helper process started on the first tap and gone after 5 idle minutes |
| `cuff/steam.py` | Steam's client API over its CEF debugger, for typing |
| `cuff/status.py` | Battery readings for the status bar |
| `cuff/config.py` | `config.toml`, the headset profile (the Frame's paths and mic node by default) and Cuff's directories |
| `pc-server/` | Cuff for PC: dictation (`server.py`), the stream's PC side (`stream.py`), the announcement (`beacon.py`), and its Windows install (`install.ps1`, `firewall.ps1`) |
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
  - The vtable layout (0 register, 1 unregister, 2 send) was found by probing SteamVR
    2.18.4 on the Frame. `vr.Mailbox` records that version.
- **It's checked, since it's undocumented.** At startup Cuff opens the mailbox once and logs
  the runtime version, noting when it isn't the tested one. If the interface is missing or
  registering fails, the button is greyed out. After each press, Room View's state (below)
  must follow within 4 s, or the button is greyed out for the rest of the run, with the
  reason in the log.
- **The button's state** (`vr.RoomView`) comes from two places, depending on whether a game
  (a scene app, Steam Link's `vrlink` included) has focus.
  - **No game:** SteamVR's `camera/enableConstructRoomView` setting. It's written as `true`
    when Room View is on and removed when it's off, whether the panel or the dashboard
    toggled it.
  - **In a game:** the setting stays as it was while Room View goes on and off over the
    game. So the state comes from the compositor's log
    (`~/.local/share/Steam/logs/vrcompositor.txt`), whose system layer logs "room view for
    scene apps was enabled" and "room view was turned off". Cuff reads the log through on
    start, and again when a new SteamVR run starts a new one, then follows it.
  - Before this, a press in a game always asked for "off" and was greyed out.
  - **When it's read:** every 2 s, every 0.25 s while a press awaits confirmation, and on
    `CameraSettingsHaveChanged`. `VREvent_RoomViewShown`/`Hidden` never arrived.
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

**Keyboard with no window to attach to:** the button is greyed out. Two things were tried
first:
- **The panel as the keyboard's target.** It flickered, because the panel swaps twins and
  hides while a keyboard is open.
- **An invisible anchor overlay**, forwarding the typing to the focused app through
  Steam.

The user chose to disable the button instead. Text goes through `ControllerKeyboardSendText`; backspace,
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
  Steam itself. It's undocumented, so Cuff checks once at startup that the call exists, and
  dims the button if it doesn't.
- **Your own tool instead.** `[dictation] command` runs an external push-to-talk command:
  it's stopped with `SIGUSR1`, types the text itself, and reports
  `{"phase": "listening" | "transcribing" | "done" | "error" | "idle", "text"}` JSON lines on
  stdout.


## Notifications and alerts

- **Toasts** (`services.Toasts`) are a one-line banner in their own row, an overlay just above
  the panel's top edge, for 4 s. One per source per 3 s, at most three waiting; the rest are
  dropped and logged.
- **Alerts** are toasts raised with `haptic=True`. Each also gets three 20 ms buzzes on the
  left controller (the wrist the panel is on), a chime (`cuff/chime.py`: two soft notes made
  into a WAV once and played with `pw-play` under a `*_filter_chain` node name, so SteamOS's
  spatial audio leaves it on the default output), and, while the panel is turned away, a
  glow on the hand.
- **Glows.** One per waiting alert, up to four in a row 36 mm apart, on the far side of the
  left controller from the head: 7 cm past the controller's middle along the line from the
  eyes, running sideways across the view, so they show just behind the controller as you look
  at it. Each is a billboarded soft glow whose bright core is twice the touch sphere's size,
  white by default, orange or red for a battery's first and second levels, with the alert's
  icon in the middle (`vr.glow_texture`). They go out when the panel is looked at, or after
  8 s. A burst of alerts plays one chime.
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

- **Finding it.** It broadcasts `{"app": "cuff-pc", "port", "name", "pairing"}` every 2 s on UDP
  8766 from each of the PC's addresses (`pc-server/beacon.py`); the headset listens
  (`cuff/pc.py`) and uses the newest address heard in the last 10 s, so it's found over home
  Wi-Fi and over the dongle link alike.
- **Pairing** (`pc-server/pairing.py`). `pair.ps1` opens pairing for 5 minutes (from the PC
  itself only; running it again while open keeps the request) and asks for the code in a
  "Pair with Cuff" window. The headset makes up a 4-digit code, shows it as an alert that
  stays pinned above the panel until pairing ends (`Toasts.pin`), then alerts again with
  "Paired with <PC>" or "Pairing closed: run pair.ps1 on <PC> again". It asks to pair
  with a random request id; the code typed on the PC (from the PC itself only) pairs only the
  request carrying it, so another device asking at the same time can't, without the code on
  the wrist. The headset then collects a token (kept in `~/.config/cuff/pc-pairing.json`, mode
  600) and sends it as a bearer token. Every request but `/health` and pairing needs one; a
  rejected upload is read before the 401 so the client sees the answer.
- **Firewall** (`pc-server/firewall.ps1`): TCP 8765 on Private networks, and on Public ones
  only from 10.35.78.0/24, the Frame's dongle subnet, which Windows classes as Public.
- **Dictation's pins.** `onnxruntime-gpu` 1.24 with cuDNN 9.10: ORT 1.31 targets CUDA 13, which
  has no Windows wheels, and cuDNN 9.27 fails with `CUDNN_BACKEND_API_FAILED`, after which ORT
  silently runs on the CPU. Without an NVIDIA GPU it runs on the CPU too, slower but working.
- **Stream stats** (`pc-server/stream.py`): OpenVR as a background app on the PC (it never
  starts SteamVR), sampled once a second: the game's app key, framerate and target, GPU and
  CPU milliseconds per frame, dropped and reprojected frames, and reprojection flags.
- `uv run server.py --selftest clip.wav` times one transcription; `--stats-only` skips the
  speech model; `uv run stream.py` prints one stream answer.
