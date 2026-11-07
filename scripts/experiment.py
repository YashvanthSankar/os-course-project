#!/usr/bin/env python3
"""Experiment driver. Run as root inside the VM from the project root.

    sudo python3 scripts/experiment.py pilot
    sudo python3 scripts/experiment.py calibrate
    sudo python3 scripts/experiment.py main
    sudo python3 scripts/experiment.py overhead
    sudo python3 scripts/experiment.py interventions
    sudo python3 scripts/experiment.py all        # everything, in this order

Each run writes data/raw/<experiment>/<run_id>/ containing bench.csv,
meta.json, cond.json and (when traced) tlat.req.csv / tlat.agg.csv /
tlat.json. Condition order inside an experiment is shuffled with a fixed
seed so slow drift in the VM host does not line up with one condition.
"""
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
TLBENCH = BUILD / "tlbench"
TLAT = BUILD / "tlat"
STRESSOR = BUILD / "stressor"
HICCUP = BUILD / "hiccup"
NOISE_LIMIT = float(os.environ.get("NOISE_LIMIT", "3.0"))  # % of vCPU time lost
RAW = Path(os.environ.get("TL_RAW", ROOT / "data" / "raw"))
PIN = "/sys/fs/bpf/tlat_slots"
PRIMARY = "shm"   # proposed tracer: trap-free shared per-thread counters

WORKER_CPUS = "0-2"
GEN_CPU = 3
TRACER_CPU = 3
DURATION = float(os.environ.get("TL_DURATION", 15))
WARMUP = 2
REPS = int(os.environ.get("TL_REPS", 5))

# Workload definitions. Rates were chosen from a sweep on the quiet host so
# the three loads span low to high utilisation without unbounded queueing
# (cpu at 21k req/s, ~75 %, diverged with p99 > 75 ms and was excluded).
WORKLOADS = {
    "cpu": {"args": ["-m", "cpu", "-w", "3", "-u", "100"],
            "rates": {"low": 7000, "mid": 12000, "high": 17000}},
    "kv":  {"args": ["-m", "kv", "-w", "3", "-u", "20", "-S", "200", "-L", "8"],
            "rates": {"low": 30000, "mid": 50000, "high": 70000}},
    "net": {"args": ["-m", "net", "-w", "3", "-u", "50"],
            "rates": {"low": 10000, "mid": 17000, "high": 24000}},
}
CFG_FILE = ROOT / "configs" / "workloads.json"
if CFG_FILE.exists():
    WORKLOADS.update(json.loads(CFG_FILE.read_text()))

STRESSORS = {
    "none": None,
    # bursty CPU hog on the worker CPUs: 40 % duty cycle in 10 ms periods
    "cpuhog": ["cpu", "-c", WORKER_CPUS, "-n", "3", "-D", "40"],
    # UDP loopback flood sent from CPU 3; RPS steers receive softirqs to 0-2
    "netflood": ["net", "-c", str(GEN_CPU), "-n", "1", "-D", "60"],
    # buffered 1 MiB write + fdatasync loop on CPU 3
    "io": ["io", "-c", str(GEN_CPU), "-n", "1"],
    # affinity intervention: hog confined to CPU 0 ...
    "hog0": ["cpu", "-c", "0", "-n", "1", "-D", "40"],
}


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, check=False, capture_output=True, text=True, **kw)


def measure_noise(dur=1.0):
    """Platform noise: % of time a spinning thread per vCPU did not run."""
    out = sh(f"{HICCUP} -d {dur} -t 20").stdout
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return 100.0, {}
    return max(c["lost_pct"] for c in d["cpus"]), d


def wait_quiet():
    """Block until host interference is below NOISE_LIMIT (noise gate)."""
    waited = 0
    while True:
        lost, d = measure_noise()
        if lost <= NOISE_LIMIT:
            return lost, d, waited
        if waited % 300 == 0:
            print(f"  host noisy: {lost:.1f}% vCPU time lost (limit {NOISE_LIMIT}%), waiting...",
                  flush=True)
        time.sleep(15)
        waited += 15


def set_rps(mask):
    """Steer loopback receive processing (RPS) to the CPUs in mask (hex)."""
    p = Path("/sys/class/net/lo/queues/rx-0/rps_cpus")
    try:
        p.write_text(mask)
    except OSError as e:
        print("warning: could not set RPS:", e)


