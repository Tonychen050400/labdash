#!/usr/bin/env python3
"""labdash - lab GPU + storage dashboard for FASRC Cannon.

Collects Slurm queue/accounting data and VAST quota data for a set of lab
accounts, then renders a single self-contained HTML page (data inlined, no
network calls, no build step, stdlib only).

  python3 labdash.py                      # write snapshot.json + index.html
  python3 labdash.py --out /path/to/dir   # somewhere the lab can read
  python3 labdash.py --json               # print the snapshot, render nothing

Configure with LABDASH_ACCOUNTS / LABDASH_PATHS or the flags below.
"""

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

if sys.version_info < (3, 7):
    sys.exit("labdash: needs Python >= 3.7 (the login node has /usr/bin/python3.11).")

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

DEFAULT_PATHS = ["/n/netscratch", "/n/holylabs"]
# Directories whose allocation only shows up in df when you ask about the directory
# itself. "{group}" is filled in per lab group.
DEFAULT_DIR_QUOTAS = ["/n/lab_storage/{group}"]
# The folders the lab actually thinks in. Every one gets a per-user table, and a
# folder with no scan yet is shown as pending rather than left out -- a missing
# table reads as "nothing here" when it means "not measured yet".
DEFAULT_FOLDERS = ["/n/holylabs/LABS/{group}", "/n/netscratch/{group}",
                   "/n/lab_storage/{group}"]
HISTORY_KEEP = 4032  # ~4 weeks at one sample per 10 min
TOP_ROWS = 15        # leaderboard rows shown before the tail is collapsed

# --------------------------------------------------------------------------
# shell helpers
# --------------------------------------------------------------------------


# FASRC's own `quota` script has a `#!/usr/bin/env python3.11` shebang, and
# python3.11 was removed from the cluster -- so the tool now dies with
# "/usr/bin/env: 'python3.11': No such file or directory" on every node. That took
# the group-quota cards off the dashboard silently, because a failed command just
# returned an empty string. Invoke it through an interpreter that exists instead of
# trusting its shebang, and resolve that once.
_PY_CANDIDATES = ("python3.12", "python3.11", "python3.13", "python3")


def _working_python():
    for name in _PY_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def quota_cmd(*args):
    """Argv for FASRC's quota tool, immune to its broken shebang."""
    script = shutil.which("quota") or "/usr/local/bin/quota"
    py = _working_python()
    return ([py, script] + list(args)) if py else [script] + list(args)


