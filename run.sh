#!/bin/bash
# labdash runner - refresh the dashboard, then optionally publish it.
#
#   ./run.sh                       # refresh into $LABDASH_OUT
#   LABDASH_PUBLISH=git ./run.sh   # ...and push to the Pages repo
#
# Safe to run from scrontab: it never fails the job on a publish error, so a
# GitHub hiccup can't stop the next collection.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Never pin a version: python3.11 vanished from the cluster once already and every
# hardcoded reference exited 127 for days without anyone noticing.
PY=$("$HERE/py.sh" --print)

# Default output: the running user's lab folder on holylabs. Override with
# LABDASH_OUT when that allocation is full (ours is -- labdash.scron points at a
# sibling account's quota) or when the lab keeps its tree somewhere else.
: "${LABDASH_OUT:=/n/holylabs/LABS/$(id -gn)/Lab/labdash}"
: "${LABDASH_PUBLISH:=none}"
: "${LABDASH_REPO:=$HOME/projects/labdash-site}"

mkdir -p "$LABDASH_OUT" || exit 1
"$PY" "$HERE/labdash.py" --out "$LABDASH_OUT" || exit 1
chmod -R a+rX "$LABDASH_OUT" 2>/dev/null

case "$LABDASH_PUBLISH" in
  git)
    if [ ! -d "$LABDASH_REPO/.git" ]; then
      echo "labdash: $LABDASH_REPO is not a git repo - see README, skipping publish"
      exit 0
    fi
    cp "$LABDASH_OUT/index.html" "$LABDASH_OUT/snapshot.json" "$LABDASH_REPO/" || exit 0
    touch "$LABDASH_REPO/.nojekyll"   # ship files verbatim, skip the Jekyll build
    git -C "$LABDASH_REPO" add -A
    if git -C "$LABDASH_REPO" diff --cached --quiet; then
      echo "labdash: no change to publish"   # normal between quiet runs, not an error
      exit 0
    fi

    # Rewrite the single commit instead of adding one. At a 15-minute cadence a
    # linear history would be ~35k commits and tens of GB a year, all of it dead
    # weight - nobody wants to diff last Tuesday's queue. The repo stays ~1.5 MB
    # forever, and the trend data lives in history.jsonl on the cluster anyway.
    MSG="labdash snapshot $(date -u +%Y-%m-%dT%H:%MZ)"
    BRANCH=$(git -C "$LABDASH_REPO" symbolic-ref --short HEAD 2>/dev/null || echo main)
    if git -C "$LABDASH_REPO" rev-parse HEAD >/dev/null 2>&1; then
      git -C "$LABDASH_REPO" commit -q --amend -m "$MSG"
    else
      git -C "$LABDASH_REPO" commit -q -m "$MSG"
    fi
    if git -C "$LABDASH_REPO" push -q --force origin "$BRANCH"; then
      echo "labdash: published to $BRANCH"
    else
      echo "labdash: push failed, snapshot still written to $LABDASH_OUT"
    fi
    ;;
  none) ;;
  *) echo "labdash: unknown LABDASH_PUBLISH=$LABDASH_PUBLISH" ;;
esac
