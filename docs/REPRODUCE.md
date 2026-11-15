# Code Guide and Reproduction Instructions

This document covers what each part of the code does, what it depends on,
and how to reproduce every number and figure in the report.

## 0. Where things are in the submission ZIP

| Folder | Contents |
|---|---|
| `latex_report/` | `main.tex`, `references.bib`, `report.pdf`, `figures/`, and the other LaTeX files (`sections/` with one file per section, `generated/` with the tables and numeric macros produced by the analysis) |
| `code/` | everything needed to build and run: `src/`, `scripts/`, `analysis/`, `configs/`, `requirements.txt`, `Makefile`, `opensource/` (bcc patch). **Run every command in this guide from inside `code/`.** |
| `documentation/` | this guide (`reproducibility_guide.md` / `.pdf`) |
| `supplementary/` | `datasets/` (one complete sample run + download link and checksum of the full raw data), `experimental_results/` (every CSV/JSON behind the report), `additional_figures/` (all figures as PDF), `other_supporting_materials/` (bcc patch test log, environment of the measured machine) |

In the GitHub repository the same files sit at the top level (`report/` instead of `latex_report/`).

## 1. Repository layout

```
src/
  tlbench.c             open-loop request benchmark (cpu | kv | net workloads)
  tlat.bpf.c            eBPF programs: scheduler + IRQ state machine, request windows
  tlat.c                user-space loader/collector (libbpf skeleton)
  tlat.h                structs shared by kernel and user side
  stressor.c            one controlled interference source: cpu | net | io
  hiccup.c              platform-noise meter (lost vCPU time)
scripts/
  setup_vm.sh           installs the toolchain on Ubuntu 24.04
  experiment.py         experiment driver (noise gate, randomized order, raw data)
  run_all.sh            one-command reproduction: build -> experiments -> analysis
  smoke_test.py         1-minute self-check of the whole pipeline (sudo make test)
analysis/
  analyze.py            stages A-E: latency, overhead, join/validation, detection, interventions
  no_os_delay.py        share of tail requests with no OS-delay signal
  robustness_checks.py  identity on switched requests, no-signal floors, noise-gate
                        waits, held-out workload rotation, fresh net runs
  preempt_fix_check.py  validation runs before/after the sched_switch preempt-flag fix
  make_tables.py        LaTeX tables and number macros for the report
data/raw/               raw run data (one directory per run; schema in section 6)
data/sample/            one complete raw run, gzip-compressed (in the repo)
results/                tables (csv), results.json, figures (pdf) produced by analyze.py
report/                 IEEE LaTeX source of the report
opensource/             patch for upstream bcc (runqlat --split) + test script
```

## 2. How the pieces fit

1. `tlbench` generates requests with Poisson inter-arrival times on a
   dedicated CPU (the generator spins and never sleeps) and hands them to
   worker threads. Each request has a stable integer id.
2. The tracer `tlat` loads `tlat.bpf.c`. Its tracepoint programs
   (`sched_switch`, `sched_wakeup[_new]`, `sched_migrate_task`,
   `softirq_entry/exit`, `irq_handler_entry/exit`) keep per-thread counters
   for the benchmark's threads: runqueue wait after preemption, wakeup delay,
   blocked time, softirq/hardirq time that interrupted the thread, and
   context-switch and migration counts.
3. Request windows are joined to those counters in one of two ways:
   - **shm mode (proposed):** the per-thread counters live in a
     `BPF_F_MMAPABLE` array pinned at `/sys/fs/bpf/tlat_slots`. `tlbench -K`
     maps it and snapshots its own slot at request begin and end with plain
     memory loads. No trap, syscall, or ring-buffer record per request.
   - **attrib mode (uprobe):** uprobes on `tl_req_begin`/`tl_req_end` take
     the snapshots in the kernel and push one 120-byte record per request
     through a ring buffer. This needs no change to the application, but
     costs two traps per request.
4. In every mode `tlat` also samples global per-CPU counters every 5 ms
   (`tlat.agg.csv`). That is the *aggregate event count* baseline.
5. `analyze.py` joins everything on the request id and the monotonic clock.

## 3. Software dependencies

| Component | Version used |
|---|---|
| Host | macOS on Apple M3 Pro, Lima 1.x with the `vz` backend |
| Guest | Ubuntu 24.04 LTS (arm64), kernel 6.8.0-142-generic, 4 vCPU, 6 GiB (12 GiB for the analysis step) |
| Compiler | gcc 13 (user space), clang 18.1.3 (BPF) |
| BPF | libbpf-dev (Ubuntu 24.04 package), bpftool v7.4.0 |
| Python | 3.12 with numpy, pandas, scikit-learn, scipy, matplotlib (Ubuntu packages; exact versions pinned in `requirements.txt`) |
| Report | IEEEtran (conference), built with tectonic or pdflatex + bibtex |