def run(cmd, timeout=60):
    """Run a command, return stdout ('' on any failure). Never raises."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


_SIZE_RE = re.compile(r"^([0-9.]+)\s*([KMGTPE]?)i?B?$", re.I)
_MULT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}


def parse_size(s):
    """'98.3Ti' / '42286G' / '66916K' / '0K' / '-' -> bytes (None if unknown)."""
    s = (s or "").strip()
    if not s or s == "-":
        return None
    m = _SIZE_RE.match(s)
    if not m:
        return None
    return int(float(m.group(1)) * _MULT[m.group(2).upper()])


def parse_count(s):
    """'23579k' / '1161' / '-' -> int (None if unknown)."""
    s = (s or "").strip().replace(",", "")
    if not s or s == "-":
        return None
    mult = 1
    if s[-1].lower() in "km":
        mult = 1000 if s[-1].lower() == "k" else 1000_000
        s = s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return None


def parse_hms(s):
    """Slurm elapsed 'D-HH:MM:SS' / 'HH:MM:SS' / 'MM:SS' -> seconds."""
    s = (s or "").strip()
    if not s or s in ("N/A", "UNLIMITED", "INVALID"):
        return None
    days = 0
    if "-" in s:
        d, _, s = s.partition("-")
        days = int(d)
    parts = [int(x) for x in s.split(":")] if s else [0]
    while len(parts) < 3:
        parts.insert(0, 0)
    return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_tres(s):
    """'cpu=16,mem=240G,node=1,gres/gpu=1,gres/gpu:nvidia_h200=1' -> parsed dict."""
    out = {"cpus": 0, "mem_bytes": 0, "nodes": 0, "gpus": 0, "gpu_model": None}
    for item in (s or "").split(","):
        key, _, val = item.partition("=")
        key, val = key.strip(), val.strip()
        if key == "cpu":
            out["cpus"] = int(float(val or 0))
        elif key == "node":
            out["nodes"] = int(float(val or 0))
        elif key == "mem":
            out["mem_bytes"] = parse_size(val) or 0
        elif key == "gres/gpu":
            out["gpus"] = int(float(val or 0))
        elif key.startswith("gres/gpu:"):
            model = key.split(":", 1)[1]
            out["gpu_model"] = model
            if not out["gpus"]:
                out["gpus"] = int(float(val or 0))
    return out


PRETTY_GPU = [
    ("h200", "H200"),
    ("h100", "H100"),
    ("a100", "A100"),
    ("rtx_pro_6000", "RTX PRO 6000"),
    ("l40", "L40S"),
    ("v100", "V100"),
    ("a40", "A40"),
]


def pretty_gpu(model):
    if not model:
        return "unspecified"
    low = model.lower()
    for needle, label in PRETTY_GPU:
        if needle in low:
            return label
    return model.replace("nvidia_", "").replace("_", " ")


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


def discover_accounts():
    """Slurm accounts this user can charge to."""
    out = run(["sacctmgr", "-nP", "show", "assoc", f"user={os.environ.get('USER','')}",
               "format=Account"])
    seen, accts = set(), []
    for line in out.splitlines():
        a = line.strip().strip("|")
        if a and a not in seen:
            seen.add(a)
            accts.append(a)
    # Lead with the user's primary Unix group so the page is named after the lab
    # people actually call it, not whichever association Slurm listed first.
    primary = run(["id", "-gn"]).strip()
    if primary in seen:
        accts.remove(primary)
        accts.insert(0, primary)
    return accts


def group_members(group):
    out = run(["getent", "group", group])
    if not out:
        return []
    parts = out.strip().split(":")
    return [u for u in parts[3].split(",") if u] if len(parts) >= 4 else []


# --------------------------------------------------------------------------
# collectors
# --------------------------------------------------------------------------

JOB_FIELDS = [
    "JobID", "UserName", "Account", "Partition", "tres-alloc",
    "TimeUsed", "TimeLimit", "NodeList", "Name", "StateCompact", "Reason",
]


def collect_jobs(accounts):
    """Live queue. Array tasks are expanded (-r) so counts are per-GPU-slot."""
    fmt = ",".join(f"{f}:|" for f in JOB_FIELDS)
    out = run(["squeue", "-A", ",".join(accounts), "-r", "-h", "-O", fmt], timeout=90)
    running, pending, other = [], [], []
    for line in out.splitlines():
        cols = line.split("|")
        if len(cols) < len(JOB_FIELDS):
            continue
        cols = [c.strip() for c in cols]
        rec = dict(zip(JOB_FIELDS, cols))
        tres = parse_tres(rec["tres-alloc"])
        job = {
            "jobid": rec["JobID"],
            "user": rec["UserName"],
            "account": rec["Account"],
            "partition": rec["Partition"],
            "state": rec["StateCompact"],
            "name": rec["Name"],
            "nodelist": rec["NodeList"],
            "reason": rec["Reason"] if rec["Reason"] != "None" else "",
            "elapsed_s": parse_hms(rec["TimeUsed"]),
            "limit_s": parse_hms(rec["TimeLimit"]),
            "gpus": tres["gpus"],
            "cpus": tres["cpus"],
            "mem_bytes": tres["mem_bytes"],
            "nodes": tres["nodes"],
            "gpu_model": pretty_gpu(tres["gpu_model"]),
        }
        if job["state"] == "R":
            running.append(job)
        elif job["state"] == "PD":
            pending.append(job)
        else:
            other.append(job)
    running.sort(key=lambda j: (-(j["gpus"] or 0), -(j["elapsed_s"] or 0)))
    return {"running": running, "pending": pending, "other": other}


def collect_sreport(accounts, days):
    """GPU-hours charged per user per account over the last `days`."""
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out = run(["sreport", "-nP", "cluster", "AccountUtilizationByUser",
               f"account={','.join(accounts)}", f"start={start}", "end=now",
               "-t", "hours", "--tres=gres/gpu"], timeout=180)
    per_user, per_account = {}, {}
    for line in out.splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 6 or cols[1] not in accounts:
            continue
        try:
            used = float(cols[5])
        except ValueError:
            continue
        acct, user = cols[1], cols[2]
        if not user:  # account rollup row
            per_account[acct] = used
        else:
            per_user.setdefault(user, {})[acct] = used
    return {"by_user": per_user, "by_account": per_account, "days": days}


def collect_fairshare(accounts):
    out = run(["sshare", "-nP", "-A", ",".join(accounts),
               "-o", "Account,User,RawShares,NormShares,RawUsage,EffectvUsage,FairShare,LevelFS"])
    res = {}
    for line in out.splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 8 or cols[1]:  # only account-level rows (empty User)
            continue
        acct = cols[0].strip()
        if acct not in accounts:
            continue

        def num(x):
            try:
                return float(x)
            except ValueError:
                return None

        res[acct] = {
            "raw_shares": cols[2],
            "norm_shares": num(cols[3]),
            "raw_usage": num(cols[4]),
            "effective_usage": num(cols[5]),
            "level_fs": num(cols[7]),
        }
    return res


def collect_quota(group, path):
    """Group quota on one filesystem."""
    out = run(quota_cmd("-g", group, path), timeout=60)
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 3 and cols[0].startswith("/"):
            return {
                "group": group,
                "path": path,
                "used_bytes": parse_size(cols[1]),
                "quota_bytes": parse_size(cols[2]),
                "files": parse_count(cols[3]) if len(cols) > 3 else None,
                "file_quota": parse_count(cols[4]) if len(cols) > 4 else None,
                "updated": _quota_stamp(out),
                "source": "quota",
            }
    return None


def _df_bytes(path):
    """(size, used) in bytes, or None."""
    out = run(["df", "-B1", "--output=size,used", path], timeout=60)
    rows = [r for r in out.splitlines()[1:] if r.strip()]
    if not rows:
        return None
    try:
        size, used = (int(x) for x in rows[0].split()[:2])
        return size, used
    except (ValueError, IndexError):
        return None


def collect_dir_quota(path, group):
    """Fileset quota for a directory on a filesystem that has no per-group quota.

    Isilon reports a *fileset's* limit through df, but only when you ask about the
    directory itself: `/n/lab_storage` answers for the whole 42 PB array while
    `/n/lab_storage/ydu_lab` answers 100 TiB. That difference is also the test for
    whether a real quota exists -- if the directory reports the same numbers as its
    mount point, there is no fileset limit and the figure would be meaningless.
    """
    here = _df_bytes(path)
    if not here:
        return None
    mount = run(["df", "--output=target", path], timeout=60).splitlines()
    mount = mount[1].strip() if len(mount) > 1 else None
    if mount and mount != path:
        top = _df_bytes(mount)
        if top and top[0] == here[0]:
            return None          # same as the whole array: not a real allocation
    size, used = here
    return {
        "group": group,
        "path": path,
        "used_bytes": used,
        "quota_bytes": size,
        "files": None,
        "file_quota": None,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "df-fileset",
    }


def _quota_stamp(out):
    m = re.search(r"Last updated\s+([0-9-]+\s+[0-9:]+)", out)
    return m.group(1) if m else None


def collect_quota_by_user(groups, path):
    """Per-user space on one filesystem, for everyone in the given groups.

    IMPORTANT semantics, verified against the tool: `quota --group-user-usage G P`
    reports each user's total on filesystem P *filesystem-wide*, not the slice that
    counts against G's quota -- passing three different groups returns byte-identical
    rows for a shared member. The group argument only selects which users to list.
    So these numbers must never be presented as a decomposition of the group quota;
    they are "how much this person holds here", and the sum can legitimately exceed
    the lab's quota when people also store under another lab's tree.
    """
    merged, stamp = {}, None
    for group in groups:
        out = run(quota_cmd("--group-user-usage", group, path), timeout=90)
        if "User" not in out:
            continue
        stamp = stamp or _quota_stamp(out)
        started = False
        for line in out.splitlines():
            if line.startswith("---"):
                started = True
                continue
            if not started:
                continue
            cols = line.split()
            if len(cols) < 3:
                continue
            rec = merged.setdefault(
                cols[0], {"user": cols[0], "bytes": None, "files": None, "groups": []})
            rec["bytes"] = max(rec["bytes"] or 0, parse_size(cols[1]) or 0)
            rec["files"] = max(rec["files"] or 0, parse_count(cols[2]) or 0)
            if group not in rec["groups"]:
                rec["groups"].append(group)
    if not merged:
        return None
    rows = sorted(merged.values(), key=lambda r: -(r["bytes"] or 0))
    return {"path": path, "groups": list(groups), "updated": stamp, "rows": rows}


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------


def build_snapshot(accounts, paths):
    t0 = time.time()
    jobs = collect_jobs(accounts)
    groups = [a for a in accounts if group_members(a)]

    storage, storage_by_user = [], []
    folders = []
    for tmpl in DEFAULT_FOLDERS:
        for g in groups:
            path = tmpl.format(group=g)
            if os.path.isdir(path):
                folders.append(path)

    for tmpl in DEFAULT_DIR_QUOTAS:
        for g in groups:
            dq = collect_dir_quota(tmpl.format(group=g), g)
            if dq:
                storage.append(dq)

    for p in paths:
        quotas = [q for g in groups
                  if (q := collect_quota(g, p)) and q["used_bytes"] is not None]
        if not quotas:
            continue
        storage.extend(quotas)
        # One scan per filesystem, not per group -- the rows are group-independent.
        by_user = collect_quota_by_user(groups, p)
        if by_user and by_user["rows"]:
            by_user["group_used_bytes"] = sum(q["used_bytes"] for q in quotas)
            by_user["group_quota_bytes"] = sum(q["quota_bytes"] or 0 for q in quotas)
            storage_by_user.append(by_user)

    # Only now, once every real reading has been attempted, fill the gaps with
    # visible placeholders. Adding them earlier duplicated every location, because
    # the real collection appends afterwards. A quota that cannot be read has to
    # appear as unreadable rather than as nothing: three cards silently vanishing is
    # what let a broken FASRC tool go unnoticed for four days.
    for g in groups:
        for pth in paths:
            if not any(q["group"] == g and q["path"] == pth for q in storage):
                storage.append({"group": g, "path": pth, "used_bytes": None,
                                "quota_bytes": None, "files": None,
                                "file_quota": None, "updated": None,
                                "source": "unavailable"})

    # per-user live compute rollup
    users = {}
    for j in jobs["running"]:
        u = users.setdefault(j["user"], _blank_user())
        u["running_jobs"] += 1
        u["running_gpus"] += j["gpus"]
        u["running_cpus"] += j["cpus"]
        u["gpu_seconds_inflight"] += (j["gpus"] or 0) * (j["elapsed_s"] or 0)
    for j in jobs["pending"]:
        u = users.setdefault(j["user"], _blank_user())
        u["pending_jobs"] += 1
        u["pending_gpus"] += j["gpus"]

    sr7 = collect_sreport(accounts, 7)
    sr30 = collect_sreport(accounts, 30)
    for user, per_acct in sr7["by_user"].items():
        users.setdefault(user, _blank_user())["gpu_hours_7d"] = sum(per_acct.values())
    for user, per_acct in sr30["by_user"].items():
        users.setdefault(user, _blank_user())["gpu_hours_30d"] = sum(per_acct.values())

    storage_lookup = {}
    for block in storage_by_user:
        for row in block["rows"]:
            key = row["user"]
            storage_lookup.setdefault(key, 0)
            storage_lookup[key] += row["bytes"] or 0
    for user, total in storage_lookup.items():
        users.setdefault(user, _blank_user())["storage_bytes"] = total

    for user, rec in users.items():
        rec["user"] = user

    gpu_models = {}
    for j in jobs["running"]:
        if j["gpus"]:
            gpu_models[j["gpu_model"]] = gpu_models.get(j["gpu_model"], 0) + j["gpus"]

    partitions = {}
    for j in jobs["running"]:
        partitions.setdefault(j["partition"], {"running_gpus": 0, "pending_gpus": 0})
        partitions[j["partition"]]["running_gpus"] += j["gpus"]
    for j in jobs["pending"]:
        partitions.setdefault(j["partition"], {"running_gpus": 0, "pending_gpus": 0})
        partitions[j["partition"]]["pending_gpus"] += j["gpus"]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_local": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "collect_seconds": round(time.time() - t0, 1),
        "host": os.uname().nodename,
        "cluster": "Cannon",
        "accounts": accounts,
        "groups": groups,
        "jobs": jobs,
        "users": sorted(users.values(), key=lambda u: -u["running_gpus"]),
        "gpu_models": gpu_models,
        "partitions": partitions,
        "sreport_7d": sr7,
        "sreport_30d": sr30,
        "fairshare": collect_fairshare(accounts),
        "storage": storage,
        "storage_by_user": storage_by_user,
        "folders": folders,
        "totals": {
            "running_jobs": len(jobs["running"]),
            "pending_jobs": len(jobs["pending"]),
            "running_gpus": sum(j["gpus"] for j in jobs["running"]),
            "pending_gpus": sum(j["gpus"] for j in jobs["pending"]),
            "active_users": len({j["user"] for j in jobs["running"]}),
            "queued_users": len({j["user"] for j in jobs["pending"]}),
        },
    }


def _blank_user():
    return {
        "running_jobs": 0, "running_gpus": 0, "running_cpus": 0,
        "pending_jobs": 0, "pending_gpus": 0, "gpu_seconds_inflight": 0,
        "gpu_hours_7d": 0.0, "gpu_hours_30d": 0.0, "storage_bytes": 0,
    }


def append_history(snap, path):
    """One compact line per run, so the page can show trend not just level."""
    rec = {
        "t": snap["generated_at"],
        "running_gpus": snap["totals"]["running_gpus"],
        "pending_gpus": snap["totals"]["pending_gpus"],
        "storage": {f"{s['group']}@{s['path']}": s["used_bytes"] for s in snap["storage"]},
    }
    try:
        with open(path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        return []
    try:
        with open(path) as fh:
            lines = fh.readlines()
    except OSError:
        return []
    if len(lines) > HISTORY_KEEP:
        lines = lines[-HISTORY_KEEP:]
        try:
            with open(path, "w") as fh:
                fh.writelines(lines)
        except OSError:
            pass
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


def fmt_bytes(n):
    if n is None:
        return "--"
    for unit, size in (("PiB", 1024**5), ("TiB", 1024**4), ("GiB", 1024**3), ("MiB", 1024**2)):
        if n >= size:
            v = n / size
            return f"{v:,.1f} {unit}" if v < 100 else f"{v:,.0f} {unit}"
    return f"{n / 1024:,.0f} KiB"


def fmt_int(n):
    return "--" if n is None else f"{n:,.0f}"


def fmt_dur(sec):
    if not sec:
        return "--"
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    return f"{h}h {m:02d}m" if h else f"{m}m"


def esc(s):
    return html.escape(str(s), quote=True)


def sev(pct):
    """Quota severity -> semantic token name. Separate from the accent hue."""
    if pct is None:
        return "unknown"
    if pct >= 95:
        return "crit"
    if pct >= 85:
        return "warn"
    return "good"


SEV_LABEL = {"crit": "Full", "warn": "Tight", "good": "Healthy", "unknown": "Unknown"}


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------


def bar(frac, tone="accent"):
    """A single magnitude bar. Length is the encoding; hue never varies by rank."""
    w = max(0.0, min(1.0, frac or 0.0)) * 100
    return (f'<span class="bar bar--{tone}"><span class="bar__fill" '
            f'style="width:{w:.2f}%"></span></span>')


def meter(used, quota):
    pct = (used / quota * 100) if (used and quota) else 0
    tone = sev(pct)
    w = max(0.0, min(100.0, pct))
    return (f'<div class="meter meter--{tone}"><div class="meter__fill" '
            f'style="width:{w:.2f}%"></div>'
            f'<div class="meter__mark" style="left:85%"></div>'
            f'<div class="meter__mark" style="left:95%"></div></div>')


def _span_label(window):
    """How much wall-clock the history actually covers, for an honest chart label."""
    try:
        t0 = datetime.fromisoformat(window[0]["t"])
        t1 = datetime.fromisoformat(window[-1]["t"])
    except (KeyError, ValueError, IndexError):
        return f"{len(window)} samples"
    hours = (t1 - t0).total_seconds() / 3600
    if hours < 48:
        return f"last {hours:.0f} h"
    return f"last {hours/24:.0f} d"


def sparkline(points, width=260, height=44):
    """Storage-over-time. Area fill, emphasized endpoint, no axis furniture."""
    vals = [p for p in points if p is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    pad = 3
    n = len(vals)
    xs = [pad + i * (width - 2 * pad) / (n - 1) for i in range(n)]
    ys = [height - pad - (v - lo) / span * (height - 2 * pad) for v in vals]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    area = f"{xs[0]:.1f},{height} " + line + f" {xs[-1]:.1f},{height}"
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" aria-hidden="true" preserveAspectRatio="none">'
        f'<polygon class="spark__area" points="{area}"/>'
        f'<polyline class="spark__line" points="{line}"/>'
        f'<circle class="spark__dot" cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="3"/>'
        f"</svg>"
    )


def render(snap, history, fragment=False, dirscan=None, gpu_guide=None):
    """Full standalone document by default; body-only when publishing as an Artifact
    (the Artifact host supplies its own doctype/head/body skeleton)."""
    worst = None
    for s in snap["storage"]:
        pct = (s["used_bytes"] / s["quota_bytes"] * 100) if s["quota_bytes"] else 0
        if worst is None or pct > worst[1]:
            worst = (s, pct)

    body = "\n".join([
        _masthead(snap, worst),
        _rules_section(snap, gpu_guide),
        _storage_section(snap, history),
        _dirscan_section(dirscan, snap.get("folders") or [], snap),
        _compute_section(snap, gpu_guide),
        _jobs_section(snap),
        _footer(snap),
        f'<script type="application/json" id="snapshot">{json.dumps(snap)}</script>',
        _script(),
    ])
    head = f"<title>{esc(_page_title(snap))}</title>\n<style>{CSS}</style>"
    if fragment:
        return head + "\n" + body
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Live GPU queue, fairshare standing and quota headroom for {esc(' + '.join(snap['accounts']))} on FASRC {esc(snap['cluster'])}.">
{head}
</head>
<body>
{body}
</body>
</html>"""


def _page_title(snap):
    label = snap["groups"][0] if snap["groups"] else "Lab"
    return f"{label} Cluster Ledger"


