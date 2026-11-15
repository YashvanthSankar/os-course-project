#!/usr/bin/env python3
"""Share of tail requests with no OS-delay signal (no own or inherited
scheduler / IRQ / blocking delay >= 10 us), per evaluation split.

    python3 analysis/no_os_delay.py --raw data/raw --res results
Writes results/no_os_delay.json. Run after analyze.py (uses its thresholds).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze as A  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--res", default="results")
    a = ap.parse_args()
    R = json.loads((Path(a.res) / "results.json").read_text())
    thr = {tuple(k.split("-")): v["p99_ns"] for k, v in R["thresholds"].items()}
    out = {}
    for exp in ("main", "interv"):
        for d in A.runs(a.raw, exp):
            c = json.loads((d / "cond.json").read_text())
            if c["tracer"] != "shm":
                continue
            b, _ = A.load_run(d, thr)
            grp = "held" if c["workload"] == "net" else ("test" if b.rep.iat[0] >= 3 else "train")
            t = A.touched(b)
            tails = b.is_tail.values == 1
            o = out.setdefault(grp, [0, 0])
            o[0] += int(t["none"].values[tails].sum())
            o[1] += int(tails.sum())
    res = {k: v[0] / max(1, v[1]) for k, v in out.items()}
    (Path(a.res) / "no_os_delay.json").write_text(json.dumps(res))
    print(res)


if __name__ == "__main__":
    main()
