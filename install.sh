#!/bin/sh
# Install, update or remove Cuff on the Steam Frame.
#   Nothing downloaded yet (Desktop Mode's Konsole):  curl -fsSL <get-cuff.sh's URL> | sh
#   On the headset, from an unpacked copy:            sh install.sh --here [options]
#   The copy Cuff keeps (for --status, --uninstall):  sh ~/.local/share/cuff/app/install.sh --here ...
#   From a PC over SSH (development):                 sh install.sh [ssh host] [options]
#     (copies this folder to ~/src/cuff on the headset and runs it there; default host: frame)
#
# Options (none: Cuff itself, then a question about each optional part, if there's a terminal):
#   --only-cuff   just Cuff itself (what the SSH form does with no options)
#   --recorder    also the recorder for the Record tab (builds it; installs Rust if needed)
#   --helper      also the panel helper, so recordings read the panels (asks for your password)
#   --player      also mpv, to watch recordings in the headset
#   --all         Cuff and every optional part
#   --status      say what's installed, change nothing
#   --uninstall   remove Cuff and its optional parts; settings and videos stay
#   --purge       with --uninstall, settings go too (~/.config/cuff)
# Any install also rebuilds an installed recorder whose source changed (when Rust is there).
#
# Logs:      journalctl --user -u cuff -f
# Debug run: systemctl --user stop cuff; PYTHONPATH=~/.local/share/cuff/app ~/.local/share/cuff/venv/bin/python -m cuff --debug
# Render:    PYTHONPATH=~/.local/share/cuff/app ~/.local/share/cuff/venv/bin/python -m cuff render /tmp/cuff.png [charm id|none]
set -eu
DIR=$(cd "$(dirname "$0")" && pwd)

# ---------------------------------------------------------------- from a PC

if [ "${1:-}" != "--here" ]; then
    HOST=frame
    case "${1:-}" in -*|"") ;; *) HOST=$1; shift ;; esac
    [ $# -gt 0 ] || set -- --only-cuff
    ssh "$HOST" 'mkdir -p ~/src/cuff'
    tar -C "$DIR" --exclude=__pycache__ --exclude=target --exclude=.venv --exclude=media -cf - . \
        | ssh "$HOST" 'tar -C ~/src/cuff -xf - 2>/dev/null'
    # -t for the questions and the password, when this has a terminal.
    if [ -t 0 ]; then T=-t; else T=; fi
    exec ssh $T "$HOST" "sh ~/src/cuff/install.sh --here $*"
fi
shift

# ---------------------------------------------------------------- on the headset

# Cuff for PC runs this over SSH: a dropped link or a Cancel hangs up on it, which mustn't stop
# it halfway through replacing Cuff.
trap '' HUP

APP=$HOME/.local/share/cuff
BIN=$APP/bin
HELPER_DIR=$HOME/.local/lib/cuff
HELPER=$HELPER_DIR/cuff-grab
UNITS=$HOME/.config/systemd/user
MPV=io.mpv.Mpv

CUFF=1 RECORDER= HELP= PLAYER= ASK=1 MODE=install PURGE=
for arg in "$@"; do
    case $arg in
        --only-cuff) ASK= ;;
        --recorder) RECORDER=1 ASK= ;;
        --helper) HELP=1 ASK= ;;
        --player) PLAYER=1 ASK= ;;
        --all) RECORDER=1 HELP=1 PLAYER=1 ASK= ;;
        --status) MODE=status ;;
        --uninstall) MODE=uninstall ;;
        --purge) PURGE=1 ;;
        *) echo "install.sh: unknown option $arg (see the top of install.sh)" >&2; exit 2 ;;
    esac
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
# A terminal to ask on: piped from curl, stdin is this script, so ask /dev/tty.
# (In a subshell: a failed redirection on a builtin ends a POSIX shell outright.)
tty_ok() { [ -r /dev/tty ] && (: </dev/tty) 2>/dev/null; }
yes_no() { # question [default y|n]: true for yes; the default when there's no terminal
    tty_ok || { [ "${2:-n}" = y ]; return; }
    printf '  %s [%s] ' "$1" "$( [ "${2:-n}" = y ] && echo Y/n || echo y/N )" >/dev/tty
    read -r reply </dev/tty || reply=
    case ${reply:-${2:-n}} in [Yy]*) return 0 ;; *) return 1 ;; esac
}

has_cargo() { command -v cargo >/dev/null 2>&1 || [ -x "$HOME/.cargo/bin/cargo" ]; }
helper_ok() { [ -f "$HELPER" ] && [ "$(stat -c %u "$HELPER")" = 0 ] && getcap "$HELPER" 2>/dev/null | grep -q cap_sys_admin; }
mpv_ok() { flatpak info "$MPV" >/dev/null 2>&1; }
framecorder_on() { systemctl --user is-enabled framecorder-ui >/dev/null 2>&1; }

