#!/bin/bash
#SBATCH --job-name=ownerscan
#SBATCH --partition=sapphire
#SBATCH --array=0-31
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=6:00:00
#SBATCH --output=logs/ownerscan_%A_%a.log
#
# Per-(group, owner) walk of the lab trees -- see ownerscan.py. Same shape as
# dirscan_array.sbatch: shards take every Nth top-level directory, write their
# own file, labdash unions them at render. One pass per directory (find -printf),
# so a shard costs about one `du` of its directories, not two.
#
#   sbatch --account=<lab> ownerscan_array.sbatch
#
# No account named here: pass it on the sbatch line, and it must be a plain lab
# account -- Kempner accounts are rejected outside the Kempner partitions.
set -uo pipefail

LABDASH_HOME="${LABDASH_HOME:-$HOME/projects/labdash}"
PY=$("$LABDASH_HOME/py.sh" --print)
N=${SLURM_ARRAY_TASK_COUNT:-1}
I=${SLURM_ARRAY_TASK_ID:-0}

# Roots: LABDASH_OWNER_ROOTS (space-separated) wins; otherwise the netscratch
# Lab/Users/Everyone trees of LABDASH_GROUPS (default: primary group), because
# netscratch is where the quota question lives. Files billed to a second group
# inside these trees are attributed to that group by this walk, so a lab with a
# Kempner sub-account does NOT need to list the sub-account's (usually empty) tree.
GROUP_LIST=${LABDASH_GROUPS:-$(id -gn)}    # not GROUPS: that name is a bash builtin
if [ -n "${LABDASH_OWNER_ROOTS:-}" ]; then
  read -ra ROOTS <<< "$LABDASH_OWNER_ROOTS"
else
  ROOTS=()
  for g in $GROUP_LIST; do
    for d in /n/netscratch/$g/Lab /n/netscratch/$g/Users /n/netscratch/$g/Everyone; do
      [ -d "$d" ] && ROOTS+=("$d")
    done
  done
fi
if [ ${#ROOTS[@]} -eq 0 ]; then
  echo "ownerscan: no directories found for group(s) '$GROUP_LIST'; set LABDASH_OWNER_ROOTS" >&2
  exit 1
fi
OUT="${LABDASH_OUT:-/n/holylabs/LABS/${GROUP_LIST%% *}/Lab/labdash}"
echo "ownerscan shard $I/$N -> $OUT  roots: ${ROOTS[*]}"

"$PY" "$LABDASH_HOME/ownerscan.py" --out "$OUT" --shard "$I" --shards "$N" --roots "${ROOTS[@]}"
chmod -R a+rX "$OUT/owner_shards" 2>/dev/null || true
exit 0
