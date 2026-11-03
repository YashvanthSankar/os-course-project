#!/usr/bin/env python3
"""Analysis pipeline: raw run data -> tables, figures and results.json.

    python3 analysis/analyze.py [--raw data/raw] [--out results]

Stages (numbering follows the assignment):
  A  latency baseline per workload / load, run-to-run spread
  B  tracer overhead (closed-loop throughput, collector CPU, BPF run time, drops)
  C  join request windows with kernel telemetry, validate the accounting
  D  tail detection: rule vs logistic regression vs random forest, using
     aligned per-request features vs aggregate event counts (H1);
     train/test split by complete run; held-out workload (net)
  E  intervention signatures (H2): tail attribution per stressor condition
"""
import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, precision_recall_curve
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

plt.rcParams.update({"font.size": 8, "axes.titlesize": 8, "axes.labelsize": 8,
                     "legend.fontsize": 7, "xtick.labelsize": 7, "ytick.labelsize": 7,
                     "figure.dpi": 150, "pdf.fonttype": 42})
COL = {"cpu": "#1f77b4", "kv": "#d62728", "net": "#2ca02c"}
RNG = np.random.default_rng(42)

SCHED = ["pre_wake_ns", "runq_ns", "wake_ns", "nivcsw", "nmigr"]
IRQ = ["softirq_ns", "hardirq_ns", "nsoftirq", "nhardirq"]
BLOCK = ["blocked_ns", "nvcsw"]
ALIGNED = SCHED + IRQ + BLOCK
# OS delay the workers suffered while this request waited in the queue
INHERIT = ["inh_sched_ns", "inh_irq_ns", "inh_blocked_ns"]
AGG_BASE = ["nswitch", "ninvol", "nwakeup", "nmigr_g", "nhardirq_g", "hardirq_ns_g",
            "nsoft_all", "softns_all", "nsoft_netrx", "nsoft_timer", "psi_cpu", "psi_io"]
NS_COLS = [c for c in ALIGNED if c.endswith("_ns")]


# ---------------------------------------------------------------- loading
def read_json(p):
    return json.loads(Path(p).read_text())


def load_agg(path):
    a = pd.read_csv(path)
    out = pd.DataFrame({"t": a["t"]})
    out["nswitch"] = a["nswitch"]
    out["ninvol"] = a["ninvol"]
    out["nwakeup"] = a["nwakeup"]
    out["nmigr_g"] = a["nmigr"]
    out["nhardirq_g"] = a["nhardirq"]
    out["hardirq_ns_g"] = a["hardirq_ns"]
    out["nsoft_all"] = a[[f"nsoft{v}" for v in range(10)]].sum(axis=1)
    out["softns_all"] = a[[f"softns{v}" for v in range(10)]].sum(axis=1)
    out["nsoft_netrx"] = a["nsoft3"]
    out["nsoft_timer"] = a["nsoft1"]
    out["psi_cpu"] = a["psi_cpu_us"].diff().fillna(0).clip(lower=0)
    out["psi_io"] = a["psi_io_us"].diff().fillna(0).clip(lower=0)
    return out


