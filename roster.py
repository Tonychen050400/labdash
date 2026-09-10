#!/usr/bin/env python3
"""roster - build the username -> email CSV skeleton, sorted so the manual work stops early.

There is no way to derive a Harvard email from a cluster username: mine is
`haoyuchen54` but my mail lands at `haoyuchen@`, so `<user>@fas.harvard.edu` would
misdeliver. `getent passwd` gives the full NAME, which is what you need to look
someone up in Outlook, but never the address. So the mapping has to be typed once
by a human.

This makes that job as small as it can be. Every member of the lab groups goes in,
but the rows are ordered by how much cluster they actually use, and each row carries
that usage. Fill in emails from the top and stop when the names stop mattering --
most of a lab roster has no jobs and no data and never needs a nudge.

  python3 roster.py --out DIR                 # writes roster.csv (keeps existing emails)
  python3 roster.py --out DIR --active-only   # skip people with no jobs and no data
"""

import argparse
import csv
import json
import os
import subprocess
import sys


def run(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def members(group):
    out = run(["getent", "group", group])
    if not out:
        return []
    parts = out.strip().split(":")
    return [u for u in parts[3].split(",") if u] if len(parts) >= 4 else []


def full_name(user):
    """GECOS field. This is the string to paste into the Outlook directory."""
    out = run(["getent", "passwd", user])
    if not out:
        return ""
    f = out.strip().split(":")
    return f[4].split(",")[0].strip() if len(f) > 4 else ""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.environ.get(
        "LABDASH_OUT", "/n/holylabs/LABS/kempner_ydu_lab/Lab/labdash"))
    ap.add_argument("--groups", default="ydu_lab,kempner_ydu_lab")
    ap.add_argument("--active-only", action="store_true")
    args = ap.parse_args()

    groups = [g for g in args.groups.split(",") if g]
    roster = {}
    for g in groups:
        for u in members(g):
            roster.setdefault(u, set()).add(g)

    snap = {}
    snap_path = os.path.join(args.out, "snapshot.json")
    try:
        with open(snap_path) as fh:
            snap = json.load(fh)
    except (OSError, json.JSONDecodeError):
        print(f"roster: no snapshot at {snap_path}, usage columns will be empty",
              file=sys.stderr)
    usage = {u["user"]: u for u in snap.get("users", [])}

    # Preserve any emails already typed in, so re-running never destroys work.
    csv_path = os.path.join(args.out, "roster.csv")
    known = {}
    try:
        with open(csv_path) as fh:
            for row in csv.DictReader(fh):
                if row.get("email", "").strip():
                    known[row["username"]] = row["email"].strip()
    except (OSError, KeyError):
        pass

    rows = []
    for user, gs in roster.items():
        u = usage.get(user, {})
        gpu30 = u.get("gpu_hours_30d", 0) or 0
        disk = u.get("storage_bytes", 0) or 0
        running = u.get("running_gpus", 0) or 0
        if args.active_only and not (gpu30 or disk or running):
            continue
        rows.append({
            "username": user,
            "full_name": full_name(user),
            "email": known.get(user, ""),
            "groups": "+".join(sorted(gs)),
            "gpu_hours_30d": int(gpu30),
            "storage_gib": int(disk / 1024**3),
            "gpus_running_now": running,
        })

    rows.sort(key=lambda r: (-r["gpu_hours_30d"], -r["storage_gib"], r["username"]))

    fields = ["username", "full_name", "email", "groups",
              "gpu_hours_30d", "storage_gib", "gpus_running_now"]
    tmp = csv_path + ".tmp"
    with open(tmp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, csv_path)

    filled = sum(1 for r in rows if r["email"])
    active = sum(1 for r in rows if r["gpu_hours_30d"] or r["storage_gib"])
    print(f"roster: {len(rows)} people -> {csv_path}")
    print(f"  {active} used the cluster in the last 30 days or hold data")
    print(f"  {len(rows) - active} are dormant - they can stay blank")
    print(f"  {filled} emails already filled in")


if __name__ == "__main__":
    main()
