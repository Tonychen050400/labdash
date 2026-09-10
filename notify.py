#!/usr/bin/env python3
"""notify - the daily Slack post about cluster state.

  1. storage: any quota at or above the threshold (silent when none are)
  2. GPU jobs sitting on non-preemptible partitions (rate-limited)

Reads the snapshot labdash already writes, so it costs nothing extra to run.

  python3 notify.py --out DIR --dry-run          # print what would be posted
  python3 notify.py --out DIR --webhook-url URL  # actually post
  SLACK_WEBHOOK_URL=... python3 notify.py --out DIR

Storage goes out as a plain daily reading of every location, worst first -- not as a
crossing alarm. An alarm only speaks when something changes, which meant a quiet day
was indistinguishable from a broken tool, and the number people actually wanted was
never in the channel. The non-preemptible-partition nudge is still rate-limited via
notify_state.json, because that one really is a nag.
"""

import argparse
import grp
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Preemptible pools are the cheap ones - jobs there are the desired behaviour.
# `gpu_test` is the short debug queue and `shared`/`serial_requeue` are CPU, so
# neither belongs in a "you are burning the good partitions" nudge. Filtering on
# the partition name alone would flag CPU jobs and labdash's own cron job.
PREEMPTIBLE_HINT = "requeue"
EXCLUDED_PARTITIONS = {"gpu_test", "test", "shared", "serial_requeue"}

FULL_THRESHOLD = 90.0   # percent
REMIND_HOURS = 24.0
MIN_GPUS_TO_FLAG = 1


def load(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def fmt_bytes(n):
    if not n:
        return "0"
    for unit, size in (("PiB", 1024**5), ("TiB", 1024**4), ("GiB", 1024**3)):
        if n >= size:
            return f"{n / size:,.1f} {unit}"
    return f"{n / 1024**2:,.0f} MiB"


def storage_report(snap, threshold):
    """The locations at or above the threshold, every day they are. Nothing else.

    Two earlier shapes were wrong in opposite directions. A crossing alarm with
    recovery notices only spoke when something changed, so a location stuck at 92%
    went unmentioned for days and a quiet channel was indistinguishable from a broken
    tool. Listing every location daily fixed that but buried the one line that
    mattered under four that did not. This lists only what is over, and says nothing
    at all when nothing is -- so any message in the channel means action is needed,
    and silence means there is nothing to do.
    """
    over = []
    for s in snap.get("storage", []):
        if not s.get("quota_bytes"):
            continue
        pct = s["used_bytes"] / s["quota_bytes"] * 100
        if pct >= threshold:
            over.append((pct, s))
    if not over:
        return []
    over.sort(key=lambda r: -r[0])

    icon = ":rotating_light:" if over[0][0] >= 98 else ":warning:"
    noun = "location is" if len(over) == 1 else "locations are"
    head = f"{icon} *{len(over)} {noun} at or above {threshold:.0f}% full*"
    lines = [f"> *{pct:.1f}%*  `{s['path']}`  {s['group']}  "
             f"\u2014 only {fmt_bytes(s['quota_bytes'] - s['used_bytes'])} left "
             f"of {fmt_bytes(s['quota_bytes'])}"
             for pct, s in over]
    return [head + "\n" + "\n".join(lines)]


def nonrequeue_alert(snap, state, now):
    """Who is holding GPUs on the non-preemptible partitions right now."""
    by_user = {}
    for j in snap.get("jobs", {}).get("running", []):
        part = j.get("partition", "")
        if (j.get("gpus") or 0) < MIN_GPUS_TO_FLAG:
            continue                              # CPU job, not the point
        if PREEMPTIBLE_HINT in part or part in EXCLUDED_PARTITIONS:
            continue
        rec = by_user.setdefault(j["user"], {"gpus": 0, "jobs": 0, "parts": set()})
        rec["gpus"] += j["gpus"]
        rec["jobs"] += 1
        rec["parts"].add(part)
    if not by_user:
        return []

    key = "nonrequeue"
    last = state.get(key, {}).get("last_sent")
    if last:
        age = (now - datetime.fromisoformat(last)).total_seconds() / 3600
        if age < REMIND_HOURS:
            return []

    lines = [f"*{u}* - {r['gpus']} GPU in {', '.join(sorted(r['parts']))}"
             for u, r in sorted(by_user.items(), key=lambda kv: -kv[1]["gpus"])]
    total = sum(r["gpus"] for r in by_user.values())
    state[key] = {"last_sent": now.isoformat(timespec="seconds")}
    return [":electric_plug: *{n} GPUs on non-preemptible partitions* "
            "(these count hardest against our fairshare - `*_requeue` is the cheap "
            "path if your job can checkpoint)\n> {body}".format(
                n=total, body="\n> ".join(lines))]


_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}


def parse_size(s):
    """'20TiB' / '4T' / '500G' -> bytes."""
    s = s.strip().upper().replace("IB", "").replace("B", "")
    if not s:
        return None
    if s[-1] in _UNITS:
        return int(float(s[:-1]) * _UNITS[s[-1]])
    return int(float(s))