def attach_agg(df, agg, windows=(5, 20, 100), step_ms=5):
    """For each request, sum aggregate counters over the window ending at
    the sample that covers its completion time. Returned as rates per ms."""
    idx = np.searchsorted(agg["t"].values, df["t_done"].values, side="left")
    idx = np.clip(idx, 0, len(agg) - 1)
    for w in windows:
        k = max(1, w // step_ms)
        roll = agg[AGG_BASE].rolling(k, min_periods=1).sum() / (k * step_ms)
        vals = roll.values[idx]
        for j, c in enumerate(AGG_BASE):
            df[f"{c}@{w}"] = vals[:, j]
    return df


SIG_FLOOR_NS = 10_000  # an OS delay below 10 us is not counted as an "event"


def add_inherited(b, workers=3):
    """Queueing-aware attribution. A request that waits in the queue is late
    because the requests ahead of it were slow; if *they* were preempted or
    interrupted, that OS delay is inherited. For each request we sum the
    significant OS delay (portion above SIG_FLOOR_NS, so that many tiny
    delays cannot add up to a proxy for queue length) of all requests that
    completed while it waited (t_enq..t_start), divided by the number of
    workers. 'qlen_proxy' (completions during the wait / workers) is kept as
    a control: it encodes pure queueing and no OS information."""
    if not len(b):
        for c in INHERIT + ["qlen_proxy"]:
            b[c] = 0.0
        return b
    order = np.argsort(b["t_end"].values, kind="stable")
    t_end = b["t_end"].values[order]
    sig = lambda v: np.clip(v.astype(float) - SIG_FLOOR_NS, 0, None)  # noqa: E731
    comps = {
        "inh_sched_ns": sig((b["pre_wake_ns"] + b["runq_ns"] + b["wake_ns"]).values),
        "inh_irq_ns": sig((b["softirq_ns"] + b["hardirq_ns"]).values),
        "inh_blocked_ns": sig(b["blocked_ns"].values),
        "qlen_proxy": np.ones(len(b)),
    }
    ia = np.searchsorted(t_end, b["t_enq"].values, side="right")
    ib = np.searchsorted(t_end, b["t_start"].values, side="right")
    ib = np.maximum(ia, ib)
    for c, v in comps.items():
        cs = np.concatenate([[0.0], np.cumsum(v[order].astype(float))])
        b[c] = (cs[ib] - cs[ia]) / workers
    return b


def load_run(d, thr_map=None):
    d = Path(d)
    cond = read_json(d / "cond.json")
    meta = read_json(d / "meta.json")
    b = pd.read_csv(d / "bench.csv")
    b = b[b["warm"] == 0].copy()
    # Latency is measured from the moment the request was actually injected.
    # The gap between scheduled and actual injection is load-generator
    # lateness: a property of the measurement harness, reported separately.
    b["lat"] = b["t_done"] - b["t_enq"]
    b["genlate"] = (b["t_enq"] - b["t_arr"]).clip(lower=0)
    info = dict(cond)
    info.update({"genlate_p99": pct(b["genlate"], 99) if len(b) else float("nan"),
                 "noise_before": cond.get("noise_before_pct"), "noise_after": cond.get("noise_after_pct")})
    info.update({"throughput": meta["throughput"], "cpu_s": meta["cpu_s"],
                 "wall_s": meta["wall_s"], "measured": meta["measured"],
                 "overflow": meta["queue_overflow"], "maxrss_kb": meta["maxrss_kb"]})
    if (d / "tlat.json").exists():
        info.update({f"tl_{k}": v for k, v in read_json(d / "tlat.json").items()})
    have_k = False
    if "kvalid" in b.columns:          # trap-free shared-counter mode
        info["coverage"] = float(b["kvalid"].mean())
        b = b[b["kvalid"] == 1].copy()
        if "oncpu_ns" not in b.columns:  # older format: derive (no validation possible)
            b["oncpu_ns"] = ((b["t_end"] - b["t_start"]) - b["runq_ns"] - b["wake_ns"]
                             - b["blocked_ns"]).clip(lower=0)
        have_k = True
    elif (d / "tlat.req.csv").exists():  # uprobe mode
        k = pd.read_csv(d / "tlat.req.csv")
        b = b.merge(k, on="id", how="left")
        info["coverage"] = float(b["runq_ns"].notna().mean())
        b = b[b["runq_ns"].notna()].copy()
        have_k = True
    if have_k:
        b = add_inherited(b)
    if have_k and (d / "tlat.agg.csv").exists():
        b = attach_agg(b, load_agg(d / "tlat.agg.csv"))
    if thr_map is not None:
        key = (cond["workload"], cond["load"])
        if key in thr_map:
            b["thr"] = thr_map[key]
            b["is_tail"] = (b["lat"] > thr_map[key]).astype(int)
    for c in ("exp", "run_id", "workload", "load", "tracer", "stressor", "seed"):
        b[c] = cond[c]
    b["rep"] = int(cond["run_id"].rsplit("-r", 1)[-1]) if "-r" in cond["run_id"] else 0
    return b, info


def runs(raw, exp):
    p = Path(raw) / exp
    return sorted(x for x in p.iterdir() if (x / "meta.json").exists()) if p.exists() else []


def pct(x, q):
    return float(np.percentile(x, q)) if len(x) else float("nan")


def boot_ci(vals, f=np.median, n=2000):
    vals = np.asarray(vals, float)
    vals = vals[~np.isnan(vals)]
    if len(vals) < 2:
        return (float("nan"), float("nan"))
    s = [f(RNG.choice(vals, len(vals))) for _ in range(n)]
    return (float(np.percentile(s, 2.5)), float(np.percentile(s, 97.5)))


# ---------------------------------------------------------------- stage A
def stage_a(raw, out, res):
    thr = {}
    calib = {}
    for d in runs(raw, "calib"):
        b, info = load_run(d)
        key = (info["workload"], info["load"])
        thr[key] = pct(b["lat"], 99)
        calib[f"{key[0]}-{key[1]}"] = {"p99_ns": thr[key], "n": len(b)}
    res["thresholds"] = calib

    rows, curves = [], {}
    for d in runs(raw, "main"):
        b, info = load_run(d)
        lat = b["lat"].values
        rows.append(dict(workload=info["workload"], load=info["load"], tracer=info["tracer"],
                         rep=b["rep"].iat[0] if len(b) else 0, n=len(lat),
                         p50=pct(lat, 50), p95=pct(lat, 95), p99=pct(lat, 99),
                         p999=pct(lat, 99.9), thr=info["throughput"],
                         cpu_util=info["cpu_s"] / info["wall_s"] / 3.0,
                         overflow=info["overflow"], genlate_p99=info["genlate_p99"],
                         noise=info.get("noise_before")))
        key = (info["workload"], info["load"], info["tracer"])
        curves.setdefault(key, []).append(np.sort(lat))
    df = pd.DataFrame(rows)
    df.to_csv(out / "latency_runs.csv", index=False)
    summ = []
    for (wl, ld, tr), g in df.groupby(["workload", "load", "tracer"]):
        r = dict(workload=wl, load=ld, tracer=tr, runs=len(g), n_med=int(g.n.median()))
        for q in ("p50", "p95", "p99", "p999"):
            r[q] = g[q].median()
            r[q + "_lo"], r[q + "_hi"] = g[q].min(), g[q].max()
        r["p99_cv"] = g["p99"].std() / g["p99"].mean()
        r["thr"] = g["thr"].median()
        r["cpu_util"] = g["cpu_util"].median()
        summ.append(r)
    sm = pd.DataFrame(summ)
    sm.to_csv(out / "latency_summary.csv", index=False)
    res["latency"] = sm.to_dict(orient="records")

    # latency inflation from tracing (open loop)
    infl = []
    for (wl, ld), g in sm.groupby(["workload", "load"]):
        if set(g.tracer) >= {"none", "shm"}:
            n0 = g[g.tracer == "none"].iloc[0]
            n1 = g[g.tracer == "shm"].iloc[0]
            infl.append(dict(workload=wl, load=ld,
                             p50_infl=n1.p50 / n0.p50 - 1, p99_infl=n1.p99 / n0.p99 - 1))
    res["latency_inflation"] = infl

    # Figure 1: CCDF
    order = ["low", "mid", "high"]
    fig, axs = plt.subplots(1, 3, figsize=(7.0, 2.0), sharey=True)
    for ax, wl in zip(axs, ["cpu", "kv", "net"]):
        for li, ld in enumerate(order):
            for tr, ls in (("none", "-"), ("shm", "--")):
                cs = curves.get((wl, ld, tr))
                if not cs:
                    continue
                x = np.concatenate(cs)
                x.sort()
                y = 1.0 - np.arange(1, len(x) + 1) / len(x)
                sel = np.unique(np.geomspace(1, len(x), 400).astype(int) - 1)
                ax.plot(x[sel] / 1e3, np.maximum(y[sel], 1e-6), ls, lw=1,
                        color=plt.cm.viridis(0.15 + 0.35 * li),
                        label=f"{ld}{' traced' if tr == 'shm' else ''}")
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_ylim(1e-5, 1)
        ax.set_title(f"{wl} workload")
        ax.set_xlabel("latency (µs)")
        ax.grid(alpha=0.3, which="both", lw=0.3)
    axs[0].set_ylabel("P(latency > x)")
    axs[2].legend(ncol=1, frameon=False, loc="lower left")
    fig.tight_layout(pad=0.3)
    fig.savefig(out / "fig_latency.pdf")
    plt.close(fig)
    return thr


# ---------------------------------------------------------------- stage B
def stage_b(raw, out, res):
    rows = []
    for d in runs(raw, "overhead"):
        info = load_run(d)[1]
        rows.append(info)
    if not rows:
        return
    df = pd.DataFrame(rows)
    df["closed"] = df["rate"] == 0
    df["collector_cpu"] = (df.get("tl_collector_user_s", 0) + df.get("tl_collector_sys_s", 0)) / df["wall_s"]
    df["bpf_cpu"] = df.get("tl_bpf_run_ns", 0) / 1e9 / df["wall_s"]
    df.to_csv(out / "overhead_runs.csv", index=False)
    summ = []
    cl = df[df.closed]
    for wl, g in cl.groupby("workload"):
        base = g[g.tracer == "none"]["throughput"].median()
        for tr, h in g.groupby("tracer"):
            loss = 1 - h["throughput"] / base
            lo, hi = boot_ci(loss.values)
            summ.append(dict(workload=wl, tracer=tr, runs=len(h),
                             thr_med=h["throughput"].median(), loss_med=float(loss.median()),
                             loss_lo=lo, loss_hi=hi,
                             collector_cpu=float(h["collector_cpu"].median()) if tr != "none" else 0.0,
                             bpf_cpu=float(h["bpf_cpu"].median()) if tr != "none" else 0.0,
                             drops=float(h.get("tl_rb_drops", pd.Series([0])).fillna(0).sum()),
                             events=float(h.get("tl_rb_submitted", pd.Series([0])).fillna(0).median()),
                             raw_mb=float(h.get("tl_raw_bytes", pd.Series([0])).fillna(0).median() / 1e6),
                             map_kb=float(h.get("tl_map_bytes_max", pd.Series([0])).fillna(0).median() / 1024)))
    sm = pd.DataFrame(summ)
    sm.to_csv(out / "overhead_summary.csv", index=False)
    res["overhead"] = sm.to_dict(orient="records")
    op = df[~df.closed & (df.tracer == "stream")]
    if len(op):
        res["stream_open"] = [dict(workload=r.workload, drops=r.get("tl_rb_drops"),
                                   events=r.get("tl_rb_submitted"),
                                   drop_frac=(r.get("tl_rb_drops") or 0) /
                                   max(1, (r.get("tl_rb_drops") or 0) + (r.get("tl_rb_submitted") or 0)))
                              for _, r in op.iterrows()]

    # Figure 2
    modes = ["count", "shm", "attrib", "stream"]
    fig, ax = plt.subplots(figsize=(3.4, 1.9))
    w = 0.38
    for i, wl in enumerate(["cpu", "kv"]):
        s = sm[sm.workload == wl].set_index("tracer")
        ys = [100 * s.loc[m, "loss_med"] if m in s.index else np.nan for m in modes]
        lo = [100 * (s.loc[m, "loss_med"] - s.loc[m, "loss_lo"]) if m in s.index else 0 for m in modes]
        hi = [100 * (s.loc[m, "loss_hi"] - s.loc[m, "loss_med"]) if m in s.index else 0 for m in modes]
        x = np.arange(len(modes)) + (i - 0.5) * w
        ax.bar(x, ys, w, yerr=[lo, hi], capsize=2, color=COL[wl], label=wl, alpha=0.85)
    ax.axhline(5, color="k", ls=":", lw=0.8)
    ax.text(len(modes) - 0.55, 5.4, "5% budget", fontsize=6, ha="right")
    ax.set_xticks(range(len(modes)))
    ax.set_xticklabels(["counters\nonly", "shared\ncounters", "uprobe\nwindows", "per-event\nstream"])
    ax.set_ylabel("throughput loss (%)")
    ax.legend(frameon=False)
    ax.grid(axis="y", alpha=0.3, lw=0.3)
    fig.tight_layout(pad=0.3)
    fig.savefig(out / "fig_overhead.pdf")
    plt.close(fig)


# ---------------------------------------------------------------- stage C
def components(b):
    """Per-request latency decomposition (ns)."""
    c = pd.DataFrame(index=b.index)
    pre = (b["t_start"] - b["t_enq"]).clip(lower=0)
    c["sched"] = b["pre_wake_ns"] + b["runq_ns"] + b["wake_ns"]
    c["irq"] = b["softirq_ns"] + b["hardirq_ns"]
    c["blocked"] = b["blocked_ns"]
    c["exec"] = (b["oncpu_ns"] - c["irq"]).clip(lower=0)
    q = (pre - b["pre_wake_ns"]).clip(lower=0)
    if "inh_sched_ns" in b:
        inh = b[INHERIT].sum(axis=1)
        scale = np.where(inh > q, q / inh.where(inh > 0, 1), 1.0)  # never exceed the wait
        c["sched"] += b["inh_sched_ns"] * scale
        c["irq"] += b["inh_irq_ns"] * scale
        c["blocked"] += b["inh_blocked_ns"] * scale
        q = (q - inh * scale).clip(lower=0)
    c["queue"] = q
    return c


def validate(b):
    """In-window accounting: (t_end - t_start) should equal
    oncpu + runq + wake + blocked up to the probe cost."""
    win = b["t_end"] - b["t_start"]
    acct = b["oncpu_ns"] + b["runq_ns"] + b["wake_ns"] + b["blocked_ns"]
    resid = (win - acct).values
    return {"resid_med_ns": float(np.median(resid)), "resid_p99_abs_ns": float(np.percentile(np.abs(resid), 99)),
            "frac_within_5us": float(np.mean(np.abs(resid) < 5000))}


# ---------------------------------------------------------------- stage D
def featurize(b, cols, norm=True):
    X = b[cols].astype(float).copy()
    if norm:
        for c in cols:
            if c.endswith("_ns"):
                X[c] = X[c] / b["thr"]
    return np.log1p(X.clip(lower=0).values)


def thin(b, neg_frac=0.05, seed=0):
    """Keep every tail request and a random neg_frac of the others, with
    weight 1/neg_frac, so all metrics below stay unbiased (weighted)."""
    keep = (b["is_tail"].values == 1) | (np.random.default_rng(seed).random(len(b)) < neg_frac)
    out = b[keep].copy()
    out["w"] = np.where(out["is_tail"].values == 1, 1.0, 1.0 / neg_frac)
    return out


def subsample(df, neg_frac=0.5, seed=0):
    pos = df[df.is_tail == 1]
    neg = df[df.is_tail == 0].sample(frac=neg_frac, random_state=seed)
    s = pd.concat([pos, neg])
    w = s["w"].values * np.where(s.is_tail.values == 1, 1.0, 1.0 / neg_frac)
    return s, w


def best_f1_threshold(y, p, w=None):
    pr, rc, th = precision_recall_curve(y, p, sample_weight=w)
    f1 = 2 * pr * rc / np.maximum(pr + rc, 1e-12)
    i = int(np.nanargmax(f1[:-1])) if len(th) else 0
    return float(th[i]) if len(th) else 0.5


def eval_scores(df, y, p, t, minutes):
    w = df["w"].values
    yhat = p >= t
    tp = float(np.sum(w[yhat & (y == 1)]))
    fp = float(np.sum(w[yhat & (y == 0)]))
    fn = float(np.sum(w[~yhat & (y == 1)]))
    prec = tp / max(1e-12, tp + fp)
    rec = tp / max(1e-12, tp + fn)
    f1 = 2 * prec * rec / max(1e-12, prec + rec)
    ap = average_precision_score(y, p, sample_weight=w) if y.sum() else float("nan")
    # bootstrap AP over runs
    aps = []
    rid = df["run_id"].values
    uniq = np.unique(rid)
    if len(uniq) > 1:
        groups = {r: np.where(rid == r)[0] for r in uniq}
        for _ in range(200):
            pick = RNG.choice(uniq, len(uniq))
            ii = np.concatenate([groups[r] for r in pick])
            if y[ii].sum():
                aps.append(average_precision_score(y[ii], p[ii], sample_weight=w[ii]))
    lo, hi = (np.percentile(aps, 2.5), np.percentile(aps, 97.5)) if aps else (np.nan, np.nan)
    return dict(ap=float(ap), ap_lo=float(lo), ap_hi=float(hi), precision=prec, recall=rec,
                f1=f1, tp=tp, fp=fp, fn=fn, fa_per_min=fp / max(minutes, 1e-9),
                base_rate=float(np.average(y, weights=w)))


def fit_models(train, cols, norm=True):
    s, w = subsample(train)
    X = featurize(s, cols, norm)
    y = s.is_tail.values
    lr = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000, C=1.0,
                                                            class_weight="balanced"))
    lr.fit(X, y, logisticregression__sample_weight=w)
    rf = RandomForestClassifier(n_estimators=150, max_depth=10, min_samples_leaf=20,
                                n_jobs=-1, random_state=0, class_weight="balanced_subsample")
    rf.fit(X, y, sample_weight=w)
    return {"logreg": lr, "forest": rf}


