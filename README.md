# labdash

A one-page dashboard of the lab's **GPU jobs** and **filesystem usage** on FASRC
Cannon, so the compute-credit and storage conversations start from numbers
instead of guesses.

Standard library only, Python 3.7+. No install, no dependencies, no services. It
shells out to `squeue` / `sreport` / `sshare` / `quota`, writes `snapshot.json`, and
renders a self-contained `index.html` with the data inlined — the page works from
`file://`, a static host, or a shared directory, and needs no backend.

```
./py.sh labdash.py --out ./out     # → out/index.html, out/snapshot.json, out/history.jsonl
./py.sh labdash.py --json          # snapshot to stdout, render nothing
```

Call interpreters through `py.sh`, never by version. `python3.11` was hardcoded in
four places, was removed from the cluster around 2026-08-22, and every one of them
exited 127 daily for a week without anyone noticing — a cron job that never runs
produces no output to notice. `py.sh` resolves whatever exists at run time (the
system `python3` is 3.6 here, so it is the last resort, not the first).

Runs in ~4 seconds. Flags: `--accounts`, `--paths`, `--fragment` (body-only HTML,
for pasting into another page).

## What it shows

**Storage** — quota and headroom per lab group per filesystem, plus a per-user
leaderboard for who is holding the space, and a trend line once history builds up.

**Compute** — the account's fairshare standing (usage ÷ shares: above 1× means the
lab has drawn more than its slice and new jobs queue behind lighter users),
GPU-hours per person over 7 and 30 days, live GPU counts, and the running job list
with elapsed time against each job's own wall limit.

## Two data traps it works around

**`df` lies here.** It reports the whole shared array — 87% full and 553 TB free
looks fine while your lab is at its own 100 TiB ceiling. Quota is per group per
filesystem: `quota -g <lab> /n/netscratch`.

**`quota --group-user-usage` is not a breakdown of the group quota.** Verified
against the tool: pass three different groups and a shared member reports
byte-identical figures, so the number is that person's *filesystem-wide* total and
the group argument only picks whose names to list. The rows can therefore sum past
the lab's quota — the excess is space those people keep under other labs' trees.
labdash scans once per filesystem, merges the member lists, and labels the table
accordingly rather than presenting a fake decomposition.

Two smaller limits worth knowing: the per-user scan is a nightly VAST report, so a
directory deleted today still shows until tomorrow; and Slurm only exposes other
people's finished jobs in aggregate (`sacct -a` returns nothing without coordinator
rights), so per-person history is GPU-hours rather than job lists.

## Running it for your own lab

Nothing in the collector is tied to one lab. Slurm accounts come from `sacctmgr`
for whoever runs it, and the storage panels are filled from that user's primary Unix
group over FASRC's standard layout (`/n/holylabs/LABS/<lab>`, `/n/netscratch/<lab>`,
`/n/lab_storage/<lab>`). Every value that is specific to *a* deployment lives in one
file, `labdash.scron`. The checklist:

**1. Clone it on Cannon as a member of your lab group.** This is the one constraint
that cannot be worked around: the per-person storage figures come from walking
directories, so an instance only sees what the user running it can read. Someone
outside the group cannot produce correct numbers for you, and neither can a shared
service account unless it is in the group.

**2. Decide which directories to walk.** Lab trees differ, so this is the one thing
to get right by hand. The rule: **point each root at the directory whose immediate
children are people's folders.** `dirscan.py` charges every child to the child's
*owner* (uid), never its name — names are nicknames (`alex` belongs to `atong`) and
attributing by name misreports real people. Two refinements happen automatically:
a child that itself holds many different owners (more than half of its
subdirectories owned by different people) is treated as a container and expanded one
level; a child holding many directories under a *few* owners is labelled `(shared)`
rather than blamed on whoever created it. So:

| your tree looks like | point the root at |
|---|---|
| `/n/holylabs/LABS/<lab>/Lab/<name>` | `/n/holylabs/LABS/<lab>/Lab` |
| `/n/holylabs/LABS/<lab>/Users/<name>` | `/n/holylabs/LABS/<lab>/Users` |
| `/n/holylabs/LABS/<lab>/<name>` (flat) | `/n/holylabs/LABS/<lab>` |
| one person's directory | don't — that is one row |

Check a candidate with `ls -ln <dir> | head`: the third column (uid) should differ
from line to line. Without any configuration, `dirscan_array.sbatch` walks whichever
of `Lab/`, `Users/`, `Everyone/` exist under holylabs and netscratch for your group,
plus `/n/lab_storage/<lab>`. Set `LABDASH_ROOTS="<dir> <dir> ..."` to override, or
`LABDASH_GROUPS="<lab> <second_lab>"` if the lab bills under two accounts with two
trees (ours does).

**3. Edit `labdash.scron`.** Four things, listed at the top of the file with the
reasons next to them: the `--account=` (a plain lab account — Kempner accounts are
rejected on `shared`), `LABDASH_OUT` (where the page is written), `LABDASH_ROOTS` from
step 2, and the `$HOME/projects/labdash` paths if you cloned elsewhere. A lab with one
account and a standard tree can delete the `LABDASH_ROOTS=` and `LABDASH_GROUPS=`
overrides entirely and let the derived defaults apply.