status() {
    say "What's installed"
    if [ -d "$APP/app/cuff" ]; then
        note "Cuff: yes ($(systemctl --user is-active cuff 2>/dev/null || true))"
        note "Version: $(cat "$APP/app/VERSION" 2>/dev/null || echo unknown)"
    else
        note "Cuff: no"
    fi
    if [ -x "$BIN/cuff-recorder" ]; then note "Recorder: yes"; else note "Recorder: no (--recorder)"; fi
    if helper_ok; then
        note "Panel helper: yes"
    elif [ -f "$HOME/.local/lib/framecorder/framecorder-grab" ]; then
        note "Panel helper: Framecorder's (works; --helper installs Cuff's own)"
    else
        note "Panel helper: no (--helper; without it, recordings are SteamVR's 16:9 view)"
    fi
    if mpv_ok; then note "Player (mpv): yes"; else note "Player (mpv): no (--player)"; fi
    if framecorder_on; then note "Framecorder's own app: on (Cuff does its job; the installer offers to turn it off)"; fi
    if systemctl --user is-active librepods >/dev/null 2>&1; then note "LibrePods: yes, AirPods battery shows (docs/AIRPODS.md)"; fi
    if [ -x "$HOME/.local/bin/soloist" ]; then note "Soloist: yes, Spotify plays on the headset (docs/SPOTIFY.md)"; fi
}

# ---- Cuff itself: the package, the bundled charms, the requirements and the service.
install_cuff() {
    say "Cuff"
    mkdir -p "$APP/app" "$UNITS"
    # Unpacked beside the old copy first, then swapped in, so a failure leaves a whole Cuff.
    rm -rf "$APP/app.new"
    mkdir -p "$APP/app.new"
    tar -C "$DIR" --exclude=__pycache__ -cf - cuff charms requirements.txt | tar -C "$APP/app.new" -xf -
    for part in cuff charms; do
        rm -rf "$APP/app/$part.old"
        [ ! -e "$APP/app/$part" ] || mv "$APP/app/$part" "$APP/app/$part.old"
        mv "$APP/app.new/$part" "$APP/app/$part"
        rm -rf "$APP/app/$part.old"
    done
    mv "$APP/app.new/requirements.txt" "$APP/app/requirements.txt"
    rm -rf "$APP/app.new"
    # Which Cuff this is (Cuff for PC and releases carry a VERSION; --status shows it).
    if [ -f "$DIR/VERSION" ]; then cp "$DIR/VERSION" "$APP/app/VERSION"; else rm -f "$APP/app/VERSION"; fi
    # This script, kept with Cuff, for --status and --uninstall later (Cuff for PC runs it
    # there, as a get-cuff.sh install leaves no unpacked copy behind).
    [ "$DIR/install.sh" -ef "$APP/app/install.sh" ] || cp "$DIR/install.sh" "$APP/app/install.sh"  # (run from that copy: nothing to do)
    cp "$DIR/cuff.service" "$UNITS/"
    # The venv sees system site-packages for dbus-python (not on PyPI as a wheel). Then the
    # dictation model (~650 MB, once), so the first tap needn't download it, and the service.
    grep -qs 'include-system-site-packages = true' "$APP/venv/pyvenv.cfg" \
        || { rm -rf "$APP/venv"; python3 -m venv --system-site-packages "$APP/venv"; }
    "$APP/venv/bin/pip" install -q --disable-pip-version-check -r "$APP/app/requirements.txt"
    PYTHONPATH=$APP/app HF_HUB_DISABLE_PROGRESS_BARS=1 "$APP/venv/bin/python" -m cuff.stt_local --download
    systemctl --user daemon-reload
    systemctl --user enable --quiet cuff
    systemctl --user restart cuff
    note "installed, and running with SteamVR from now on"
}

# ---- The recorder (recorder/, built here: no one ships it for the headset's chip).
install_recorder() {
    say "Recorder"
    if ! has_cargo; then
        note "Building it needs Rust, which installs for you alone (no password), about 300 MB."
        if [ -z "$RECORDER" ] && ! yes_no "Install Rust?" y; then note "skipped"; return; fi
        curl --proto '=https' -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --no-modify-path
    fi
    note "Building (a few minutes; it runs at low priority)..."
    sh "$DIR/recorder/install.sh" --here
    recorder_source >"$APP/recorder.source"
    systemctl --user restart cuff
}

# A fingerprint of recorder/'s source, to tell whether the built recorder is this Cuff's.
recorder_source() {
    (cd "$DIR" && find recorder -type f -not -path 'recorder/target/*' | LC_ALL=C sort | xargs sha256sum)         | sha256sum | cut -d' ' -f1
}