def run_minutes(df):
    return df.groupby("run_id").size().shape[0] * df.attrs.get("dur_min", 0.25)


def stage_d(frames, out, res):
    dev = frames[frames.workload.isin(["cpu", "kv"])]
    reps = sorted(dev.rep.unique())
    n_train = max(1, int(round(0.6 * len(reps))))  # 5 reps -> 0,1,2 train; 3,4 test
    train = dev[dev.rep.isin(reps[:n_train])]
    test = dev[dev.rep.isin(reps[n_train:])]
    held = frames[frames.workload == "net"]
    dur_min = 15 / 60.0
    sets = {k: v for k, v in {"test (cpu+kv, unseen runs)": test,
                              "held-out workload (net)": held}.items()
            if len(v) and v.is_tail.sum() > 0}
    res["split"] = {"train_runs": int(train.run_id.nunique()), "test_runs": int(test.run_id.nunique()),
                    "held_runs": int(held.run_id.nunique()), "train_rows": len(train),
                    "test_rows": len(test), "held_rows": len(held),
                    "train_tail_rate": float(np.average(train.is_tail, weights=train.w))}

    feature_sets = {
        "aligned": ALIGNED + INHERIT,
        "aligned (own window only)": ALIGNED,
        "queue-length proxy (control)": ["qlen_proxy"],
        "control + aligned": ["qlen_proxy"] + ALIGNED + INHERIT,
        "control + aggregate": ["qlen_proxy"] + [f"{c}@20" for c in AGG_BASE],
        "aggregate@20ms": [f"{c}@20" for c in AGG_BASE],
        "aligned+aggregate": ALIGNED + INHERIT + [f"{c}@20" for c in AGG_BASE],
    }
    results, curves = [], {}
    models_cache = {}
    for fs, cols in feature_sets.items():
        ms = fit_models(train, cols)
        models_cache[fs] = ms
        Xtr = featurize(train, cols)
        for mname, m in ms.items():
            ptr = m.predict_proba(Xtr)[:, 1]
            t = best_f1_threshold(train.is_tail.values, ptr, train.w.values)
            for sname, sdf in sets.items():
                p = m.predict_proba(featurize(sdf, cols))[:, 1]
                r = eval_scores(sdf, sdf.is_tail.values, p, t, sdf.run_id.nunique() * dur_min)
                r.update(features=fs, model=mname, set=sname)
                results.append(r)
                curves[(fs, mname, sname)] = precision_recall_curve(sdf.is_tail.values, p,
                                                                    sample_weight=sdf.w.values)[:2]

    # interpretable rules
    def os_delay(df):
        return ((df.pre_wake_ns + df.runq_ns + df.wake_ns + df.softirq_ns + df.hardirq_ns +
                 df.blocked_ns + df.inh_sched_ns + df.inh_irq_ns + df.inh_blocked_ns) / df.thr).values

    t = best_f1_threshold(train.is_tail.values, os_delay(train), train.w.values)
    res["rule_threshold_frac_of_p99"] = t
    for sname, sdf in sets.items():
        p = os_delay(sdf)
        r = eval_scores(sdf, sdf.is_tail.values, p, t, sdf.run_id.nunique() * dur_min)
        r.update(features="aligned", model="rule: OS delay > τ", set=sname)
        results.append(r)
        curves[("aligned", "rule", sname)] = precision_recall_curve(sdf.is_tail.values, p,
                                                                    sample_weight=sdf.w.values)[:2]
    # best single aggregate counter rule
    best = None
    for c in AGG_BASE:
        col = f"{c}@20"
        ap = average_precision_score(train.is_tail.values, train[col].values,
                                     sample_weight=train.w.values)
        if best is None or ap > best[1]:
            best = (col, ap)
    col = best[0]
    t = best_f1_threshold(train.is_tail.values, train[col].values, train.w.values)
    for sname, sdf in sets.items():
        p = sdf[col].values
        r = eval_scores(sdf, sdf.is_tail.values, p, t, sdf.run_id.nunique() * dur_min)
        r.update(features="aggregate@20ms", model=f"rule: {col.split('@')[0]} > τ", set=sname)
        results.append(r)
        curves[("aggregate@20ms", "rule", sname)] = precision_recall_curve(
            sdf.is_tail.values, p, sample_weight=sdf.w.values)[:2]
    # sensitivity: evaluate only on runs whose pre-run platform noise was <= 3 %
    sens = []
    for fs in ("aligned", "aggregate@20ms", "queue-length proxy (control)", "control + aligned",
               "control + aggregate"):
        m = models_cache[fs]["forest"]
        for sname, sdf in sets.items():
            q = sdf[sdf.noise <= 3.0]
            if len(q) and q.is_tail.sum():
                p = m.predict_proba(featurize(q, feature_sets[fs]))[:, 1]
                sens.append(dict(features=fs, set=sname, runs=int(q.run_id.nunique()),
                                 ap=float(average_precision_score(q.is_tail.values, p,
                                                                  sample_weight=q.w.values))))
    res["sensitivity_quiet_runs"] = sens
    det = pd.DataFrame(results)
    det.to_csv(out / "detection.csv", index=False)
    res["detection"] = det.to_dict(orient="records")

    # feature importance (forest, aligned)
    rf = models_cache["aligned"]["forest"]
    res["importance"] = dict(sorted(zip(ALIGNED + INHERIT, map(float, rf.feature_importances_)),
                                    key=lambda kv: -kv[1]))
    lr = models_cache["aligned"]["logreg"]
    res["logreg_coef"] = dict(zip(ALIGNED + INHERIT, map(float, lr[-1].coef_[0])))

    # ablations (forest)
    abl = {"all aligned": ALIGNED + INHERIT,
           "− inherited": ALIGNED,
           "− scheduler": IRQ + BLOCK + ["inh_irq_ns", "inh_blocked_ns"],
           "− interrupt": SCHED + BLOCK + ["inh_sched_ns", "inh_blocked_ns"],
           "− blocking": SCHED + IRQ + ["inh_sched_ns", "inh_irq_ns"],
           "scheduler only": SCHED + ["inh_sched_ns"]}
    for w in (5, 20, 100):
        abl[f"aggregate {w} ms"] = [f"{c}@{w}" for c in AGG_BASE]
    arows = []
    for name, cols in abl.items():
        m = fit_models(train, cols)["forest"]
        for sname, sdf in sets.items():
            p = m.predict_proba(featurize(sdf, cols))[:, 1]
            arows.append(dict(ablation=name, set=sname,
                              ap=float(average_precision_score(sdf.is_tail.values, p,
                                                               sample_weight=sdf.w.values))))
    ab = pd.DataFrame(arows)
    ab.to_csv(out / "ablation.csv", index=False)
    res["ablation"] = ab.to_dict(orient="records")

    # Figure 3: PR curves
    fig, axs = plt.subplots(1, max(1, len(sets)), figsize=(7.0, 2.1), sharey=True, squeeze=False)
    axs = axs[0]
    styles = [(("aligned", "forest"), "aligned · forest", "#1f77b4", "-"),
              (("aligned", "logreg"), "aligned · logistic", "#1f77b4", "--"),
              (("aligned", "rule"), "aligned · rule", "#1f77b4", ":"),
              (("aggregate@20ms", "forest"), "aggregate · forest", "#ff7f0e", "-"),
              (("aggregate@20ms", "logreg"), "aggregate · logistic", "#ff7f0e", "--"),
              (("aggregate@20ms", "rule"), "aggregate · rule", "#ff7f0e", ":")]
    for ax, sname in zip(axs, sets):
        for key, lab, c, ls in styles:
            pr = curves.get((key[0], key[1], sname))
            if pr is None:
                continue
            ax.plot(pr[1], pr[0], ls, color=c, lw=1, label=lab)
        ax.axhline(np.average(sets[sname].is_tail, weights=sets[sname].w), color="grey", lw=0.6, ls="-.")
        ax.set_title(sname)
        ax.set_xlabel("recall")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1.02)
        ax.grid(alpha=0.3, lw=0.3)
    axs[0].set_ylabel("precision")
    axs[1].legend(frameon=False, loc="upper right", ncol=1)
    fig.tight_layout(pad=0.3)
    fig.savefig(out / "fig_pr.pdf")
    plt.close(fig)

    # Figure 4: ablation bars
    fig, ax = plt.subplots(figsize=(3.4, 2.0))
    names = list(abl)
    x = np.arange(len(names))
    for i, sname in enumerate(sets):
        ys = [ab[(ab.ablation == n) & (ab.set == sname)].ap.iloc[0] for n in names]
        ax.bar(x + (i - 0.5) * 0.4, ys, 0.4, label=sname.split(",")[0].replace(" workload", ""),
               color=["#4c72b0", "#55a868"][i])
    ax.set_xticks(x)
    ax.set_xticklabels(names, rotation=35, ha="right")
    ax.set_ylabel("PR-AUC (forest)")
    ax.set_ylim(0, 1)
    ax.legend(frameon=False, fontsize=6)
    ax.grid(axis="y", alpha=0.3, lw=0.3)
    fig.tight_layout(pad=0.3)
    fig.savefig(out / "fig_ablation.pdf")
    plt.close(fig)


