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


def prev_seconds(prev):
    sec = {}
    for f in glob.glob(os.path.join(prev, "owner_shards", "*.json")) if prev else []:
        try:
            for r in json.load(open(f)).get("roots", []):
                for e in r.get("entries", []):
                    sec[os.path.join(r["root"], e["dir"])] = e.get("seconds") or 0
        except (OSError, json.JSONDecodeError):
            pass
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
    a = ap.parse_args()
    sec = prev_seconds(a.prev)
    targets = []
    for root in a.roots:
        for top in dirs(root) or []:
            kids = dirs(top)
            if kids is None:                 # private: one target, recorded unreadable
                targets.append(top)
                continue
            targets.append(top + "/.")
            for k in kids:
                if sec.get(k, 0) > SPLIT_S and (grand := dirs(k)):
                    targets += [k + "/."] + grand
                else:
                    targets.append(k)
    cost = lambda t: sec.get(t.rstrip("/").removesuffix("/."), UNKNOWN_S) if t.endswith("/.") \
        else sec.get(t, UNKNOWN_S)
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
