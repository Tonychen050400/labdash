#!/usr/bin/env python3
"""ownerscan - bytes under the lab trees by (quota group, file owner).

Answers the question the other two measurements cannot:

  `quota --group-user-usage G P`  gives each person's total on the filesystem,
                                  regardless of which group's quota it bills to
                                  (verified: identical rows for every G).
  dirscan                         charges a directory to the directory's owner and
                                  subtracts whole subtrees billed to other groups.

Neither says "who holds the bytes that count against ydu_lab's quota" -- which is
the only question that matters on the day the lab has to get under a limit. This
walk attributes every FILE to (its group, its owner), so a tree where one person
chgrp'd 26 TiB to the Kempner quota lands in that group's table under their name,
and a shared dataset bills to whoever uploaded it, which is what the filesystem
does too.

One pass. `find -printf` yields group, owner and apparent size per file, so this
costs the same metadata walk as a single `du` -- not the two dirscan runs.

This is slow and IO-heavy: run it from the batch job, never on a login node.
Results are written after every directory, so a job that hits its time limit
still leaves everything it finished.

  python3 ownerscan.py --out DIR --roots /n/netscratch/ydu_lab/Lab ...
"""

import argparse
import json
import os
import pwd
import subprocess
import sys
import time
from datetime import datetime, timezone

RESULT = {"roots": []}
DIR_TIMEOUT = 5 * 3600      # a single top-level directory; inside the job's limit


def write(path):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(RESULT, fh)
    os.replace(tmp, path)


def refuse_on_login_node(force):
    if force or os.environ.get("SLURM_JOB_ID"):
        return
    sys.exit("ownerscan: this walks every file; submit ownerscan_array.sbatch "
             "instead of running it on a login node (or pass --force).")


def owner_of(path):
    try:
        return pwd.getpwuid(os.stat(path).st_uid).pw_name
    except KeyError:
        return f"uid:{os.stat(path).st_uid}"
    except OSError:
        return "unknown"


def walk(path, loose=False):
    """-> (by_group_user, complete, error). by_group_user[group][user] = [bytes, files].
    loose=True counts only the files directly inside `path` (the companion to
    walking its subdirectories as separate targets, so nothing goes uncounted)."""
    agg = {}
    depth = ["-maxdepth", "1"] if loose else []
    # stderr goes to a file, never a pipe. A tree with many private subdirectories
    # makes find write one "Permission denied" line per directory; once a pipe's
    # 64 KB buffer filled, find blocked writing stderr while we blocked reading
    # stdout -- a deadlock that held 37k-file directories for the full 12-hour
    # limit, and the timeout check below never ran because no line ever arrived.
    import tempfile
    errf = tempfile.TemporaryFile(mode="w+")
    try:
        p = subprocess.Popen(["find", path] + depth + ["-type", "f", "-printf", "%g\t%u\t%s\n"],
                             stdout=subprocess.PIPE, stderr=errf, text=True,
                             errors="replace")
    except OSError as e:
        return agg, False, str(e)
    t0 = time.time()
    timed_out = False
    for line in p.stdout:
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 3:
            continue
        g, u, s = parts
        try:
            n = int(s)
        except ValueError:
            continue
        rec = agg.setdefault(g, {}).setdefault(u, [0, 0])
        rec[0] += n
        rec[1] += 1
        if time.time() - t0 > DIR_TIMEOUT:
            timed_out = True
            p.kill()
            break
    p.wait()
    errf.seek(0)
    err = errf.read().strip()
    errf.close()
    if timed_out:
        return agg, False, "timeout"
    # find exits 1 after printing everything it COULD reach when some subdirectory
    # is unreadable; the totals are then an undercount, which must be recorded as
    # such rather than passed off as a measurement.
    return agg, p.returncode == 0, (err.splitlines()[-1][:200] if err else None)