def _masthead(snap, worst):
    t = snap["totals"]
    s, pct = worst if worst else (None, None)
    tiles = [
        _tile("GPUs running", fmt_int(t["running_gpus"]),
              f"across {fmt_int(t['running_jobs'])} jobs / {fmt_int(t['active_users'])} people"),
        _tile("GPU slots queued", fmt_int(t["pending_gpus"]),
              f"{fmt_int(t['queued_users'])} people waiting", tone="warn" if t["pending_gpus"] > 200 else None),
    ]
    if s:
        tiles.append(_tile(
            f"{s['group']} on {s['path'].split('/')[-1]}",
            f"{pct:.1f}%",
            f"{fmt_bytes(s['used_bytes'])} of {fmt_bytes(s['quota_bytes'])}",
            tone=sev(pct)))
    return f"""<header class="masthead">
  <div class="masthead__id">
    <p class="eyebrow">FASRC {esc(snap['cluster'])} &middot; {esc(' + '.join(snap['accounts']))}</p>
    <h1>Cluster Ledger</h1>
    <p class="lede">Where the lab's GPU hours and disk quota are actually going, so the
      credit and storage conversations can start from numbers.</p>
  </div>
  <div class="masthead__meta">
    <p class="refreshed" data-at="{esc(snap['generated_at'])}">
      <span class="refreshed__rel">just now</span>
      <span class="refreshed__abs">{esc(snap['generated_local'])} &middot; refreshes every 15 min</span>
    </p>
    <div class="tiles">{''.join(tiles)}</div>
  </div>
</header>"""


def _tile(label, value, sub, tone=None):
    cls = f" tile--{tone}" if tone else ""
    return (f'<div class="tile{cls}"><p class="tile__label">{esc(label)}</p>'
            f'<p class="tile__value">{esc(value)}</p>'
            f'<p class="tile__sub">{esc(sub)}</p></div>')



def _rules_section(snap, guide):
    """House rules, at the top, each one carrying the number that justifies it.

    Everything here was already somewhere on this page. It is repeated at the top
    because the page is long and the two cheapest habits -- checkpointing onto a
    requeue partition, and not asking for an H100 by reflex -- were being missed by
    people who never scrolled to the Compute section. Numbers come from the live data
    rather than being written in, so a rule cannot quietly become false when FASRC
    reprices a partition.
    """
    rules = []

    # 1. preemptible vs dedicated, the single biggest lever
    parts = (guide or {}).get("partitions") or []
    pre = [p for p in parts if p.get("preemptible") and p.get("gpu_price")]
    ded_h = [p for p in parts if not p.get("preemptible")
             and any("H100" in m["label"] for m in p.get("models", []))]
    if pre and ded_h:
        ratio = ded_h[0]["gpu_price"] / pre[0]["gpu_price"]
        rules.append((
            "If your job can checkpoint, submit to a <code>*_requeue</code> partition",
            f"{ratio:.1f}&times; cheaper",
            f"The same H100 costs {pre[0]['gpu_price']:.0f} per GPU-hour there against "
            f"{ded_h[0]['gpu_price']:.0f} on <code>{esc(ded_h[0]['partition'])}</code>. "
            f"Identical hardware &mdash; you are paying for the right not to be "
            f"interrupted."))

    # 2. card choice for work that never synchronises
    rtx = next((p for p in parts if p["partition"] == "kempner_rtx"), None)
    if rtx:
        rules.append((
            "No cross-GPU communication? Ask for <code>kempner_rtx</code>",
            "96 GiB per card",
            "Sweeps, one seed per GPU, eval fleets and data generation never touch the "
            "interconnect. It is the cheapest dedicated partition and holds more memory "
            "per card than the 80 GiB parts. Gradient sync (DDP, FSDP, ZeRO) is the "
            "exception &mdash; those nodes reach a neighbour at only 53 GB/s."))

    # 3. fairshare, using the lab's own standing
    st = []
    for acct, f in (snap.get("fairshare") or {}).items():
        if f.get("effective_usage") and f.get("norm_shares"):
            st.append((f["effective_usage"] / f["norm_shares"], acct))
    if st:
        st.sort(reverse=True)
        rules.append((
            "Hours you burn now slow everyone's queue later",
            f"{st[0][0]:.1f}&times; our share",
            f"There is no hard GPU-hour budget here; the cost is fairshare. "
            f"<code>{esc(st[0][1])}</code> has drawn {st[0][0]:.1f}&times; its slice, so "
            f"new jobs from the whole account queue behind lighter users."))

    # 4. the storage rule people get wrong
    worst = None
    for q in snap.get("storage", []):
        if q.get("quota_bytes"):
            pct = q["used_bytes"] / q["quota_bytes"] * 100
            if worst is None or pct > worst[0]:
                worst = (pct, q)
    if worst:
        rules.append((
            "Check the quota, never <code>df</code>",
            f"{worst[0]:.0f}% full",
            f"<code>df</code> reports the whole shared array and says everything is "
            f"fine while we sit at our own ceiling. Ours: "
            f"<code>quota -g {esc(worst[1]['group'])} {esc(worst[1]['path'])}</code> "
            f"&mdash; {esc(fmt_bytes(worst[1]['quota_bytes'] - worst[1]['used_bytes']))} "
            f"left. Every allocation also has a separate file-count limit."))

    # 5. scratch is scratch
    rules.append((
        "<code>/n/netscratch</code> is scratch, not storage",
        "90-day purge",
        "Files there are deleted after 90 days and are not backed up. Keep only what "
        "can be regenerated or re-downloaded; a directory has vanished from under this "
        "lab before."))

    # 6. the JDS rules, which arrive as email if ignored
    rules.append((
        "Request the cores you will use, and use the GPU you hold",
        "FASRC emails you",
        "Job Defense Shield mails the lab when a job takes 4 cores and uses 1, or holds "
        "a GPU at 0% utilisation. Both waste fairshare that everyone shares. "
        "<code>jobstats &lt;jobid&gt;</code> shows what a job actually used."))

    items = []
    for n, (title, badge, body) in enumerate(rules, 1):
        items.append(f"""<article class="rule">
  <p class="rule__n">{n}</p>
  <div class="rule__body">
    <h4>{title}</h4>
    <p>{body}</p>
  </div>
  <span class="rule__badge">{badge}</span>
</article>""")

    return f"""<section class="section" id="rules">
  <header class="section__head">
    <h2>How to use the cluster well</h2>
    <p>Six habits, each with the number from this page that justifies it. The two at
      the top are worth more than everything else combined.</p>
  </header>
  <div class="rules">{''.join(items)}</div>
</section>"""


def _storage_section(snap, history):
    if not snap["storage"]:
        return ""
    cards = []
    # A location whose quota could not be read gets its own card saying so. It sorts
    # last but it is never dropped: three cards silently vanishing is what let a
    # broken FASRC tool go unnoticed for four days.
    unavailable = [q for q in snap["storage"] if not q.get("quota_bytes")]
    ordered = sorted((q for q in snap["storage"] if q.get("quota_bytes")),
                     key=lambda q: -(q["used_bytes"] / q["quota_bytes"] * 100))
    for u in unavailable:
        cards.append(f"""<article class="card">
  <header class="card__head">
    <div><p class="eyebrow">{esc(u['path'])}</p><h3>{esc(u['group'])}</h3></div>
    <span class="pill pill--unknown">unreadable</span>
  </header>
  <p class="figure figure--unknown">&mdash;</p>
  <p class="stamp">The quota could not be read for this group here. Everything else on
    this page is unaffected; only this figure is missing.</p>
</article>""")
    for s in ordered:
        pct = (s["used_bytes"] / s["quota_bytes"] * 100) if s["quota_bytes"] else None
        tone = sev(pct)
        key = f"{s['group']}@{s['path']}"
        window = history[-HISTORY_KEEP:] if history else []
        series = [h["storage"].get(key) for h in window]
        # Only draw a trend once there is enough history to mean something, and label
        # it with the span actually covered rather than the retention window.
        spark, spark_label = "", ""
        if len([v for v in series if v]) >= 6:
            spark = sparkline(series)
            spark_label = f"Trend, {_span_label(window)}"
        free = (s["quota_bytes"] - s["used_bytes"]) if s["quota_bytes"] else None
        # Two different things wear the same card. A group quota follows FILE
        # OWNERSHIP across the whole filesystem -- kempner_ydu_lab is charged
        # 254 GiB on /n/netscratch while no such directory exists, because the
        # files sit in other people's trees with that group set. A fileset quota
        # is genuinely about one directory. Saying which is which on the card.
        scope = ("files owned by this group, anywhere on the filesystem"
                 if s.get("source") != "df-fileset"
                 else "this directory's own allocation")
        files_pct = (s["files"] / s["file_quota"] * 100) if s.get("file_quota") else None
        cards.append(f"""<article class="card">
  <header class="card__head">
    <div>
      <p class="eyebrow">{esc(s['path'])}</p>
      <h3>{esc(s['group'])}</h3>
      <p class="scope">{scope}</p>
    </div>
    <span class="pill pill--{tone}">{SEV_LABEL[tone]}</span>
  </header>
  <p class="figure figure--{tone}">{pct:.1f}<span class="figure__unit">%</span></p>
  {meter(s['used_bytes'], s['quota_bytes'])}
  <dl class="kv">
    <div><dt>Used</dt><dd>{esc(fmt_bytes(s['used_bytes']))}</dd></div>
    <div><dt>Free</dt><dd>{esc(fmt_bytes(free))}</dd></div>
    <div><dt>Quota</dt><dd>{esc(fmt_bytes(s['quota_bytes']))}</dd></div>
    <div><dt>{'Files' if not files_pct else 'File count'}</dt><dd>{esc(fmt_int(s['files']))}{f" <span class='of'>of {esc(fmt_int(s['file_quota']))} ({files_pct:.0f}%)</span>" if files_pct else ''}</dd></div>
  </dl>
  {f'<div class="card__spark"><span class="spark__label">{esc(spark_label)}</span>{spark}</div>' if spark else ''}
  <p class="stamp">Quota read {esc(s.get('updated') or 'unknown')}</p>
</article>""")

    # The filesystem-wide per-user tables that used to live here are gone. They
    # answered "how much does this person hold anywhere on this filesystem", which
    # nobody asked, and sitting beside a folder table they implied the folder was
    # the whole filesystem. Still collected into snapshot.json for anyone who wants
    # the raw numbers.
    tables = []

    return f"""<section class="section" id="storage">
  <header class="section__head">
    <h2>Storage</h2>
    <p>Quota is per lab group per filesystem &mdash; <code>df</code> shows the whole
      shared array and will tell you everything is fine while the lab is at its ceiling.
      Each allocation has <em>two</em> independent limits, space and file count, and
      hitting either one fails writes while the other still looks fine. Space is the
      binding constraint everywhere here.</p>
  </header>
  <div class="cards">{''.join(cards)}</div>
  {''.join(tables)}
</section>"""


