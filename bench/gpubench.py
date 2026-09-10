#!/usr/bin/env python3
"""Measure what actually matters for picking a GPU on this cluster: bf16 matmul
throughput (the compute ML jobs are bound by), memory bandwidth, and VRAM.

Vendor spec sheets are not used anywhere. Numbers people act on should come from
the hardware they will actually queue for."""
import json, os, sys, time
import torch

def bf16_tflops(n=8192, iters=60, warmup=15):
    a = torch.randn(n, n, device="cuda", dtype=torch.bfloat16)
    b = torch.randn(n, n, device="cuda", dtype=torch.bfloat16)
    for _ in range(warmup):
        a @ b
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    for _ in range(iters):
        a @ b
    torch.cuda.synchronize()
    dt = time.perf_counter() - t0
    return (2 * n**3 * iters) / dt / 1e12

def bandwidth_gbs(mb=2048, iters=30):
    n = mb * 1024 * 1024 // 4
    src = torch.empty(n, device="cuda", dtype=torch.float32).uniform_()
    dst = torch.empty_like(src)
    for _ in range(5):
        dst.copy_(src)
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    for _ in range(iters):
        dst.copy_(src)
    torch.cuda.synchronize()
    dt = time.perf_counter() - t0
    return (2 * src.numel() * 4 * iters) / dt / 1e9   # read + write

p = torch.cuda.get_device_properties(0)
out = {
    "name": torch.cuda.get_device_name(0),
    "vram_gib": round(p.total_memory / 2**30, 1),
    "sm_count": p.multi_processor_count,
    "capability": f"{p.major}.{p.minor}",
    "bf16_tflops": round(bf16_tflops(), 1),
    "bandwidth_gbs": round(bandwidth_gbs()),
    "partition": os.environ.get("SLURM_JOB_PARTITION", "?"),
    "node": os.uname().nodename,
    "torch": torch.__version__,
}
# Card-to-card bandwidth is the number that decides whether DDP hurts here. The
# can_device_access_peer boolean is not enough: it comes back True over plain PCIe
# too, so "peer access works" says nothing about whether gradient sync will be slow.
# Measure the actual transfer rate instead.
def p2p_gbs(mb=1024, iters=20):
    src = torch.empty(mb * 1024 * 1024 // 4, device="cuda:0", dtype=torch.float32).uniform_()
    dst = torch.empty_like(src, device="cuda:1")
    for _ in range(5):
        dst.copy_(src)
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    for _ in range(iters):
        dst.copy_(src)
    torch.cuda.synchronize()
    return (src.numel() * 4 * iters) / (time.perf_counter() - t0) / 1e9

if torch.cuda.device_count() > 1:
    out["gpus_visible"] = torch.cuda.device_count()
    out["p2p_enabled"] = bool(torch.cuda.can_device_access_peer(0, 1))
    try:
        out["p2p_gbs"] = round(p2p_gbs())
    except Exception as exc:
        out["p2p_gbs"] = None
        out["p2p_error"] = str(exc)[:120]
print(json.dumps(out))
