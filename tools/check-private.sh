#!/bin/sh
# Fail if a tree holds anything that must not be published: the workspace's own files,
# audio clips, screenshots, or names and addresses from the developer's setup.
#   sh tools/check-private.sh [dir]        (default: this repo's tracked files)
# tools/publish.sh runs it on the public copy; CI runs it on every push there.
set -u
DIR=${1:-}
fail=0

if [ -n "$DIR" ]; then
    FILES=$(cd "$DIR" && find . -type f -not -path "./.git/*" | sed 's|^\./||')
else
    DIR=$(git rev-parse --show-toplevel)
    FILES=$(cd "$DIR" && git ls-files)
fi

# Files and folders that belong to the private workspace only.
for f in $(printf '%s\n' "$FILES" | grep -E '^(AGENTS\.md|docs/PROJECT-INFRASTRUCTURE\.md|docs/VALIDATION\.md|docs/handoffs/|media/|tools/private-words$)|\.(mp3|wav|ogg|flac)$'); do
    echo "private file: $f"
    fail=1
done

# Words that only belong to a developer's own setup (case-insensitive): home network
# addresses and Steam IDs here, and the developer's own names and private projects in
# tools/private-words beside this script (one extended regex a line). That file stays in the
# private workspace, so the public copy doesn't list what it keeps out.
PATTERNS='192\.168\.|7656119[0-9]{10}'
WORDS=$(dirname "$0")/private-words
[ -f "$WORDS" ] || WORDS=/dev/null
for f in $(printf '%s\n' "$FILES" | grep -v -E '^tools/(check-private\.sh|private-words)$|\.(png|jpg|ico)$|(^|/)uv\.lock$'); do
    hits=$(grep -n -i -E -e "$PATTERNS" -f "$WORDS" "$DIR/$f" 2>/dev/null | head -3)
    if [ -n "$hits" ]; then
        printf '%s\n' "$hits" | sed "s|^|$f:|"
        fail=1
    fi
done

if [ "$fail" = 0 ]; then
    echo "check-private: clean"
fi
exit $fail
