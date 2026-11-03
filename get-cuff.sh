#!/bin/sh
# Download Cuff and install it on this headset, in one line from Desktop Mode's Konsole:
#   curl -fsSL https://raw.githubusercontent.com/Brobert-in-aus/cuff/main/get-cuff.sh | sh
# It asks about the optional parts. To choose up front: ... | sh -s -- --all (or --only-cuff,
# --recorder, --helper, --player; see the top of install.sh).
# It installs the latest release. CUFF_ARCHIVE=<a .tar.gz of the repo> picks another copy (a
# fork, or https://github.com/Brobert-in-aus/cuff/archive/refs/heads/main.tar.gz for what's unreleased).
set -eu
REPO=https://github.com/Brobert-in-aus/cuff
if [ -z "${CUFF_ARCHIVE:-}" ]; then
    # GitHub sends /releases/latest on to /releases/tag/<newest tag>.
    TAG=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$REPO/releases/latest" | sed -n 's|.*/releases/tag/||p')
    [ -n "$TAG" ] || { echo "Couldn't find Cuff's latest release at $REPO/releases" >&2; exit 1; }
    CUFF_ARCHIVE=$REPO/archive/refs/tags/$TAG.tar.gz
fi
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
echo "Downloading Cuff from $CUFF_ARCHIVE"
curl -fsSL "$CUFF_ARCHIVE" | tar -C "$WORK" -xzf -
SRC=$(find "$WORK" -maxdepth 2 -name install.sh -path "*/install.sh" | head -1)
[ -n "$SRC" ] || { echo "No install.sh in that archive" >&2; exit 1; }
sh "$SRC" --here "$@"
