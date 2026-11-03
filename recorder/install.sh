#!/bin/sh
# Build Cuff's recorder on the headset and install it to ~/.local/share/cuff/bin/: cuff-recorder,
# and cuff-grab (the panel helper's program; Cuff's install.sh --helper gives it its permission).
#   On the headset, from Cuff's folder:   sh recorder/install.sh --here
#   From a PC over SSH (development):     sh recorder/install.sh [ssh host]   (default: frame)
# Needs Rust (rustup) on the headset. SteamOS already has the headers (FFmpeg, PipeWire, Vulkan,
# glslc). Thin LTO and 4 jobs keep the build within the headset's free memory while SteamVR runs.
set -eu
DIR=$(cd "$(dirname "$0")" && pwd)
SRC=src/cuff-recorder
BUILD='. ~/.cargo/env && cd ~/'"$SRC"' && CARGO_PROFILE_RELEASE_LTO=thin nice -n 19 cargo build --release -j 4 --bins && mkdir -p ~/.local/share/cuff/bin && install -m 755 target/release/cuff-recorder target/release/cuff-grab ~/.local/share/cuff/bin/ && echo "installed cuff-recorder and cuff-grab in ~/.local/share/cuff/bin"'

if [ "${1:-}" = "--here" ]; then
    mkdir -p ~/"$SRC"
    tar -C "$DIR" --exclude=target -cf - . | tar -C ~/"$SRC" -xf -
    sh -c "$BUILD"
else
    HOST=${1:-frame}
    ssh "$HOST" "mkdir -p ~/$SRC"
    tar -C "$DIR" --exclude=target -cf - . | ssh "$HOST" "tar -C ~/$SRC -xf - 2>/dev/null"
    ssh "$HOST" "$BUILD"
fi