The exact versions of the machine that produced the report are written
automatically to `data/raw/environment.txt` (uname, lscpu, compiler,
libbpf, bpftool, Python packages, kernel BPF config, available sched
tracepoints).

### Hardware and software requirements

| Item | Requirement |
|---|---|
| CPU | 4 cores (arm64 or x86-64). Workers use CPUs 0-2, the load generator and collector CPU 3 |
| Memory | 6 GiB for the experiments; 12 GiB for the full analysis (`analyze.py` holds all runs in memory) |
| Disk | about 25 GiB free (raw data of all 336 runs is about 20 GB) |
| OS | Linux with BTF, kernel 5.8 or newer (ring buffer); tested on Ubuntu 24.04, kernel 6.8. A Linux VM on macOS works (section 4) |
| Privileges | root (`sudo`) to load BPF programs and pin maps under `/sys/fs/bpf` |
| Time | `sudo make test`: 1 minute. Full campaign: about 3 hours plus 30 minutes of analysis, on an otherwise idle machine |

Install everything with `bash scripts/setup_vm.sh` (apt packages). If you
prefer pip for the analysis: `pip install -r requirements.txt`.

Kernel requirements: `CONFIG_DEBUG_INFO_BTF=y` (for `vmlinux.h` / CO-RE and
`tp_btf`), `CONFIG_UPROBES=y` (attrib mode only), `CONFIG_RPS=y`
(softirq-flood intervention). Ubuntu 24.04 generic kernels have all three.

## 4. Setting up the VM (on macOS)

```bash
limactl create --name=ebpf --cpus=4 --memory=12 --disk=40 --vm-type=vz \
  --set '.containerd.system=false | .containerd.user=false' template://ubuntu-24.04
limactl start ebpf
limactl shell ebpf
# inside the VM:
mkdir -p ~/work && cp -r /Users/<you>/Team_01_Assignment_A1 ~/work/   # home is mounted read-only
cd ~/work/Team_01_Assignment_A1/code && bash scripts/setup_vm.sh && make
```

Any Ubuntu 24.04 machine (bare metal is better) works the same way:
`bash scripts/setup_vm.sh && make`.

## 5. Reproducing the results

**Keep the host idle.** In a VM, other host load shows up as multi-millisecond
gaps that the guest kernel cannot see. The driver has a *noise gate*: before
every run, `hiccup` measures how much time a spinning thread per vCPU loses.
If that exceeds `NOISE_LIMIT`, the run waits. The default is 3%; the
reported campaign used 5% after the first eight runs, so set
`NOISE_LIMIT=5` to match it (`sudo NOISE_LIMIT=5 bash scripts/run_all.sh`). The measurement
before and after each run is stored in its `cond.json`.

**Quick self-check first (about 1 minute):** `sudo make test` runs four
short real runs (`scripts/smoke_test.py`) and checks that every request gets
kernel features, nothing is dropped, the accounting identity holds for
at least 99% of requests, a CPU hog raises preemption wait at least 5x, uprobe
records join by request id, and the aggregate samples are written.

The bcc patch is tested separately with `bash opensource/build_and_test.sh`.

One command (about 3 hours of experiments plus ~30 minutes of analysis
on a quiet machine; 336 runs, ~20 GB of raw data):

```bash
cd ~/work/Team_01_Assignment_A1/code   # in the GitHub repository: the repository root
sudo NOISE_LIMIT=5 bash scripts/run_all.sh   # build, all experiments, analysis
```

`run_all.sh` writes results to `code/results/` and the report's tables and
figures to `../latex_report/generated/` and `../latex_report/figures/`.
Note that `fixval` reruns the 18 validation runs with the current code,
which already contains the torn-read fix, so its numbers match the
"after fix" results.

Or step by step:

```bash
sudo python3 scripts/experiment.py calibrate      # 5 runs/condition (seeds 1000-1004); threshold = median p99
sudo python3 scripts/experiment.py main           # 3 workloads x 3 loads x {none, shm} x 5 reps
sudo python3 scripts/experiment.py overhead       # closed loop, 5 tracer modes x 5 reps (+ open-loop stream/uprobe)
sudo python3 scripts/experiment.py interventions  # stressors one at a time, 5 reps each
sudo python3 scripts/experiment.py abtest         # interleaved untraced/traced A/B, kv mid, 8+8 runs
sudo python3 scripts/experiment.py netrcv         # net client receiver on CPU 3 (with generator) vs CPU 2
sudo python3 scripts/experiment.py fixval         # 18 validation runs after the torn-read fix in ksnap_take
sudo python3 scripts/experiment.py fixval2        # same 18 runs after the sched_switch preempt-flag fix
sudo python3 scripts/experiment.py netfresh       # 9 fresh net runs (unused seeds) for a clean held-out test
python3 analysis/analyze.py --raw data/raw --out results     # ~30 min, needs ~7 GB RAM
python3 analysis/no_os_delay.py --raw data/raw --res results
python3 analysis/robustness_checks.py --raw data/raw --res results   # identity subset, floors, rotation, fresh net
python3 analysis/preempt_fix_check.py --raw data/raw --res results   # fixval vs fixval2 (preempt-flag fix)
python3 analysis/make_tables.py --res results --out report/generated
```