# An update that didn't ask for the recorder still rebuilds an installed one whose source
# changed, so recorder fixes reach it and the charm and recorder keep speaking the same options.
refresh_recorder() {
    [ -x "$BIN/cuff-recorder" ] && [ -d "$DIR/recorder" ] || return 0
    [ "$(cat "$APP/recorder.source" 2>/dev/null)" != "$(recorder_source)" ] || return 0
    has_cargo || { say "Recorder"; note "out of date, but Rust isn't here to rebuild it (--recorder)"; return 0; }
    RECORDER=1
    install_recorder
    if helper_ok && ! cmp -s "$BIN/cuff-grab" "$HELPER"; then
        note "The panel helper changed too; it needs your password: sh install.sh --here --helper"
    fi
}

# ---- The panel helper: cuff-grab, owned by root with cap_sys_admin. Reading another
# program's framebuffers needs that permission; only this small helper holds it.
install_helper() {
    say "Panel helper"
    if helper_ok && cmp -s "$BIN/cuff-grab" "$HELPER"; then note "already in place"; return; fi
    [ -x "$BIN/cuff-grab" ] || { note "needs the recorder first (--recorder)"; return; }
    note "It lets recordings read the headset's panels: any shape, both eyes, no cost to the game."
    note "Giving it that permission needs your password, once."
    if ! tty_ok; then note "Run this again in a terminal: sh install.sh --here --helper"; return; fi
    if passwd -S "$USER" 2>/dev/null | grep -q " NP "; then
        note "SteamOS doesn't have a password for you yet. Pick one now (you'll need it for sudo):"
        passwd </dev/tty
    fi
    if sudo -v </dev/tty \
        && sudo install -D -o root -g root -m 755 "$BIN/cuff-grab" "$HELPER" \
        && sudo setcap cap_sys_admin+ep "$HELPER"; then
        note "in place: $HELPER"
        systemctl --user restart cuff
    else
        note "not installed. Recordings fall back to SteamVR's view (16:9, left eye)."
    fi
}

# ---- mpv, to watch recordings (the Record tab's Watch page starts it through Steam).
install_player() {
    say "Player"
    if mpv_ok; then note "mpv is already installed"; return; fi
    flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
    flatpak install --user -y --noninteractive flathub "$MPV"
    note "installed. The first Play adds a \"Cuff Player\" shortcut to Steam."
}

# ---- Framecorder's own app does the Record tab's job, with a recorder of its own.
offer_framecorder_off() {
    framecorder_on || return 0
    say "Framecorder"
    note "Framecorder's own app is on. Its recorder competes with Cuff's for the encoder."
    if yes_no "Turn Framecorder's app off (its files and your videos stay)?" y; then
        systemctl --user disable --now framecorder-ui framecorder-sync framecorder-update.timer 2>/dev/null || true
        note "off"
    fi
}

uninstall() {
    say "Removing Cuff"
    systemctl --user disable --now cuff 2>/dev/null || true
    rm -rf "$APP" "$HOME/.cache/cuff" "$UNITS/cuff.service" "$UNITS/cuff.service.d" "$HOME/src/cuff-recorder"
    systemctl --user daemon-reload
    note "Cuff, its recorder and its cache are gone. Videos stay in ~/Videos/framecorder."
    if [ -e "$HELPER" ]; then
        if tty_ok && yes_no "Remove the panel helper too (needs your password)?" y; then
            sudo rm -rf "$HELPER_DIR" </dev/tty && note "panel helper removed"
        else
            note "the panel helper stays: sudo rm -rf $HELPER_DIR"
        fi
    fi
    if mpv_ok && yes_no "Remove mpv too?" n; then flatpak uninstall --user -y "$MPV"; fi
    if [ "$PURGE" ]; then rm -rf "$HOME/.config/cuff"; note "settings removed"; else note "Settings stay in ~/.config/cuff (--purge removes them)."; fi
    note "Steam keeps the \"Cuff Player\" shortcut, if one was added; remove it from the library."
}

case $MODE in
    status) status; exit 0 ;;
    uninstall) uninstall; exit 0 ;;
esac

install_cuff
if [ "$ASK" ]; then
    say "Optional parts"
    note "The Record tab needs the recorder; the panel helper makes recordings sharper; mpv plays them."
    yes_no "Install the recorder?" y && RECORDER=1
    yes_no "Install the panel helper (needs your password)?" y && HELP=1
    yes_no "Install mpv to watch recordings?" y && PLAYER=1
fi
if [ "$RECORDER" ]; then install_recorder; else refresh_recorder; fi
[ -z "$HELP" ] || install_helper
[ -z "$PLAYER" ] || install_player
offer_framecorder_off
status
if [ -z "${CUFF_FROM_PC_APP:-}" ]; then  # Cuff for PC, which ran this, pairs and says what's next itself
    echo
    echo "Next, on your PC: Cuff for PC (CuffForPC-Setup from the GitHub releases), then pair."
    echo "Optional extras: AirPods battery (docs/AIRPODS.md), Spotify on the headset (docs/SPOTIFY.md)."
fi
