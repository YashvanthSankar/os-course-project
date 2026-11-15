# Code Guide and Reproduction Instructions

This document covers what each part of the code does, what it depends on,
and how to reproduce every number and figure in the report.

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
  measure_pages.py      page length of each report section
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
docs/LEARN.md           background on every concept used
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
| Python | 3.12 with numpy, pandas, scikit-learn, scipy, matplotlib (Ubuntu packages) |
| Report | IEEEtran (conference), built with tectonic or pdflatex + bibtex |

The exact versions of the machine that produced the report are written
automatically to `data/raw/environment.txt` (uname, lscpu, compiler,
libbpf, bpftool, Python packages, kernel BPF config, available sched
tracepoints).

Kernel requirements: `CONFIG_DEBUG_INFO_BTF=y` (for `vmlinux.h` / CO-RE and
`tp_btf`), `CONFIG_UPROBES=y` (attrib mode only), `CONFIG_RPS=y`
(softirq-flood intervention). Ubuntu 24.04 generic kernels have all three.

## 4. Setting up the VM (on macOS)

```bash
limactl create --name=ebpf --cpus=4 --memory=6 --disk=30 --vm-type=vz \
  --set '.containerd.system=false | .containerd.user=false' template://ubuntu-24.04
limactl start ebpf
limactl shell ebpf
# inside the VM:
rsync -a /Users/<you>/Study/OS/project/ ~/work/project/   # home is mounted read-only
cd ~/work/project && bash scripts/setup_vm.sh && make
```

Any Ubuntu 24.04 machine (bare metal is better) works the same way:
`bash scripts/setup_vm.sh && make`.

## 5. Reproducing the results

**Keep the host idle.** In a VM, other host load shows up as multi-millisecond
gaps that the guest kernel cannot see. The driver has a *noise gate*: before
every run, `hiccup` measures how much time a spinning thread per vCPU loses.
If that exceeds `NOISE_LIMIT` (default 3%; the reported campaign used 5%
after the first eight runs), the run waits. The measurement
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
cd ~/work/project
sudo bash scripts/run_all.sh          # build, all experiments, analysis
```

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
| Latency CCDFs (Fig. latency) | `results/fig_latency.pdf`, `latency_summary.csv` |
| Overhead (Fig. overhead, Table) | `results/fig_overhead.pdf`, `overhead_summary.csv` |
| Detection PR curves, metrics | `results/fig_pr.pdf`, `detection.csv` |
| Feature/window ablation | `results/fig_ablation.pdf`, `ablation.csv` |
| Intervention signatures (H2) | `results/fig_signature.pdf`, `interventions_summary.csv`, `results.json:h2_tests`, `results.json:signature_matrix` |
| A/B overhead test | `results/abtest_runs.csv`, `results.json:abtest` |
| net receiver placement | `results/netrcv_runs.csv`, `results.json:netrcv` |
| Tails with no OS signal | `results/no_os_delay.json` |
| Identity on switched requests, no-signal floors, noise-gate waits, held-out rotation, fresh net | `results/robustness_checks.json` |
| Preempt-flag fix: identity before/after, how often the case occurred | `results/preempt_fix_check.json` |
| H2 count after Holm correction | `report/generated/macros.tex` (`hTwoRaw`, `hTwoHolm`), from `results.json:h2_tests` |
| Every number quoted in the report | `report/generated/macros.tex` (generated by `make_tables.py`) |
| Section page lengths | `python3 scripts/measure_pages.py report/main.pdf` |
| Accounting validation, coverage, drops | `results/validation.csv`, `results.json:validation` |
| Platform noise per run | `data/raw/*/*/cond.json` (`noise_before_pct`, `noise_after_pct`) |

## 6. Data dictionary

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
| Load levels, service times | `WORKLOADS` in `scripts/experiment.py` (optional override: `configs/workloads.json`) |
| CPU layout | `WORKER_CPUS=0-2`, `GEN_CPU=3`, `TRACER_CPU=3` |
| Stressor intensity | `STRESSORS` (duty cycle `-D`, threads `-n`, CPUs `-c`) |
| Aggregation windows | `attach_agg(windows=(5, 20, 100))` in `analysis/analyze.py` |
| Tail definition | median p99 of 5 calibration runs, per workload and load |
| Train/test split | reps 0-2 train, reps 3-4 test (cpu+kv); `net` held out entirely |

## 8. Known limitations

- tlbench must link the `-K` reader for shm mode. Unmodified binaries need attrib (uprobe) mode, which costs more on short requests.
- Slots are indexed by `tid % 4096`. Collisions are detected and counted, and their requests are dropped (zero in all our runs).
- Virtualized timing: host preemption of vCPUs is invisible to the guest. The noise gate limits it but cannot remove it entirely.