def load_dirscan(out_dir):
    """Every scan file under the output dir, merged into one view.

    Three things write here and none of them may clobber the others: the nightly
    single job, a one-off job covering different trees, and job-array shards that
    each walk a slice of the SAME tree. So roots are merged by path and, within a
    root, entries are merged by directory name -- a union, not a replacement.
    Completeness is then recomputed from the union rather than trusted from any one
    writer, because a shard finishing its slice says nothing about the whole tree.
    """
    paths = [os.path.join(out_dir, "dirscan.json")]
    for sub in ("shards",):
        d = os.path.join(out_dir, sub)
        try:
            paths += sorted(os.path.join(d, f) for f in os.listdir(d)
                            if f.endswith(".json"))
        except OSError:
            pass
    try:
        for entry in sorted(os.scandir(out_dir), key=lambda e: e.name):
            if entry.is_dir() and entry.name != "shards":
                side = os.path.join(entry.path, "dirscan.json")
                if os.path.exists(side):
                    paths.append(side)
    except OSError:
        pass

    # Merge in TIMESTAMP order, not the order the paths happen to be listed in.
    # Path order put subdirectories last, so a recovery file stamped 04:30 was
    # overwriting shard measurements taken 13-21 hours later -- 2.4 TiB of
    # since-deleted data kept counting on a filesystem the dashboard alerts on.
    docs = []
    for path in paths:
        try:
            with open(path) as fh:
                docs.append(json.load(fh))
        except (OSError, json.JSONDecodeError):
            continue
    docs.sort(key=lambda d: d.get("generated_at") or "")

    meta, roots = None, {}
    for doc in docs:
        meta = {k: v for k, v in doc.items() if k != "roots"}
        for r in doc.get("roots", []):
            key = r.get("root")
            acc = roots.setdefault(key, {"root": key, "entries": {}, "total_dirs": 0,
                                         "total_stamp": "", "error": None,
                                         "targets": None, "targets_stamp": ""})
            for e in r.get("entries", []):
                # Merge PER FIELD, not by replacing the record. Bytes and file count
                # legitimately come from different runs -- a slow directory records
                # inodes=None on purpose -- so a wholesale replace silently dropped
                # 18.6M known file counts. Newer wins per field; a null never
                # displaces a real measurement.
                prev = acc["entries"].get(e.get("dir"))
                if prev is None:
                    acc["entries"][e.get("dir")] = dict(e)
                    continue
                merged = dict(prev)
                for field in ("bytes", "inodes"):
                    if e.get(field) is not None:
                        merged[field] = e[field]
                for field in ("owner", "seconds"):
                    if e.get("bytes") is not None and e.get(field) is not None:
                        merged[field] = e[field]
                acc["entries"][e.get("dir")] = merged
            # Take the NEWEST run's total, not the largest ever seen. The rules for
            # what counts as a measurable directory change (a container that used to
            # expand into 34 children now measures as one shared store), so a running
            # max leaves a stale denominator that reads as "78/111 still scanning"
            # when the tree is actually 8/10 short.
            stamp = doc.get("generated_at") or ""
            if stamp >= acc["total_stamp"]:
                acc["total_stamp"] = stamp
                acc["total_dirs"] = r.get("total_dirs") or acc["total_dirs"]
            # The newest run's target list is the authority on what still EXISTS.
            # Without it the merge only ever unions, so a directory someone deleted
            # keeps its last measured size forever -- 916 GiB of deleted data stayed
            # on the leaderboard and made the tool look broken to the person who had
            # just cleaned up.
            if r.get("targets") and stamp >= acc["targets_stamp"]:
                acc["targets_stamp"] = stamp
                acc["targets"] = set(r["targets"])
            acc["error"] = acc["error"] or r.get("error")
    if meta is None:
        return None

    out = []
    for r in roots.values():
        if r["targets"] is not None:
            gone = [n for n in r["entries"] if n not in r["targets"]]
            for n in gone:
                del r["entries"][n]
        # Drop a parent that also has children measured. When the crawler learned to
        # expand shared containers, the old whole-directory measurement and the new
        # per-child ones both survived the merge under different keys, counting
        # 12.9 TiB of datasets twice and roughly doubling one person's total.
        names = set(r["entries"])
        superseded = {n for n in names
                      if any(k != n and k.startswith(n + "/") for k in names)}
        entries = sorted((e for n, e in r["entries"].items() if n not in superseded),
                         key=lambda e: -(e.get("bytes") or 0))
        # A directory whose `du` timed out leaves an entry with no measurement. It
        # must not count towards completeness -- otherwise a tree reports 67/67
        # complete while three directories contribute nothing, which is exactly the
        # "every worker succeeded, the answer is still wrong" failure this crawler
        # has hit repeatedly.
        measured = [e for e in entries if e.get("bytes") is not None]
        unmeasured = [e for e in entries if e.get("bytes") is None]
        out.append({
            "root": r["root"],
            "entries": measured,
            "scanned": len(measured),
            # Resolved = measured OR definitively unreadable. Progress has to count
            # the second kind too: a private directory is settled in 0.0 seconds and
            # will never yield a number, so counting only measured ones left the
            # display one short forever and read as a stuck scan.
            "resolved": len(measured) + len(unmeasured),
            "total_dirs": r["total_dirs"] or None,
            "complete": (bool(r["total_dirs"])
                         and len(measured) >= r["total_dirs"]
                         and not unmeasured),
            "unmeasured": [e["dir"] for e in unmeasured],
            "error": r["error"],
        })
    meta["roots"] = sorted(out, key=lambda r: r["root"] or "")
    return meta


SUBTREES = ("Lab", "Everyone")


def _display_folder(root):
    """The folder a reader thinks in, which is a level above what we scan.

    Per-person directories live one level down (`<lab>/Lab/<person>`), so the scan
    has to start at `Lab` and `Everyone` to see people at all. Nobody asked about
    those two separately though - the question is about `<lab>` - so the halves are
    folded back together for display.
    """
    head, tail = os.path.split(root.rstrip("/"))
    return head if tail in SUBTREES else root


def _quota_for_folder(snap, folder):
    """The quota line this folder is charged against, if there is one.

    A folder and a quota are not the same thing and the numbers will not match:
    quota follows the file's GROUP across the whole filesystem, so files our group
    owns inside another lab's tree count against us while sitting outside this
    folder. Showing both without saying so invites the reader to treat the gap as
    an error in one of them.
    """
    best = None
    for q in snap.get("storage", []):
        if q.get("source") == "df-fileset":
            if q["path"] == folder:
                return q
            continue
        group = q.get("group") or ""
        if folder.rstrip("/").endswith("/" + group) and folder.startswith(q["path"]):
            best = q
    return best


def _dirscan_section(scan, folders=(), snap=None):
    """The breakdown `quota` cannot give: who holds what inside one folder."""
    grouped = {}
    for r in (scan or {}).get("roots", []):
        grouped.setdefault(_display_folder(r["root"]), []).append(r)
    for f in folders:
        grouped.setdefault(f, [])
    if not grouped:
        return ""

    panels = []
    for folder, parts in sorted(grouped.items()):
        if not parts:
            panels.append(f"""<div class="panel">
  <header class="panel__head">
    <div><h3>{esc(folder)}</h3>
      <p class="stamp">not walked yet</p></div>
    <span class="pill pill--unknown">Queued</span>
  </header>
  <p class="note">The nightly walk has not reached this folder yet. It runs at 02:00
    and covers the largest trees last, so this fills in overnight.</p>
</div>""")
            continue
        errors = [r for r in parts if r.get("error")]
        if errors and not any(r.get("entries") for r in parts):
            panels.append(f"""<div class="panel"><header class="panel__head">
  <h3>{esc(folder)}</h3></header>
  <p class="note">Could not scan: {esc(errors[0]['error'])}</p></div>""")
            continue

        # One person can hold several directories, and across both subtrees; charge
        # them the sum, and keep the subtree in the label so `Everyone/` shared data
        # is never mistaken for someone's personal files.
        by_owner = {}
        for r in parts:
            leaf = os.path.basename(r["root"].rstrip("/"))
            prefix = f"{leaf}/" if leaf in SUBTREES and len(parts) > 1 else ""
            for e in r.get("entries", []):
                if not (e.get("bytes") or 0):
                    continue
                o = by_owner.setdefault(e["owner"], {"owner": e["owner"], "bytes": 0,
                                                     "inodes": 0, "dirs": [],
                                                     "parts": []})
                o["bytes"] += e["bytes"] or 0
                # Unknown must stay unknown through the sum. Coercing None to 0 made
                # a person with an unmeasured file count read as "0 files".
                if e["inodes"] is None or o["inodes"] is None:
                    o["inodes"] = None
                else:
                    o["inodes"] += e["inodes"]
                o["dirs"].append(prefix + e["dir"])
                o["parts"].append({"dir": prefix + e["dir"], "bytes": e["bytes"] or 0,
                                   "inodes": e["inodes"]})
        rows = sorted(by_owner.values(), key=lambda o: -o["bytes"])
        if not rows:
            continue
        total = sum(o["bytes"] for o in rows) or 1
        ino_known = [o["inodes"] for o in rows if o["inodes"] is not None]
        total_ino = sum(ino_known)
        ino_partial = len(rows) - len(ino_known)
        peak = rows[0]["bytes"] or 1

        body = []
        for i, o in enumerate(rows):
            share = o["bytes"] / total
            dirs = ", ".join(o["dirs"])
            extra = " is-extra" if i >= TOP_ROWS else ""
            multi = len(o["parts"]) > 1
            # Only rows that actually split are expandable; a caret that opens a
            # single line repeating the row above is noise.
            name = (f'<button class="disclose" type="button" aria-expanded="false">'
                    f'<span class="caret" aria-hidden="true"></span>{esc(o["owner"])}'
                    f'<span class="count">{len(o["parts"])}</span></button>'
                    if multi else esc(o["owner"]))
            body.append(f"""<tr class="row{extra}{' is-parent' if multi else ''}" data-user="{esc(o['owner'])} {esc(dirs)}">
  <td class="num dim cell-rank">{i + 1}</td>
  <td class="cell-user">{name}</td>
  <td class="mono dim">{esc(dirs if len(dirs) <= 34 else dirs[:31] + '...')}</td>
  <td class="num" data-sort="{o['bytes']}">{esc(fmt_bytes(o['bytes']))}</td>
  <td class="cell-bar" title="{esc(o['owner'])}: {esc(fmt_bytes(o['bytes']))}, {share*100:.1f}% of this folder">{bar(o['bytes']/peak)}</td>
  <td class="num dim">{share*100:.1f}%</td>
  <td class="num dim">{esc(fmt_int(o['inodes'])) if o['inodes'] is not None else '&mdash;'}</td>
</tr>""")
            if multi:
                items = "".join(
                    f'<li><span class="mono">{esc(pt["dir"])}</span>'
                    f'<span class="pbar">{bar(pt["bytes"] / (o["bytes"] or 1))}</span>'
                    f'<b>{esc(fmt_bytes(pt["bytes"]))}</b>'
                    f'<span class="dim">{esc(fmt_int(pt["inodes"]))} files</span></li>'
                    for pt in sorted(o["parts"], key=lambda x: -x["bytes"]))
                body.append(f'<tr class="detail{extra}" hidden><td></td>'
                            f'<td colspan="6"><ul class="parts">{items}</ul></td></tr>')

        failed = [d for r in parts for d in (r.get("unmeasured") or [])]
        q = _quota_for_folder(snap or {}, folder)
        recon = ""
        scan_settled = all((r.get("resolved", r.get("scanned", 0)) >= (r.get("total_dirs") or 0))
                           for r in parts)
        if q and q.get("used_bytes") and scan_settled:
            gap = q["used_bytes"] - total
            if abs(gap) > 0.02 * q["used_bytes"]:
                fileset = q.get("source") == "df-fileset"
                pct = q["used_bytes"] / q["quota_bytes"] * 100 if q.get("quota_bytes") else 0
                nbad = len(failed)
                # Deliberately NOT reconciled line by line. The two numbers answer
                # different questions and chasing them into agreement produced three
                # rewrites and one wrong explanation; the lab's call is to keep them
                # separate and say so once.
                why = []
                if q.get("source") != "df-fileset":
                    # Both directions are real and were each verified on this
                    # cluster, which is why the leaderboard can land either side of
                    # the quota -- and why it can exceed the group's limit without
                    # anything being wrong.
                    why.append("a group quota counts this group's files anywhere on "
                               "the filesystem, while this folder holds files charged "
                               "to <em>other</em> groups &mdash; setgid directories "
                               "under <code>kempner_ydu_lab</code> and even "
                               "<code>kempner_rcai_lab</code> live in here, so the two "
                               "totals are not the same set of bytes and the folder "
                               "figure is not bounded by the quota limit")
                if nbad:
                    why.append(f"{nbad} director{'y' if nbad == 1 else 'ies'} here "
                               f"(<code>{esc(', '.join(failed[:6]))}</code>) "
                               f"{'is' if nbad == 1 else 'are'} private and readable only "
                               f"by the owner, so the leaderboard figure is a floor")
                why.append("and a filer charges for block rounding and its own overhead")
                recon = (f'<p class="note note--flag"><b>Leaderboard: '
                         f'{esc(fmt_bytes(total))} that we can read. Quota: '
                         f'{esc(fmt_bytes(q["used_bytes"]))} of '
                         f'{esc(fmt_bytes(q["quota_bytes"]))}, {pct:.0f}% used.</b> '
                         f'These will not match &mdash; {"; ".join(why)}. '
                         f'Use the leaderboard for who to ask, the quota for how full '
                         f'it is.</p>')

        more = len(rows) - TOP_ROWS
        done = sum(r.get("scanned", 0) for r in parts)
        resolved = sum(r.get("resolved", r.get("scanned", 0)) for r in parts)
        want = sum(r.get("total_dirs") or 0 for r in parts)
        # Two different states were sharing one label: a scan genuinely in progress,
        # and a finished scan with directories nobody but their owner can read. The
        # second showed as "Still scanning 59/59", which reads as a stuck job.
        if all(r.get("complete") for r in parts):
            partial = ""
        elif resolved < want:
            partial = (f'<span class="pill pill--warn">Still scanning &mdash; {resolved}'
                       f'/{want or "?"} directories</span>')
        else:
            partial = (f'<span class="pill pill--warn">{len(failed)} unreadable</span>'
                       if failed else '<span class="pill pill--warn">Incomplete</span>')
        panels.append(f"""<div class="panel">
  <header class="panel__head">
    <div>
      <h3>{esc(folder)}</h3>
      <p class="stamp">{len(rows)} people &middot; {esc(fmt_bytes(total))} of files &middot;
        {esc(fmt_int(total_ino))}{'+' if ino_partial else ''} files &middot; <b>walked by us, this folder only</b>
        {partial}</p>
    </div>
    <div class="controls">
      <input class="filter" type="search" placeholder="Find a name&hellip;"
             aria-label="Filter this table by username">
      {f'<button class="toggle" type="button">Show all {len(rows)}</button>' if more > 0 else ''}
    </div>
  </header>
  <div class="scroll"><table class="table table--sortable">
    <thead><tr><th class="num">#</th><th>Owner</th><th>Directory</th><th class="num">Space</th>
      <th>Relative</th><th class="num">Share</th><th class="num">Files</th></tr></thead>
    <tbody>{''.join(body)}</tbody>
  </table></div>
  <p class="empty" hidden>No one here matches that name.</p>
  {recon}
</div>""")

    if not panels:
        return ""
    return f"""<section class="section" id="directories">
  <header class="section__head">
    <h2>Inside the lab folders</h2>
    <p>This is the one thing <code>quota</code> cannot answer. It reports per
      <em>filesystem</em>, and on VAST it returns each person's filesystem-wide total
      regardless of which tree the bytes are in &mdash; while <code>/n/lab_storage</code>
      is Isilon, where it refuses entirely. These numbers come from walking the folders
      instead, so they really are "what is in here".</p>
  </header>
  {''.join(panels)}
  <p class="note note--bare">Attributed by each directory's <b>owner</b>, not its name
    &mdash; the names are nicknames that do not match usernames (<code>alex</code>
    belongs to atong, <code>agopalak</code> to agopalakrishnan). An
    <code>Everyone/</code> prefix marks shared data rather than someone's own files.
    Scanned {esc(scan.get('generated_local', 'unknown'))}; the walk is expensive, so it
    runs once a day rather than with every refresh.</p>
</section>"""



