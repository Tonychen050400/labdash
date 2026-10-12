#!/bin/bash
# Daily per-(group, owner) snapshot of the netscratch trees, published whole.
#
# Each run writes into its own directory, and only when EVERY shard finished does
# a final job point owner_current at it. A partial run never mixes with an older
# one: the first version unioned shards from three runs, which is how one
# directory could be counted at two granularities, and why a two-day-old figure
# sat on the page with nothing saying so. Old snapshots stay until replaced.
#
#   LABDASH_OUT=... LABDASH_ACCOUNT=ydu_lab ./ownerscan_submit.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=$("$HERE/py.sh" --print)
GROUP_LIST=${LABDASH_GROUPS:-$(id -gn)}
OUT="${LABDASH_OUT:-/n/holylabs/LABS/${GROUP_LIST%% *}/Lab/labdash}"
ACCT="${LABDASH_ACCOUNT:-${GROUP_LIST%% *}}"
SHARDS=${OWNERSCAN_SHARDS:-64}
if [ -n "${LABDASH_OWNER_ROOTS:-}" ]; then read -ra ROOTS <<< "$LABDASH_OWNER_ROOTS"; else
  ROOTS=(); for g in $GROUP_LIST; do for d in /n/netscratch/$g/Lab /n/netscratch/$g/Users /n/netscratch/$g/Everyone; do [ -d "$d" ] && ROOTS+=("$d"); done; done; fi
[ ${#ROOTS[@]} -gt 0 ] || { echo "ownerscan_submit: no roots" >&2; exit 1; }

# One run at a time. The first lab_storage walk ran past the next 03:00 firing;
# a second full run on top of it doubles the metadata load on shared filers and
# races for the same publish step. If one is still queued or running, skip --
# tomorrow's firing picks up.
if squeue --me -h -o '%j' | grep -qxE 'ownerscan|ownerscan_publish'; then
  echo "ownerscan_submit: previous run still in the queue, not starting another"
  exit 0
fi

RUN="$OUT/owner_runs/$(date +%Y%m%d-%H%M)"
PREV=$(readlink -f "$OUT/owner_current" 2>/dev/null || true)
mkdir -p "$RUN" "$HERE/logs"
"$PY" "$HERE/ownerscan_plan.py" --run "$RUN" --out "$OUT" ${PREV:+--prev "$PREV"} --shards "$SHARDS" --roots "${ROOTS[@]}" || exit 1

ARR=$(sbatch --parsable --account="$ACCT" --partition=sapphire --array=0-$((SHARDS-1)) \
  --cpus-per-task=1 --mem=4G --time=1-00:00:00 --job-name=ownerscan \
  --output="$HERE/logs/ownerscan_%A_%a.log" \
  --wrap="$PY $HERE/ownerscan.py --out $RUN --paths-file $RUN/targets.\$SLURM_ARRAY_TASK_ID.txt --shard 0 --shards 1 --roots ${ROOTS[*]}") || exit 1
# Publish only a complete snapshot: every task COMPLETED and every target recorded.
FIN=$(sbatch --parsable --account="$ACCT" --partition=shared --dependency=afterany:$ARR \
  --cpus-per-task=1 --mem=1G --time=00:10:00 --job-name=ownerscan_publish \
  --output="$HERE/logs/ownerscan_publish_%j.log" --wrap="
bad=\$(sacct -j $ARR -X --noheader --format=State | grep -vc COMPLETED)
want=\$(cat $RUN/targets.*.txt | grep -c .)
got=\$($PY -c \"import json,glob;print(sum(len(r['entries']) for f in glob.glob('$RUN/owner_shards/*.json') for r in json.load(open(f))['roots']))\")
echo \"run $RUN: tasks not completed=\$bad, targets \$got/\$want\"
if [ \"\$bad\" -eq 0 ] && [ \"\$got\" -ge \"\$want\" ]; then
  date -Is > $RUN/COMPLETE; ln -sfn $RUN $OUT/owner_current.tmp && mv -T $OUT/owner_current.tmp $OUT/owner_current
  chmod -R a+rX $RUN; echo published
  ls -1d $OUT/owner_runs/*/ | head -n -3 | xargs -r rm -rf   # keep the newest three
else echo 'NOT published: keeping the previous snapshot'; fi")
echo "ownerscan run $RUN: array $ARR, publish job $FIN"
