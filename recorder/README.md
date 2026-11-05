# Cuff's recorder

The program behind Cuff's **Record** tab (`charms/record`, `docs/RECORDING.md`): it records
what the Steam Frame's panels actually show, keeps a clip buffer, and takes commands from
Cuff on stdin.

## Credits

This is the recorder from [Framecorder](https://github.com/coah80/framecorder) by coah80,
under the MIT licence in [LICENSE](LICENSE), which applies to this folder. The panel
capture, lens inversion, shader, encoder and muxer are Framecorder's work; see its
`docs/how-it-works.md` for how they fit together.

Cuff's changes:

- **Just the recorder:** Framecorder's dashboard tab, phone sync, apps and installer are
  left out, and Cuff runs the recorder itself. The panel helper (`cuff-grab`) is
  Framecorder's `framecorder-grab`, renamed.
- **FFmpeg 8:** built against `ffmpeg-sys-next` 8, which SteamOS 0.6 needs.
- **Stabilisation** of the cropped views (see `src/stabilise.rs`).

## Building

On the headset, with Rust (`rustup`) and SteamOS's own headers, which it already has:

```sh
sh recorder/install.sh --here
```

That builds `cuff-recorder` and `cuff-grab` and installs them to `~/.local/share/cuff/bin/`.
Cuff's installer does this for you (`install.sh --recorder`). The recorder reads the panels
through the helper at `~/.local/lib/cuff/cuff-grab`, which `install.sh --helper` puts in
place, owned by root with `cap_sys_admin`. Framecorder's own
`~/.local/lib/framecorder/framecorder-grab` works too.