def load_gpu_guide(out_dir):
    """Measured GPU comparison, if bench/gpubench.py has been run on each card."""
    try:
        with open(os.path.join(out_dir, "gpu_guide.json")) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None



# What each kind of job is actually limited by. The point of naming the bottleneck
# rather than just the winning card is that it transfers: someone with a workload not
# on this list can still work out which column to read.
WORKLOADS = [
    ("Sweeps, seeds, eval or inference fleets, data generation",
     "nothing shared &mdash; each GPU runs its own job",
     "RTX PRO 6000", "No weights cross between cards, so the interconnect never "
     "matters and the only question is throughput per card."),
    ("Single-GPU training, model comfortably fits",
     "raw compute", "H100 / H200",
     "Twice the bf16 throughput of the RTX and nearly three times an A100's."),
    ("Model, batch or context does not fit in 80 GiB",
     "VRAM", "H200, then RTX PRO 6000",
     "141 GiB and 96 GiB are the only options above 80; the RTX is the cheaper of "
     "the two by a wide margin."),
    ("Autoregressive generation / LLM decoding",
     "memory bandwidth, not compute", "H200",
     "Decoding re-reads the weights every token, so 4,800 GB/s is worth more here "
     "than peak FLOPs. The RTX is the weakest card on this axis."),
    ("Multi-GPU with gradient sync &mdash; DDP, FSDP, ZeRO, tensor parallel",
     "the interconnect", "anything with NVLink",
     "Avoid the RTX nodes: measured 53 GB/s card-to-card, PCIe speed and 27&times; "
     "slower than a card reaching its own memory."),
    ("Fine-tuning a small model, or debugging",
     "queue time, not the card", "whatever is idle",
     "A 40 GiB A100 on a requeue partition starts sooner and costs least; the card "
     "is rarely the limit while you are still finding bugs."),
]


def _gpu_speed_section(guide):
    """Capability comparison with no prices in it at all.

    Cost and capability are separate decisions and mixing them into one number was
    what made the earlier "0.53x value" pill unreadable. Here: how fast, how much
    memory, how well the cards talk to each other -- and which workload each axis
    actually decides.
    """
    cards = (guide or {}).get("cards") or []
    if not cards:
        return ""
    top_tf = max((c["bf16_expected"] or 0) for c in cards) or 1
    top_mem = max((c["mem_expected"] or 0) for c in cards) or 1
    top_vram = max((c["vram_gib"] or 0) for c in cards) or 1

    rows = []
    for c in cards:
        meas = ""
        if c.get("bf16_measured"):
            pct = c["bf16_measured"] / (c["bf16_expected"] or 1) * 100
            meas = f'<span class="dim">{c["bf16_measured"]:.0f} ({pct:.0f}%)</span>'
        link = ("NVLink" if c["nvlink"] else
                (f"PCIe &mdash; {fmt_int(c['p2p_measured_gbs'])} GB/s measured"
                 if c.get("p2p_measured_gbs") else "PCIe only"))
        rows.append(f"""<tr class="row">
  <td class="cell-user">{esc(c['label'])}</td>
  <td class="num">{c['vram_gib']}</td>
  <td class="cell-bar">{bar((c['vram_gib'] or 0) / top_vram)}</td>
  <td class="num">{esc(fmt_int(c['bf16_expected']))}</td>
  <td class="cell-bar">{bar((c['bf16_expected'] or 0) / top_tf)}</td>
  <td class="num dim">{meas}</td>
  <td class="num">{esc(fmt_int(c['mem_expected']))}</td>
  <td class="cell-bar">{bar((c['mem_expected'] or 0) / top_mem)}</td>
  <td class="{'dim' if c['nvlink'] else ''}">{link}</td>
</tr>""")

    work = "".join(
        f"""<tr class="row">
  <td>{task}</td>
  <td class="dim">{bound}</td>
  <td class="cell-user">{pick}</td>
  <td class="dim">{why}</td>
</tr>""" for task, bound, pick, why in WORKLOADS)

    return f"""<div class="panel">
  <header class="panel__head">
    <div><h3>How fast each card is, and what it suits</h3>
      <p class="stamp">no prices here &mdash; capability only. Vendor specs, with
        measured values on this cluster in grey where we have them</p></div>
  </header>
  <div class="scroll"><table class="table table--sortable">
    <thead><tr>
      <th>Card</th><th class="num">VRAM GiB</th><th></th>
      <th class="num">bf16 TFLOP/s</th><th></th><th class="num">measured</th>
      <th class="num">Memory GB/s</th><th></th><th>Card-to-card</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table></div>
  <div class="scroll"><table class="table">
    <thead><tr><th>If you are running&hellip;</th><th>limited by</th>
      <th>ask for</th><th>why</th></tr></thead>
    <tbody>{work}</tbody>
  </table></div>
</div>"""


def _gpu_guide_section(guide):
    """What a GPU-hour costs and what you get for it, one card per partition.

    Priced by PARTITION, because that is where Slurm actually sets it
    (TRESBillingWeights). An earlier version priced by card model and was wrong in a
    way that inverted the advice: the same H100 costs 418 on gpu_requeue and 2649 on
    kempner_h100, so the first decision is whether the job survives preemption, not
    which card it lands on. Card speeds are vendor specs on one consistent basis;
    values measured on this cluster are shown as a check, never mixed into a ratio.
    """
    parts = (guide or {}).get("partitions") or []
    if not parts:
        return ""
    cheap = guide.get("cheapest") or min(p["gpu_price"] for p in parts)

    out = []
    for p in parts:
        rel = p["gpu_price"] / cheap
        pre = p["preemptible"]
        tone = "good" if rel <= 1.05 else "warn" if rel <= 2.5 else "crit"
        badge = ('<span class="pill pill--good">preemptible</span>' if pre else
                 f'<span class="pill pill--{tone}">dedicated</span>')
        cards = []
        for m in p["models"]:
            meas = ""
            if m.get("bf16_measured"):
                pct = m["bf16_measured"] / (m["bf16_expected"] or 1) * 100
                meas = (f' <span class="dim">(measured {m["bf16_measured"]:.0f}, '
                        f'{pct:.0f}% of peak)</span>')
            cards.append(
                f'<li><b>{esc(m["label"])}</b> &middot; {m["vram_gib"]} GiB &middot; '
                f'{esc(fmt_int(m["bf16_expected"]))} TFLOP/s'
                f'{"" if m["nvlink"] else " &middot; no NVLink"}{meas}</li>')
        return_note = ("" if pre else
                       '<p class="gcard__meas">not preemptible &mdash; runs to its wall limit</p>')
        if pre:
            return_note = ('<p class="gcard__meas">can be killed and requeued at any '
                           'time; needs checkpointing</p>')
        out.append(f"""<article class="gcard gcard--{tone}">
  <header class="gcard__head">
    <div><h4><code>{esc(p['partition'])}</code></h4></div>
    {badge}
  </header>
  <p class="gcard__figure">{p['gpu_price']:.0f}<span class="gcard__unit">/GPU-hr</span></p>
  <dl class="gcard__rel">
    <div><dt>vs cheapest</dt><dd class="{'good' if rel <= 1.05 else 'cost'}">{rel:.2f}&times;</dd></div>
    <div><dt>cards here</dt><dd>{len(p['models'])}</dd></div>
  </dl>
  <ul class="gcard__cards">{''.join(cards)}</ul>
  {return_note}
</article>""")

    ded = [p for p in parts if not p["preemptible"]]
    pre = [p for p in parts if p["preemptible"]]
    spread = (min(p["gpu_price"] for p in ded) / cheap) if ded else None
    h100_ded = next((p for p in ded if any("H100" in m["label"] for m in p["models"])), None)
    h100_pre = next((p for p in pre if any("H100" in m["label"] for m in p["models"])), None)
    hero = ""
    if h100_ded and h100_pre:
        r = h100_ded["gpu_price"] / h100_pre["gpu_price"]
        hero = (f" The same H100 costs <b>{r:.1f}&times; more</b> on "
                f"<code>{esc(h100_ded['partition'])}</code> than on "
                f"<code>{esc(h100_pre['partition'])}</code> &mdash; identical hardware, "
                f"the whole difference is whether your job can be interrupted.")

    return f"""<div class="panel">
  <header class="panel__head">
    <div><h3>What a GPU-hour costs</h3>
      <p class="stamp">fairshare price from each partition's Slurm
        <code>TRESBillingWeights</code> &middot; card speeds are vendor specs,
        spot-checked here {esc(guide.get('measured_local', ''))}</p></div>
  </header>
  <p class="note note--lead"><b>The first question is not which card &mdash; it is
    whether your job can be preempted.</b>{hero} If it checkpoints, a
    <code>*_requeue</code> partition is the cheapest GPU on the cluster and still
    reaches H100s and H200s.</p>
  <div class="gcards">{''.join(out)}</div>
  <p class="note"><b>If you cannot be preempted</b>, then card choice matters, and
    <code>kempner_rtx</code> is the value pick among the dedicated partitions: cheapest
    of them, and 96&nbsp;GiB per card fits models the 80&nbsp;GiB parts cannot. The
    tradeoff is the interconnect &mdash; measured here, an RTX card reaches its
    neighbour at only <b>53 GB/s</b>, PCIe speed and 27&times; slower than reaching its
    own memory. So anything that synchronises every step (DDP, FSDP, ZeRO, tensor
    parallelism) can lose more to communication than it saves.<br>
    <b>Most of what this lab runs does not synchronise at all.</b> A hyperparameter
    sweep, one seed per GPU, an eval or inference fleet, a data-generation rollout
    &mdash; each GPU minds its own work, nothing crosses the interconnect, and more
    cards for the same fairshare is simply more throughput.</p>
</div>"""