def scan_root(root, state, out_path, shard, shards):
    try:
        names = sorted(e.name for e in os.scandir(root) if e.is_dir(follow_symlinks=False))
    except OSError as e:
        state["error"] = str(e)
        write(out_path)
        return
    state["total_dirs"] = len(names)
    for i, name in enumerate(names):
        if i % shards != shard:
            continue
        path = os.path.join(root, name)
        t0 = time.time()
        entry = {"dir": name, "dir_owner": owner_of(path), "by": {}, "bytes": None,
                 "files": None, "complete": False, "unreadable": False, "error": None,
                 "seconds": 0.0}
        try:
            next(iter(os.scandir(path)), None)
        except PermissionError:
            entry["unreadable"] = True
            state["entries"].append(entry)
            write(out_path)
            print(f"  {name:<28} UNREADABLE (private directory), not counted", flush=True)
            continue
        except OSError:
            pass
        by, ok, err = walk(path)
        entry["by"] = {g: {u: {"bytes": v[0], "files": v[1]} for u, v in users.items()}
                       for g, users in by.items()}
        entry["bytes"] = sum(v[0] for users in by.values() for v in users.values())
        entry["files"] = sum(v[1] for users in by.values() for v in users.values())
        entry["complete"] = ok
        entry["error"] = err
        entry["seconds"] = round(time.time() - t0, 1)
        state["entries"].append(entry)
        state["scanned"] = len(state["entries"])
        write(out_path)
        groups = ", ".join(f"{g}={sum(v[0] for v in us.values())/2**40:.2f}T"
                           for g, us in sorted(by.items()))
        print(f"  {name:<28} {entry['bytes']/2**40:7.2f} TiB  {entry['files']:>10,} files"
              f"  [{groups}]  {entry['seconds']}s{'' if ok else '  INCOMPLETE: ' + str(err)}",
              flush=True)
    state["complete"] = all(e["complete"] or e["unreadable"] for e in state["entries"])


LOOSE = "/."


def scan_paths(paths, out_path, shard, shards):
    """Explicit targets (for directories too big for one find inside the time limit).
    Each target is recorded under its parent root with dir = path relative to it;
    a target ending in /. counts only the files sitting directly in that directory."""
    states = {}
    for i, target in enumerate(paths):
        if i % shards != shard:
            continue
        loose = target.endswith(LOOSE)
        real = target[: -len(LOOSE)] if loose else target
        root = next((r for r in ("/n/netscratch/ydu_lab/Lab", "/n/netscratch/ydu_lab/Everyone")
                     if real.startswith(r + "/")), os.path.dirname(real))
        st = states.get(root)
        if st is None:
            st = {"root": root, "entries": [], "scanned": 0, "total_dirs": None,
                  "complete": False, "error": None, "partial_targets": True}
            states[root] = st
            RESULT["roots"].append(st)
        name = os.path.relpath(real, root) + (LOOSE if loose else "")
        t0 = time.time()
        by, ok, err = walk(real, loose=loose)
        entry = {"dir": name, "dir_owner": owner_of(real),
                 "by": {g: {u: {"bytes": v[0], "files": v[1]} for u, v in us.items()}
                        for g, us in by.items()},
                 "bytes": sum(v[0] for us in by.values() for v in us.values()),
                 "files": sum(v[1] for us in by.values() for v in us.values()),
                 "complete": ok, "unreadable": False, "error": err,
                 "seconds": round(time.time() - t0, 1)}
        st["entries"].append(entry)
        st["scanned"] = len(st["entries"])
        write(out_path)
        print(f"  {name:<40} {entry['bytes']/2**40:7.2f} TiB {entry['files']:>10,} files "
              f"{entry['seconds']}s{'' if ok else '  INCOMPLETE: ' + str(err)}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--roots", nargs="+", default=[])
    ap.add_argument("--paths-file", help="explicit target list, one per line (see scan_paths)")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.shards < 1 or not 0 <= args.shard < args.shards:
        sys.exit(f"ownerscan: shard {args.shard} unreachable with --shards {args.shards}")
    refuse_on_login_node(args.force)

    # Separate directory from dirscan's shards/: labdash globs that one as dirscan
    # output and would try to read these as directory entries.
    shard_dir = os.path.join(args.out, "owner_shards")
    os.makedirs(shard_dir, exist_ok=True)
    run = ((os.environ.get("SLURM_ARRAY_JOB_ID") or os.environ.get("SLURM_JOB_ID")
            or "manual") + "a" + (os.environ.get("SLURM_RESTART_COUNT") or "0"))
    out_path = os.path.join(shard_dir, f"ownerscan.{run}.{args.shard:03d}.json")

    RESULT["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    RESULT["generated_local"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    RESULT["host"] = os.uname().nodename
    write(out_path)
    if args.paths_file:
        with open(args.paths_file) as fh:
            paths = [l.strip() for l in fh if l.strip()]
        scan_paths(paths, out_path, args.shard, args.shards)
    for root in args.roots:
        print(f"[{datetime.now():%H:%M:%S}] scanning {root}", flush=True)
        state = {"root": root, "entries": [], "scanned": 0, "total_dirs": None,
                 "complete": False, "error": None}
        RESULT["roots"].append(state)
        if not os.path.isdir(root):
            state["error"] = "not a directory or not reachable"
            write(out_path)
            continue
        t0 = time.time()
        scan_root(root, state, out_path, args.shard, args.shards)
        tot = sum(e["bytes"] or 0 for e in state["entries"])
        print(f"[{datetime.now():%H:%M:%S}] {root}: {len(state['entries'])} dirs, "
              f"{tot/2**40:.2f} TiB, {time.time()-t0:.1f}s", flush=True)
    print(f"wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
