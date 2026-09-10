#!/usr/bin/env python3
"""dirscan - per-user space and file counts under specific lab DIRECTORIES.

labdash's storage tables come from `quota`, which only answers per *filesystem*
and (on VAST) reports each user's filesystem-wide total. Neither can say "who is
using what under /n/holylabs/LABS/ydu_lab", and /n/lab_storage is Isilon, where
`quota --group-user-usage` refuses outright ("only supported for VAST
filesystems"). The only way to get that breakdown is to walk the tree.

Both lab trees are laid out one directory per person, so this walks the top level
and attributes each subtree to the directory's OWNER rather than its name -- the
names are nicknames and do not match usernames (`alex` belongs to atong,
`agopalak` to agopalakrishnan). Attributing by name would misreport real people.

This is slow and IO-heavy: run it from the batch job, never on a login node.
Results are written after every directory, so a job that hits its time limit
still leaves everything it finished.

  python3 dirscan.py --out DIR --roots /n/holylabs/LABS/ydu_lab/Lab ...
"""

import argparse
import json
import zlib
import os
import pwd
import subprocess
import sys
import time
from datetime import datetime, timezone


SLOW_DIR_SECONDS = 600   # past this, skip the second walk and record no file count
# The byte measurement gets a long leash (6h, inside the job's 16h limit): four
# directories hit an earlier 2h cap and returned nothing at all, which is worse
# than taking a long time.


def du(path, inodes=False, allocated=False, timeout=21600):
    """One `du -s` measurement -> (value, complete). `complete` is False when du
    exited non-zero, which means it could not read part of the tree and the total
    it printed is an UNDERCOUNT. Treating that as a finished measurement is how a
    partial number passes for a real one."""
    # Apparent size is the default measurement, not allocated blocks. Both cost
    # exactly one walk, so this is a free correction: allocated blocks ran 27.7%
    # above the files' own size on this filer (Isilon stores FEC protection data
    # beside every file and rounds to block boundaries), which put the folder total
    # at 108 TiB against a 100 TiB limit -- a number that cannot be true as a
    # "how much data is in here" figure. `--allocated` is kept for diagnosing that
    # gap, but the dashboard reports what the files themselves weigh.
    cmd = ["du", "-s"] + (["--inodes"] if inodes else
                          ["--block-size=1"] if allocated else
                          ["--apparent-size", "--block-size=1"]) + [path]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None, False
    out = (p.stdout or "").strip().split("\n")[-1]
    try:
        return int(out.split("\t")[0]), p.returncode == 0
    except (ValueError, IndexError):
        return None, False


def owner_of(path):
    try:
        return pwd.getpwuid(os.stat(path).st_uid).pw_name
    except (OSError, KeyError):
        try:
            return f"uid:{os.stat(path).st_uid}"
        except OSError:
            return "unknown"


# A "container" is a directory holding other people's directories rather than one
# person's data -- /n/lab_storage/ydu_lab/Lab is owned by the PI and holds ten
# members' trees; Everyone/datasets is owned by one person and holds four people's
# datasets. Charging either to its creator both blames the wrong person and hides
# everyone else.
#
# This is decided from OWNERSHIP, not from the name. A name list ("Lab", "Everyone",
# "shared") was the first attempt and it was wrong in both directions: Lab/datasets
# and Lab/data are empty, temp_datasets and models hold one person's files despite
# the plural names, while a container can be called anything at all.
MIN_OWNERS_FOR_CONTAINER = 2

# Names that are structure, never a person. Attributing one of these to whoever
# created it is always wrong: Everyone/Lab holds 20.5 TiB of datasets two levels
# down and was being charged to one member. This is a much weaker claim than
# matching a directory to a user by name -- it only says "this is not somebody's
# personal folder", which is safe, whereas name-to-user matching was wrong 34% of
# the time and is still not done anywhere.
STRUCTURAL_NAMES = {"lab", "everyone", "datasets", "dataset", "data", "models",
                    "shared", "shared_data", "public", "common",
                    "temp_datasets", "checkpoints"}


def _child_owners(path):
    """Distinct owners of a directory's immediate subdirectories."""
    owners = set()
    try:
        for e in os.scandir(path):
            if e.is_dir(follow_symlinks=False):
                try:
                    owners.add(os.stat(e.path).st_uid)
                except OSError:
                    pass
    except OSError:
        return set()
    return owners