def _compute_section(snap, guide=None):
    users = [u for u in snap["users"]
             if u["running_gpus"] or u["pending_jobs"] or u["gpu_hours_30d"] > 0]
    users.sort(key=lambda u: (-u["gpu_hours_30d"], -u["running_gpus"]))
    max_30d = max([u["gpu_hours_30d"] for u in users] or [1]) or 1
    max_run = max([u["running_gpus"] for u in users] or [1]) or 1
    lab_30d = sum(snap["sreport_30d"]["by_account"].values())
    lab_7d = sum(snap["sreport_7d"]["by_account"].values())

    rows = []
    for u in users:
        rows.append(f"""<tr class="row" data-user="{esc(u['user'])}">
  <td class="cell-user">{esc(u['user'])}</td>
  <td class="num">{u['running_gpus'] or ''}</td>
  <td class="cell-bar">{bar(u['running_gpus']/max_run) if u['running_gpus'] else ''}</td>
  <td class="num dim">{fmt_int(u['pending_jobs']) if u['pending_jobs'] else ''}</td>
  <td class="num">{fmt_int(u['gpu_hours_7d']) if u['gpu_hours_7d'] else ''}</td>
  <td class="num">{fmt_int(u['gpu_hours_30d']) if u['gpu_hours_30d'] else ''}</td>
  <td class="cell-bar">{bar(u['gpu_hours_30d']/max_30d) if u['gpu_hours_30d'] else ''}</td>
  <td class="num dim"{f' data-sort="{u["storage_bytes"]}"' if u['storage_bytes'] else ''}>{esc(fmt_bytes(u['storage_bytes']) if u['storage_bytes'] else '--')}</td>
</tr>""")

    acct_rows = []
    for acct in snap["accounts"]:
        fs = snap["fairshare"].get(acct, {})
        h7 = snap["sreport_7d"]["by_account"].get(acct, 0)
        h30 = snap["sreport_30d"]["by_account"].get(acct, 0)
        eff = fs.get("effective_usage")
        norm = fs.get("norm_shares")
        over = (eff / norm) if (eff and norm) else None
        tone = "crit" if (over and over > 3) else "warn" if (over and over > 1.5) else "good"
        acct_rows.append(f"""<tr>
  <td class="cell-user">{esc(acct)}</td>
  <td class="num">{fmt_int(h7)}</td>
  <td class="num">{fmt_int(h30)}</td>
  <td class="num dim">{f'{norm*100:.2f}%' if norm else '--'}</td>
  <td class="num dim">{f'{eff*100:.2f}%' if eff else '--'}</td>
  <td><span class="pill pill--{tone}">{f'{over:.1f}x share' if over else '--'}</span></td>
</tr>""")

    # Two different cuts of the SAME running GPUs. Rendered as one undifferentiated
    # row they read as a single list that mysteriously double-counts, so each cut gets
    # its own labelled group and the labels state the shared total outright.
    running_gpus = snap["totals"]["running_gpus"]
    model_chips = "".join(
        f'<span class="chip"><b>{fmt_int(n)}</b> {esc(model)}</span>'
        for model, n in sorted(snap["gpu_models"].items(), key=lambda kv: -kv[1]))

    part_chips = []
    for name, v in sorted(snap["partitions"].items(), key=lambda kv: -kv[1]["running_gpus"]):
        if not v["running_gpus"] and not v["pending_gpus"]:
            continue  # CPU-only partitions the lab happens to touch; not the story here
        queued = f' <i>+{fmt_int(v["pending_gpus"])} waiting</i>' if v["pending_gpus"] else ""
        preempt = " &middot; preemptible" if "requeue" in name else ""
        part_chips.append(
            f'<span class="chip" title="{esc(name)}{preempt}">'
            f'<b>{fmt_int(v["running_gpus"])}</b> {esc(name)}{queued}</span>')

    chip_groups = f"""<div class="chipgroup">
    <p class="eyebrow">By hardware &mdash; {fmt_int(running_gpus)} GPUs running</p>
    <div class="chips">{model_chips}</div>
  </div>
  <div class="chipgroup">
    <p class="eyebrow">By queue &mdash; the same {fmt_int(running_gpus)} GPUs, plus what is waiting</p>
    <div class="chips">{''.join(part_chips)}</div>
    <p class="note note--bare">Both rows count the same running GPUs, sliced two ways &mdash;
      they are not additive. The <code>*_requeue</code> partitions are preemptible: those
      jobs can be killed and requeued the moment a priority job needs the node.</p>
  </div>"""

    return f"""<section class="section" id="compute">
  <header class="section__head">
    <h2>Compute</h2>
    <p>There is no hard GPU-hour budget on {esc(snap['cluster'])} &mdash; the lab's cost is
      <em>fairshare</em>. Hours burned now push the whole account's priority down for
      everyone later, which is what makes this table worth reading together.</p>
  </header>

  <div class="panel">
    <header class="panel__head"><h3>Account standing</h3>
      <p class="stamp">{fmt_int(lab_7d)} GPU-hours last 7 days &middot; {fmt_int(lab_30d)} last 30</p>
    </header>
    <div class="scroll"><table class="table">
      <thead><tr><th>Account</th><th class="num">GPU-hours 7d</th><th class="num">GPU-hours 30d</th>
        <th class="num">Shares</th><th class="num">Usage</th><th>Standing</th></tr></thead>
      <tbody>{''.join(acct_rows)}</tbody>
    </table></div>
    <p class="note">Standing = effective usage &divide; normalized shares. Above 1x the
      account has drawn more than its slice and new jobs queue behind lighter users.</p>
  </div>

  {_gpu_speed_section(guide)}
  {_gpu_guide_section(guide)}
  <div class="chipgroups">{chip_groups}</div>

  <div class="panel">
    <header class="panel__head">
      <div>
        <h3>Per-person compute</h3>
        <p class="stamp">{len(users)} people with activity &middot; live queue + Slurm accounting, both accounts</p>
      </div>
      <div class="controls">
        <input class="filter" type="search" placeholder="Find a name&hellip;"
               aria-label="Filter this table by username">
      </div>
    </header>
    <div class="scroll"><table class="table table--sortable">
      <thead><tr>
        <th>User</th><th class="num">GPUs now</th><th></th><th class="num">Jobs queued</th>
        <th class="num">GPU-hours 7d</th><th class="num">GPU-hours 30d</th><th></th><th class="num">Disk</th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table></div>
    <p class="empty" hidden>No one here matches that name.</p>
    <p class="note">A GPU-hour is one GPU held for one hour &mdash; 8 GPUs for 3 hours
      is 24 of them. It measures time <em>allocated</em>, not work done: a GPU idling at
      0% bills exactly the same as one running flat out, which is what the cluster's
      idle-GPU warnings are about. Two windows are shown because they say different
      things &mdash; a big 7-day number inside a similar 30-day number is someone who
      just started, while a 30-day number spread evenly is a steady long-running load.</p>
  </div>
</section>"""


def _jobs_section(snap):
    running = snap["jobs"]["running"][:60]
    rows = []
    for j in running:
        frac = (j["elapsed_s"] / j["limit_s"]) if (j["elapsed_s"] and j["limit_s"]) else 0
        rows.append(f"""<tr>
  <td class="mono">{esc(j['jobid'])}</td>
  <td class="cell-user">{esc(j['user'])}</td>
  <td class="mono dim">{esc(j['name'][:28])}</td>
  <td>{esc(j['partition'])}</td>
  <td class="num">{j['gpus'] or ''}</td>
  <td class="dim">{esc(j['gpu_model'] if j['gpus'] else '')}</td>
  <td class="num">{esc(fmt_dur(j['elapsed_s']))}</td>
  <td class="cell-bar" title="{frac*100:.0f}% of time limit">{bar(frac)}</td>
  <td class="mono dim">{esc(j['nodelist'])}</td>
</tr>""")

    pend = {}
    for j in snap["jobs"]["pending"]:
        pend[j["reason"] or "Unknown"] = pend.get(j["reason"] or "Unknown", 0) + 1
    reason_chips = "".join(
        f'<span class="chip"><b>{fmt_int(n)}</b> {esc(r)}</span>'
        for r, n in sorted(pend.items(), key=lambda kv: -kv[1])[:6])

    more = len(snap["jobs"]["running"]) - len(running)
    return f"""<section class="section" id="jobs">
  <header class="section__head">
    <h2>Running jobs</h2>
    <p>Longest and largest first. The bar is elapsed time against the job's own wall
      limit &mdash; a bar near full on a preemptible partition is about to requeue.</p>
  </header>
  <div class="panel">
    <div class="scroll"><table class="table table--sortable">
      <thead><tr><th>Job</th><th>User</th><th>Name</th><th>Partition</th><th class="num">GPU</th>
        <th>Type</th><th class="num">Elapsed</th><th>vs limit</th><th>Node</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table></div>
    {f'<p class="note">{more} more running jobs in snapshot.json.</p>' if more > 0 else ''}
  </div>
  <div class="chips">{reason_chips}</div>
</section>"""


def _footer(snap):
    return f"""<footer class="footer">
  <p><b>Snapshot {esc(snap['generated_local'])}</b> from {esc(snap['host'])} in
     {snap['collect_seconds']}s &middot; sources: <code>squeue</code>, <code>sreport</code>,
     <code>sshare</code>, <code>quota</code>.</p>
  <p class="dim">Group quota refreshes about hourly; the per-user storage scan is a nightly
     VAST report, so a directory deleted today still shows until tomorrow. Slurm accounting
     only exposes other people's jobs in aggregate, so per-person history is GPU-hours,
     not job lists.</p>
</footer>"""


