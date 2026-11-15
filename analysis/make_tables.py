#!/usr/bin/env python3
"""Generate LaTeX tables and macros for the report from results/.

    python3 analysis/make_tables.py --res results --out report/generated

Every number quoted in the report comes from these generated files, so
the paper cannot drift from the analysis output.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def us(ns):
    return ns / 1e3


def fmt_us(ns):
    v = us(ns)
    if v >= 10000:
        return f"{v/1000:.0f}\\,ms"
    if v >= 1000:
        return f"{v/1000:.1f}\\,ms"
    return f"{v:.0f}\\,$\\mu$s"


def pct(x, nd=1):
    return f"{100 * x:.{nd}f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", default="results")
    ap.add_argument("--out", default="report/generated")
    a = ap.parse_args()
    res_dir, out = Path(a.res), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    R = json.loads((res_dir / "results.json").read_text())
    macros = []

    def M(name, val):
        macros.append(f"\\newcommand{{\\{name}}}{{{val}}}")

    # ---------------- latency, memory, stability, false-alarm macros
    lat = pd.read_csv(res_dir / "latency_summary.csv")
    base = lat[lat.tracer == "none"]
    for _, r in base.iterrows():
        k = f"{r.workload.capitalize()}{r.load.capitalize()}"
        for q, qn in (("p50", "PFifty"), ("p95", "PNinetyFive"), ("p99", "PNinetyNine")):
            M(f"lat{k}{qn}", f"{r[q] / 1e3:.0f}")
    M("cvMin", f"{base.p99_cv.min():.2f}"); M("cvMax", f"{base.p99_cv.max():.1f}")
    ov = pd.read_csv(res_dir / "overhead_summary.csv")
    M("memCnt", f"{ov[ov.tracer == 'shm'].map_kb.max() / 1024:.1f}")
    M("memRb", f"{ov[ov.tracer == 'attrib'].map_kb.max() / 1024:.0f}")
    stab = pd.DataFrame(R.get("attribution_stability_std", []))
    if not stab.empty:
        sc = [c for c in stab.columns if c.startswith("share_")]
        v = stab[sc].values.ravel()
        M("stabMed", f"{100 * np.median(v):.1f}")
        M("stabNinety", f"{100 * np.percentile(v, 90):.1f}")
        mx = stab.set_index("cond")[sc].max(axis=1)
        M("stabMax", f"{100 * mx.max():.0f}"); M("stabMaxCond", mx.idxmax().replace("-", " "))
    det0 = pd.read_csv(res_dir / "detection.csv")
    for fs, key in (("aligned", "Al"), ("aggregate@20ms", "Ag")):
        g = det0[(det0.features == fs) & (det0.model == "forest")]
        M(f"faK{key}Min", f"{g.fa_per_min.min() / 1e3:.0f}")
        M(f"faK{key}Max", f"{g.fa_per_min.max() / 1e3:.0f}")

    # ---------------- latency table
    lat = pd.read_csv(res_dir / "latency_summary.csv")
    rows = []
    for wl in ("cpu", "kv", "net"):
        for ld in ("low", "mid", "high"):
            g = lat[(lat.workload == wl) & (lat.load == ld)]
            if g.empty:
                continue
            n0 = g[g.tracer == "none"]
            n1 = g[g.tracer == "shm"]
            if n0.empty:
                continue
            r0 = n0.iloc[0]
            cells = [wl if ld == "low" else "", ld, f"{r0.thr/1000:.1f}k",
                     fmt_us(r0.p50), fmt_us(r0.p99), fmt_us(r0.p999), f"{r0.p99_cv:.2f}"]
            cells.append(fmt_us(n1.iloc[0].p99) if not n1.empty else "--")
            rows.append(" & ".join(cells) + " \\\\")
        rows.append("\\midrule")
    rows = rows[:-1]
    (out / "tab_latency.tex").write_text(
        "\\begin{table}[t]\\centering\\caption{Baseline latency ($\\mu$s unless noted), median of 5 runs; "
        "CV = run-to-run coefficient of variation of p99. Last column: p99 with \\sys{} tracing.}"
        "\\label{tab:latency}\\footnotesize\\setlength{\\tabcolsep}{3.2pt}\n"
        "\\begin{tabular}{llrrrrrr}\\toprule\n"
        "wl & load & req/s & p50 & p99 & p99.9 & CV & p99 traced\\\\\\midrule\n" +
        "\n".join(rows) + "\n\\bottomrule\\end{tabular}\\end{table}\n")

    # ---------------- overhead table
    ov = pd.read_csv(res_dir / "overhead_summary.csv")
    names = {"none": "untraced", "count": "counters only", "shm": "\\sys{} shared",
             "attrib": "uprobe windows", "stream": "per-event stream"}
    rows = []
    for tr in ("none", "count", "shm", "attrib", "stream"):
        cells = [names[tr]]
        for wl in ("cpu", "kv"):
            g = ov[(ov.workload == wl) & (ov.tracer == tr)]
            if g.empty:
                cells += ["--", "--"]
                continue
            r = g.iloc[0]
            cells += [f"{r.thr_med/1000:.1f}", f"{r.ns_per_req:.0f}"]
        g = ov[ov.tracer == tr]
        cells.append(f"{g.drops.sum():.0f}")
        rows.append(" & ".join(cells) + " \\\\")
        for wl in ("cpu", "kv"):
            h = ov[(ov.workload == wl) & (ov.tracer == tr)]
            if not h.empty:
                M(f"ovLoss{wl.capitalize()}{tr.capitalize()}", pct(h.iloc[0].loss_med))
                M(f"ovCpu{wl.capitalize()}{tr.capitalize()}", pct(h.iloc[0].bpf_cpu + h.iloc[0].collector_cpu))
                M(f"ovNs{wl.capitalize()}{tr.capitalize()}", f"{h.iloc[0].ns_per_req:.0f}")
    (out / "tab_overhead.tex").write_text(
        "\\begin{table}[t]\\centering\\caption{Tracing overhead, closed loop, 5 runs each: median throughput "
        "(k\\,req/s) and tracer CPU per request (BPF program run time + collector CPU, ns; excludes the "
        "uprobe trap cost). Run-to-run throughput spread is up to $\\pm$17\\%.}"
        "\\label{tab:overhead}\\footnotesize\\setlength{\\tabcolsep}{2.6pt}\n"
        "\\begin{tabular}{lrrrrr}\\toprule\n"
        " & \\multicolumn{2}{c}{cpu} & \\multicolumn{2}{c}{kv} & \\\\\n"
        "mode & kreq/s & ns/req & kreq/s & ns/req & drops\\\\\\midrule\n" +
        "\n".join(rows) + "\n\\bottomrule\\end{tabular}\\end{table}\n")
    if "stream_open" in R and R["stream_open"]:
        so = pd.DataFrame(R["stream_open"])
        M("streamDropPct", pct(so.drop_frac.max(), 2))

    # ---------------- detection table
    det = pd.DataFrame(R["detection"])
    order = [("aligned", "rule: OS delay > τ"), ("aligned", "logreg"), ("aligned", "forest"),
             ("aggregate@20ms", None),
             ("aggregate@20ms", "logreg"), ("aggregate@20ms", "forest"),
             ("queue-length proxy (control)", "logreg"), ("control + aggregate", "forest"),
             ("control + aligned", "forest")]
    label = {"aligned": "aligned", "aligned (own window only)": "own only",
             "aggregate@20ms": "aggregate", "queue-length proxy (control)": "queue proxy",
             "control + aggregate": "proxy+aggr.", "control + aligned": "proxy+aligned"}
    sets = list(dict.fromkeys(det.set))
    short = {"logreg": "LR", "forest": "RF"}
    rows, prev = [], None
    for fs, mdl in order:
        g = det[det.features == fs]
        g = g[g.model.str.startswith("rule")] if mdl is None else g[g.model == mdl]
        if g.empty:
            continue
        m0 = g.iloc[0].model
        mname = "rule" if m0.startswith("rule") else short.get(m0, m0)
        cells = [label[fs] if fs != prev else "", mname]
        if prev is not None and fs != prev:
            rows.append("\\addlinespace[2pt]")
        prev = fs
        for sname in sets:
            h = g[g.set == sname]
            if h.empty:
                cells.append("--")
                continue
            r = h.iloc[0]
            cells.append(f"{r.ap:.2f} [{r.ap_lo:.2f}, {r.ap_hi:.2f}]")
            cells.append(f"{r.f1:.2f}")
        rows.append(" & ".join(cells) + " \\\\")
    (out / "tab_detect.tex").write_text(
        "\\begin{table}[t]\\centering\\caption{Tail-request detection: PR-AUC [run-bootstrap 95\\% CI] and F1 at "
        "the F1-optimal training threshold. "
        "Train: cpu/kv reps 0--2; test: reps 3--4; net never seen. LR logistic regression, RF random "
        "forest; rules: total OS delay $>\\tau$ (aligned), PSI CPU $>\\tau$ (aggregate). Base rate "
        "(= PR-AUC of a random ranking): test \\baseApTest, net \\baseApHeld.}"
        "\\label{tab:detect}\\scriptsize\\setlength{\\tabcolsep}{2.5pt}\n"
        "\\begin{tabular}{llrrrr}\\toprule\n"
        "& & \\multicolumn{2}{c}{test (cpu/kv)} & \\multicolumn{2}{c}{held-out (net)}\\\\\n"
        "features & model & PR-AUC & F1 & PR-AUC & F1\\\\\\midrule\n" +
        "\n".join(rows) + "\n\\bottomrule\\end{tabular}\\end{table}\n")
    for fs, key in (("aligned", "Al"), ("aggregate@20ms", "Ag"), ("queue-length proxy (control)", "Q"),
                    ("control + aligned", "QAl"), ("control + aggregate", "QAg")):
        for sname, sk in zip(sets, ("Test", "Held")):
            for mdl, mk in (("forest", "F"), ("logreg", "L")):
                h = det[(det.features == fs) & (det.model == mdl) & (det.set == sname)]
                if not h.empty:
                    M(f"ap{key}{mk}{sk}", f"{h.iloc[0].ap:.2f}")
                    M(f"fa{key}{mk}{sk}", f"{h.iloc[0].fa_per_min:.0f}")
    h = det[(det.model.str.startswith("rule")) & (det.features == "aligned")]
    for sname, sk in zip(sets, ("Test", "Held")):
        hh = h[h.set == sname]
        if not hh.empty:
            M(f"apRule{sk}", f"{hh.iloc[0].ap:.2f}")
            M(f"pRule{sk}", f"{hh.iloc[0].precision:.2f}")
            M(f"rRule{sk}", f"{hh.iloc[0].recall:.2f}")
    h = det[(det.model.str.startswith("rule")) & (det.features == "aggregate@20ms")]
    for sname, sk in zip(sets, ("Test", "Held")):
        hh = h[h.set == sname]
        if not hh.empty:
            M(f"apAggRule{sk}", f"{hh.iloc[0].ap:.2f}")
    lr = pd.read_csv(res_dir / "latency_runs.csv")
    M("genOk", int((lr.genlate_p99 <= 60000).sum()))
    M("genAll", len(lr))
    M("genMax", fmt_us(lr.genlate_p99.max()))
    M("noiseMed", f"{lr.noise.median():.1f}")
    if "rule_threshold_frac_of_p99" in R:
        M("ruleTau", f"{R['rule_threshold_frac_of_p99']:.2f}")

    # ---------------- interventions table
    tests = R.get("h2_tests", [])
    nice = {"cpuhog": "CPU hog", "netflood": "softirq flood", "io": "I/O writer",
            "hog0-moved": "move workers off hog"}
    mname = {"own_sched_mean": "sched", "own_irq_mean": "irq", "own_blocked_mean": "blocked"}
    rows = []
    for t in tests:
        p = t["p"]
        ps = f"{p:.3f}" if p >= 0.001 else "$<$0.001"
        rows.append(f"{t['workload']} & {nice[t['intervention']]} & {mname[t['metric']]} & "
                    f"{us(t['control_val']):.1f} $\\to$ {us(t['interv']):.1f} & {ps} & "
                    f"{t['p99_ratio']:.1f}$\\times$ \\\\")
    (out / "tab_interv.tex").write_text(
        "\\begin{table}[t]\\centering\\caption{Interventions (H2), 5 runs per condition at mid load. "
        "Predicted component: mean own-window delay per request ($\\mu$s), control $\\to$ intervention; "
        "one-sided Mann--Whitney $p$; p99 change.}"
        "\\label{tab:interv}\\footnotesize\\setlength{\\tabcolsep}{2.4pt}\n"
        "\\begin{tabular}{llrrrr}\\toprule\n"
        "wl & intervention & signal & control $\\to$ interv. & $p$ & p99\\\\\\midrule\n" +
        "\n".join(rows) + "\n\\bottomrule\\end{tabular}\\end{table}\n")

    # ---------------- validation and misc macros
    v = R.get("validation", {})
    M("valRuns", v.get("runs", 0))
    M("valWithin", pct(v.get("frac_within_5us_min", 0), 1))
    M("valCoverage", pct(v.get("coverage_min", 0), 2))
    M("valDrops", f"{v.get('drops_total', 0):.0f}")
    sp = R.get("split", {})
    M("trainRuns", sp.get("train_runs", 0))
    M("testRuns", sp.get("test_runs", 0))
    M("heldRuns", sp.get("held_runs", 0))
    ew = R.get("early_warning", {})
    for k, kk in (("test", "Test"), ("heldout", "Held")):
        if k in ew:
            M(f"ewAp{kk}", f"{ew[k]['ap']:.2f}")
            M(f"ewPers{kk}", f"{ew[k]['ap_persistence']:.2f}")
            M(f"ewBase{kk}", f"{ew[k]['base_rate']:.2f}")
            M(f"ewBasePct{kk}", f"{100 * ew[k]['base_rate']:.0f}")
    # signature fold changes: \sigCpuCpuhogRunq etc.
    sm = R.get("signature_matrix")
    if sm:
        key = {"CPU hog": "Hog", "softirq flood": "Flood", "I/O writer": "Io", "move off hog": "Move"}
        ck = {"own_runq_mean": "Runq", "own_wake_mean": "Wake", "own_irq_mean": "Irq",
              "own_blocked_mean": "Blk"}
        # use the annotation (unclipped fold change); log2fc is clipped for colour
        for row, anns in zip(sm["rows"], sm["ann"]):
            wl, nm = row.split(": ")
            for c, a_ in zip(sm["cols"], anns):
                val = a_.split("×")[0] if "×" in a_ else "1.0"
                M(f"sig{wl.capitalize()}{key[nm]}{ck[c]}", val)
    ab = R.get("abtest")
    if ab:
        M("abRuns", ab["runs"][0])
        M("abPfiftyNone", f"{ab['p50_none']/1e3:.1f}")
        M("abPfiftyShm", f"{ab['p50_shm']/1e3:.1f}")
        M("abPfiftyP", f"{ab['p50_p']:.2f}")
        M("abPninetyNone", fmt_us(ab["p99_none"]))
        M("abPninetyShm", fmt_us(ab["p99_shm"]))
        M("abPninetyP", f"{ab['p99_p']:.2f}")
        M("abCollShm", ab["collapses_shm"])
        M("abCollNone", ab["collapses_none"])
        M("abCollP", f"{ab['collapse_fisher_p']:.2f}")
    nr = R.get("netrcv")
    if nr and "gen" in nr and "own" in nr:
        M("rcvGenRespP", fmt_us(nr["gen"]["resp_p99"]))
        M("rcvOwnRespP", fmt_us(nr["own"]["resp_p99"]))
        M("rcvGenP", fmt_us(nr["gen"]["p99"]))
        M("rcvOwnP", fmt_us(nr["own"]["p99"]))
        M("rcvGenServer", fmt_us(nr["gen"]["server_p99"]))
        M("rcvPval", f"{nr.get('p99_p', float('nan')):.2f}")
        M("rcvOwnServer", fmt_us(nr["own"]["server_p99"]))
    th = R.get("thresholds", {})
    for k, v in th.items():
        wl, ld = k.split("-")
        M(f"thr{wl.capitalize()}{ld.capitalize()}", fmt_us(v["p99_ns"]))
        M(f"thrMax{wl.capitalize()}{ld.capitalize()}", fmt_us(max(v.get("calib_p99s_ns", [v["p99_ns"]]))))
    ab_rows = pd.read_csv(res_dir / "ablation.csv") if (res_dir / "ablation.csv").exists() else None
    if ab_rows is not None:
        kk = {"all aligned": "All", "− inherited": "NoInh", "− scheduler": "NoSched",
              "− interrupt": "NoIrq", "− blocking": "NoBlk", "scheduler only": "SchedOnly",
              "aggregate 5 ms": "AggFive", "aggregate 20 ms": "AggTwenty", "aggregate 100 ms": "AggHundred"}
        for _, r in ab_rows.iterrows():
            sk = "Test" if r["set"].startswith("test") else "Held"
            if r.ablation in kk:
                M(f"abl{kk[r.ablation]}{sk}", f"{r.ap:.2f}")
    # base rates per evaluation split (random classifier PR-AUC)
    for sname, sk in zip(sets, ("Test", "Held")):
        h = det[det.set == sname]
        if not h.empty:
            M(f"base{sk}", f"{100 * h.iloc[0].base_rate:.1f}")
            M(f"baseAp{sk}", f"{h.iloc[0].base_rate:.2f}")
    # absolute values behind the intervention fold changes (median of per-run means, us)
    iv = res_dir / "interventions_summary.csv"
    if iv.exists():
        S = pd.read_csv(iv).set_index("cond")
        def us_(v):
            v = v / 1e3
            return f"{v:.2f}" if v < 10 else f"{v:.1f}"
        pairs = {"Hog": ("cpuhog", "none"), "Io": ("io", "none"), "Move": ("hog0-moved", "hog0-shared"),
                 "Flood": ("netflood", "none")}
        cols = {"Runq": "own_runq_mean", "Wake": "own_wake_mean", "Irq": "own_irq_mean", "Blk": "own_blocked_mean"}
        for wl in ("cpu", "kv", "net"):
            for pk, (ci, cc) in pairs.items():
                if f"{wl}-{ci}" in S.index and f"{wl}-{cc}" in S.index:
                    for ck, col in cols.items():
                        M(f"abs{wl.capitalize()}{pk}{ck}Ctl", us_(S.loc[f"{wl}-{cc}", col]))
                        M(f"abs{wl.capitalize()}{pk}{ck}Int", us_(S.loc[f"{wl}-{ci}", col]))
    rc = res_dir / "robustness_checks.json"
    if rc.exists():
        Z = json.loads(rc.read_text())
        nw = Z.get("noise_wait", {})
        if nw:
            M("nwRuns", nw["runs"]); M("nwWaited", nw["waited"])
            M("nwMed", f"{nw['median_wait_s_of_waiting']:.0f}"); M("nwMax", f"{nw['max_wait_s'] / 60:.0f}")
        for k, kk in (("identity_campaign", "Camp"), ("identity_fixed", "Fix")):
            if k in Z:
                M(f"id{kk}AllMin", f"{100 * Z[k]['within_all_min']:.1f}")
                M(f"id{kk}SwMin", f"{100 * Z[k]['within_switched_min']:.1f}")
                M(f"id{kk}SwMed", f"{100 * Z[k]['within_switched_median']:.2f}")
                M(f"id{kk}SwShare", f"{100 * Z[k]['switched_share']:.1f}")
                M(f"id{kk}Runs", Z[k]["runs"])
        for f, v in Z.get("no_signal", {}).items():
            M(f"noSigFloor{ {'5us': 'Five', '10us': 'Ten', '20us': 'Twenty'}[f] }", f"{100 * v:.0f}")
        def g(d, key):
            return d.get(key, {}).get("ap")
        for held, hk in (("cpu", "Cpu"), ("kv", "Kv"), ("net", "Net")):
            d = Z.get("rotation", {}).get(held, {})
            for fs, fk in (("aligned", "Al"), ("aggregate@20ms", "Ag"), ("queue proxy", "Q")):
                for mdl, mk in (("forest", "F"), ("logreg", "L")):
                    v = d.get(f"{fs}|{mdl}|held")
                    if v:
                        M(f"rot{hk}{fk}{mk}", f"{v['ap']:.2f}")
                        M(f"rot{hk}{fk}{mk}Lo", f"{v['lo']:.2f}")
                        M(f"rot{hk}{fk}{mk}Hi", f"{v['hi']:.2f}")
            v = d.get("aligned|forest|held")
            if v:
                M(f"rot{hk}Base", f"{v['base_rate']:.2f}")
        d = Z.get("netfresh", {})
        for fs, fk in (("aligned", "Al"), ("aggregate@20ms", "Ag"), ("queue proxy", "Q")):
            for mdl, mk in (("forest", "F"), ("logreg", "L")):
                v = d.get(f"{fs}|{mdl}|netfresh")
                if v:
                    M(f"fresh{fk}{mk}", f"{v['ap']:.2f}")
                    M(f"fresh{fk}{mk}Lo", f"{v['lo']:.2f}")
                    M(f"fresh{fk}{mk}Hi", f"{v['hi']:.2f}")
        v = d.get("aligned|forest|netfresh")
        if v:
            M("freshBase", f"{v['base_rate']:.2f}"); M("freshRuns", v["runs"])
            M("freshBasePct", f"{100 * v['base_rate']:.1f}")
    fv = res_dir / "preempt_fix_check.json"
    if fv.exists():
        Z = json.loads(fv.read_text())
        a, b = Z["fixval"], Z["fixval2"]
        M("idFixTwoSwMin", f"{100 * b['within_switched_min']:.1f}")
        M("idFixTwoSwMed", f"{100 * b['within_switched_median']:.1f}")
        M("idFixOneSwMin", f"{100 * a['within_switched_min']:.1f}")
        M("preSleep", b.get("preempt_sleeping_total", 0))
        M("preAll", f"{b.get('npreempt_total', 0):,}")
        M("noWake", b.get("no_wake_gap_total", 0))
        M("preAllPct", f"{100 * b.get('npreempt_share_of_switches', 0):.0f}")
        M("noWakePct", f"{100 * b.get('no_wake_gap_share_of_switches', 0):.1f}")
        M("noWakeUs", f"{b.get('no_wake_gap_mean_us', 0):.0f}")
    # H2 with Holm correction over the nine pre-stated tests
    h2 = R.get("h2_tests", [])
    if h2:
        ps = sorted(t["p"] for t in h2)
        keep = 0
        for i, p_ in enumerate(ps):
            if p_ <= 0.05 / (len(ps) - i):
                keep += 1
            else:
                break
        M("hTwoRaw", sum(p_ < 0.05 for p_ in ps)); M("hTwoHolm", keep); M("hTwoN", len(ps))
    ns = res_dir / "no_os_delay.json"
    if ns.exists():
        z = json.loads(ns.read_text())
        for k, kk in (("test", "Test"), ("held", "Held"), ("train", "Train")):
            if k in z:
                M(f"noSig{kk}", f"{100 * z[k]:.0f}")
    (out / "macros.tex").write_text("\n".join(macros) + "\n")
    print(f"wrote {len(list(out.iterdir()))} files to {out}")


if __name__ == "__main__":
    main()
