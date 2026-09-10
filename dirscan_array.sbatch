#!/bin/bash
#SBATCH --job-name=dirscan
#SBATCH --partition=sapphire
#SBATCH --account=ydu_lab
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
#   sbatch dirscan_array.sbatch
#   sbatch --array=0-15 dirscan_array.sbatch     # gentler on the filers
#   LABDASH_RESUME=1 sbatch dirscan_array.sbatch # re-measure only what is missing
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

OUT="${LABDASH_OUT:-/n/holylabs/LABS/kempner_ydu_lab/Lab/labdash}"
PY=$("${LABDASH_HOME:-$HOME/projects/labdash}/py.sh" --print)
N=${SLURM_ARRAY_TASK_COUNT:-1}
I=${SLURM_ARRAY_TASK_ID:-0}

"$PY" "${LABDASH_HOME:-$HOME/projects/labdash}/dirscan.py" \
  --out "$OUT" --shard "$I" --shards "$N" ${LABDASH_RESUME:+--skip-existing} \
  --roots /n/holylabs/LABS/ydu_lab/Lab \
          /n/holylabs/LABS/ydu_lab/Everyone \
          /n/holylabs/LABS/kempner_ydu_lab/Lab \
          /n/holylabs/LABS/kempner_ydu_lab/Everyone \
          /n/lab_storage/ydu_lab \
          /n/netscratch/ydu_lab/Lab \
          /n/netscratch/ydu_lab/Everyone

# chmod is best-effort: it must not decide the job's exit status. Two shards
# reported FAILED purely because this returned non-zero after finishing their work.
chmod -R a+rX "$OUT/shards" 2>/dev/null || true
exit 0
