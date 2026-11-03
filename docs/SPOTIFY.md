# Spotify on the headset

Cuff's Now Playing shows and controls whatever media player is playing. To play Spotify on
the headset itself (picked from your phone like any Spotify Connect speaker), use Spotify's
own headless player, [Soloist](https://developer.spotify.com/documentation/soloist), through
[soloist-mpris](https://github.com/TobiasVeiga00/soloist-mpris). That gives it the media
controls Cuff reads, and Now Playing's tab then reads **Spotify** while the headset is the
device that's playing.

Players built on librespot (spotifyd, raspotify, go-librespot) connect, but Spotify refuses
them the key for each song on some accounts, so nothing plays.

1. **Get a Soloist API key:** on the Spotify for Developers dashboard
   (https://developer.spotify.com/dashboard), open the Spotify Soloist API Key page, accept
   the terms and generate a key. It needs a Premium account. The key is yours: keep it to
   yourself.
2. **Install soloist-mpris** on the headset, in Desktop Mode's Konsole. Its installer asks
   for the key (input hidden), downloads Soloist from Spotify, starts the user services and
   walks you through picking the headset in the Spotify app:
   ```sh
   curl -fsSL https://github.com/TobiasVeiga00/soloist-mpris/releases/latest/download/install.sh | SOLOIST_DEVICE_NAME="Steam Frame" bash
   ```
   Read the script first if you like; it installs under your home directory, and only asks
   for root to open the firewall while you pair.
3. **Pick the headset** in the Spotify app on your phone. Now Playing shows the song, and
   while nothing plays it says to pick the headset in the Spotify app.

- **Updates:** Soloist builds stop working 90 days after they're built. soloist-mpris has
  a weekly timer that fetches Spotify's current build.
- **Logs:** `journalctl --user -u soloist -u soloist-mpris`.
