#!/bin/sh
# Install or update Cuff on a SteamVR Linux headset (the Steam Frame).
#   On the headset (Desktop Mode's Konsole), from an unpacked copy:   sh install.sh --here
#   Or with nothing downloaded yet:   curl -fsSL <get-cuff.sh's URL> | sh   (see get-cuff.sh)
#   From a PC over SSH (development):  sh install.sh [ssh host]         (default host: frame)
# Logs:      journalctl --user -u cuff -f
# Debug run: systemctl --user stop cuff; PYTHONPATH=~/.local/share/cuff/app ~/.local/share/cuff/venv/bin/python -m cuff --debug
# Render:    PYTHONPATH=~/.local/share/cuff/app ~/.local/share/cuff/venv/bin/python -m cuff render /tmp/cuff.png [charm id|none]
# Uninstall (keeps ~/.config/cuff):
#   systemctl --user disable --now cuff; rm -rf ~/.local/share/cuff ~/.cache/cuff ~/.config/systemd/user/cuff.service ~/.config/systemd/user/cuff.service.d
set -eu
DIR=$(cd "$(dirname "$0")" && pwd)
APP=.local/share/cuff
if [ "${1:-}" = "--here" ]; then
    HERE=1
else
    HERE=
    HOST=${1:-frame}
fi

# Run a shell snippet on the headset: here, or over SSH.
on_headset() {
    if [ "$HERE" ]; then (cd ~ && sh -c "$1"); else ssh "$HOST" "$1"; fi
}

# Cuff's own files: the package, the bundled charms, the requirements and the service.
on_headset "mkdir -p $APP/app ~/.config/systemd/user && rm -rf $APP/app/cuff $APP/app/charms"
if [ "$HERE" ]; then
    tar -C "$DIR" --exclude=__pycache__ -cf - cuff charms requirements.txt | tar -C ~/$APP/app -xf -
    cp "$DIR/cuff.service" ~/.config/systemd/user/
else
    tar -C "$DIR" --exclude=__pycache__ -cf - cuff charms requirements.txt | ssh "$HOST" "tar -C $APP/app -xf -"
    scp -q "$DIR/cuff.service" "$HOST:.config/systemd/user/"
fi

# The venv sees system site-packages for dbus-python (not on PyPI as a wheel). Then the
# dictation model (~650 MB, once), so the first tap needn't download it, and the service.
on_headset "grep -qs 'include-system-site-packages = true' $APP/venv/pyvenv.cfg || { rm -rf $APP/venv; python3 -m venv --system-site-packages $APP/venv; }
$APP/venv/bin/pip install -q --disable-pip-version-check -r $APP/app/requirements.txt
PYTHONPATH=$APP/app HF_HUB_DISABLE_PROGRESS_BARS=1 $APP/venv/bin/python -m cuff.stt_local --download
systemctl --user daemon-reload && systemctl --user enable cuff && systemctl --user restart cuff
sleep 3; systemctl --user --no-pager status cuff | head -8 || true"
echo "Cuff is installed. Next, on your PC: Cuff for PC (pc-server/install.ps1), then pair."
