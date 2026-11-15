#!/usr/bin/env python3
"""Robustness checks behind the measurement-validity and H1 claims in the report.

    python3 analysis/robustness_checks.py --raw data/raw --res results

Run after analyze.py (uses its calibrated thresholds). Writes
results/robustness_checks.json. One pass over the raw runs computes:

  noise_wait     how long the noise gate made runs wait (it never rejects)
  identity       accounting identity within 5 us, all requests vs. requests
                 with >= 1 context switch (the non-trivial subset), for the
                 campaign binary and for the re-run after the torn-read fix
  no_signal      share of tail requests without OS signal at 5/10/20 us floors
  rotation       train on two workloads, test on the third, all three ways
  netfresh       frozen standard model (train cpu+kv reps 0-2) on fresh net
                 runs whose seeds were never used while designing features
"""
import argparse
import gc
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze as A  # noqa: E402

FLOORS = (5_000, 10_000, 20_000)
FEATS = {
    "aligned": A.ALIGNED + A.INHERIT,
    "aggregate@20ms": [f"{c}@20" for c in A.AGG_BASE],
    "queue proxy": ["qlen_proxy"],
}


def identity(b):
    win = (b.t_end - b.t_start).values
    acct = (b.oncpu_ns + b.runq_ns + b.wake_ns + b.blocked_ns).values
    ok = np.abs(win - acct) < 5000
    sw = (b.nvcsw + b.nivcsw).values >= 1
    return dict(n=int(len(b)), within_all=float(ok.mean()),
                n_switched=int(sw.sum()),
                within_switched=float(ok[sw].mean()) if sw.any() else float("nan"))


def no_signal_counts(b, acc):
    tails = b.is_tail.values == 1
    if not tails.any():
        return
    base = A.SIG_FLOOR_NS
    for f in FLOORS:
        A.SIG_FLOOR_NS = f
        bb = A.add_inherited(b[["t_enq", "t_start", "t_end", "pre_wake_ns", "runq_ns", "wake_ns",
                                "blocked_ns", "softirq_ns", "hardirq_ns"]].copy())
        bb["is_tail"] = b.is_tail.values
        t = A.touched(bb)
        c = acc.setdefault(f, [0, 0])
        c[0] += int(t["none"].values[tails].sum())
        c[1] += int(tails.sum())
    A.SIG_FLOOR_NS = base


def ap_ci(df, p):
    y, w = df.is_tail.values, df.w.values
    ap = average_precision_score(y, p, sample_weight=w)
    rid = df.run_id.values
    uniq = np.unique(rid)
    groups = {r: np.where(rid == r)[0] for r in uniq}
    rng = np.random.default_rng(1)
    aps = []
    for _ in range(200):
        ii = np.concatenate([groups[r] for r in rng.choice(uniq, len(uniq))])
        if y[ii].sum():
            aps.append(average_precision_score(y[ii], p[ii], sample_weight=w[ii]))
    return dict(ap=float(ap), lo=float(np.percentile(aps, 2.5)), hi=float(np.percentile(aps, 97.5)),
                base_rate=float(np.average(y, weights=w)), runs=int(len(uniq)))


def evaluate(train, sets):
    out = {}
    for fs, cols in FEATS.items():
        ms = A.fit_models(train, cols)
        for mname, m in ms.items():
            for sname, sdf in sets.items():
                if len(sdf) and sdf.is_tail.sum():
                    p = m.predict_proba(A.featurize(sdf, cols))[:, 1]
                    out[f"{fs}|{mname}|{sname}"] = ap_ci(sdf, p)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--res", default="results")
    a = ap.parse_args()
    raw, res = Path(a.raw), Path(a.res)
    R = json.loads((res / "results.json").read_text())
    thr = {tuple(k.split("-")): v["p99_ns"] for k, v in R["thresholds"].items()}
    out = {}

    # noise gate waits (all runs)
    waits = []
    for c in raw.glob("*/*/cond.json"):
        d = json.loads(c.read_text())
        waits.append((d["exp"], float(d.get("noise_wait_s", 0) or 0)))
    w = np.array([x for _, x in waits])
    out["noise_wait"] = dict(runs=int(len(w)), waited=int((w > 0).sum()),
                             median_wait_s_of_waiting=float(np.median(w[w > 0])) if (w > 0).any() else 0.0,
                             max_wait_s=float(w.max()), total_wait_min=float(w.sum() / 60))

    frames, ident, nosig = [], {"campaign": [], "fixed": []}, {}
    for exp in ("main", "interv", "fixval", "netfresh"):
        for d in A.runs(raw, exp):
            cond = json.loads((d / "cond.json").read_text())
            if cond["tracer"] != "shm":
                continue
            b, info = A.load_run(d, thr)
            if "is_tail" not in b or not len(b):
                continue
            ident["fixed" if exp in ("fixval", "netfresh") else "campaign"].append(identity(b))
            if exp in ("main", "interv"):
                no_signal_counts(b, nosig)
            b["run_id"] = f"{exp}/{info['run_id']}"
            b["exp"] = exp
            b = A.thin(b, seed=len(frames))
            keep = (["is_tail", "w", "thr", "workload", "rep", "run_id", "exp", "qlen_proxy"] +
                    A.ALIGNED + A.INHERIT + [c for c in b.columns if "@" in c])
            b = b[keep]
            num = [c for c in keep if c not in ("workload", "run_id", "exp", "is_tail", "rep")]
            b[num] = b[num].astype("float32")
            frames.append(b)
            gc.collect()

    for k, rows in ident.items():
        if rows:
            df = pd.DataFrame(rows)
            out[f"identity_{k}"] = dict(runs=len(df),
                                        within_all_min=float(df.within_all.min()),
                                        within_all_median=float(df.within_all.median()),
                                        switched_share=float(df.n_switched.sum() / df.n.sum()),
                                        within_switched_min=float(df.within_switched.min()),
                                        within_switched_median=float(df.within_switched.median()))
    metas = [json.loads((d / "meta.json").read_text()) for d in A.runs(raw, "fixval")]
    out["ksnap_retries_per_run_median"] = float(np.median([m.get("ksnap_retries", 0) for m in metas])) if metas else None
    out["no_signal"] = {f"{f // 1000}us": v[0] / max(1, v[1]) for f, v in nosig.items()}

    F = pd.concat(frames, ignore_index=True)
    camp = F[F.exp.isin(["main", "interv"])]

    # held-out rotation: train on two workloads (reps 0-2), test on the third
    rot = {}
    for held in ("cpu", "kv", "net"):
        dev = camp[camp.workload != held]
        train = dev[dev.rep <= 2]
        rot[held] = evaluate(train, {"held": camp[camp.workload == held]})
        print("rotation", held, "done", flush=True)
    out["rotation"] = rot

    # fresh net: standard split, frozen definitions, never-seen seeds
    dev = camp[camp.workload.isin(["cpu", "kv"])]
    out["netfresh"] = evaluate(dev[dev.rep <= 2], {"netfresh": F[F.exp == "netfresh"]})

    (res / "robustness_checks.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v for k, v in out.items() if k not in ("rotation", "netfresh")}, indent=1))


if __name__ == "__main__":
    main()
