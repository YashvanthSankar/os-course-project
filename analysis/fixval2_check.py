#!/usr/bin/env python3
"""Effect of honouring the sched_switch preempt flag (third review).

    python3 analysis/fixval2_check.py --raw data/raw --res results

fixval and fixval2 are the same 18 conditions with the same seeds. fixval
ran the binary with only the torn-read fix; fixval2 also counts a switch-out
as involuntary when the kernel's preempt flag is set (the test __schedule()
uses for nivcsw). Writes results/fixval2.json with, per experiment:
identity on switched requests, the runqueue share of off-CPU time inside
request windows, and (fixval2 only) how often a target thread was preempted
after it had already set a sleeping state.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze as A  # noqa: E402
from review_checks import identity  # noqa: E402


def summarize(raw, exp, thr):
    rows, runq, off, ps, sw = [], 0.0, 0.0, 0, 0
    npre, nwg, nwg_ns, win_ns = 0, 0, 0, 0.0
    for d in A.runs(raw, exp):
        b, _ = A.load_run(d, thr)
        if not len(b):
            continue
        rows.append(identity(b))
        runq += float(b.runq_ns.sum())
        off += float((b.runq_ns + b.wake_ns + b.blocked_ns).sum())
        tj = json.loads((d / "tlat.json").read_text())
        mj = json.loads((d / "meta.json").read_text())
        win_ns += float((b.t_end - b.t_start).sum())
        if "preempt_sleeping" in tj:
            ps += tj["preempt_sleeping"]
            npre += tj.get("npreempt", 0)
            nwg += tj.get("no_wake_gap", 0)
            nwg_ns += tj.get("no_wake_gap_ns", 0)
            sw += mj.get("nvcsw", 0) + mj.get("nivcsw", 0)
    ws = [r["within_switched"] for r in rows if r["n_switched"]]
    out = dict(runs=len(rows),
               switched_share=sum(r["n_switched"] for r in rows) / max(1, sum(r["n"] for r in rows)),
               within_switched_min=float(np.min(ws)),
               within_switched_median=float(np.median(ws)),
               within_all_min=float(min(r["within_all"] for r in rows)),
               runq_share_of_offcpu=runq / off if off else float("nan"))
    if sw:
        out["preempt_sleeping_share_of_switches"] = ps / sw
        out["preempt_sleeping_total"] = ps
        out["npreempt_total"] = npre
        out["npreempt_share_of_switches"] = npre / sw
        out["no_wake_gap_total"] = nwg
        out["no_wake_gap_share_of_switches"] = nwg / sw
        out["no_wake_gap_mean_us"] = nwg_ns / nwg / 1e3 if nwg else 0.0
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--res", default="results")
    a = ap.parse_args()
    raw, res = Path(a.raw), Path(a.res)
    R = json.loads((res / "results.json").read_text())
    thr = {tuple(k.split("-")): v["p99_ns"] for k, v in R["thresholds"].items()}
    out = {exp: summarize(raw, exp, thr) for exp in ("fixval", "fixval2")}
    (res / "fixval2.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