def run_one(exp, run_id, workload, load, tracer, stressor="none", seed=1,
            rate=None, worker_cpus=WORKER_CPUS, duration=DURATION, extra=()):
    out = RAW / exp / run_id
    if (out / "meta.json").exists():
        print(f"skip {exp}/{run_id} (done)")
        return out
    out.mkdir(parents=True, exist_ok=True)
    ready = out / "ready"
    ready.unlink(missing_ok=True)
    wl = WORKLOADS[workload]
    r = rate if rate is not None else wl["rates"][load]
    noise_before, _, waited = wait_quiet()
    cond = dict(exp=exp, run_id=run_id, workload=workload, load=load, rate=r,
                tracer=tracer, stressor=stressor, seed=seed,
                worker_cpus=worker_cpus, duration=duration, warmup=WARMUP, extra=list(extra),
                started=time.time(), noise_before_pct=noise_before, noise_wait_s=waited)
    (out / "cond.json").write_text(json.dumps(cond))

    set_rps("7" if stressor == "netflood" else "0")
    stress = None
    if STRESSORS.get(stressor):
        stress = subprocess.Popen([str(STRESSOR)] + STRESSORS[stressor] +
                                  ["-d", str(duration + WARMUP + 3)])
        time.sleep(0.5)

    bench = subprocess.Popen(
        [str(TLBENCH)] + wl["args"] +
        ["-r", str(r), "-d", str(duration), "-W", str(WARMUP), "-s", str(seed),
         "-c", worker_cpus, "-g", str(GEN_CPU), "-o", str(out / "bench.csv"),
         "-M", str(out / "meta.json"), "-R", str(ready)] +
        (["-K", PIN] if tracer == "shm" else []) + list(extra),
        stdout=subprocess.PIPE, text=True)
    bench.stdout.readline()  # "tlbench pid=... ready" once threads exist
    tl = None
    if tracer == "none":
        ready.touch()
    else:
        tl = subprocess.Popen([str(TLAT), "-p", str(bench.pid), "-b", str(TLBENCH),
                               "-m", tracer, "-o", str(out / "tlat"), "-i", "5",
                               "-R", str(ready), "-C", str(TRACER_CPU), "-K", PIN],
                              stderr=open(out / "tlat.log", "w"))
    rc = bench.wait()
    if tl:
        tl.wait(timeout=30)
    if stress:
        stress.terminate()
        stress.wait()
    set_rps("0")
    ready.unlink(missing_ok=True)
    cond["noise_after_pct"] = measure_noise()[0]
    (out / "cond.json").write_text(json.dumps(cond))
    meta = json.loads((out / "meta.json").read_text()) if (out / "meta.json").exists() else {}
    print(f"{exp}/{run_id}: rc={rc} thr={meta.get('throughput', 0):.0f}/s "
          f"done={meta.get('measured')} noise={noise_before:.1f}/{cond['noise_after_pct']:.1f}%",
          flush=True)
    return out


def record_env():
    env = RAW / "environment.txt"
    env.parent.mkdir(parents=True, exist_ok=True)
    cmds = ["uname -a", "cat /etc/os-release | head -4", "lscpu | head -20",
            "free -m", "clang --version | head -1", "gcc --version | head -1",
            "bpftool version", "dpkg -s libbpf-dev | grep Version",
            "python3 -c 'import sklearn,pandas,numpy;print(sklearn.__version__,pandas.__version__,numpy.__version__)'",
            "zgrep -E 'CONFIG_(BPF|DEBUG_INFO_BTF|UPROBES|BPF_JIT|BPF_EVENTS|RPS)=' /proc/config.gz 2>/dev/null || grep -E 'CONFIG_(BPF|DEBUG_INFO_BTF|UPROBES|BPF_JIT|BPF_EVENTS|RPS)=' /boot/config-$(uname -r)",
            "ls /sys/kernel/tracing/events/sched/ | tr '\\n' ' '",
            "sysctl kernel.bpf_stats_enabled"]
    with env.open("w") as f:
        for c in cmds:
            f.write(f"$ {c}\n{sh(c).stdout}\n")


def exp_pilot():
    for wl in WORKLOADS:
        run_one("pilot", f"{wl}-closed", wl, "max", "none", rate=0 if wl != "net" else 40000,
                duration=5)


def exp_calibrate():
    # Separate seed range so thresholds never come from evaluation runs.
    # Five calibration runs per condition; the threshold is the median of
    # their p99s, because a single run can contain a platform-induced
    # collapse episode that inflates its p99 by 10-100x.
    for wl, d in WORKLOADS.items():
        for load in d["rates"]:
            run_one("calib", f"{wl}-{load}", wl, load, PRIMARY, seed=1000)
    plan = [(wl, load, k) for wl, d in WORKLOADS.items() for load in d["rates"] for k in range(1, 5)]
    random.Random(17).shuffle(plan)
    for wl, load, k in plan:
        run_one("calib", f"{wl}-{load}-c{k}", wl, load, PRIMARY, seed=1000 + k)


def exp_abtest():
    """Strictly interleaved A/B: does tracing change the collapse rate?"""
    for rep in range(8):
        for tracer in (("none", PRIMARY) if rep % 2 == 0 else (PRIMARY, "none")):
            run_one("abtest", f"kv-mid-{tracer}-r{rep}", "kv", "mid", tracer, seed=200 + rep)