def dir_alerts(scan, overrides, state, now, threshold):
    """Thresholds for DIRECTORIES, which have no quota of their own.

    /n/lab_storage reports no quota at all - every `quota` call there falls back to
    df of the whole 42 PB array - so "90% full" has no denominator the system can
    supply. The size of the lab's allocation has to be told to us once; until then
    this stays silent rather than inventing a number.
    """
    msgs = []
    if not scan:
        return msgs
    for r in scan.get("roots", []):
        root = r.get("root")
        quota = overrides.get(root)
        if not quota or not r.get("complete"):
            continue                     # no denominator, or a partial scan
        used = sum(e.get("bytes") or 0 for e in r.get("entries", []))
        pct = used / quota * 100
        key = f"dir:{root}"
        prev = state.get(key, {})
        was_over, last = prev.get("over", False), prev.get("last_sent")
        over = pct >= threshold

        due = over and (not was_over or not last or
                        (now - datetime.fromisoformat(last)).total_seconds() / 3600
                        >= REMIND_HOURS)
        if due:
            icon = ":rotating_light:" if pct >= 98 else ":warning:"
            msgs.append(
                f"{icon} `{root}` is at {pct:.1f}% of its {fmt_bytes(quota)} allocation\n"
                f"> {fmt_bytes(used)} used, *{fmt_bytes(max(quota - used, 0))} free* "
                f"(nightly scan)")
            state[key] = {"over": True, "last_sent": now.isoformat(timespec="seconds")}
        elif not over and was_over:
            msgs.append(f":white_check_mark: `{root}` is back under {threshold:.0f}% "
                        f"- now {pct:.1f}%")
            state[key] = {"over": False, "last_sent": now.isoformat(timespec="seconds")}
        elif not over:
            state[key] = {"over": False, "last_sent": last}
    return msgs


def read_webhook(path):
    """Load the URL from a file, refusing it if the permissions leak it."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return ""
    if mode & 0o077:
        print(f"notify: {path} is readable by others - chmod 600 it first",
              file=sys.stderr)
        return ""
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        pass
    return ""


def post(webhook, text, key="text"):
    body = json.dumps({key: text}).encode()
    req = urllib.request.Request(
        webhook, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode()[:200]}"
    except (urllib.error.URLError, OSError) as e:
        return False, str(e)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    primary = grp.getgrgid(os.getgid()).gr_name
    ap.add_argument("--out", default=os.environ.get(
        "LABDASH_OUT", f"/n/holylabs/LABS/{primary}/Lab/labdash"))
    ap.add_argument("--webhook-url", default=os.environ.get("SLACK_WEBHOOK_URL", ""))
    ap.add_argument("--webhook-file", default=os.path.expanduser(
        "~/.config/labdash/webhook"),
        help="file holding the webhook URL; anyone with it can post to the channel, "
             "so it must not live in the group-readable output dir or in git")
    # Workflow Builder delivers the POST body as workflow variables, so the key has
    # to match the variable that workflow declared. A plain Incoming Webhook wants
    # "text". Someone else's workflow may want something else entirely.
    ap.add_argument("--payload-key", default=os.environ.get("SLACK_PAYLOAD_KEY", "text"))
    ap.add_argument("--threshold", type=float, default=FULL_THRESHOLD)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the message and leave the state file untouched")
    ap.add_argument("--storage-only", action="store_true")
    ap.add_argument("--jobs-only", action="store_true")
    # /n/lab_storage exposes no quota, so its allocation has to be supplied by hand.
    # Repeatable:  --dir-quota /n/lab_storage/ydu_lab=20TiB
    ap.add_argument("--dir-quota", action="append", default=[],
                    metavar="PATH=SIZE",
                    help="allocation size for a directory that has no quota")
    # Configurable, not baked in: the site already moved from a personal account to
    # the lab org once, and the old URL 404s rather than redirecting.
    ap.add_argument("--site-url", default=os.environ.get(
        "LABDASH_SITE_URL", "https://embodied-minds-lab.github.io/labdash-site/"))
    args = ap.parse_args()

    snap = load(os.path.join(args.out, "snapshot.json"))
    if not snap:
        sys.exit(f"notify: no snapshot.json in {args.out} - run labdash.py first")

    state_path = os.path.join(args.out, "notify_state.json")
    state = load(state_path, {}) or {}
    now = datetime.now(timezone.utc)

    overrides = {}
    for item in args.dir_quota:
        path, _, size = item.partition("=")
        val = parse_size(size)
        if val:
            overrides[path] = val
        else:
            print(f"notify: ignoring unparsable --dir-quota {item!r}", file=sys.stderr)

    msgs = []
    if not args.jobs_only:
        msgs += storage_report(snap, args.threshold)
    if not args.storage_only:
        msgs += nonrequeue_alert(snap, state, now)

    if not msgs:
        print("notify: nothing to say")
        return

    text = "\n\n".join(msgs) + f"\n\n<{args.site_url}|Open the dashboard>"

    webhook = args.webhook_url or read_webhook(args.webhook_file)
    if args.dry_run or not webhook:
        if not webhook and not args.dry_run:
            print("notify: no webhook configured, showing the message instead\n")
        print("-" * 68)
        print(text)
        print("-" * 68)
        return   # state deliberately not saved, so a dry run stays repeatable

    ok, detail = post(webhook, text, args.payload_key)
    print(f"notify: {'sent' if ok else 'FAILED'} - {detail}")
    if ok:
        tmp = state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, state_path)


if __name__ == "__main__":
    main()
