# Recording

Cuff's **Record** tab records what the headset shows: a clip of the last few seconds at any
time, or a full recording, started and stopped from the tab. The record button on the quick
row saves a clip by default; the Record page's **Quick action** setting makes it start and
stop a recording instead, or both (save the clip, then start recording). While a recording
runs, the button stops it. It works with or without a game running. The Watch page plays your clips and
recordings in the headset, and Now Playing controls the player.

- **Where videos go:** `~/Videos/framecorder/`, with clips in `~/Videos/framecorder/clips/`.
- **Settings:** the Video page (shape, eye, Steady, quality, frame rate) and the Audio
  page (game audio, mic, clips and their length).
- **Steady** smooths out small head movements in the 16:9, 1:1 and 9:16 views. Light
  records a little less of the view (77° instead of 84° across, 16:9) and Strong a little
  less again (71°), to leave room to move the picture against your head. It's off by
  default.

## Credits

The recorder in `recorder/` is from [Framecorder](https://github.com/coah80/framecorder) by
coah80, under the MIT licence kept in `recorder/LICENSE`. It reads what the headset's panels
show through a small helper, undoes the lens distortion on the GPU, encodes in hardware, and
keeps the clip buffer in memory. `recorder/README.md` lists what Cuff changed.

## Setting it up

Cuff's installer does it: say yes to the recorder, the panel helper and mpv when it asks, or
run it with `--all` (see the README). From an unpacked copy on the headset:
`sh install.sh --here --all`.

- **The recorder** is built on the headset (a few minutes). The installer installs Rust
  first if needed (no password; about 300 MB), and puts `cuff-recorder` and `cuff-grab` in
  `~/.local/share/cuff/bin/`. Each update rebuilds it if its source changed. If the panel
  helper changed too, the installer says so: run it again with `--helper` (it asks for your
  password).
- **The panel helper** is `cuff-grab`, installed to `~/.local/lib/cuff/` owned by root
  with `cap_sys_admin`. Reading another program's framebuffers needs that permission, so
  the installer asks for your password once. If SteamOS has no password for you yet, it
  has you pick one first. Without the helper, recordings come from SteamVR's view (see
  Notes). A helper Framecorder's installer put in place
  (`~/.local/lib/framecorder/framecorder-grab`) works too.
- **mpv** comes from Flathub, installed for you alone. The first time you press Play, Cuff
  adds a "Cuff Player" shortcut to Steam: a window only shows in VR when Steam launches it.
- **Framecorder:** if its own app is installed and on, the installer offers to turn it
  off. Its recorder would compete with Cuff's for the encoder.

The Record tab appears once `cuff-recorder` is installed. Its log is
`~/.local/state/cuff/record.log`, and its settings are in `~/.config/cuff/record/`.

## Notes

- **Cuff's panel is in the video** when you look at it while recording, as everything on
  the panels is.
- **Without the panel helper,** the recorder records SteamVR's view
  instead: 16:9, left eye only, and it costs the game a little GPU time.
- **No clip binding:** holding the left stick to clip was Framecorder's tab's. Clip from the
  Record tab instead.
- **Framecorder's tab** stays listed under SteamVR's "launch program" until it's
  unregistered (`IVRApplications::RemoveApplicationManifest` on
  `~/.local/share/framecorder/framecorder.vrmanifest`).