Runs that already finished are skipped, so an interrupted campaign resumes
where it stopped. Condition order is shuffled with fixed seeds (7, 11, 13).

Environment overrides: `TL_DURATION` (seconds per run, default 15),
`TL_REPS` (default 5), `TL_RAW` (output directory), `NOISE_LIMIT`.

### Which output backs which part of the report

| Report item | File |
|---|---|
| Data-path diagram (Fig. 1) | drawn in `report/sections/method.tex` (TikZ) |
| Latency CCDFs (Fig. 2) | `results/fig_latency.pdf`, `latency_summary.csv` |
| Overhead (Fig. 6, tracer ns per request in Sec. V-D) | `results/fig_overhead.pdf`, `overhead_summary.csv` |
| Detection PR curves (Fig. 3), Table I | `results/fig_pr.pdf`, `detection.csv` |
| Feature/window ablation (Fig. 4) | `results/fig_ablation.pdf`, `ablation.csv` |
| Intervention signatures (H2, Fig. 5) | `results/fig_signature.pdf`, `interventions_summary.csv`, `results.json:h2_tests`, `results.json:signature_matrix` |
| A/B overhead test | `results/abtest_runs.csv`, `results.json:abtest` |
| net receiver placement | `results/netrcv_runs.csv`, `results.json:netrcv` |
| Tails with no OS signal | `results/no_os_delay.json` |
| Identity on switched requests, no-signal floors, noise-gate waits, held-out rotation, fresh net | `results/robustness_checks.json` |
| Preempt-flag fix: identity before/after, how often the case occurred | `results/preempt_fix_check.json` |
| H2 count after Holm correction | `report/generated/macros.tex` (`hTwoRaw`, `hTwoHolm`), from `results.json:h2_tests` |
| Every number quoted in the report | `report/generated/macros.tex` (generated by `make_tables.py`) |
| Accounting validation, coverage, drops | `results/validation.csv`, `results.json:validation` |
| Platform noise per run | `data/raw/*/*/cond.json` (`noise_before_pct`, `noise_after_pct`) |

## 6. Data dictionary

**Full raw data (all 336 runs):** `tailtag_raw_data.tar.zst`, 4.4 GB
compressed, about 20 GB unpacked, produced by our own experiments. Download:
https://drive.google.com/drive/folders/1IVK3r3uroNqU_zwaOjeYwiymoN8KJPTw?usp=share_link
(SHA-256 `668d096981e4fab57d59fcc6694d261638905369f4944c9bea45317d415feda5`).
Unpack with `zstd -dc tailtag_raw_data.tar.zst | tar -x` and place the
resulting `raw/` folder as `code/data/raw/` to rerun only the analysis.

Each run directory `data/raw/<experiment>/<run_id>/` contains:

**cond.json:** experiment, workload, load, rate (req/s; 0 = closed loop),
tracer (`none|count|shm|attrib|stream`), stressor, seed, worker CPUs,
duration, warmup, noise before/after (% vCPU time lost).

**meta.json** (from tlbench): calibrated compute speed, completed/measured
requests, throughput, process CPU seconds, max RSS, voluntary/involuntary
context switches.

**bench.csv**, one row per request:

| column | meaning |
|---|---|
| id | request id (stable join key) |
| tid, cpu | worker thread id and CPU at completion |
| t_arr | scheduled arrival (CLOCK_MONOTONIC ns) |
| t_enq | actual injection time; `t_enq - t_arr` = generator lateness |
| t_start, t_end | service window on the worker |
| t_done | completion (client receive time in `net`) |
| warm | 1 = warm-up request, excluded from analysis |
| pre_wake_ns | wakeup-to-run delay right before the request started (shm) |
| runq_ns | runnable-but-preempted wait inside the window |
| wake_ns | wakeup-to-run delay after blocking inside the window |
| blocked_ns | time asleep (I/O, locks) inside the window |
| softirq_ns, hardirq_ns | interrupt time that interrupted this thread |
| nvcsw, nivcsw, nmigr | voluntary/involuntary switches, migrations |
| nsoftirq, nhardirq | interrupt counts |
| kvalid | 1 = the thread's shared slot was valid (no collision) |

**tlat.req.csv** (attrib mode only): the same per-request features, produced
in the kernel and joined on `id`.