CSS = """
:root{
  --bg:#F4F6F8; --surface:#FFFFFF; --surface-2:#FAFBFD; --surface-3:#EEF1F5;
  --ink:#101519; --ink-2:#4C5763; --ink-3:#7C8794;
  --line:#E1E6EB; --line-2:#CDD5DD;
  --accent:#1F5FA8; --accent-soft:#DCE8F6;
  --good:#17784C; --good-soft:#DCEFE4;
  --warn:#93610B; --warn-soft:#F7EBD5;
  --crit:#B23129; --crit-soft:#F8E1DE;
  --shadow:0 1px 2px rgba(16,21,25,.05), 0 8px 24px -16px rgba(16,21,25,.25);
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --sans:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#0B0F14; --surface:#131A21; --surface-2:#171F27; --surface-3:#1D262F;
  --ink:#E8EDF2; --ink-2:#9AA6B2; --ink-3:#6A7784;
  --line:#232D37; --line-2:#33404C;
  --accent:#5FA0EC; --accent-soft:#17293C;
  --good:#43BE85; --good-soft:#122A20;
  --warn:#D9A244; --warn-soft:#2C2213;
  --crit:#EE7365; --crit-soft:#31191A;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -16px rgba(0,0,0,.8);
}}
:root[data-theme="dark"]{
  --bg:#0B0F14; --surface:#131A21; --surface-2:#171F27; --surface-3:#1D262F;
  --ink:#E8EDF2; --ink-2:#9AA6B2; --ink-3:#6A7784;
  --line:#232D37; --line-2:#33404C;
  --accent:#5FA0EC; --accent-soft:#17293C;
  --good:#43BE85; --good-soft:#122A20;
  --warn:#D9A244; --warn-soft:#2C2213;
  --crit:#EE7365; --crit-soft:#31191A;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -16px rgba(0,0,0,.8);
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--bg); color:var(--ink);
  font-family:var(--sans); font-size:15px; line-height:1.55;
  -webkit-font-smoothing:antialiased;
  padding:32px 24px 64px; display:flex; flex-direction:column; gap:40px;
  max-width:1200px; margin-inline:auto;
}
h1,h2,h3{margin:0; text-wrap:balance; letter-spacing:-.02em; font-weight:640}
h1{font-size:2.1rem; line-height:1.1}
h2{font-size:1.35rem}
h3{font-size:1rem}
p{margin:0}
code{font-family:var(--mono); font-size:.86em; background:var(--surface-3);
  padding:.1em .34em; border-radius:3px}
.eyebrow{font-family:var(--mono); font-size:.68rem; text-transform:uppercase;
  letter-spacing:.11em; color:var(--ink-3); margin-bottom:.35em}
.dim{color:var(--ink-3)}
.mono{font-family:var(--mono); font-size:.82rem}
.num{text-align:right; font-variant-numeric:tabular-nums}

/* masthead */
.masthead{display:grid; grid-template-columns:minmax(320px,1.1fr) minmax(280px,1fr);
  gap:32px; align-items:start;
  border-bottom:1px solid var(--line); padding-bottom:32px}
.lede{color:var(--ink-2); max-width:52ch; margin-top:.6em}
.masthead__meta{display:flex; flex-direction:column; gap:10px}
.refreshed{display:flex; flex-direction:column; align-items:flex-end; gap:1px;
  font-family:var(--mono); font-size:.68rem; line-height:1.4}
.refreshed__rel{color:var(--ink-2); font-weight:600}
.refreshed__abs{color:var(--ink-3)}
.refreshed.is-stale .refreshed__rel{color:var(--warn)}
.tiles{display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px}
.tile{background:var(--surface); border:1px solid var(--line); border-radius:8px;
  padding:14px 16px; box-shadow:var(--shadow); border-top:3px solid var(--line-2)}
.tile--good{border-top-color:var(--good)}
.tile--warn{border-top-color:var(--warn)}
.tile--crit{border-top-color:var(--crit)}
.tile__label{font-family:var(--mono); font-size:.66rem; text-transform:uppercase;
  letter-spacing:.09em; color:var(--ink-3)}
.tile__value{font-size:1.9rem; line-height:1.15; font-weight:600;
  font-variant-numeric:tabular-nums; letter-spacing:-.03em; margin:.12em 0}
.tile--crit .tile__value{color:var(--crit)}
.tile--warn .tile__value{color:var(--warn)}
.tile__sub{font-size:.78rem; color:var(--ink-3)}

/* sections */
.section{display:flex; flex-direction:column; gap:20px}
.section__head{display:flex; flex-direction:column; gap:6px;
  border-left:3px solid var(--accent); padding-left:14px}
.section__head p{color:var(--ink-2); max-width:74ch; font-size:.92rem}

.cards{display:grid; grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); gap:16px}
.card{background:var(--surface); border:1px solid var(--line); border-radius:10px;
  padding:18px; box-shadow:var(--shadow); display:flex; flex-direction:column; gap:12px}
.card__head{display:flex; justify-content:space-between; align-items:flex-start; gap:12px}
.figure{font-size:2.6rem; line-height:1; font-weight:600; letter-spacing:-.04em;
  font-variant-numeric:tabular-nums}
.figure--unknown{color:var(--ink-3)}
.figure--crit{color:var(--crit)} .figure--warn{color:var(--warn)} .figure--good{color:var(--good)}
.figure__unit{font-size:1rem; font-weight:500; color:var(--ink-3); margin-left:.12em}
.card__spark{display:flex; flex-direction:column; gap:4px}
.spark{width:100%; height:44px; display:block}
.spark__label{font-family:var(--mono); font-size:.64rem; text-transform:uppercase;
  letter-spacing:.09em; color:var(--ink-3)}
.spark__area{fill:var(--accent-soft)}
.spark__line{fill:none; stroke:var(--accent); stroke-width:2;
  vector-effect:non-scaling-stroke; stroke-linejoin:round}
.spark__dot{fill:var(--accent); stroke:var(--surface); stroke-width:2}

.meter{position:relative; height:9px; border-radius:5px; background:var(--surface-3);
  overflow:hidden}
.meter__fill{position:absolute; inset:0 auto 0 0; border-radius:5px; background:var(--accent)}
.meter--crit .meter__fill{background:var(--crit)}
.meter--warn .meter__fill{background:var(--warn)}
.meter--good .meter__fill{background:var(--good)}
.meter__mark{position:absolute; top:0; bottom:0; width:1px; background:var(--surface);
  opacity:.75}

.kv{display:grid; grid-template-columns:1fr 1fr; gap:8px 16px; margin:0}
.kv div{display:flex; justify-content:space-between; gap:8px;
  border-bottom:1px dotted var(--line); padding-bottom:4px}
.kv dt{font-size:.78rem; color:var(--ink-3)}
.kv .of{color:var(--ink-3); font-weight:400}
.kv dd{margin:0; font-size:.84rem; font-variant-numeric:tabular-nums; font-weight:550}
.stamp{font-family:var(--mono); font-size:.68rem; color:var(--ink-3)}
.scope{font-size:.72rem; color:var(--ink-3); margin-top:.25em; max-width:30ch}

.panel{background:var(--surface); border:1px solid var(--line); border-radius:10px;
  box-shadow:var(--shadow); overflow:hidden}
.panel__head{display:flex; justify-content:space-between; align-items:baseline;
  gap:16px; flex-wrap:wrap; padding:16px 18px; border-bottom:1px solid var(--line);
  background:var(--surface-2)}
.note{padding:10px 18px 14px; font-size:.8rem; color:var(--ink-3)}
.rules{display:grid; gap:10px}
.rule{display:grid; grid-template-columns:2.2rem 1fr auto; gap:14px; align-items:start;
  background:var(--surface); border:1px solid var(--line); border-radius:10px;
  padding:14px 16px; box-shadow:var(--shadow)}
.rule__n{font-family:var(--mono); font-size:1.15rem; font-weight:600; color:var(--accent);
  line-height:1.2; font-variant-numeric:tabular-nums}
.rule__body h4{margin:0 0 4px; font-size:.95rem; font-weight:640; letter-spacing:-.01em}
.rule__body p{font-size:.84rem; color:var(--ink-2); max-width:82ch}
.rule__badge{font-family:var(--mono); font-size:.68rem; white-space:nowrap;
  background:var(--accent-soft); color:var(--accent); padding:4px 10px;
  border-radius:20px; font-weight:600}
@media (max-width:700px){
  .rule{grid-template-columns:1.8rem 1fr; }
  .rule__badge{grid-column:2; justify-self:start; margin-top:4px}
}
.gcards{display:grid; grid-template-columns:repeat(auto-fit,minmax(215px,1fr));
  gap:12px; padding:14px 18px 4px}
.gcard{background:var(--surface); border:1px solid var(--line); border-radius:10px;
  padding:14px; display:flex; flex-direction:column; gap:8px;
  border-top:3px solid var(--line-2)}
.gcard--good{border-top-color:var(--good)}
.gcard--warn{border-top-color:var(--warn)}
.gcard--crit{border-top-color:var(--crit)}
.gcard__head{display:flex; justify-content:space-between; align-items:flex-start; gap:8px}
.gcard h4{margin:0; font-size:.95rem; font-weight:640; letter-spacing:-.01em}
.gcard__part{font-family:var(--mono); font-size:.66rem; color:var(--ink-3); margin-top:2px}
.gcard__part code{background:none; padding:0}
.gcard__figure{font-size:1.9rem; line-height:1; font-weight:600; letter-spacing:-.03em;
  font-variant-numeric:tabular-nums}
.gcard__unit{font-size:.8rem; font-weight:500; color:var(--ink-3); margin-left:.25em}
.gcard__spec{font-size:.74rem; color:var(--ink-2); line-height:1.45}
.gcard__rel--3{grid-template-columns:repeat(3,1fr)}
.gcard__rel dd.good{color:var(--good)}
.gcard__rel{display:grid; grid-template-columns:1fr 1fr; gap:6px 10px; margin:2px 0 0;
  border-top:1px dotted var(--line); padding-top:8px}
.gcard__rel dt{font-size:.6rem; color:var(--ink-3); font-family:var(--mono);
  text-transform:uppercase; letter-spacing:.06em}
.gcard__rel dd{margin:0; font-size:.98rem; font-weight:600;
  font-variant-numeric:tabular-nums}
.gcard__rel dd.cost{color:var(--crit)}
.gcard__meas{font-family:var(--mono); font-size:.64rem; color:var(--ink-3);
  border-top:1px dotted var(--line); padding-top:6px; line-height:1.4}
.gcard__cards{list-style:none; margin:0; padding:0; display:flex;
  flex-direction:column; gap:3px; font-size:.72rem; color:var(--ink-2)}
.gcard__cards b{color:var(--ink)}
.note--lead{padding:14px 18px 4px; font-size:.88rem; color:var(--ink-2); max-width:78ch}\n.note--lead b{color:var(--ink)}\n.note--flag{color:var(--ink-2); background:var(--surface-2);
  border-top:1px solid var(--line)}
.note--flag b{color:var(--ink)}
.note--flag code{display:inline-block; margin-top:6px; font-size:.76rem}
.scroll{overflow-x:auto}

.table{width:100%; border-collapse:collapse; font-size:.87rem}
.table th{font-family:var(--mono); font-size:.66rem; text-transform:uppercase;
  letter-spacing:.08em; color:var(--ink-3); font-weight:500; text-align:left;
  padding:10px 12px; border-bottom:1px solid var(--line); white-space:nowrap}
.table th.num{text-align:right}
.table td{padding:8px 12px; border-bottom:1px solid var(--line); white-space:nowrap}
.table tbody tr:last-child td{border-bottom:0}
.table tbody tr:hover{background:var(--surface-2)}
.cell-user{font-family:var(--mono); font-size:.82rem; font-weight:550}
.cell-bar{width:110px; min-width:110px}
.cell-rank{width:2.5em; font-size:.76rem}
.disclose{display:inline-flex; align-items:center; gap:6px; background:none; border:0;
  padding:0; cursor:pointer; font:inherit; color:inherit}
.disclose:hover{color:var(--accent)}
.caret{width:0; height:0; border-left:5px solid currentColor;
  border-top:4px solid transparent; border-bottom:4px solid transparent;
  transition:transform .12s ease}
.disclose[aria-expanded="true"] .caret{transform:rotate(90deg)}
.count{font-size:.66rem; color:var(--ink-3); background:var(--surface-3);
  border-radius:8px; padding:0 5px}
tr.detail td{background:var(--surface-2); padding:0}
.parts{list-style:none; margin:0; padding:6px 0 10px}
.parts li{display:grid; grid-template-columns:minmax(10ch,22ch) 90px auto 11ch;
  gap:12px; align-items:center; padding:4px 12px; font-size:.82rem}
.parts b{text-align:right; font-variant-numeric:tabular-nums; font-weight:600}
.parts .dim{text-align:right; font-variant-numeric:tabular-nums; font-size:.76rem}
.row-rest td{color:var(--ink-3); font-style:italic}

/* Long tail: rendered but folded away until asked for, or searched into view. */
.panel:not(.is-open) tbody tr.is-extra{display:none}
.panel.is-open tbody tr.row-rest{display:none}
.controls{display:flex; gap:8px; align-items:center; flex-wrap:wrap}
.filter{font-family:var(--mono); font-size:.76rem; padding:5px 10px; width:17ch;
  color:var(--ink); background:var(--surface); border:1px solid var(--line-2);
  border-radius:6px}
.filter::placeholder{color:var(--ink-3)}
.filter:focus{outline:2px solid var(--accent); outline-offset:1px; border-color:var(--accent)}
.toggle{font-family:var(--mono); font-size:.72rem; padding:5px 11px; cursor:pointer;
  color:var(--ink-2); background:var(--surface); border:1px solid var(--line-2);
  border-radius:6px}
.toggle:hover{color:var(--ink); border-color:var(--ink-3)}
tr.is-hit td{background:var(--accent-soft)}
.empty{padding:14px 18px; font-size:.82rem; color:var(--ink-3)}
.table--sortable th{cursor:pointer; user-select:none}
.table--sortable th:hover{color:var(--ink)}
.table--sortable th[aria-sort]{color:var(--accent)}

.bar{display:block; height:7px; border-radius:4px; background:var(--surface-3);
  overflow:hidden}
.bar__fill{display:block; height:100%; border-radius:4px; background:var(--accent)}
.bar--muted .bar__fill{background:var(--line-2)}

.chipgroups{display:flex; flex-direction:column; gap:16px}
.chipgroup{display:flex; flex-direction:column; gap:8px}
.chipgroup .eyebrow{margin-bottom:0}
.note--bare{padding:0; max-width:74ch}
.chips{display:flex; flex-wrap:wrap; gap:8px}
.chip{font-family:var(--mono); font-size:.72rem; padding:4px 10px; border-radius:20px;
  border:1px solid var(--line); background:var(--surface); color:var(--ink-2)}
.chip b{color:var(--ink); font-weight:600; font-variant-numeric:tabular-nums}
.chip i{font-style:normal; color:var(--ink-3)}

.pill{font-family:var(--mono); font-size:.66rem; text-transform:uppercase;
  letter-spacing:.07em; padding:3px 9px; border-radius:20px; white-space:nowrap}
.pill--good{background:var(--good-soft); color:var(--good)}
.pill--warn{background:var(--warn-soft); color:var(--warn)}
.pill--crit{background:var(--crit-soft); color:var(--crit)}
.pill--unknown{background:var(--surface-3); color:var(--ink-3)}

.footer{border-top:1px solid var(--line); padding-top:20px; font-size:.82rem;
  color:var(--ink-2); display:flex; flex-direction:column; gap:8px; max-width:80ch}

:focus-visible{outline:2px solid var(--accent); outline-offset:2px; border-radius:3px}
@media (prefers-reduced-motion:reduce){*{transition:none!important; animation:none!important}}
@media (max-width:820px){
  body{padding:24px 16px 48px}
  .masthead{grid-template-columns:1fr}
  h1{font-size:1.7rem}
}
"""


