#!/usr/bin/env python3
"""Quick self-test of the whole pipeline (about 1 minute).

    sudo make test        (or: sudo python3 scripts/smoke_test.py)

Runs four short real runs through the same driver as the experiments, in a
temporary directory, and checks:

  1. shm mode: every measured request has valid kernel features, nothing is
     lost, and the accounting identity (on-CPU + runqueue + wakeup + blocked
     = service window) holds within 5 us for >= 99% of requests
  2. attribution follows the cause: a CPU hog on the worker CPUs raises the
     mean preemption wait per request at least 5x over the run without it
  3. attrib (uprobe) mode: the kernel-side request records join with the
     benchmark's requests by id
  4. count mode: the 5 ms aggregate samples are written and see switches
  5. the BPF verifier accepted the program (implied by 1-4)

Exits 0 only if every check passes.
"""
import csv
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="tailtag-smoke-"))
os.environ["TL_RAW"] = str(TMP)
os.environ["TL_DURATION"] = "4"
os.environ.setdefault("NOISE_LIMIT", "100")   # a self-test should not wait for a quiet host
sys.path.insert(0, str(Path(__file__).resolve().parent))
import experiment as E  # noqa: E402

failures = []


def check(name, ok, detail):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        failures.append(name)


def measured(path):
    with open(path) as f:
        return [r for r in csv.DictReader(f) if r["warm"] == "0"]


def mean(xs):
    return sum(xs) / max(1, len(xs))


def main():
    if os.geteuid() != 0:
        sys.exit("run as root: sudo make test")
    for p in (E.TLBENCH, E.TLAT, E.STRESSOR):
        if not Path(p).exists():
            sys.exit(f"missing {p}: run make first")
    E.sh("sysctl -w kernel.bpf_stats_enabled=1")

    # 1. shm mode, no stressor
    d = E.run_one("smoke", "cpu-shm", "cpu", "low", "shm", seed=1)
    rows = measured(d / "bench.csv")
    tj = json.loads((d / "tlat.json").read_text())
    valid = mean([r["kvalid"] == "1" for r in rows])
    ident = mean([abs((int(r["t_end"]) - int(r["t_start"])) -
                      (int(r["oncpu_ns"]) + int(r["runq_ns"]) + int(r["wake_ns"]) + int(r["blocked_ns"])))
                  < 5000 for r in rows])
    check("requests measured", len(rows) > 1000, f"{len(rows)} requests")
    check("kernel features on every request", valid == 1.0, f"{100 * valid:.2f}% valid")
    check("no lost events / slot collisions", tj["rb_drops"] == 0 and tj["slot_collisions"] == 0,
          f"drops={tj['rb_drops']} collisions={tj['slot_collisions']}")
    check("accounting identity within 5 us", ident >= 0.99, f"{100 * ident:.2f}% of requests")
    base = mean([int(r["runq_ns"]) for r in rows])

    # 2. same run with a CPU hog on the worker CPUs
    d = E.run_one("smoke", "cpu-shm-hog", "cpu", "low", "shm", stressor="cpuhog", seed=1)
    hog = mean([int(r["runq_ns"]) for r in measured(d / "bench.csv")])
    check("CPU hog raises preemption wait", hog >= 5 * max(base, 1),
          f"{base / 1e3:.2f} -> {hog / 1e3:.2f} us per request")

    # 3. uprobe windows
    d = E.run_one("smoke", "cpu-attrib", "cpu", "low", "attrib", seed=1)
    ids = {r["id"] for r in measured(d / "bench.csv")}
    with open(d / "tlat.req.csv") as f:
        kids = {r["id"] for r in csv.DictReader(f)}
    cov = len(ids & kids) / max(1, len(ids))
    check("uprobe records join by request id", cov >= 0.99, f"{100 * cov:.2f}% of requests")

    # 4. counters-only mode
    d = E.run_one("smoke", "cpu-count", "cpu", "low", "count", seed=1)
    with open(d / "tlat.agg.csv") as f:
        agg = list(csv.DictReader(f))
    sw = sum(int(r["nswitch"]) for r in agg)
    check("aggregate samples written", len(agg) > 100 and sw > 0, f"{len(agg)} samples, {sw} switches")

    print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILED: ' + ', '.join(failures)}"
          f"  (run data in {TMP})")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
