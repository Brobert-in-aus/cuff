"""Plays clips and recordings in the headset, for the Record charm's Watch page.

The player is mpv from Flathub (io.mpv.Mpv): it plays the recorder's HEVC with hardware
decoding, and its MPRIS plugin lets Now Playing control it (play/pause, next and previous
video, volume). SteamOS has no video player of its own.

A window only shows in VR when Steam starts it (gamescope shows the app Steam launched, not
any window on its display), so mpv runs from a non-Steam shortcut, "Cuff Player", which this
module adds to Steam once (SteamClient.Apps.AddShortcut, through cuff.steam's connection to
Steam's client) and remembers in player.json. Its launch options play a playlist file
(playlist.m3u) that's rewritten before each launch.
"""
import json
import os
import subprocess
import sys
import time

from cuff import steam
from cuff.atomic import atomic_write_json

RESTART_S = 1.0  # between stopping an open player and starting the new playlist
NAME = "Cuff Player"
MPV = "io.mpv.Mpv"
FLATPAK = "/usr/bin/flatpak"


def mpv_installed():
    try:
        return subprocess.run([FLATPAK, "info", MPV], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class Player:
    def __init__(self, config_dir, cache_dir):
        # The shortcut's id outlives an uninstall (or Steam would get a second shortcut);
        # the playlist is rewritten before every Play.
        self.state_file = os.path.join(config_dir, "player.json")
        self.playlist = os.path.join(cache_dir, "playlist.m3u")

    def _options(self):
        # --keep-open: at the end it stays, paused on the last frame, until Close player (or
        # another Play); quitting when the playlist ran out looked like a crash.
        return f"run {MPV} --fullscreen --force-window=yes --keep-open=yes --playlist={self.playlist}"

    def _appid(self):
        try:
            with open(self.state_file) as f:
                return int(json.load(f)["appid"])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"record: {self.state_file}: {e!r}; adding the player shortcut again", file=sys.stderr, flush=True)
            return None

    def _shortcut(self):
        """The Cuff Player shortcut's app id, added to Steam the first time."""
        appid = self._appid()
        if appid is None:
            appid = int(steam.steam_eval(
                f"SteamClient.Apps.AddShortcut({json.dumps(NAME)}, {json.dumps(FLATPAK)}, "
                f"{json.dumps(os.path.expanduser('~'))}, {json.dumps(self._options())})"))
            atomic_write_json(self.state_file, {"appid": appid})
        # Name and options every time: they're cheap, and put right anything changed by hand.
        steam.steam_eval(f"SteamClient.Apps.SetShortcutName({appid}, {json.dumps(NAME)})")
        steam.steam_eval(f"SteamClient.Apps.SetShortcutLaunchOptions({appid}, {json.dumps(self._options())})")
        return appid

    @staticmethod
    def _game_id(appid):
        return str((appid << 32) | 0x02000000)  # a non-Steam shortcut's game id

    def play(self, paths):
        """Plays paths in order. Returns an error message, or None."""
        if not mpv_installed():
            return "Install mpv first: flatpak install flathub io.mpv.Mpv"
        try:
            os.makedirs(os.path.dirname(self.playlist), exist_ok=True)
            with open(self.playlist, "w") as f:
                f.write("#EXTM3U\n" + "".join(p + "\n" for p in paths))
            game = self._game_id(self._shortcut())
            # One player at a time: stop one that's open, so it starts on the new playlist.
            steam.steam_eval(f"SteamClient.Apps.TerminateApp({json.dumps(game)}, false)")
            time.sleep(RESTART_S)  # (the charm worker's thread, not the overlay loop)
            steam.steam_eval(f"SteamClient.Apps.RunGame({json.dumps(game)}, '', -1, 100)")
        except (OSError, ValueError, StopIteration, EOFError, ConnectionError, TypeError) as e:
            return f"Couldn't start the player ({e})"
        return None

    def close(self):
        appid = self._appid()
        if appid is not None:
            try:
                steam.steam_eval(f"SteamClient.Apps.TerminateApp({json.dumps(self._game_id(appid))}, false)")
            except (OSError, ValueError, StopIteration, EOFError, ConnectionError):
                pass
