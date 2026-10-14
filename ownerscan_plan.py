#!/usr/bin/env python3
"""Build one day's ownerscan: a target list split so no shard is stuck behind one
huge directory, balanced across shards by how long each target took last time.

Every top-level directory under each root becomes its children plus its loose
files ("<dir>/."). A child that took longer than SPLIT_S last run is split one
level further. Targets are then dealt to shards greedily, longest first, using the
previous run's seconds (unknown targets assume UNKNOWN_S), so the slowest shard is
as short as it can be. Writes <run>/targets.<k>.txt for k in 0..shards-1.

  ownerscan_plan.py --run DIR --prev DIR --shards 64 --roots R1 R2 ...
"""
import argparse, glob, heapq, json, os

SPLIT_S = 1800
UNKNOWN_S = 30
MAX_DEPTH = 4        # levels below a top-level dir we are willing to split into
# Never split a directory into more pieces than this. A dataset with one folder per
# object (lab_storage .../akiruga/datasets/srn_nmr_*, ~36k each) turned one night's
# plan into 76,779 targets, a 20 MiB page, and failing Pages deploys.
MAX_FANOUT = 200


def prev_seconds(prev, out=None):
    """Seconds per directory, to plan the split and the balance -- never counted.

    Only how LONG a directory takes to walk is borrowed here, which depends on its
    file count and the filer, not on how bytes are attributed. Precedence: the
    owner scan's own timings (published run first, then newer unpublished ones,
    since a run that timed out still recorded what it finished); the old directory
    walk's du timings fill in only paths the owner scan has never measured, so a
    first walk of a tree is not planned blind -- and once the owner scan has
    measured a path, the old figures stop mattering, even though their files stay
    on disk.
    """
    def read(files):
        sec = {}
        for f in files:
            try:
                for r in json.load(open(f)).get("roots", []):
                    for e in r.get("entries", []):
                        d = e.get("dir") or ""
                        # "<dir>/." is only the files sitting directly in <dir>: a
                        # sub-second listing. Keyed as <dir> it overwrote that
                        # directory's real time (4 h became 0.1 s) and the planner
                        # stopped splitting it.
                        if d.endswith("/.") or d == ".":
                            continue
                        k = os.path.join(r["root"], d).rstrip("/")
                        sec[k] = max(sec.get(k, 0), e.get("seconds") or 0)
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        return sec
    published = read(glob.glob(os.path.join(prev, "owner_shards", "*.json")) if prev else [])
    newer = read(glob.glob(os.path.join(out, "owner_runs", "*", "owner_shards", "*.json"))
                 if out else [])
    old_walk = read(glob.glob(os.path.join(out, "shards", "dirscan.*.json")) if out else [])
    sec = dict(old_walk)
    sec.update(newer)
    sec.update(published)
    return sec


def dirs(p):
    try:
        return sorted(e.path for e in os.scandir(p) if e.is_dir(follow_symlinks=False))
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--prev")
    ap.add_argument("--shards", type=int, default=64)
    ap.add_argument("--roots", nargs="+", required=True)
    ap.add_argument("--out", help="labdash output dir, for unpublished runs and du timings")
    a = ap.parse_args()
    sec = prev_seconds(a.prev, a.out)
    keys = sorted(sec)

    def known(p):
        """Recorded seconds for p, else the sum recorded for anything under it."""
        if p in sec:
            return sec[p]
        import bisect
        i, tot = bisect.bisect_left(keys, p + "/"), 0
        while i < len(keys) and keys[i].startswith(p + "/"):
            tot += sec[keys[i]]
            i += 1
        return tot or None

    est = {}

    def expand(p, depth, inherited=None):
        """Split p while it is slow. A child with no timing of its own gets an equal
        share of its parent's: estimating each at UNKNOWN_S would turn one 6-hour
        directory into forty 30-second ones and the balancer would stack them."""
        c = known(p)
        if c is None:
            c = inherited
        if c is not None and c > SPLIT_S and depth < MAX_DEPTH:
            kids = dirs(p)
            if kids and len(kids) <= MAX_FANOUT:
                out = [p + "/."]
                for k in kids:
                    out += expand(k, depth + 1, c / len(kids))
                return out
        est[p] = c if c is not None else UNKNOWN_S
        return [p]

    targets = []
    for root in a.roots:
        for top in dirs(root) or []:
            kids = dirs(top)
            if kids is None:                 # private: one target, recorded unreadable
                targets.append(top)
                continue
            targets.append(top + "/.")
            for k in kids:
                targets += expand(k, 1)

    def cost(t):
        if t.endswith("/."):
            return 1.0                       # loose files: one directory listing
        return est.get(t, UNKNOWN_S)
    heap = [(0.0, k, []) for k in range(a.shards)]
    for t in sorted(targets, key=cost, reverse=True):
        load, k, lst = heapq.heappop(heap)
        lst.append(t)
        heapq.heappush(heap, (load + cost(t), k, lst))
    os.makedirs(a.run, exist_ok=True)
    worst = 0
    for load, k, lst in heap:
        with open(os.path.join(a.run, f"targets.{k}.txt"), "w") as fh:
            fh.write("\n".join(lst) + "\n")
        worst = max(worst, load)
    print(f"{len(targets)} targets over {a.shards} shards; slowest shard ~{worst/60:.0f} min "
          f"by last run's timings")


if __name__ == "__main__":
    main()
