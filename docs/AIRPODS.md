# AirPods battery with LibrePods

AirPods don't give Linux a battery level the usual way, so Cuff gets it from
[LibrePods](https://github.com/librepods-org/librepods), which talks to them over Apple's own
protocol. Cuff reads its log, so LibrePods needs to run as a systemd **user service named
`librepods`**. Cuff then shows the lower bud's level in the status bar (charging while
either bud is).

On the headset, in Desktop Mode's Konsole:

1. **Pair the AirPods** with the headset as usual, in Bluetooth settings. To do it in a
   terminal instead: open the case, hold its back button until the light flashes white,
   then in `bluetoothctl` run `scan on`, `pair <MAC>`, `trust <MAC>` and `connect <MAC>`.
2. **Install Rust** if you haven't, with no sudo:
   `curl --proto '=https' -sSf https://sh.rustup.rs | sh`, then open a new Konsole.
3. **Build LibrePods' Linux app.** Use the Rust rewrite on the `linux/rust` branch. The
   Qt app on `main` needs Qt libraries SteamOS doesn't have. No one ships it for the
   headset's ARM chip, so it builds on the headset, which already has the libraries it
   needs:
   ```sh
   git clone -b linux/rust https://github.com/librepods-org/librepods.git ~/src/librepods
   cd ~/src/librepods/linux-rust
   CARGO_PROFILE_RELEASE_LTO=thin nice -n 19 cargo build --release -j 4
   mkdir -p ~/.local/share/librepods
   cp target/release/librepods ~/.local/share/librepods/
   ```
   It takes a few minutes. Thin LTO and 4 jobs keep the build within the headset's free
   memory while SteamVR runs.
4. **Run it as a user service.** Save this as `~/.config/systemd/user/librepods.service`:
   ```ini
   [Unit]
   Description=LibrePods (AirPods controls, headless)
   After=bluetooth.target pipewire-pulse.service

   [Service]
   ExecStart=%h/.local/share/librepods/librepods --no-tray
   Restart=always
   RestartSec=10

   [Install]
   WantedBy=default.target
   ```
   Then: `systemctl --user daemon-reload && systemctl --user enable --now librepods`.
   If you paired the AirPods for the first time just now, run
   `systemctl --user restart librepods` once they're connected: on a first connection
   BlueZ hasn't yet worked out what the AirPods offer, so LibrePods skips them.
   Reconnections after that are picked up.
5. **Check it:** with the AirPods connected,
   `journalctl --user -u librepods | grep "Battery Info"` should show lines such as
   `component: Left, level: 97`. Cuff picks it up within a few seconds; no restart needed.

- **Other names:** if your service has another name, set it as `librepods_unit` in Cuff's
  profile (`cuff/config.py`).
- **Its log:** Cuff reads LibrePods' "Received Battery Info" log lines. If a LibrePods
  update rewords them, the headphone battery disappears from the status bar until Cuff
  catches up.
- **Other earbuds:** ones that report their battery to Linux themselves (most do, over
  Bluetooth's hands-free profile) need none of this.
