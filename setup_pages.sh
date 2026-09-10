#!/bin/bash
# One-time wiring of the GitHub Pages remote, then verify the site is actually live.
#
#   ./setup_pages.sh Embodied-Minds-Lab/labdash-site
#
# Run it after creating the (empty) repo on github.com. It adds the remote, force
# pushes the current snapshot, then polls the Pages URL until it answers 200 so you
# learn the site works here rather than by opening it and guessing.
set -uo pipefail

SLUG="${1:-}"
if [[ ! "$SLUG" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "usage: $0 <owner>/<repo>    e.g. $0 Embodied-Minds-Lab/labdash-site" >&2
  exit 2
fi
OWNER="${SLUG%%/*}"
NAME="${SLUG##*/}"
REPO="${LABDASH_REPO:-$HOME/projects/labdash-site}"
URL="https://$(echo "$OWNER" | tr '[:upper:]' '[:lower:]').github.io/$NAME/"

[ -d "$REPO/.git" ] || { echo "no repo at $REPO - run labdash's run.sh first" >&2; exit 1; }

git -C "$REPO" remote remove origin 2>/dev/null
git -C "$REPO" remote add origin "git@github.com:$SLUG.git"
echo "remote  -> git@github.com:$SLUG.git"

if ! git -C "$REPO" push --force -u origin main; then
  cat >&2 <<EOF

Push failed. Usually one of:
  - the repo does not exist yet     -> create it at https://github.com/new (empty, no README)
  - the name is wrong               -> check <owner>/<repo>
  - this key is not on that account -> ssh -T git@github.com should greet you as the owner
EOF
  exit 1
fi

echo
echo "Pushed. Now enable Pages once, in the browser:"
echo "  https://github.com/$SLUG/settings/pages"
echo "  Source: 'Deploy from a branch'  ->  branch 'main'  /  folder '(root)'  ->  Save"
echo
echo "Waiting for $URL to go live (first build usually takes under a minute)..."

for i in $(seq 1 40); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL" || echo 000)
  if [ "$CODE" = "200" ]; then
    echo
    echo "LIVE: $URL"
    echo "Add LABDASH_PUBLISH=git to the scrontab line and every refresh republishes."
    exit 0
  fi
  printf '\r  attempt %2d/40 - HTTP %s ' "$i" "$CODE"
  sleep 15
done

echo
echo "Still not answering 200. The push worked, so this is almost certainly the"
echo "Pages setting above not saved yet. Check $URL again in a few minutes."
exit 1