# ---------------------------------------------------------------- stage E
CATS = ["queue", "sched", "irq", "blocked", "exec"]


def attribute(b):
    """Dominant excess component of every tail request, relative to the
    run's non-tail medians."""
    c = components(b)
    med = c[b.is_tail == 0].median()
    ex = (c - med).clip(lower=0)
    tails = ex[b.is_tail == 1]
    if not len(tails):
        return pd.Series(0.0, index=CATS), ex
    dom = tails.idxmax(axis=1)
    share = dom.value_counts(normalize=True).reindex(CATS).fillna(0.0)
    return share, ex


TOUCH = ["sched", "irq", "blocked"]


def touched(b):
    """Which OS event types touched each request: >= SIG_FLOOR_NS of that
    delay in its own window, or inherited from requests queued ahead of it."""
    own = {"sched": b.pre_wake_ns + b.runq_ns + b.wake_ns,
           "irq": b.softirq_ns + b.hardirq_ns,
           "blocked": b.blocked_ns}
    inh = {"sched": b.inh_sched_ns, "irq": b.inh_irq_ns, "blocked": b.inh_blocked_ns}
    t = pd.DataFrame({k: (own[k] >= SIG_FLOOR_NS) | (inh[k] > 0) for k in TOUCH})
    t["none"] = ~t[TOUCH].any(axis=1)
    return t