**tlat.agg.csv**, one row per 5 ms: `t` plus deltas of system-wide
`nswitch`, `ninvol`, `nwakeup`, `nmigr`, `nhardirq`, `hardirq_ns`,
`nsoft0..9`, `softns0..9` (softirq vectors: 1 TIMER, 2 NET_TX, 3 NET_RX,
4 BLOCK, 7 SCHED, 9 RCU) and cumulative PSI `psi_cpu_us`, `psi_io_us`.

**tlat.json:** records, ring-buffer drops, collector CPU, BPF run time and
count (`kernel.bpf_stats_enabled=1`), map memory, slot collisions. From
`fixval2` on also: `npreempt` (target switch-outs with the kernel's preempt
flag), `preempt_sleeping` (of those, with a sleep state already set),
`no_wake_gap`/`no_wake_gap_ns` (switch-ins after a voluntary switch-out with
no wakeup seen; charged to runqueue wait). `meta.json` from tlbench has
`ksnap_retries` and `ksnap_giveups` (snapshots still torn after 8 tries).

## 7. Configuration knobs

| What | Where |
|---|---|
| Load levels, service times | `configs/workloads.json` (per workload: `tlbench` arguments and the low/mid/high request rates; overrides the defaults in `scripts/experiment.py`) |
| CPU layout | `WORKER_CPUS=0-2`, `GEN_CPU=3`, `TRACER_CPU=3` |
| Stressor intensity | `STRESSORS` (duty cycle `-D`, threads `-n`, CPUs `-c`) |
| Aggregation windows | `attach_agg(windows=(5, 20, 100))` in `analysis/analyze.py` |
| Tail definition | median p99 of 5 calibration runs, per workload and load |
| Train/test split | reps 0-2 train, reps 3-4 test (cpu+kv); `net` held out entirely |

## 8. Known limitations

- tlbench must link the `-K` reader for shm mode. Unmodified binaries need attrib (uprobe) mode, which costs more on short requests.
- Slots are indexed by `tid % 4096`. Collisions are detected and counted, and their requests are dropped (zero in all our runs).
- Virtualized timing: host preemption of vCPUs is invisible to the guest. The noise gate limits it but cannot remove it entirely.

## 9. Evaluation metrics

| Metric | Meaning | Where |
|---|---|---|
| p50 / p95 / p99 / p99.9 | latency percentiles per run, from injection to completion; we report the median over 5 runs | `latency_summary.csv` |
| CV of p99 | run-to-run coefficient of variation of p99 (spread across the 5 repetitions) | `latency_summary.csv` |
| Tracer CPU per request (ns) | BPF program run time plus collector CPU, divided by completed requests; the stable overhead measure | `overhead_summary.csv` |
| Throughput loss (%) | 1 - throughput(traced) / median throughput(untraced), closed loop, with bootstrap 95% CI | `overhead_summary.csv`, Fig. 6 |
| Dropped events, map memory | ring-buffer drops and kernel map size | `tlat.json` per run, `overhead_summary.csv` |
| Identity residual | \|(t_end - t_start) - (on-CPU + runqueue + wakeup + blocked)\| per request; share within 5 us | `validation.csv`, `robustness_checks.json` |
| Tail request | latency above the median p99 of five calibration runs for the same workload and load | `results.json:thresholds` |
| PR-AUC | area under the precision-recall curve for ranking tail requests; a random ranking scores the base rate (fraction of tail requests). Used instead of accuracy because tails are rare | `detection.csv` |
| Precision, recall, F1 | at the threshold that maximizes F1 on the training runs | `detection.csv` |
| False alarms per minute | normal requests flagged as tail, per minute of run time | `detection.csv` |
| Bootstrap 95% CI | resampling whole runs (not requests), so the interval reflects run-to-run variation | `detection.csv` (`ap_lo`, `ap_hi`) |
| Fold change | mean per-request delay of a component with the stressor / without it | `interventions_summary.csv`, Fig. 5 |
| Mann-Whitney U, Holm | one-sided test of each pre-stated signature on 5 vs. 5 per-run means; Holm corrects for running 9 tests | `results.json:h2_tests` |
| Attribution stability | standard deviation, across the 5 runs of a condition, of the share of tail latency given to each component | `results.json:attribution_stability_std` |

## 10. Quick self-check

`sudo make test` (from `code/`) runs four short real runs in a temporary
directory and prints one line per check. Expected output ends with
`ALL CHECKS PASSED`:

```
[PASS] requests measured: ~28000 requests
[PASS] kernel features on every request: 100.00% valid
[PASS] no lost events / slot collisions: drops=0 collisions=0
[PASS] accounting identity within 5 us: >= 99% of requests
[PASS] CPU hog raises preemption wait
[PASS] uprobe records join by request id: 100.00% of requests
[PASS] aggregate samples written
ALL CHECKS PASSED
```