def exp_fixval():
    """Validation re-run after the torn-read fix in ksnap_take (TODO 4):
    same conditions as the main/intervention runs, new seeds."""
    plan = [(wl, st, rep) for wl in ("cpu", "kv", "net") for st in ("none", "cpuhog") for rep in range(3)]
    random.Random(19).shuffle(plan)
    for wl, st, rep in plan:
        run_one("fixval", f"{wl}-{st}-r{rep}", wl, "mid", PRIMARY, stressor=st, seed=700 + rep)


def exp_netfresh():
    """Fresh held-out net runs (seeds never used before) for a test set that
    was not seen while the features were designed (TODO 1)."""
    plan = [(load, rep) for load in WORKLOADS["net"]["rates"] for rep in range(3)]
    random.Random(23).shuffle(plan)
    for load, rep in plan:
        run_one("netfresh", f"net-{load}-shm-r{rep}", "net", load, PRIMARY, seed=500 + rep)


def exp_netrcv():
    """Client receiver co-located with the spinning generator (CPU 3) vs on CPU 2."""
    for rep in range(4):
        for where in ("gen", "own"):
            extra = ["-G", "2"] if where == "own" else []
            run_one("netrcv", f"net-mid-rcv{where}-r{rep}", "net", "mid", PRIMARY,
                    seed=300 + rep, extra=extra)


def exp_main():
    plan = []
    for wl, d in WORKLOADS.items():
        for load in d["rates"]:
            for tracer in ("none", PRIMARY):
                for rep in range(REPS):
                    plan.append((f"{wl}-{load}-{tracer}-r{rep}", wl, load, tracer, 1 + rep))
    random.Random(7).shuffle(plan)
    for run_id, wl, load, tracer, seed in plan:
        run_one("main", run_id, wl, load, tracer, seed=seed)


def exp_overhead():
    plan = []
    for wl in ("cpu", "kv"):
        for tracer in ("none", "count", "shm", "attrib", "stream"):
            for rep in range(REPS):
                plan.append((f"{wl}-{tracer}-r{rep}", wl, tracer, 1 + rep))
    random.Random(11).shuffle(plan)
    for run_id, wl, tracer, seed in plan:
        run_one("overhead", run_id, wl, "max", tracer, seed=seed, rate=0,
                duration=min(10, DURATION))
    # open loop at mid load: event loss of streaming, and the uprobe variant
    # of the per-request attribution (feature equivalence with shm)
    for wl in ("cpu", "kv"):
        for rep in range(min(3, REPS)):
            run_one("overhead", f"{wl}-stream-open-r{rep}", wl, "mid", "stream", seed=1 + rep)
            run_one("overhead", f"{wl}-attrib-open-r{rep}", wl, "mid", "attrib", seed=1 + rep)


def exp_interventions():
    plan = []
    for wl in ("cpu", "kv"):
        for st in ("none", "cpuhog", "netflood", "io"):
            for rep in range(REPS):
                plan.append((f"{wl}-{st}-r{rep}", wl, st, WORKER_CPUS, 1 + rep))
        # affinity intervention: hog on CPU 0 with workers sharing it vs moved away
        for rep in range(REPS):
            plan.append((f"{wl}-hog0-shared-r{rep}", wl, "hog0", "0-2", 1 + rep))
            plan.append((f"{wl}-hog0-moved-r{rep}", wl, "hog0", "1-2", 1 + rep))
    # "moved" runs use two workers' CPUs, so their rate is scaled by 2/3 to
    # keep per-CPU utilisation equal to the "shared" runs
    for st in ("none", "cpuhog"):
        for rep in range(REPS):
            plan.append((f"net-{st}-r{rep}", "net", st, WORKER_CPUS, 1 + rep))
    random.Random(13).shuffle(plan)
    for run_id, wl, st, cpus, seed in plan:
        rate = None
        if cpus == "1-2":
            rate = int(WORKLOADS[wl]["rates"]["mid"] * 2 / 3)
        run_one("interv", run_id, wl, "mid", PRIMARY, stressor=st, seed=seed,
                worker_cpus=cpus, rate=rate)


def main():
    if os.geteuid() != 0:
        sys.exit("run as root (BPF + RPS sysfs need it)")
    sh("sysctl -w kernel.bpf_stats_enabled=1")
    record_env()
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    steps = {"pilot": exp_pilot, "calibrate": exp_calibrate, "main": exp_main,
             "overhead": exp_overhead, "interventions": exp_interventions,
             "abtest": exp_abtest, "netrcv": exp_netrcv,
             "fixval": exp_fixval, "netfresh": exp_netfresh}
    if what == "all":
        for k in ("calibrate", "main", "overhead", "interventions", "abtest", "netrcv",
                  "fixval", "netfresh"):
            steps[k]()
    else:
        steps[what]()


if __name__ == "__main__":
    main()