def _is_people_container(path, kids):
    """Does this container hold PEOPLE's directories, or shared data?

    Both look alike structurally, so the test is how the children map to owners.
    A people container is close to one directory per person (Lab/ has 10 children
    and 10 distinct owners). A shared data store is many directories under a few
    owners (Everyone/datasets has 41 children but 4 owners, 32 of them one person's)
    -- those are datasets the whole lab reads, and charging them to whoever created
    the folder both blames one person and hides the rest.
    """
    if not kids:
        return False
    owners = set()
    for k in kids:
        try:
            owners.add(os.stat(os.path.join(path, k)).st_uid)
        except OSError:
            pass
    return len(owners) / len(kids) > 0.5


SHARED_OWNER = "(shared)"

# Bytes inside our tree that belong to ANOTHER group's quota. They show up because
# people create setgid directories to charge data elsewhere -- there are subtrees
# under /n/netscratch/ydu_lab owned by kempner_ydu_lab and even by another lab
# entirely (kempner_rcai_lab). Those bytes are not ours to manage and not counted
# against our quota, so counting them made the leaderboard exceed the 100 TiB limit
# and invited exactly the "this is broken" reaction it got.
#
# Found with a DEPTH-BOUNDED directory scan on purpose. A full walk to filter by
# group would double the cost of the crawl, which is the mistake the inode pass
# already taught. Depth 3 is not a guess: it is the depth that actually completed
# when measured, and it found every foreign subtree we know of, because people create
# a setgid directory near the top of their own space. Anything deeper is missed --
# an accepted limit, stated rather than hidden, and the log prints what it excluded
# so the number is auditable.
FOREIGN_SCAN_DEPTH = 3


LOOSE_SUFFIX = "/."          # entry key for "the files sitting directly in here"


