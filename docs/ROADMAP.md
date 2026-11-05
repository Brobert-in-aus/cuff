# Roadmap

Planned work, roughly in order. Nothing here is started unless it says so.

## Cuff for PC as a desktop app (done)

Shipped in v1.0.0 (2026-11-03; `docs/PC-APP.md`). Cuff for PC is a tray app with a window
(Overview, Headset setup, Recordings, Cuff settings, Connection, Type on the Frame, Log),
installed by a normal Windows installer with nothing to install first. It starts at logon
and updates itself from GitHub releases. It installs and updates Cuff on the headset over
SSH and pairs it, downloads recordings and screenshots (also automatically), edits every
Cuff setting, types on the headset from the PC keyboard and sends the clipboard, and shows
the link (dongle or network). Enter and the arrow keys go through Steam's key call, which
isn't yet tried in the headset.

## A customisable quick row (done, needs a headset check)

Done (2026-10-26, `cuff/quickrow.py`):
- **Charms add buttons** with `[quick.<id>]` in `charm.toml`, a `quick(button)` method for
  the look (glyph, style, short text), and `press("quick:<id>")`. The record button moved
  into the Record charm this way; Now Playing offers play/pause.
- **Users choose** in Settings > Quick: tap a slot, then its button (one in the row swaps
  places), Empty, or Defaults. Saved in `~/.config/cuff/quick.json`.
- **A new charm's button is offered, not added.**

Checked with renders on the headset. Still to check in the headset: how choosing feels.
Later, if charms offer many buttons: a second page of choices (the grid holds 12).

## A better Now Playing (mostly done)

Done (2026-10-26):
- skip back and forward 15 s;
- a scrub bar with elapsed and remaining time;
- picking the player when several are open;
- playback speed.

Tested against mpv over SSH. Still to come:

- **Shuffle and repeat** (`Shuffle`, `LoopStatus`), where the player has them.
- **The output** it plays through, as a shortcut to the Audio tab's choice.

## Stabilised recordings (done, needs a headset check)

The Record charm's **Steady** setting (Off, Light, Strong) is built (`recorder/src/stabilise.rs`,
`recorder/shaders/stabilise.comp`).

- **How it works:** each frame the recorder smooths the headset's orientation and turns the
  crop against what's left, within the margin a narrower crop leaves (Light 3.0°, Strong
  5.1° for 16:9). A small GPU pass rebuilds the lookup grid in the same submission as the
  conversion.
- **Measured on the headset** (2026-10-26, panels idle):
  - 0.05 ms of CPU a frame;
  - the recorder at 4.9% of a core, the same as without;
  - no change in the conversion's GPU time.
  - At startup the GPU's grid matches the ordinary one within 0.22 panel px with no
    rotation, and the CPU's within 0.000 px for a test turn.
- **Still to check in the headset:**
  - how it looks;
  - whether the head pose matches each scanout frame: `--pose-predict` sets how far ahead
    the pose is read, default 0;
  - the smoothing strengths (Light τ 0.12 s, Strong 0.35 s).
- **Later, if live smoothing lags too much:** record the head pose alongside a slightly
  wider crop, and stabilise with look-ahead when the video's played or exported.

## Gaze in recordings (researched)

A marker where the wearer is looking, drawn into Record's video. Researched 2026-10-26,
off-head; nothing is built yet.

- **Where gaze comes from:** SteamVR's `eyetracking` tool
  (`/opt/steamvr/tools/eyetracking/bin/linuxarm64/eyetracking`) runs as a background app and
  serves gaze to apps. `frame_hmd_additional.vrsettings` allows it (`allowEyeTracking`,
  15-90 Hz).
- **Routes to it, in order of preference:**
  - **OpenVR input:** an action of type `eyetracking`, read with
    `IVRInput::GetEyeTrackingDataRelativeToNow` (`IVRInput_011`, slot 12), which gives a
    gaze origin and target. Unknowns: the binding's device path (no example binding ships
    with SteamVR, and the Frame's input profile lists no eye components), and whether SteamVR
    gives input to an overlay or background app at all; it doesn't for hand poses.
    Off-head, every path tried returned `InvalidHandle` (3) with nothing tracked.
  - **OpenXR:** `XR_EXT_eye_gaze_interaction`. Needs an OpenXR session, which the recorder
    and Cuff don't have.
  - **The tool's shared memory**, `/dev/shm/eye-server.mmap` (readable by anyone,
    undocumented client/server protocol with protobuf messages). A last resort: it can
    change with any SteamVR update. Off-head, nothing in it changed.
  - **On the PC, while streaming:** the PC's vrlink driver has a "Share eye tracking data to
    other apps on this PC" setting (`/settings/driver_vrlink/shareEyeTrackingData`, off by
    default), next to "Share face tracking data … via OSC". So gaze reaches the PC, and
    Cuff for PC could read it there and pass it back, if the headset-side routes fail.
- **Drawing it** is cheap: the recorder turns the gaze direction (in the head's frame) into
  a point in the crop, through the inverse of the stabilising turn when Steady is on, and
  `convert.comp` draws a ring round it from a push constant.
- **Next:** run `tools/gaze_probe.py` on the headset while wearing it and looking around.
  It tries the OpenVR route with three likely paths, and lists which bytes of the shared
  memory change. Whichever route gives a moving gaze decides how to build it.

## Smaller items

- **Too-weak battery packs:** an alert when a pack can't keep up from the moment it's
  plugged in (the battery keeps falling while "plugged in"), not only when it runs flat.