def _script():
    return """<script>
(function () {
  // "Updated N minutes ago", recomputed in the browser. The page is a static file
  // that a tab can sit on for hours, so an absolute stamp alone never tells anyone
  // it has gone stale. Past two refresh intervals it turns amber.
  var el = document.querySelector('.refreshed');
  if (el && el.dataset.at) {
    var rel = el.querySelector('.refreshed__rel');
    var tick = function () {
      var mins = Math.round((Date.now() - new Date(el.dataset.at).getTime()) / 60000);
      var text;
      if (!isFinite(mins) || mins < 0) { text = 'updated just now'; }
      else if (mins < 1) { text = 'updated just now'; }
      else if (mins < 60) { text = 'updated ' + mins + ' min ago'; }
      else if (mins < 1440) {
        var h = Math.round(mins / 60);
        text = 'updated ' + h + (h === 1 ? ' hour ago' : ' hours ago');
      } else {
        var d = Math.round(mins / 1440);
        text = 'updated ' + d + (d === 1 ? ' day ago' : ' days ago');
      }
      rel.textContent = text;
      el.classList.toggle('is-stale', mins > 35);   // two refresh intervals
    };
    tick();
    setInterval(tick, 30000);
  }

  // Click a header to sort. Numeric columns sort numerically, others lexically.
  document.querySelectorAll('table.table--sortable').forEach(function (table) {
    var head = table.tHead.rows[0];
    Array.from(head.cells).forEach(function (th, idx) {
      if (!th.textContent.trim()) return;
      th.tabIndex = 0;
      function sort() {
        var body = table.tBodies[0];
        var rows = Array.from(body.rows);
        var asc = th.getAttribute('aria-sort') === 'ascending';
        Array.from(head.cells).forEach(function (c) { c.removeAttribute('aria-sort'); });
        th.setAttribute('aria-sort', asc ? 'descending' : 'ascending');
        var num = function (cell) {
          // A byte quantity is displayed in whatever unit keeps it readable
          // (GiB/TiB/PiB), so the rendered text alone can't be compared across
          // rows -- "620 TiB" and "1,010 GiB" both parse to a bare 620 and 1010
          // if only digits are kept, putting the smaller quantity first. Cells
          // with a real magnitude carry the raw byte count in data-sort; prefer
          // that, and only fall back to parsing the text for columns without it.
          if (cell.hasAttribute('data-sort')) {
            var raw = parseFloat(cell.getAttribute('data-sort'));
            if (!isNaN(raw)) return raw;
          }
          var v = parseFloat((cell.textContent || '').replace(/[^0-9.\\-]/g, ''));
          return isNaN(v) ? -Infinity : v;
        };
        var numeric = th.classList.contains('num');
        // Detail rows are not sortable content - they belong to the row above and
        // have to travel with it, or sorting scatters them across the table.
        var pairs = rows.filter(function (r) { return !r.classList.contains('detail'); })
          .map(function (r) {
            var d = r.nextElementSibling;
            return [r, (d && d.classList.contains('detail')) ? d : null];
          });
        pairs.sort(function (a, b) {
          var x = a[0].cells[idx], y = b[0].cells[idx];
          if (!x || !y) return 0;
          var r = numeric ? num(x) - num(y)
                          : (x.textContent || '').localeCompare(y.textContent || '');
          return asc ? r : -r;
        });
        pairs.forEach(function (p) {
          body.appendChild(p[0]);
          if (p[1]) body.appendChild(p[1]);
        });
      }
      th.addEventListener('click', sort);
      th.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sort(); }
      });
    });
  });

  // Click a name to see how that person's total splits across their directories.
  document.querySelectorAll('.disclose').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var row = btn.closest('tr');
      var detail = row && row.nextElementSibling;
      if (!detail || !detail.classList.contains('detail')) return;
      var open = detail.hasAttribute('hidden');
      if (open) { detail.removeAttribute('hidden'); } else { detail.setAttribute('hidden', ''); }
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  });

  // Fold the long tail open, and let anyone search their way to their own row -
  // including rows still folded away.
  document.querySelectorAll('.panel').forEach(function (panel) {
    var input = panel.querySelector('.filter');
    var toggle = panel.querySelector('.toggle');
    var empty = panel.querySelector('.empty');
    var rows = Array.from(panel.querySelectorAll('tbody tr.row'));
    var rest = panel.querySelector('tbody tr.row-rest');
    if (!rows.length) return;

    if (toggle) {
      var shut = toggle.textContent;
      toggle.addEventListener('click', function () {
        var open = panel.classList.toggle('is-open');
        toggle.textContent = open ? 'Show top 15' : shut;
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      });
    }

    if (!input) return;
    input.addEventListener('input', function () {
      var q = input.value.trim().toLowerCase();
      var details = Array.from(panel.querySelectorAll('tbody tr.detail'));
      if (!q) {
        // Hand control back to the CSS so the fold state applies again.
        rows.forEach(function (r) { r.style.display = ''; r.classList.remove('is-hit'); });
        details.forEach(function (d) { d.style.display = ''; });
        if (rest) rest.style.display = '';
        if (empty) empty.hidden = true;
        return;
      }
      details.forEach(function (d) { d.style.display = 'none'; });
      var hits = 0;
      rows.forEach(function (r) {
        var match = (r.dataset.user || '').toLowerCase().indexOf(q) !== -1;
        // An explicit display beats the .is-extra rule, so folded rows can surface.
        r.style.display = match ? 'table-row' : 'none';
        r.classList.toggle('is-hit', match);
        if (match) hits++;
      });
      if (rest) rest.style.display = 'none';
      if (empty) empty.hidden = hits > 0;
    });
  });
})();
</script>"""


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.environ.get("LABDASH_OUT", "."),
                    help="output directory for index.html / snapshot.json / history.jsonl")
    ap.add_argument("--accounts", default=os.environ.get("LABDASH_ACCOUNTS", ""),
                    help="comma-separated Slurm accounts (default: all yours)")
    ap.add_argument("--paths", default=os.environ.get("LABDASH_PATHS", ""),
                    help=f"comma-separated filesystems (default: {','.join(DEFAULT_PATHS)})")
    ap.add_argument("--json", action="store_true", help="print snapshot JSON, skip HTML")
    ap.add_argument("--fragment", action="store_true",
                    help="also write fragment.html (body only, for publishing as an Artifact)")
    args = ap.parse_args()

    if not shutil.which("squeue"):
        sys.exit("labdash: no squeue on PATH - run this on a cluster login node.")

    accounts = [a for a in args.accounts.split(",") if a] or discover_accounts()
    if not accounts:
        sys.exit("labdash: could not determine any Slurm account.")
    paths = [p for p in args.paths.split(",") if p] or DEFAULT_PATHS

    snap = build_snapshot(accounts, paths)

    if args.json:
        json.dump(snap, sys.stdout, indent=2)
        print()
        return

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    history = append_history(snap, os.path.join(out, "history.jsonl"))

    dirscan = load_dirscan(out)
    gpu_guide = load_gpu_guide(out)
    files = [("snapshot.json", json.dumps(snap, indent=2)),
             ("index.html", render(snap, history, dirscan=dirscan, gpu_guide=gpu_guide))]
    if args.fragment:
        files.append(("fragment.html",
                      render(snap, history, fragment=True, dirscan=dirscan, gpu_guide=gpu_guide)))

    # write via temp + rename so a reader never sees a half-written page
    for name, text in files:
        tmp = os.path.join(out, name + ".tmp")
        with open(tmp, "w") as fh:
            fh.write(text)
        os.replace(tmp, os.path.join(out, name))

    t = snap["totals"]
    print(f"labdash: {t['running_gpus']} GPUs running, {t['pending_gpus']} queued, "
          f"{len(snap['storage'])} quotas, {snap['collect_seconds']}s -> {out}/index.html")


if __name__ == "__main__":
    main()