def loose_bytes(path):
    """Bytes of files directly in `path`, ignoring subdirectories.

    Needed because splitting a directory into its children stops measuring the
    directory itself, so every file sitting loose at that level silently vanished
    from the totals -- netscratch dropped from 102 TiB to 80 while its quota was
    going UP, which is how the bug surfaced. Uses a one-level `find` rather than
    `du -S`: it reads a single directory instead of recursing, so the correction is
    effectively free.
    """
    try:
        out = subprocess.run(
            ["find", path, "-maxdepth", "1", "-type", "f", "-printf", "%s\n"],
            capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return None
    total = 0
    for line in out.stdout.split():
        try:
            total += int(line)
        except ValueError:
            pass
    return total


def _foreign_subtrees(root, our_group):
    """{target_dir: bytes} for subtrees under `root` charged to another group."""
    try:
        out = subprocess.run(
            ["find", root, "-maxdepth", str(FOREIGN_SCAN_DEPTH), "-type", "d",
             "!", "-group", our_group, "-prune", "-print"],
            capture_output=True, text=True, timeout=1800).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or line == root:
            continue
        rel = os.path.relpath(line, root)
        size, ok = du(line)
        if size:
            found[rel] = found.get(rel, 0) + size
    return found


def _group_of(path):
    try:
        import grp
        return grp.getgrgid(os.stat(path).st_gid).gr_name
    except Exception:
        return None

# A directory that took longer than this last time gets split into its children on
# the next run. Wall clock for the whole crawl is bounded by the single slowest
# directory -- 32 shards cannot beat one 5.1-hour `du` -- so the only way to go
# faster is to stop treating that directory as one unit. Splitting is measured, not
# guessed: it uses the previous run's own timings, so it adapts as data moves.
SPLIT_ABOVE_SECONDS = 1800


def _slow_dirs(out_dir, root):
    """Directories to split: slow last time, OR already split last time.

    The second half matters. Splitting makes each child fast, so a purely
    timing-based rule un-splits the parent on the very next run, it goes slow again,
    and the scan oscillates between split and whole -- with the target list and the
    totals changing every night. Once a directory has been split it stays split.
    """
    slow = set()
    import glob as _g
    files = [os.path.join(out_dir, "dirscan.json")] + _g.glob(
        os.path.join(out_dir, "shards", "*.json"))
    for f in files:
        try:
            with open(f) as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        for r in doc.get("roots", []):
            if r.get("root") != root:
                continue
            for e in r.get("entries", []):
                name = e.get("dir") or ""
                if (e.get("seconds") or 0) > SPLIT_ABOVE_SECONDS:
                    slow.add(name)
                # Evidence of a previous split: keep it split.
                if "/" in name and not name.endswith(LOOSE_SUFFIX):
                    slow.add(name.rsplit("/", 1)[0])
    return slow


def _scan_targets(root, slow=()):
    """Names to measure, plus which are shared and which are loose-file stubs.

    Returns (targets, shared_names). A target ending in LOOSE_SUFFIX means "only the
    files directly inside this directory" -- the companion to splitting a directory
    into its children, so nothing at the parent level goes uncounted.
    """
    try:
        names = sorted(e.name for e in os.scandir(root) if e.is_dir(follow_symlinks=False))
    except OSError:
        raise
    targets, shared = [], set()
    for n in names:
        path = os.path.join(root, n)
        try:
            kids = sorted(e.name for e in os.scandir(path)
                          if e.is_dir(follow_symlinks=False))
        except OSError:
            kids = []
        # Order matters. A people container is expanded even if its name is
        # structural -- lab_storage/Lab is called "Lab" and really does hold ten
        # members' directories, so testing the name first would re-merge them into
        # one anonymous row and undo the attribution.
        if (len(_child_owners(path)) >= MIN_OWNERS_FOR_CONTAINER
                and kids and _is_people_container(path, kids)):
            targets += [f"{n}/{k}" for k in kids] + [n + LOOSE_SUFFIX]
            continue
        if n.lower() in STRUCTURAL_NAMES or (
                kids and len(_child_owners(path)) >= MIN_OWNERS_FOR_CONTAINER):
            shared.add(n)
        # Too slow as one unit last time: measure its children instead so the work
        # spreads across shards. Attribution is unaffected -- the children are still
        # this person's, and the display already folds a person's directories into
        # one row.
        if n in slow and kids:
            targets += [f"{n}/{k}" for k in kids] + [n + LOOSE_SUFFIX]
            if n in shared:
                shared.discard(n)
                shared.update(f"{n}/{k}" for k in kids)
                shared.add(n + LOOSE_SUFFIX)
            continue
        targets.append(n)
    return targets, shared


def scan_root(root, state, out_path, shard=0, shards=1, already=(), want_apparent=False,
              slow=()):
    try:
        names, shared = _scan_targets(root, slow)
    except OSError as exc:
        state["error"] = str(exc)
        return

    our_group = _group_of(root)
    foreign = _foreign_subtrees(root, our_group) if our_group else {}
    if foreign:
        state["foreign_group"] = our_group
        state["foreign_bytes_total"] = sum(foreign.values())
        print(f"  {len(foreign)} subtree(s) belong to another group, "
              f"{sum(foreign.values())/2**40:.2f} TiB excluded", flush=True)

    # total_dirs stays the FULL count in every shard, so a merged view can tell
    # whether the union is finished without knowing how the work was split.
    state["total_dirs"] = len(names)
    state["targets"] = names          # what exists RIGHT NOW, across all shards
    state["shard"] = f"{shard}/{shards}"
    # Shard by a hash of the NAME, never by position in the list. Each task lists
    # the tree itself and the tasks start hours apart, so a position-based slice
    # stops tiling the set the moment anything is added or removed in between --
    # that silently lost two directories while every shard reported success.
    # crc32 (not the built-in hash, which is randomised per process) keeps the
    # assignment identical across tasks and across reruns.
    names = [n for n in names
             if zlib.crc32(n.encode()) % shards == shard and n not in already]
    for name in names:
        path = os.path.join(root, name)
        t0 = time.time()
        # Check readability first, and cheaply -- opening the directory is O(1) while
        # `du` on an unreadable tree burns its whole timeout and then prints 0. That 0
        # used to be stored as a real measurement, so four private directories showed
        # up as people with no data at all instead of as gaps in the total.
        try:
            next(iter(os.scandir(path)), None)
        except PermissionError:
            state["entries"].append({
                "dir": name, "owner": owner_of(path), "bytes": None, "inodes": None,
                "bytes_apparent": None, "unreadable": True, "partial": True,
                "seconds": 0.0,
            })
            state["scanned"] = len(state["entries"])
            write(out_path)
            print(f"  {name:<24} UNREADABLE (private directory), not counted", flush=True)
            continue
        except OSError:
            pass
        if name.endswith(LOOSE_SUFFIX):
            # Only the files at this level; the subdirectories are separate targets.
            real = os.path.join(root, name[: -len(LOOSE_SUFFIX)])
            lb = loose_bytes(real)
            state["entries"].append({
                "dir": name, "owner": owner_of(real), "bytes": lb, "inodes": None,
                "bytes_allocated": None, "loose_level": True,
                "unreadable": lb is None, "partial": lb is None,
                "seconds": round(time.time() - t0, 1),
            })
            state["scanned"] = len(state["entries"])
            write(out_path)
            print(f"  {name:<24} loose files only: {lb}", flush=True)
            continue
        by, by_ok = du(path)
        # A directory we cannot enter (mode 0700 belonging to someone else) makes du
        # exit non-zero after printing 0. Storing that 0 as a real measurement hid two
        # members' data entirely: it counted as "measured", was skipped by resume, and
        # rendered as a person with no files. An unreadable directory is UNKNOWN, not
        # empty, and has to be reported as such.
        if not by_ok and not by:
            by = None
        # The inode pass is a SECOND full walk. On small trees the NFS attribute
        # cache makes it nearly free, which is why it was unconditional at first --
        # but on a multi-TiB directory the cache cannot hold the tree and the cost
        # simply doubles. One shard spent 3 hours on a single 3.65 TiB directory and
        # then timed out on this pass anyway, losing the whole shard. Bytes are what
        # the quota alert runs on, so past a threshold the file count is dropped
        # rather than risking the measurement that matters.
        elapsed = time.time() - t0
        ino, _ = du(path, inodes=True, timeout=1800) if elapsed < SLOW_DIR_SECONDS else (None, True)
        # Apparent size = the bytes in the files themselves, with no block rounding and
        # no protection overhead. Measuring it alongside allocated size is what turns
        # "the crawl says 107 TiB but the quota says 94" from an unexplained
        # contradiction into a number we can actually attribute.
        alloc, _ = du(path, allocated=True) if want_apparent else (None, True)
        # Subtract anything under this target that another group is charged for.
        foreign_here = sum(v for k, v in foreign.items()
                           if k == name or k.startswith(name + os.sep))
        if by is not None and foreign_here:
            by = max(by - foreign_here, 0)
        state["entries"].append({
            "dir": name,
            "foreign_bytes": foreign_here or None,
            # A shared store belongs to the lab, not to whoever happened to create
            # the folder. Naming a person here would both misplace the blame and
            # bury everyone else who put data in it.
            "owner": SHARED_OWNER if name in shared else owner_of(path),
            "bytes": by,
            "inodes": ino,
            "bytes_allocated": alloc,
            "unreadable": (not by_ok and by is None),
            "seconds": round(time.time() - t0, 1),
            "partial": not by_ok,
        })
        state["scanned"] = len(state["entries"])
        write(out_path)               # checkpoint after every directory
        print(f"  {name:<24} owner={state['entries'][-1]['owner']:<18} "
              f"bytes={by} inodes={ino} {state['entries'][-1]['seconds']}s", flush=True)
    state["complete"] = True


RESULT = {}


def write(out_path):
    tmp = out_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(RESULT, fh, indent=2)
    os.replace(tmp, out_path)


def refuse_on_login_node(force):
    """A tree walk on a login node is exactly what the cluster staff ask people not
    to do, and login nodes are recycled often enough that a long scan there would be
    killed halfway anyway. Inside any allocation -- sbatch, scrontab or srun --
    SLURM_JOB_ID is set, so its absence means this is an interactive shell."""
    if force or os.environ.get("SLURM_JOB_ID"):
        return
    sys.exit(
        "dirscan: refusing to crawl from outside a Slurm allocation.\n"
        "  This walks millions of files; on a login node it is both antisocial and\n"
        "  liable to be killed partway through.\n\n"
        "  Submit it instead:   sbatch ~/projects/labdash/dirscan.sbatch\n"
        "  Or for a quick test: srun -p shared -c 1 --mem=4G -t 30 python3.11 "
        "dirscan.py ...\n"
        "  (--force overrides, but you almost certainly want one of the above.)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="directory to write dirscan.json into")
    ap.add_argument("--roots", nargs="+", required=True)
    ap.add_argument("--force", action="store_true",
                    help="crawl even outside a Slurm allocation (don't)")
    # Crawling is embarrassingly parallel across top-level directories, so a job
    # array can split it. Useful ceiling is the DIRECTORY COUNT, not the core count:
    # with 57 directories, shard 58 and beyond simply get nothing, and wall clock
    # bottoms out at whatever the single slowest directory takes.
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--split-slow", action="store_true", default=True,
                    help="split directories that ran over SPLIT_ABOVE_SECONDS last run")
    ap.add_argument("--no-split-slow", dest="split_slow", action="store_false")
    ap.add_argument("--apparent", dest="apparent", action="store_true",
                    help="also measure allocated blocks (a second walk; doubles runtime)")
    ap.add_argument("--skip-existing", action="store_true",
                    help="skip directories already measured in a previous run")
    args = ap.parse_args()

    if args.shards < 1 or not 0 <= args.shard < args.shards:
        sys.exit(f"dirscan: shard {args.shard} is unreachable with --shards "
                 f"{args.shards} (need 0 <= shard < shards) -- it would measure "
                 f"nothing while appearing to succeed.")

    refuse_on_login_node(args.force)

    os.makedirs(args.out, exist_ok=True)
    if args.shards > 1:
        shard_dir = os.path.join(args.out, "shards")
        os.makedirs(shard_dir, exist_ok=True)
        # The job id is in the filename deliberately. Naming a shard file by index
        # alone means a later run overwrites the earlier one -- and a resume run,
        # which by design measures almost nothing, then replaces a full result with
        # an empty file. That destroyed a completed 204-directory crawl once.
        # Distinct names let the merge union old and new instead.
        run = ((os.environ.get("SLURM_ARRAY_JOB_ID") or os.environ.get("SLURM_JOB_ID")
                or "manual") + "a" + (os.environ.get("SLURM_RESTART_COUNT") or "0"))
        out_path = os.path.join(shard_dir, f"dirscan.{run}.{args.shard:03d}.json")
    else:
        out_path = os.path.join(args.out, "dirscan.json")

    # Merge rather than overwrite. Trees are scanned at very different costs -- one
    # takes minutes, netscratch takes hours -- so runs get split across jobs, and a
    # run that only covers two roots must not wipe the results for the other five.
    # It also means two jobs can never half-write each other's output.
    previous = []
    if args.shards == 1:
        try:
            with open(out_path) as fh:
                previous = json.load(fh).get("roots", [])
        except (OSError, json.JSONDecodeError):
            pass
    # Resume support: a run that lost shards to a time limit should re-measure only
    # what is missing, not repeat hours of finished work.
    done = {}
    if args.skip_existing:
        import glob as _glob
        for f in ([os.path.join(args.out, "dirscan.json")]
                  + _glob.glob(os.path.join(args.out, "shards", "*.json"))
                  + _glob.glob(os.path.join(args.out, "*", "dirscan.json"))):
            try:
                with open(f) as fh:
                    for r in json.load(fh).get("roots", []):
                        done.setdefault(r.get("root"), set()).update(
                            e["dir"] for e in r.get("entries", []) if e.get("bytes") is not None)
            except (OSError, json.JSONDecodeError):
                pass
        if done:
            print(f"resume: {sum(len(v) for v in done.values())} directories already "
                  f"measured, skipping them", flush=True)

    mine = set(args.roots)
    RESULT["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    RESULT["generated_local"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    RESULT["host"] = os.uname().nodename
    RESULT["roots"] = [r for r in previous if r.get("root") not in mine]
    if RESULT["roots"]:
        print(f"carrying forward {len(RESULT['roots'])} tree(s) from a previous run",
              flush=True)
    write(out_path)

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
        slow = _slow_dirs(args.out, root) if args.split_slow else set()
        if slow:
            print(f"  splitting {len(slow)} slow director{'y' if len(slow)==1 else 'ies'}: "
                  f"{sorted(slow)[:4]}", flush=True)
        scan_root(root, state, out_path, args.shard, args.shards,
                  done.get(root, set()), args.apparent, slow)
        state["seconds"] = round(time.time() - t0, 1)
        state["total_bytes"] = sum(e["bytes"] or 0 for e in state["entries"])
        state["total_inodes"] = sum(e["inodes"] or 0 for e in state["entries"])
        write(out_path)
        print(f"[{datetime.now():%H:%M:%S}] {root}: {state['scanned']} dirs, "
              f"{state['total_bytes'] / 2**40:.2f} TiB, {state['seconds']}s", flush=True)

    RESULT["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write(out_path)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