**4. Run the collector once by hand** — `./py.sh labdash.py --out <dir>` takes ~4
seconds and is fine on a login node; open the `index.html` it wrote. The directory
walk is **not** fine on a login node: submit it,
`LABDASH_OUT=<dir> sbatch --account=<lab> dirscan_array.sbatch`, and the page grows
its per-person tables when the shards land.

**5. `scrontab labdash.scron`** — see the next section for what that installs.

**6. Publish** (two sections down) — and before you do, agree as a lab that a page
naming every member with their disk usage and GPU-hours is something you want on the
open web. Pages sites are public even from a private repo.

## Keeping it fresh

**Do not leave this running on a login node.** A `while true; do ...; sleep 900;
done &` in an ssh session dies when the session drops, dies again when the node
reboots, and is the kind of thing RC asks people to stop doing. Use `scrontab`,
which is Slurm's own cron and the sanctioned way to run recurring work on Cannon.

```
scrontab labdash.scron      # install       (edit: scrontab -e, list: scrontab -l)
```

Each firing is a **real Slurm job on a compute node**: one CPU core, 2 GB, a 10-minute
limit, `shared` partition, **no GPU** — a refresh costs about 4 seconds of one core.
Nobody has to be logged in. You can close your laptop, your ssh session can drop, the
login node can reboot: Slurm still fires the job and the page still updates. That is
the whole reason to use scrontab over a background process or a personal crontab.

Two things that bite people here:

- **Account.** On `shared` you must charge a plain lab account, not a Kempner one —
  Slurm rejects `kempner_*` accounts outside the Kempner partitions with "Invalid
  account or account/partition combination specified". Same restriction as
  `gpu_requeue`.
- **`--output=/dev/null`.** These entries discard their output deliberately, which
  means a broken job is silent. Check it is alive with `scrontab -l`, `squeue --me`,
  or — easiest — the page's own "updated N minutes ago" stamp, which turns amber past
  35 minutes (two missed refreshes).

`labdash.scron` installs four entries, not one: the 15-minute page refresh, a daily
02:00 submit of the directory walk (`dirscan_array.sbatch`, a 32-task array on
`sapphire` — parallel because serial holylabs took 87 minutes), a 09:00 storage
alert to Slack, and a roster refresh. Only the first is required to have a page.

Output defaults to `/n/holylabs/LABS/<your primary group>/Lab/labdash`; set
`LABDASH_OUT` to put it elsewhere. (Ours is elsewhere: `ydu_lab`'s holylabs
allocation is at 100% and writes there fail, so `labdash.scron` points every entry at
a sibling account's quota.)

## Getting it in front of the lab

Pick one. The page is a single self-contained file, so all three serve the same
artifact.

**1. Shared directory + SSH tunnel** — zero infrastructure, works today. Everyone
already has cluster SSH.

```
ssh -L 8899:localhost:8899 you@login.rc.fas.harvard.edu \
  'cd $LABDASH_OUT && python3 -m http.server 8899'
# then open http://localhost:8899
```

**2. GitHub Pages** — a clickable URL, no tunnel, no login. This is the one in use.
Outbound HTTPS works from the cluster. The site repo is separate from this source
repo (it holds one force-amended commit of generated HTML, so nothing you would want
history for belongs in it); any account can own it — a personal one is fine, and the
repo can be transferred to a lab org later.

```
# 1. create an EMPTY repo at https://github.com/new  (no README, no .gitignore)
# 2. wire it up and watch for the site to answer 200:
./setup_pages.sh <owner>/<repo>
# 3. the script prints the Settings->Pages step; do it once
```

A **public** repo gets Pages for free. A private repo needs Pro (free for Harvard
students via GitHub Education) — but note that Pages *sites* are public either way;
only Enterprise Cloud can put a login in front of one. Everyone named on the page is
therefore visible to anyone with the link.

Each publish **amends the single commit and force-pushes**, so the repo stays ~1.5 MB
forever. A linear history would be tens of GB a year of snapshots nobody will ever
diff; the trend data lives in `history.jsonl` on the cluster instead.

Publishing runs on the same 15-minute refresh. Pages deploys began failing with 503 at
2026-08-17T15:15Z after **86 consecutive successes** on this same setup, so the cause
is GitHub-side; failure showed no correlation with the gap between pushes (two pushes
1.7 min apart both deployed fine), which is what ruled out the tempting
"force-push orphaned the SHA" explanation. A failed deploy leaves the site one cycle
stale, never down, and `run.sh` does not fail the job on a push error, so a GitHub
hiccup cannot stop the next collection either.

**3. Open OnDemand** — if the lab already uses `vdi.rc.fas.harvard.edu`, the file
browser opens `index.html` directly with no tunnel and no external hosting.

## Files

| | |
|---|---|
| `labdash.py` | collector + renderer, the whole tool |
| `py.sh` | finds a usable Python; every entry point goes through it |
| `run.sh` | refresh, then optionally publish; scrontab-safe |
| `labdash.scron` | scrontab entries: 15-min refresh, nightly walk, daily alert, roster — **and the only file holding a lab's specific paths/accounts** |
| `dirscan.py`, `dirscan_array.sbatch` | the per-person directory walk and the array job that runs it |
| `notify.py` | daily storage alert to Slack |
| `roster.py` | username → email skeleton for the alert, typed in once by a human |
| `setup_pages.sh` | one-time GitHub Pages wiring, verifies the site answers 200 |
| `bench/` | GPU throughput measurements behind the hardware guide |
| `history.jsonl` | one line per run, in the output dir; feeds the trend lines |
