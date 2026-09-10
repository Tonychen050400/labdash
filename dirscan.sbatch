#!/bin/bash
#SBATCH --job-name=dirscan
#SBATCH --partition=shared
#SBATCH --account=ydu_lab
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=12:00:00
#SBATCH --output=%x_%j.log
#
# Per-user breakdown of the two lab DIRECTORIES, which `quota` cannot answer.
# One core on purpose: this is pure NFS metadata IO, so extra cores would sit idle
# and get flagged. Never run this on a login node.
#
#   sbatch dirscan.sbatch
set -uo pipefail

OUT="${LABDASH_OUT:-/n/holylabs/LABS/kempner_ydu_lab/Lab/labdash}"
PY=$(command -v python3.11 || command -v python3)

"$PY" "${LABDASH_HOME:-$HOME/projects/labdash}/dirscan.py" \
  --out "$OUT" \
  --roots /n/holylabs/LABS/ydu_lab/Lab /n/holylabs/LABS/ydu_lab/Everyone \
          /n/lab_storage/ydu_lab

chmod a+r "$OUT/dirscan.json" 2>/dev/null
