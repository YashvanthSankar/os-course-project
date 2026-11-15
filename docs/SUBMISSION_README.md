# TailTag: Trap-Free eBPF Attribution of OS-Induced Tail Latency

**Assignment A1** (eBPF-based attribution and early warning of OS-induced tail latency), Operating Systems course.

| Member | Roll no. |
|---|---|
| Yashvanth S | CS24I1029 |
| Jeevanandham T | CS24I1035 |
| Nithillakrishi PY | CS24I1039 |
| Akula Sai Teja | CS24I1054 |

Repository: https://github.com/YashvanthSankar/os-course-project

## What the project does

When a request to a service is slow, the operating system is often the
cause: the thread waited for a CPU, was interrupted, or blocked. Standard
eBPF tools only show such delays as totals over time. **TailTag** keeps
per-thread counters of preemption wait, wakeup delay, blocked time and
interrupt time in a BPF map that the application maps into its own memory,
so every request can be tagged with the OS delay it suffered using plain
memory reads, with no kernel trap per request (122-220 ns of tracer CPU per
request, 47-53x cheaper than uprobes).

We tested two hypotheses over 336 runs of three workloads (cpu, kv, net):

- **H1** (per-request kernel data detects slow requests better than
  system-wide event counts): **not supported in general.** Queueing explains
  most slow requests; per-request data helps only for tails caused inside
  the request's own window.
- **H2** (each controlled stressor moves the predicted delay component):
  **7 of 9** predictions hold (**5 of 9** after Holm correction); the misses
  revealed an interrupt path we had not predicted.

## What we built

About 3,800 lines of code, all written for this project:

| Component | Language | Lines | Purpose |
|---|---|---|---|
| `code/src/tlat.bpf.c` | C (eBPF) | 445 | kernel programs on 7 scheduler and interrupt tracepoints; per-thread state machine for preemption, wakeup, blocked and IRQ time |
| `code/src/tlat.c` | C | 328 | loader and collector: four tracing modes, 5 ms system-wide sampling, map pinning |
| `code/src/tlbench.c` | C | 811 | open-loop benchmark (cpu, kv, net workloads) with stable request ids and trap-free counter reads |
| `code/src/stressor.c`, `hiccup.c` | C | 274 | controlled interventions (CPU hog, softirq flood, I/O, affinity) and the VM-noise meter |
| `code/scripts/experiment.py`, `smoke_test.py` | Python | 354 | automated campaign of 336 runs with noise gate and randomized order; one-minute self-test |
| `code/analysis/` | Python | 1,479 | per-request latency breakdown, inherited delay, detection models, statistical tests, every figure and table |
| `code/opensource/runqlat-split.patch` | C (eBPF) | 224 | patch for bcc `runqlat` separating wakeup from preemption latency; built and tested |

How the results were checked:

- predictions for every intervention were written down before running it
- train/test split by whole runs, an unseen workload, rotation of the held-out workload, and a fresh replication with new seeds
- bootstrap confidence intervals over runs, and Holm correction for multiple tests
- an accounting identity on every request, which found two real bugs (a torn read and an ignored preempt flag); both were fixed and the affected runs repeated
- a noise gate that waits for a quiet host before every run

## Folder structure

```
Team_<No>_Assignment_A1/
├── README.md                     this file
├── latex_report/
│   ├── main.tex                  IEEE conference template (IEEEtran)
│   ├── references.bib
│   ├── report.pdf                compiled 6-page report
│   ├── figures/                  figures used in the report (PDF)
│   ├── sections/                 one .tex file per report section
│   └── generated/                tables and numeric macros written by the analysis
├── code/
│   ├── src/                      eBPF tracer, collector, benchmark, stressor, noise meter (C)
│   ├── scripts/                  setup, experiment driver, one-command reproduction, self-test
│   ├── analysis/                 analysis pipeline (Python): tables, figures, statistics
│   ├── configs/workloads.json    workload parameters and load levels
│   ├── requirements.txt          Python dependencies (pinned)
│   ├── Makefile                  build, `make test`, `make package`
│   └── opensource/               patch for bcc runqlat (+ build/test script, PR text)
├── documentation/
│   ├── reproducibility_guide.md  full setup, commands, configuration, outputs, metrics
│   └── reproducibility_guide.pdf same guide as PDF
└── supplementary/
    ├── datasets/                 one complete raw run + download link of the full data
    ├── experimental_results/     every CSV/JSON behind the report's numbers
    ├── additional_figures/       all result figures (PDF)
    └── other_supporting_materials/  bcc patch test log, environment of the measured machine
```

## Quick start (Ubuntu 24.04, root, 4 CPUs)

```bash
cd code
bash scripts/setup_vm.sh          # install toolchain (apt)
make                              # build tracer, benchmark, stressor, noise meter
sudo make test                    # 1-minute self-check, ends with ALL CHECKS PASSED
sudo bash scripts/run_all.sh      # full reproduction: all experiments + analysis (~3.5 h)
```

`run_all.sh` regenerates `code/results/` and the report's tables, numbers
and figures in `latex_report/generated/` and `latex_report/figures/`. On
macOS, run the same commands inside a Linux VM; see
`documentation/reproducibility_guide.md`, section 4.

## Compiling the report

```bash
cd latex_report
pdflatex main && bibtex main && pdflatex main && pdflatex main
```

It also compiles unchanged on Overleaf (pdfLaTeX) and with Tectonic.

## Data

`supplementary/datasets/` contains one complete raw run and its data
dictionary. The full raw data of all 336 runs (about 20 GB, 4.4 GB
compressed) is linked from `supplementary/datasets/README.md` together
with its SHA-256 checksum.
