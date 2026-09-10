#!/bin/bash
# Resolve a usable Python 3, newest first, and run the given script with it.
#
# Exists because the cluster's interpreters move. python3.11 was present when this
# project started, was hardcoded in four places, and was removed around 2026-08-22 --
# after which the Slack alert and the roster refresh exited 127 every day and nobody
# could tell, because a cron job that never runs produces no output to notice. The
# system `python3` is 3.6 here, too old, so it is the last resort rather than the first.
#
#   py.sh --print          print the interpreter path
#   py.sh script.py ...    run the script with it
set -uo pipefail
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  p=$(command -v "$c" 2>/dev/null) || continue
  v=$("$p" -c 'import sys;print(sys.version_info>=(3,7))' 2>/dev/null)
  [ "$v" = "True" ] || continue
  if [ "${1:-}" = "--print" ]; then echo "$p"; exit 0; fi
  exec "$p" "$@"
done
echo "py.sh: no Python >= 3.7 found on $(hostname)" >&2
exit 1
