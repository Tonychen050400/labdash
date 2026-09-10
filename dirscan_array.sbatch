#!/bin/bash
#SBATCH --job-name=dirscan
#SBATCH --partition=sapphire
#SBATCH --array=0-31
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=16:00:00
#SBATCH --output=logs/dirscan_%A_%a.log
#
# Parallel tree walk. Crawling is embarrassingly parallel across top-level
# directories, so each array task takes every Nth directory and writes its own
# shard file; labdash merges them by directory name at render time.
#
#   sbatch --account=<lab> dirscan_array.sbatch
#   sbatch --account=<lab> --array=0-15 dirscan_array.sbatch     # gentler on the filers
#   LABDASH_RESUME=1 sbatch --account=<lab> dirscan_array.sbatch # re-measure only what is missing
#
# No account is named in this file: pass it on the sbatch line (labdash.scron does),
# and note it must be a plain lab account -- Kempner accounts are rejected outside
# the Kempner partitions.
#
# On width: the useful ceiling is the DIRECTORY COUNT, not the core count. These
# trees have 57-58 top-level directories, so past ~58 tasks the extra ones get
# nothing, and wall clock bottoms out at whatever the single slowest directory
# takes (13 min for the worst one on holylabs). 32 is chosen over 100 for a second
# reason: this is pure NFS metadata IO against filers the whole cluster shares, and
# a hundred concurrent walks is a load spike other people would feel.
#
# sapphire over shared: 112 cores/node at billing 181 works out ~18% cheaper per
# core than shared's 48 at 95, and both are CPU-only so neither touches GPU fairshare.
set -uo pipefail

LABDASH_HOME="${LABDASH_HOME:-$HOME/projects/labdash}"
PY=$("$LABDASH_HOME/py.sh" --print)
N=${SLURM_ARRAY_TASK_COUNT:-1}
I=${SLURM_ARRAY_TASK_ID:-0}

# Which trees to walk. LABDASH_ROOTS (space-separated) wins outright. Otherwise the
# roots are derived from the lab group(s) -- LABDASH_GROUPS, default the running
# user's primary group -- over FASRC's standard layout: Lab/, Users/ and Everyone/
# under holylabs and netscratch, plus the lab_storage fileset, keeping only the
# directories that exist.
#
# Point a root at the directory whose CHILDREN are people's folders. dirscan charges
# each child to its owner (uid, not name), and only expands a child one level
# further when that child itself holds many different owners. A root that is one
# person's directory becomes one row.
GROUP_LIST=${LABDASH_GROUPS:-$(id -gn)}    # not GROUPS: that name is a bash builtin
if [ -n "${LABDASH_ROOTS:-}" ]; then
  read -ra ROOTS <<< "$LABDASH_ROOTS"
else
  ROOTS=()
  for g in $GROUP_LIST; do
    for d in /n/holylabs/LABS/$g/Lab /n/holylabs/LABS/$g/Users /n/holylabs/LABS/$g/Everyone \
             /n/netscratch/$g/Lab /n/netscratch/$g/Users /n/netscratch/$g/Everyone \
             /n/lab_storage/$g; do
      [ -d "$d" ] && ROOTS+=("$d")
    done
  done
fi
if [ ${#ROOTS[@]} -eq 0 ]; then
  echo "dirscan: no directories found for group(s) '$GROUP_LIST'; set LABDASH_ROOTS" >&2
  exit 1
fi
OUT="${LABDASH_OUT:-/n/holylabs/LABS/${GROUP_LIST%% *}/Lab/labdash}"
echo "dirscan shard $I/$N -> $OUT  roots: ${ROOTS[*]}"

"$PY" "$LABDASH_HOME/dirscan.py" \
  --out "$OUT" --shard "$I" --shards "$N" ${LABDASH_RESUME:+--skip-existing} \
  --roots "${ROOTS[@]}"

# chmod is best-effort: it must not decide the job's exit status. Two shards
# reported FAILED purely because this returned non-zero after finishing their work.
chmod -R a+rX "$OUT/shards" 2>/dev/null || true
exit 0
