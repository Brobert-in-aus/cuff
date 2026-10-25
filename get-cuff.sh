#!/bin/sh
# Download Cuff and install it on this headset, in one line from Desktop Mode's Konsole:
#   curl -fsSL https://raw.githubusercontent.com/Brobert-in-aus/cuff/main/get-cuff.sh | sh
# CUFF_ARCHIVE=<a .tar.gz of the repo> picks another copy (a release, a fork, a branch).
set -eu
ARCHIVE=${CUFF_ARCHIVE:-https://github.com/Brobert-in-aus/cuff/archive/refs/heads/main.tar.gz}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
echo "Downloading Cuff from $ARCHIVE"
curl -fsSL "$ARCHIVE" | tar -C "$WORK" -xzf -
SRC=$(find "$WORK" -maxdepth 2 -name install.sh -path "*/install.sh" | head -1)
[ -n "$SRC" ] || { echo "No install.sh in that archive" >&2; exit 1; }
sh "$SRC" --here
