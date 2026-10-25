#!/bin/sh
# Build the public copy of Cuff: this repo's current commit, without the workspace's own
# files, as a fresh repository with a single commit. It never pushes.
#   sh tools/publish.sh <out dir> <github owner> ["Author Name <email>"]
# Why fresh: this repo's history holds code from before Cuff's clean-room rewrite (from a
# GPL-3.0 project) and the developer's own network details. The public history starts here.
# Then: create <owner>/cuff on GitHub (empty), and in <out dir>:
#   git remote add origin git@github.com:<owner>/cuff.git && git push -u origin main
set -eu
OUT=${1:?usage: publish.sh <out dir> <github owner> ["Author <email>"]}
OWNER=${2:?usage: publish.sh <out dir> <github owner> ["Author <email>"]}
AUTHOR=${3:-}
ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "commit or stash your changes first" >&2; exit 1; }
[ ! -e "$OUT" ] || { echo "$OUT already exists" >&2; exit 1; }

mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
git archive HEAD | (cd "$OUT" && tar -xf -)  # (a "C:" path given to tar -C reads as a remote host)
# The private workspace's files (the project bootstrapper's, local media, and the
# developer's validation log).
rm -rf "$OUT/AGENTS.md" "$OUT/docs/PROJECT-INFRASTRUCTURE.md" "$OUT/docs/handoffs" "$OUT/media" \
    "$OUT/docs/VALIDATION.md"
# The public repo's own address.
for f in get-cuff.sh README.md; do
    sed -i "s|github.com/OWNER/cuff|github.com/$OWNER/cuff|g; s|githubusercontent.com/OWNER/cuff|githubusercontent.com/$OWNER/cuff|g" "$OUT/$f"
done
if grep -rn "OWNER/cuff" "$OUT/get-cuff.sh" "$OUT/README.md" "$OUT/install.sh" "$OUT/docs"; then
    echo "an OWNER placeholder is left" >&2
    exit 1
fi
sh "$ROOT/tools/check-private.sh" "$OUT"

VERSION=$(git describe --tags --always)
cd "$OUT"
git init -q -b main
git add -A
if [ -n "$AUTHOR" ]; then
    git -c "user.name=${AUTHOR% <*}" -c "user.email=$(echo "$AUTHOR" | sed 's/.*<\(.*\)>.*/\1/')" commit -q -m "Cuff ($VERSION)"
else
    git commit -q -m "Cuff ($VERSION)"
fi
echo "Public copy ready in $OUT ($(git rev-list --count HEAD) commit, $(git ls-files | wc -l) files)."
echo "Next: create $OWNER/cuff on GitHub, then: git -C \"$OUT\" remote add origin git@github.com:$OWNER/cuff.git && git -C \"$OUT\" push -u origin main"
