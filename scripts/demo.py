#!/usr/bin/env python3
"""Live demo for the presentation (about 1 minute).

    sudo python3 scripts/demo.py

1. Starts a traced run of the cpu workload and, while it runs, lists the
   eBPF programs and the shared per-thread map that are loaded in the kernel.
2. Runs the same workload again with a CPU hog on the worker CPUs.
3. Prints, for the slowest 1% of requests in each run, where their time went
   (queue, scheduler, interrupts, blocked, own computation), so the effect of
   the hog shows up as scheduler delay on exactly those requests.
"""
import csv
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="tailtag-demo-"))
os.environ["TL_RAW"] = str(TMP)
os.environ["TL_DURATION"] = "5"
os.environ.setdefault("NOISE_LIMIT", "100")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import experiment as E  # noqa: E402


def show_loaded_bpf():
    time.sleep(3)
    progs = E.sh("bpftool prog show").stdout.splitlines()
    names = sorted({l.split(" name ")[1].split()[0] for l in progs
                    if " name " in l and ("tracing" in l or "uprobe" in l or "kprobe" in l)})
    names = [n for n in names if n.startswith("on_")]
    maps = [l for l in E.sh("bpftool map show").stdout.splitlines() if "slots" in l or "gcnt" in l]
    print("\n  eBPF programs loaded in the kernel right now:")
    print("   ", ", ".join(names))
    print("  BPF maps (per-thread 'slots' table is memory-mapped by the application):")
    for m in maps:
        print("   ", " ".join(m.split()[:6]))


def breakdown(run_dir):
    with open(run_dir / "bench.csv") as f:
        rows = [r for r in csv.DictReader(f) if r["warm"] == "0" and r["kvalid"] == "1"]
    lat = sorted(int(r["t_done"]) - int(r["t_enq"]) for r in rows)
    p50, p99 = lat[len(lat) // 2], lat[int(len(lat) * 0.99)]
    tail = [r for r in rows if int(r["t_done"]) - int(r["t_enq"]) >= p99]
    def mean(key):
        return sum(key(r) for r in tail) / len(tail) / 1000
    comp = {
        "queue (waiting for a worker)": mean(lambda r: int(r["t_start"]) - int(r["t_enq"])),
        "scheduler (preempted / wakeup)": mean(lambda r: int(r["pre_wake_ns"]) + int(r["runq_ns"]) + int(r["wake_ns"])),
        "interrupts (IRQ + softirq)": mean(lambda r: int(r["softirq_ns"]) + int(r["hardirq_ns"])),
        "blocked (I/O, locks)": mean(lambda r: int(r["blocked_ns"])),
        "own computation": mean(lambda r: int(r["oncpu_ns"]) - int(r["softirq_ns"]) - int(r["hardirq_ns"])),
    }
    return len(rows), p50 / 1000, p99 / 1000, len(tail), comp


def main():
    if os.geteuid() != 0:
        sys.exit("run as root: sudo python3 scripts/demo.py")
    E.sh("sysctl -w kernel.bpf_stats_enabled=1")
    print("TailTag live demo: cpu workload, 7000 requests/s, 5 s per run\n")
    print("[1/2] traced run, no interference ...")
    t = threading.Thread(target=show_loaded_bpf)
    t.start()
    quiet = E.run_one("demo", "quiet", "cpu", "low", "shm", seed=1)
    t.join()
    print("\n[2/2] same run with a CPU hog on the worker CPUs ...")
    hog = E.run_one("demo", "cpuhog", "cpu", "low", "shm", stressor="cpuhog", seed=1)

    a, b = breakdown(quiet), breakdown(hog)
    print("\nResult: where did the time of the slowest 1% of requests go?")
    print(f"{'':34s}{'no interference':>17s}{'with CPU hog':>15s}")
    print(f"{'requests traced':34s}{a[0]:>17,d}{b[0]:>15,d}")
    print(f"{'p50 latency (us)':34s}{a[1]:>17.0f}{b[1]:>15.0f}")
    print(f"{'p99 latency (us)':34s}{a[2]:>17.0f}{b[2]:>15.0f}")
    print("  mean per tail request (us):")
    for k in a[4]:
        print(f"    {k:30s}{a[4][k]:>17.1f}{b[4][k]:>15.1f}")
    print("\nEvery request was tagged with its own kernel delays, read from the shared")
    print("BPF map with plain memory loads (no system call or trap per request).")
    print(f"(raw run data: {TMP})")


if __name__ == "__main__":
    main()
