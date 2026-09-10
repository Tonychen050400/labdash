#!/usr/bin/env python3
"""Build gpu_guide.json: what a GPU-hour actually costs, and on which cards.

The first version of this priced cards by dividing a node's billing weight by its
GPU count. That was wrong twice over: nodes in one partition are not identical (an
H200 node came out anywhere from 473 to 2683), and more importantly price is not a
property of the card at all. Slurm sets it per PARTITION via TRESBillingWeights, so
the same H100 costs 2648.8 on kempner_h100 and 418.25 on gpu_requeue -- a 6.3x
spread that has nothing to do with the hardware and everything to do with whether
the job can be preempted. That is the decision this page has to surface first.
"""
import glob, json, os, re, subprocess, sys
from datetime import datetime

OUT = os.environ.get("LABDASH_OUT", "/n/holylabs/LABS/kempner_ydu_lab/Lab/labdash")
LOGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")

# Vendor specs, one consistent basis for every card. Measured values ride along as a
# reality check but never enter a ratio: the H100 hit 65% of its peak here and the
# RTX 79%, so mixing measured-vs-peak across cards would distort the comparison.
SPECS = {
    "nvidia_rtx_pro_6000_blackwell_server_edition":
        dict(label="RTX PRO 6000", vram=96, bf16=503, mem=1792, nvlink=False),
    "nvidia_h200":              dict(label="H200",      vram=141, bf16=990, mem=4800, nvlink=True),
    "nvidia_h100_80gb_hbm3":    dict(label="H100 80GB", vram=80,  bf16=990, mem=3350, nvlink=True),
    "nvidia_a100-sxm4-80gb":    dict(label="A100 80GB", vram=80,  bf16=312, mem=2039, nvlink=True),
    "nvidia_a100-sxm4-40gb":    dict(label="A100 40GB", vram=40,  bf16=312, mem=1555, nvlink=True),
}
PARTITIONS = ["gpu_requeue", "kempner_requeue", "kempner_rtx", "gpu",
              "kempner_h100", "kempner_h200"]


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def gpu_price(part):
    """Authoritative: the partition's own billing weight for one GPU."""
    w = re.search(r"Gres/gpu=([\d.]+)", sh("scontrol", "show", "partition", part))
    return float(w.group(1)) if w else None


def models_in(part):
    out = sh("sinfo", "-h", "-p", part, "-o", "%G")
    found = {}
    for m in re.findall(r"gpu:([a-z0-9_.\-]+):?(\d*)", out):
        name = m[0]
        if name in SPECS:
            found[name] = found.get(name, 0) + 1
    return sorted(found, key=lambda n: -SPECS[n]["bf16"])


measured = {}
for path in sorted(glob.glob(os.path.join(LOGS, "bench*_*.log")), key=os.path.getmtime):
    for line in open(path, errors="replace"):
        if not line.strip().startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        for key, spec in SPECS.items():
            if spec["label"].split()[0].lower() in d.get("name", "").lower():
                measured[key] = d

parts = []
for p in PARTITIONS:
    price = gpu_price(p)
    if not price:
        continue
    models = models_in(p)
    if not models:
        continue
    parts.append({
        "partition": p,
        "gpu_price": price,
        "preemptible": "requeue" in p,
        "models": [{
            "key": k,
            "label": SPECS[k]["label"],
            "vram_gib": SPECS[k]["vram"],
            "bf16_expected": SPECS[k]["bf16"],
            "mem_expected": SPECS[k]["mem"],
            "nvlink": SPECS[k]["nvlink"],
            "bf16_measured": measured.get(k, {}).get("bf16_tflops"),
            "p2p_measured_gbs": measured.get(k, {}).get("p2p_gbs"),
        } for k in models],
    })

if not parts:
    sys.exit("collect: no partitions resolved")

# A flat card list too, deduped across partitions and with the cheapest partition
# that reaches each one. Cost belongs to the partition, capability belongs to the
# card -- they are different questions and the page asks them separately.
seen, cards = {}, []
for pt in sorted(parts, key=lambda x: x["gpu_price"]):
    for m in pt["models"]:
        if m["key"] in seen:
            continue
        seen[m["key"]] = True
        cards.append(dict(m, cheapest_partition=pt["partition"],
                          cheapest_price=pt["gpu_price"]))
cards.sort(key=lambda c: -(c["bf16_expected"] or 0))
parts.sort(key=lambda x: x["gpu_price"])
doc = {
    "measured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    "measured_local": datetime.now().strftime("%Y-%m-%d"),
    "partitions": parts,
    "cards": cards,
    "cheapest": parts[0]["gpu_price"],
}
tmp = os.path.join(OUT, "gpu_guide.json") + ".tmp"
with open(tmp, "w") as fh:
    json.dump(doc, fh, indent=2)
os.replace(tmp, os.path.join(OUT, "gpu_guide.json"))
print(f"collect: {len(parts)} partitions -> {OUT}/gpu_guide.json")
for c in cards:
    print(f"  card {c['label']:<14} {c['vram_gib']:>4} GiB  {c['bf16_expected']:>4} TFLOP/s  "
          f"{c['mem_expected']:>5} GB/s  nvlink={c['nvlink']}  cheapest via {c['cheapest_partition']}")
for p in parts:
    tag = "preemptible" if p["preemptible"] else "dedicated  "
    best = p["models"][0]
    print(f"  {p['partition']:<17} {p['gpu_price']:>8.1f}/GPU  {p['gpu_price']/parts[0]['gpu_price']:.2f}x  "
          f"{tag}  best card: {best['label']:<13} ({len(p['models'])} model(s))")
