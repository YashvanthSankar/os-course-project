# TODO: review fixes before submission

Collected from two external reviews and checked against the repo (2026-11-06).

## 1. Ask Yashvanth (today)
- [ ] **When was inherited delay finalised?** LEARN.md says the first version "leaked queue length" and was then fixed (10 µs floor + queue-length control). Were net (held-out) results looked at during that fix? Git can't answer this, because the whole analysis came in one commit (`3b528da`).
  - If net was **not** looked at: say so in the method section.
  - If it **was**: say so honestly in threats to validity, and ideally re-test on fresh net runs.
- [ ] **Where is the raw data?** `data/raw/` (~16 GB, 291 runs) is not in the repo.

## 2. Raw data
- [ ] Upload `data/raw/` (Drive / Zenodo) and link it from README.md. Optionally commit one small sample run.
- Data dictionary already exists in [docs/REPRODUCE.md](docs/REPRODUCE.md) §6. No new schema needed.

## 3. Report fixes (no raw data needed)
- [ ] **Queueing control = diagnosis, not prediction.** `qlen_proxy` counts completions *during* the wait, so it is only known after the fact. Reword in results §H1.
- [ ] **Add base rates to the detection table**, separately per split:
  - cpu/kv test: **20.4%** (random PR-AUC ≈ 0.20)
  - held-out net: **4.9%** (random PR-AUC ≈ 0.05)
- [ ] **H1 line for net:** "On net (base rate 4.9%), aligned features reach about 2× the PR-AUC of aggregate counts (0.42–0.53 vs 0.18–0.26)."
  - Do NOT write "aggregates ≈ random". At 4–5× the base rate they still carry signal.
- [ ] **Cite two close prior works** in related work:
  - Rezvani, Jahanshahi, Wong, "Characterizing In-Kernel Observability of Latency-Sensitive Request-level Metrics with eBPF", ISPASS 2024. Contrast: they *reconstruct* request metrics from syscalls; we *explain* them with scheduler/IRQ time.
  - "SchedBlame: Who Ran While You Waited? Culprit-Attributed CPU Contention for Containers on Stock Kernels", arXiv 2609.02052. Label it as an **arXiv preprint**. Contrast: culprit container vs per-request attribution.
- [ ] **Noise gate:** the gate *waits* rather than rejecting, so "runs rejected" is always 0. Instead, summarise `noise_wait_s` from the `*_runs.csv` files (how many runs waited, median and max wait).

## 4. Code fix
- [ ] **Torn read in `ksnap_take`** ([src/tlbench.c](src/tlbench.c)): `oncpu_ts` and `oncpu_ns` are read as two separate loads. A preemption between them double-counts off-CPU time. Fix: read `oncpu_ts`, read the counters, re-read `oncpu_ts`, retry if it changed.
  - Then either re-run, or list it as a known limitation (rare; may explain part of the 2.2% outside 5 µs).

## 5. Analysis fixes (need `data/raw`)
- [ ] **Identity check on the meaningful subset.** For a request that never left the CPU, on-CPU = time between snapshots, so the identity holds by construction. Report the within-5 µs % for requests with **≥1 context switch** separately.
- [ ] **"No OS signal" sensitivity:** recompute the 57% with 5 / 10 / 20 µs floors (`SIG_FLOOR_NS` in `analyze.py`).
- [ ] **Absolute values for intervention ratios** (94–285× etc.): add µs before → after. First check whether `results/interventions_summary.csv` already has them.

## 6. If time allows
- [ ] **Rotate the held-out workload:** train on 2, test on the 3rd, all three ways, with CIs. Currently `net` is hard-coded as held-out (`analyze.py:511`).
- [ ] **Extend the open-loop A/B latency test** beyond kv-mid to other workloads/loads.
- [ ] One bare-metal run to bound VM steal time.

## Already done (no action)
- Overhead table (5 modes; ns/req includes BPF run time) and kv-mid A/B test.
- H1 wording ("holds for generalization only").
- "We found no…" gap wording.
- Interrupt time "charged to the interrupted thread".
- Slot collisions counted (0 in all runs); TID reuse harmless since only deltas are used.
- References: already 8 peer-reviewed, including ghOSt and Wasted Cores.
- "No signal" threshold is stated (10 µs).
