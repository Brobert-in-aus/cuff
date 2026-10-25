#!/bin/sh
# Publish Cuff: this repo's current commit, without the workspace's own files, as the public
# repository's content.
#
# First time, a fresh repository with a single commit:
#   sh tools/publish.sh <out dir> <github owner> ["Author Name <email>"]
#   Then create <owner>/cuff on GitHub (empty) and push <out dir>, for example:
#   cd <out dir> && gh repo create <owner>/cuff --public --source . --remote origin --push
#
# After that, each update is one new commit on top of the public main:
#   sh tools/publish.sh --update <public clone> [-m "message"] [--push]
#   It pulls the clone, replaces its files with this commit's public copy (so files removed
#   here go there too), and commits as this repo's git identity, with the message
#   "Cuff (<this commit>)" unless -m gives one. Nothing changed, nothing committed. It pushes
#   only with --push.
#
# Why not this repo's history: it holds code from before Cuff's clean-room rewrite (from a
# GPL-3.0 project) and the developer's own network details. The public history starts at the
# first publish, and update messages never copy this repo's commit messages.
set -eu
usage() {
    echo 'usage: publish.sh <out dir> <github owner> ["Author <email>"]' >&2
    echo '       publish.sh --update <public clone> [-m "message"] [--push]' >&2
    exit 1
}

ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "commit or stash your changes first" >&2; exit 1; }
VERSION=$(git describe --tags --always)

# build <dir> <owner>: the public copy of HEAD into an empty <dir>, checked.
build() {
    git archive HEAD | (cd "$1" && tar -xf -)  # (a "C:" path given to tar -C reads as a remote host)
    # The private workspace's files (the project bootstrapper's, local media, and the
    # developer's validation log).
    rm -rf "$1/AGENTS.md" "$1/docs/PROJECT-INFRASTRUCTURE.md" "$1/docs/handoffs" "$1/media" \
        "$1/docs/VALIDATION.md"
    # The public repo's own address.
    for f in get-cuff.sh README.md; do
        sed -i "s|github.com/OWNER/cuff|github.com/$2/cuff|g; s|githubusercontent.com/OWNER/cuff|githubusercontent.com/$2/cuff|g" "$1/$f"
    done
    if grep -rn "OWNER/cuff" "$1/get-cuff.sh" "$1/README.md" "$1/install.sh" "$1/docs"; then
        echo "an OWNER placeholder is left" >&2
        exit 1
    fi
    sh "$ROOT/tools/check-private.sh" "$1"
}

if [ "${1:-}" = "--update" ]; then
    CLONE=${2:-}
    [ -n "$CLONE" ] || usage
    shift 2
    MESSAGE="Cuff ($VERSION)" PUSH=
    while [ $# -gt 0 ]; do
        case $1 in
            -m) [ $# -ge 2 ] || usage; MESSAGE=$2; shift 2 ;;
            --push) PUSH=1; shift ;;
            *) usage ;;
        esac
    done
    [ -d "$CLONE/.git" ] || { echo "$CLONE is not a git clone of the public repo" >&2; exit 1; }
    CLONE=$(cd "$CLONE" && pwd)
    URL=$(git -C "$CLONE" remote get-url origin)
    OWNER=$(echo "$URL" | sed -n 's|.*github\.com[:/]\([^/]*\)/cuff\(\.git\)\{0,1\}$|\1|p')
    [ -n "$OWNER" ] || { echo "origin isn't a github.com/<owner>/cuff repo: $URL" >&2; exit 1; }
    [ -z "$(git -C "$CLONE" status --porcelain)" ] || { echo "$CLONE has uncommitted changes" >&2; exit 1; }
    git -C "$CLONE" checkout -q main
    git -C "$CLONE" pull -q --ff-only origin main

    WORK=$(mktemp -d)
    trap 'rm -rf "$WORK"' EXIT
    build "$WORK" "$OWNER"
    # Replace the clone's files with the new copy, so deletions carry over too.
    git -C "$CLONE" ls-files -z | (cd "$CLONE" && xargs -0 rm -f --)
    (cd "$WORK" && tar -cf - .) | (cd "$CLONE" && tar -xf -)
    git -C "$CLONE" add -A
    if git -C "$CLONE" diff --cached --quiet; then
        echo "The public repo already matches $VERSION; nothing to commit."
        exit 0
    fi
    git -C "$CLONE" diff --cached --stat | tail -1
    git -C "$CLONE" -c "user.name=$(git config user.name)" -c "user.email=$(git config user.email)" \
        commit -q -m "$MESSAGE"
    echo "Committed \"$MESSAGE\" in $CLONE."
    if [ -n "$PUSH" ]; then
        git -C "$CLONE" push -q origin main
        echo "Pushed to $URL."
    else
        echo "Next: git -C \"$CLONE\" push origin main"
    fi
    exit 0
fi

OUT=${1:-}
OWNER=${2:-}
[ -n "$OUT" ] && [ -n "$OWNER" ] || usage
AUTHOR=${3:-}
[ ! -e "$OUT" ] || { echo "$OUT already exists (for a later update, use --update)" >&2; exit 1; }
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
build "$OUT" "$OWNER"

cd "$OUT"
git init -q -b main
git add -A
if [ -n "$AUTHOR" ]; then
    git -c "user.name=${AUTHOR% <*}" -c "user.email=$(echo "$AUTHOR" | sed 's/.*<\(.*\)>.*/\1/')" commit -q -m "Cuff ($VERSION)"
else
    git commit -q -m "Cuff ($VERSION)"
fi
echo "Public copy ready in $OUT ($(git rev-list --count HEAD) commit, $(git ls-files | wc -l) files)."
echo "Next: create $OWNER/cuff on GitHub and push it:"
echo "  cd \"$OUT\" && gh repo create $OWNER/cuff --public --source . --remote origin --push"