def stage_e(raw, thr, out, res):
    rows = []
    sig = {}
    for d in runs(raw, "interv"):
        b, info = load_run(d, thr)
        if "is_tail" not in b:
            continue
        cond = info["run_id"].rsplit("-r", 1)[0]
        share, ex = attribute(b)
        comp = components(b)
        r = dict(cond=cond, workload=info["workload"], stressor=info["stressor"],
                 run_id=info["run_id"], tail_rate=float(b.is_tail.mean()),
                 p99=pct(b.lat, 99), p50=pct(b.lat, 50),
                 sched_med=float(comp.sched.median()), sched_p99=pct(comp.sched, 99),
                 irq_p99=pct(comp.irq, 99), blocked_p99=pct(comp.blocked, 99),
                 nmigr_mean=float(b.nmigr.mean()), nivcsw_mean=float(b.nivcsw.mean()),
                 softirq_mean=float(b.softirq_ns.mean()),
                 # primary H2 signal: mean own-window delay per request, by category
                 own_sched_mean=float((b.pre_wake_ns + b.runq_ns + b.wake_ns).mean()),
                 own_runq_mean=float(b.runq_ns.mean()),
                 own_wake_mean=float((b.pre_wake_ns + b.wake_ns).mean()),
                 own_irq_mean=float((b.softirq_ns + b.hardirq_ns).mean()),
                 own_blocked_mean=float(b.blocked_ns.mean()))
        r.update({f"share_{k}": float(v) for k, v in share.items()})
        tch = touched(b)
        tails = b.is_tail.values == 1
        normal = ~tails
        for k in TOUCH + ["none"]:
            r[f"touch_{k}"] = float(tch[k].values[tails].mean()) if tails.any() else float("nan")
            r[f"touchn_{k}"] = float(tch[k].values[normal].mean()) if normal.any() else float("nan")
        rows.append(r)
    df = pd.DataFrame(rows)
    if df.empty:
        return
    df.to_csv(out / "interventions_runs.csv", index=False)
    summ = df.groupby(["workload", "cond"]).median(numeric_only=True).reset_index()
    summ.to_csv(out / "interventions_summary.csv", index=False)
    res["interventions"] = summ.to_dict(orient="records")

    # H2 tests: does the predicted signature move vs. the no-stressor control?
    tests = []
    expect = {"cpuhog": ("own_sched_mean", "greater"), "netflood": ("own_irq_mean", "greater"),
              "io": ("own_blocked_mean", "greater"), "hog0-moved": ("own_sched_mean", "less")}
    for wl in df.workload.unique():
        for cond, (metric, alt) in expect.items():
            ctrl_name = "hog0-shared" if cond == "hog0-moved" else "none"
            a = df[(df.workload == wl) & (df.cond == f"{wl}-{cond}")][metric]
            c = df[(df.workload == wl) & (df.cond == f"{wl}-{ctrl_name}")][metric]
            if len(a) >= 3 and len(c) >= 3:
                u, p = mannwhitneyu(a, c, alternative=alt)
                # also check that the *other* signals did not move the same way
                others = {}
                for m2 in ("own_sched_mean", "own_irq_mean", "own_blocked_mean"):
                    if m2 == metric:
                        continue
                    a2 = df[(df.workload == wl) & (df.cond == f"{wl}-{cond}")][m2]
                    c2 = df[(df.workload == wl) & (df.cond == f"{wl}-{ctrl_name}")][m2]
                    others[m2] = {"interv": float(a2.median()), "control": float(c2.median()),
                                  "p_two_sided": float(mannwhitneyu(a2, c2).pvalue)}
                mag = {"cpuhog": "touch_sched", "netflood": "touch_irq", "io": "touch_blocked",
                       "hog0-moved": "touch_sched"}[cond]
                am = df[(df.workload == wl) & (df.cond == f"{wl}-{cond}")][mag]
                cm = df[(df.workload == wl) & (df.cond == f"{wl}-{ctrl_name}")][mag]
                tests.append(dict(workload=wl, intervention=cond, control=ctrl_name, metric=metric,
                                  interv=float(a.median()), control_val=float(c.median()), p=float(p),
                                  touched_metric=mag, touched_interv=float(am.median()),
                                  touched_control=float(cm.median()),
                                  touched_p=float(mannwhitneyu(am, cm, alternative=alt).pvalue),
                                  n=(len(a), len(c)), other_ratios=others,
                                  p99_ratio=float(df[(df.workload == wl) & (df.cond == f"{wl}-{cond}")].p99.median() /
                                                  df[(df.workload == wl) & (df.cond == f"{wl}-{ctrl_name}")].p99.median())))
    res["h2_tests"] = tests

    # stability of attribution across reps
    stab = df.groupby(["workload", "cond"])[[f"share_{k}" for k in CATS]].std().reset_index()
    res["attribution_stability_std"] = stab.to_dict(orient="records")

    # Figure 5: which OS event types touched the tail requests, per condition
    order = ["none", "cpuhog", "netflood", "io", "hog0-shared", "hog0-moved"]
    labels = {"none": "none", "cpuhog": "CPU\nhog", "netflood": "softirq\nflood", "io": "I/O",
              "hog0-shared": "hog0\nshared", "hog0-moved": "hog0\nmoved"}
    ccol = {"sched": "#1f77b4", "irq": "#ff7f0e", "blocked": "#9467bd", "none": "#bbbbbb"}
    wls = [w for w in ["cpu", "kv", "net"] if w in set(summ.workload)]
    widths = [max(1, len([o for o in order if f"{w}-{o}" in set(summ.cond)])) for w in wls]
    fig, axs = plt.subplots(1, len(wls), figsize=(7.0, 1.9), sharey=True,
                            gridspec_kw={"width_ratios": widths}, squeeze=False)
    axs = axs[0]
    bw = 0.2
    for ax, wl in zip(axs, wls):
        sdf = summ[summ.workload == wl].set_index("cond")
        conds = [o for o in order if f"{wl}-{o}" in sdf.index]
        x = np.arange(len(conds))
        for j, k in enumerate(TOUCH + ["none"]):
            v = [sdf.loc[f"{wl}-{o}", f"touch_{k}"] for o in conds]
            ax.bar(x + (j - 1.5) * bw, v, bw, color=ccol[k], label=k if k != "none" else "no OS event")
        ax.set_xticks(x)
        ax.set_xticklabels([labels[o] for o in conds], fontsize=6)
        ax.set_title(f"{wl} workload")
        ax.set_ylim(0, 1.05)
        ax.grid(axis="y", alpha=0.3, lw=0.3)
    axs[0].set_ylabel("fraction of tail\nrequests touched")
    axs[-1].legend(frameon=False, fontsize=6, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    fig.tight_layout(pad=0.3)
    fig.savefig(out / "fig_signature.pdf")
    plt.close(fig)


# ---------------------------------------------------------------- early warning
def window_frame(b, rid, bucket_ms=10, min_frac=0.05):
    """Per-run window table for the early-warning test (full data)."""
    rows = []
    for _ in [0]:
        t0 = b.t_arr.min()
        w = ((b.t_done - t0) // (bucket_ms * 1_000_000)).astype(int)
        tmp = pd.DataFrame({
            "w": w.values, "is_tail": b.is_tail.values,
            "sched": ((b.pre_wake_ns + b.runq_ns + b.wake_ns) / b.thr).values,
            "irq": ((b.softirq_ns + b.hardirq_ns) / b.thr).values,
            "blocked": (b.blocked_ns / b.thr).values,
            "ninvol": b["ninvol@20"].values, "nsoft": b["nsoft_all@20"].values,
            "psi_cpu": b["psi_cpu@20"].values})
        g = tmp.groupby("w")
        m = g.mean()
        agg = pd.DataFrame({
            "episode": m.is_tail > min_frac, "n": g.size(),
            "sched_mean": m.sched, "irq_mean": m.irq, "blocked_mean": m.blocked,
            "ninvol": m.ninvol, "nsoft": m.nsoft, "psi_cpu": m.psi_cpu})
        nxt = agg["episode"].shift(-1)
        agg = agg.iloc[:-1].copy()
        agg["next_episode"] = nxt.iloc[:-1].astype(bool).values
        agg["run_id"] = rid
        agg["workload"] = b.workload.iat[0]
        agg["rep"] = b.rep.iat[0]
        rows.append(agg)
    return rows[0]


def early_warning(W, out, res):
    """Window-level: predict whether window t+1 is a tail episode (>5 % of
    its requests above p99) from telemetry observed in window t."""
    if W is None or not len(W):
        return
    feats = ["sched_mean", "irq_mean", "blocked_mean", "ninvol", "nsoft", "psi_cpu", "n"]
    dev = W[W.workload.isin(["cpu", "kv"])]
    reps = sorted(dev.rep.unique())
    n_train = max(1, int(round(0.6 * len(reps))))
    tr, te, he = dev[dev.rep.isin(reps[:n_train])], dev[dev.rep.isin(reps[n_train:])], W[W.workload == "net"]
    m = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000, class_weight="balanced"))
    m.fit(np.log1p(tr[feats].clip(lower=0)), tr.next_episode)
    out_r = {}
    for name, d in (("test", te), ("heldout", he)):
        if d.next_episode.sum() == 0:
            continue
        p = m.predict_proba(np.log1p(d[feats].clip(lower=0)))[:, 1]
        persist = d["episode"].astype(float).values  # naive: next = current
        out_r[name] = {"ap": float(average_precision_score(d.next_episode, p)),
                       "ap_persistence": float(average_precision_score(d.next_episode, persist)),
                       "base_rate": float(d.next_episode.mean()), "windows": int(len(d))}
    res["early_warning"] = out_r


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--out", default="results")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    res = {}
    thr = stage_a(a.raw, out, res)
    stage_b(a.raw, out, res)

    frames, val, ew = [], [], []
    for exp in ("main", "interv"):
        for d in runs(a.raw, exp):
            cond = read_json(d / "cond.json")
            if cond["tracer"] != "shm":
                continue
            b, info = load_run(d, thr)
            if "is_tail" not in b or not len(b):
                continue
            v = validate(b)
            v.update(run_id=f"{exp}/{info['run_id']}", coverage=info.get("coverage"),
                     drops=info.get("tl_rb_drops"))
            val.append(v)
            b["run_id"] = f"{exp}/{info['run_id']}"
            b["noise"] = float(info.get("noise_before") or 0.0)
            ew.append(window_frame(b, b["run_id"].iat[0]))
            b = thin(b, seed=len(frames))
            keep = (["id", "lat", "thr", "is_tail", "w", "workload", "load", "stressor", "rep",
                     "run_id", "qlen_proxy", "noise"] + ALIGNED + INHERIT + [c for c in b.columns if "@" in c])
            b = b[keep]
            num = [c for c in keep if c not in ("workload", "load", "stressor", "run_id", "id",
                                                "is_tail", "rep", "noise")]
            b[num] = b[num].astype("float32")
            frames.append(b)
    V = pd.DataFrame(val)
    V.to_csv(out / "validation.csv", index=False)
    res["validation"] = {"runs": len(V), "coverage_min": float(V.coverage.min()),
                         "drops_total": float(V.drops.fillna(0).sum()),
                         "resid_med_ns_median": float(V.resid_med_ns.median()),
                         "frac_within_5us_min": float(V.frac_within_5us.min())}
    F = pd.concat(frames, ignore_index=True)
    stage_d(F, out, res)
    stage_e(a.raw, thr, out, res)
    early_warning(pd.concat(ew) if ew else None, out, res)

    def clean(o):
        if isinstance(o, dict):
            return {str(k): clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, (np.floating, float)):
            return None if math.isnan(o) else float(o)
        if isinstance(o, np.integer):
            return int(o)
        return o
    (out / "results.json").write_text(json.dumps(clean(res), indent=1))
    print(json.dumps(clean({k: res[k] for k in ("validation", "split") if k in res}), indent=1))


if __name__ == "__main__":
    main()
