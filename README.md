# TailTag: eBPF attribution of OS-induced tail latency

OS course project. The question is which operating-system events (scheduler
preemption, wakeup delay, interrupts/softirqs, blocking) made each slow
request slow, and whether a compact set of kernel signals detects and
explains tail requests better than aggregate event counts.

Main pieces:

- **`src/tlat.bpf.c`**: eBPF programs on scheduler and IRQ tracepoints that keep per-thread wait accounting.
- **Shared-counter mode**: per-thread counters in an mmap-able BPF map, read by the application at request boundaries. No trap, syscall or event is needed per request.
- **`src/tlbench.c`**: open-loop benchmark with stable request IDs (cpu, kv and net workloads).
- **`src/stressor.c`**: controlled interventions (CPU hog, softirq flood via RPS, I/O, affinity).
- **`src/hiccup.c`**: platform-noise meter, used as a gate before every run.
- **`analysis/analyze.py`**: per-request decomposition, detection (rule / logistic / forest; split by run; held-out workload), ablations, intervention tests, figures.
- **`opensource/`**: upstream patch for bcc `runqlat` (`-S`, separating wakeup and preemption latency).

Course submission archive: `bash scripts/make_submission.sh 01` builds
`dist/Team_01_Assignment_A1.zip` in the required layout (README, latex_report,
code, documentation, supplementary).

Individual files (in [dist/](dist/), rebuilt with `make package`):

- [TailTag_report.pdf](dist/TailTag_report.pdf): the 6-page IEEE report
- [report_latex.zip](dist/report_latex.zip): LaTeX sources of the report
- [project_code.zip](dist/project_code.zip): code, scripts, analysis, results, docs, sample run
- [CODE_GUIDE.pdf](dist/CODE_GUIDE.pdf): code guide and reproduction instructions

Docs:

- [docs/LEARN.md](docs/LEARN.md): background concepts, from scratch
- [docs/REPRODUCE.md](docs/REPRODUCE.md): code guide, dependencies, one-command reproduction, data dictionary

Quick start (Ubuntu 24.04, inside the VM):

```bash
bash scripts/setup_vm.sh && make
sudo make test                 # 1-minute self-check of the whole pipeline
sudo bash scripts/run_all.sh   # full reproduction
```

## Team & Contributions

All four members contributed equally. Main areas of each member:

> **Draft, to be confirmed by the team.** This list was inferred from the
> git history (`git log --author=<name> --stat`): it records who committed
> which files, which may not capture work done together or committed by
> someone else.

| Member | Roll no. | Areas (from git history) |
|---|---|---|
| Yashvanth S | CS24I1029 | Trap-free shared-counter mode (`src/tlat.bpf.c`, `src/tlbench.c`), `src/hiccup.c` noise meter and noise gate; inherited-delay and queue-length analysis, validation and signature analysis (`analysis/analyze.py`), robustness and preempt-fix checks; torn-read and preempt-flag fixes; bcc `runqlat` patch and its test script (`opensource/`); report draft (introduction, related work, methodology) and later results revisions; `docs/REPRODUCE.md`, code guide PDF, smoke test, sample data, README, `make package` and submission files (`dist/`) |
| Jeevanandham T | CS24I1035 | Initial code base: benchmark (`src/tlbench.c`), eBPF tracer and collector (`src/tlat.bpf.c`, `src/tlat.c`, `src/tlat.h`), stressor (`src/stressor.c`), experiment driver (`scripts/experiment.py`), first analysis pipeline (`analysis/analyze.py`), Makefile and VM setup; learning guide (`docs/LEARN.md`); review findings list (`TODO.md`) |
| Nithillakrishi PY | CS24I1039 | Report structure and page budget, related work and references (`report/sections/related.tex`, `report/refs.bib`), author block, novelty scoping; detection-table layout (`analysis/make_tables.py`) |
| Akula Sai Teja | CS24I1054 | Median-of-5 calibration, A/B and receiver-placement experiments; final results and figures (`results/`), table and macro generator (`analysis/make_tables.py`), signature heatmap; report results, abstract and conclusion; first version of the PR description for the bcc patch |

## Data

- **Sample run** (in the repo): [data/sample/](data/sample/) — one complete raw run, gzip-compressed.
- **Full raw data** (~20 GB, 336 runs): [Google Drive](https://drive.google.com/drive/folders/1IVK3r3uroNqU_zwaOjeYwiymoN8KJPTw?usp=share_link). Archive `tailtag_raw_data.tar.zst`, 4.4 GB, SHA-256 `668d096981e4fab57d59fcc6694d261638905369f4944c9bea45317d415feda5`.
- **Processed results** (every table and figure in the report): [results/](results/)
- **Data dictionary:** [docs/REPRODUCE.md](docs/REPRODUCE.md) §6
