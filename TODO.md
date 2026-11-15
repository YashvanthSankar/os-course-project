# TODO: review fixes before submission

Collected from two external reviews and checked against the repo (2026-11-06).
Status updated 2026-11-14. Open items are marked **[ ]**.

## 1. Ask Yashvanth
- [x] **When was inherited delay finalised? Was net looked at?** Yes. The 10 µs floor and the queue-length control were designed on early *smoke-test* data (short, noisy runs) that included net, before the final campaign, so net was not fully unseen. Now stated in threats to validity. Re-tested on **9 fresh net runs** with unused seeds (`netfresh`): aligned PR-AUC **0.04** vs aggregate **0.10** (base rate 0.011). H1 did not hold there, and the report now says so (see §6).
- [x] **Where is the raw data?** In the Linux VM at `~/work/project/data/raw` (19 GB, 318 runs). Compressed archive: see §2.

## 2. Raw data
- [ ] **Upload the archive and put the link in README.md and `data/sample/README.md`** (needs your Drive/Zenodo account). The archive is `~/Study/OS/tailtag_raw_data.tar.zst` on Yashvanth's Mac (4.4 GB, all 336 runs incl. `fixval2`, integrity checked; raw data ~20 GB), SHA-256 `668d096981e4fab57d59fcc6694d261638905369f4944c9bea45317d415feda5`.
- [x] One small sample run committed: [data/sample/](data/sample/) (`main/cpu-low-shm-r0`, 3.7 MB, gzip).
- [x] Data dictionary already exists in [docs/REPRODUCE.md](docs/REPRODUCE.md) §6.

## 3. Report fixes
- [x] **Queueing control = diagnosis, not prediction**: reworded in results §H1.
- [x] **Base rates in the detection table**: caption now gives test 0.20, net 0.05; text gives 20.4% / 4.9%.
- [x] **H1 line for net**: "about twice the PR-AUC of aggregate counts", and "the aggregates still carry signal, at 4–5× the base rate".
- [x] **Cited two close prior works** (both verified: Crossref DOI 10.1109/ISPASS61541.2024.00013; arXiv 2609.02052, labelled as a preprint): Rezvani et al. ISPASS 2024 (reconstruct vs. explain) and SchedBlame (culprit containers vs. per-request).
- [x] **Noise gate**: results now say the gate waits rather than rejects; 35 of 318 runs waited (median 15 s, longest 20 min).

## 4. Code fix
- [x] **Torn read in `ksnap_take`** fixed with a retry when `oncpu_ts` changes (seqlock-style); retries are counted in `meta.json` (`ksnap_retries`, median 1 per run). 18 validation runs (`fixval`): identity on switched requests worst run **90.9% → 98.0%**. The campaign used the old binary; this is stated in the report.

## 5. Analysis fixes
- [x] **Identity on the meaningful subset**: only 0.44% of requests switched inside their window; for those, the median run holds 99.97%, worst 90.9% (98.0% after the fix). In the report.
- [x] **"No OS signal" sensitivity**: 50% / 55% / 58% of tails at 5 / 10 / 20 µs floors. In the report.
- [x] **Absolute values for intervention ratios**: added in µs (e.g. preemption wait 0.02 → 4.64 µs for the 285× case; the ratios are large because the baseline is tens of ns).

## 6. If time allows
- [x] **Rotated the held-out workload** (`analysis/robustness_checks.py`). Held-out cpu: forest aligned 0.58 vs aggregate 0.33, but logistic 0.43 vs 0.56. Held-out kv: 0.61 vs 0.57, logistic 0.50 vs 0.63. Held-out net: 0.42/0.50 vs 0.25/0.26. **The net advantage does not generalize.** The report now says H1 is not supported as a general claim, and explains why (fresh-net tails are 69–99% client-side, outside the request window).
- [ ] **Extend the open-loop A/B latency test** beyond kv-mid. Not done (needs a quiet host for about 30 min).
- [ ] **One bare-metal run** to bound VM steal time. Not possible on this setup; listed as future work.

## 7. Third review (2026-11-08), on main @ a799313
Open items, most important first.

### Fix before submission
- [x] **`sched_switch` ignores the `preempt` flag** ([src/tlat.bpf.c:171](src/tlat.bpf.c#L171)). `involuntary = prev_state == 0` misses a thread preempted after it set `TASK_INTERRUPTIBLE` but before `schedule()` (futex / condvar / `epoll_wait` paths). Its off-CPU time goes to blocked/wake, or is lost and breaks the identity. Fix: `int involuntary = preempt || prev_state == 0;`. Check the 6.8 `__schedule()` source first, then rerun the 18 `fixval` runs. If no rerun is possible, add a sentence to threats to validity.
- [x] **H2 count after multiple-testing correction** (results §H2). 9 one-sided 5-vs-5 tests; the minimum attainable p is 1/252 ≈ 0.004. Holm keeps 5 of 9 (cpu hog, cpu move, kv hog, kv I/O, net hog); cpu flood and kv move are at p = 0.048. Write "7 of 9 at p < 0.05 uncorrected; 5 of 9 after Holm". Also say that cpu flood is only 0.16 µs/request in absolute terms.
- [x] **Abstract overclaims the bcc patch.** It says "contribute a `runqlat` patch to bcc", but `opensource/PR_DESCRIPTION.md` says "not yet built or submitted". Change it to "prepare", or build, test and open the PR, then link it. The patch keeps upstream's `get_task_state(prev) == TASK_RUNNING`, so it has the same preemption blind spot.
- [x] **Raw data size mismatch.** (fixed: ~20 GB raw, 336 runs, 4.4 GB archive) README says ~16 GB, §1 above says 19 GB. Fix it together with the upload link (§2).

### Wording / statistics
- [x] **5% overhead claim** (results §Overhead). The A/B test (n = 8, p99 335 vs 146 µs, p = 0.33) has no power to show a 5% effect. Lead with the per-request cost instead: 120–220 ns against 20–100 µs of service is 0.2–1%. Say "no effect detected at n = 8" and drop "therefore met".
- [x] **H2 "and only that component" is never tested.** For kv I/O, IRQ time rose 23× while blocked time rose 5.7×. Either drop "and only that one" from H2 in the intro, or report the off-target ratios (already in `results.json → h2_tests[].other_ratios`).
- [x] **All H1 features are post-hoc, not only the queue control.** Inherited delay uses the same wait window, and own-window features finish at request end. Add one sentence: "H1 tests diagnosis; only the early-warning result is predictive."
- [x] **One-sided vs two-sided tests.** The text uses one-sided tests and the Fig. 5 stars use two-sided ones. Say so in the caption.

### Minor (mention, or fix if time allows)
- [x] **Nested hard IRQs are counted twice.** A softirq's duration includes any hard IRQ inside it, and `irq = softirq_ns + hardirq_ns`. It is a small overestimate; state it.
- [x] **Loopback NET_RX runs in the sender's context**, so a net thread's own protocol work is charged as "interrupt time". Add it to threats to validity.
- [x] Header comment in `src/tlat.bpf.c:19` describes only uprobe windows; add `MODE_SHM`.
- [x] `ksnap_take` ([src/tlbench.c:178](src/tlbench.c#L178)) silently returns a possibly torn snapshot after 8 retries. Count give-ups in `meta.json`.
- [x] `slot_ts()` claims a slot with a plain write. Use `__sync_val_compare_and_swap` (collisions were 0, so this is theoretical).
- [x] The app writes `last_wake_delay = 0` into a slot the kernel also writes (benign race). Add a comment.
- [x] Expect a viva question on the overhead table: kv "counters only" is 33% *faster* than untraced (closed-loop noise, ±17%).

## Already done (no action)
- Overhead table (5 modes; ns/req includes BPF run time) and kv-mid A/B test.
- "We found no…" gap wording.
- Interrupt time "charged to the interrupted thread".
- Slot collisions counted (0 in all runs); TID reuse harmless since only deltas are used.
- References: 14 in total: 9 papers (8 peer-reviewed conference papers + 1 CACM article), 1 arXiv preprint, 1 conference talk, 3 docs/software/blog. All verified.
- "No signal" threshold stated (10 µs), now with 5/20 µs sensitivity.

### Assignment checklist pass (2026-11-14)
- Overhead comparison is now a figure; F1 and false alarms/min, p95, memory and attribution stability added to the report.
- `sudo make test` self-check added; bcc patch built and tested (all 4 checks pass, applies to current master).

### Outcome of §7 (2026-11-14)
- **preempt flag:** fixed exactly as suggested (`preempt || prev_state == 0`, same as `__schedule()` in v6.8, checked in the source), plus a fallback that charges any off-CPU gap with no observed wakeup to runqueue wait. Counters added (`npreempt`, `preempt_sleeping`, `no_wake_gap` in `tlat.json`). Rerun of the same 18 conditions (`fixval2`, `analysis/preempt_fix_check.py`): 15,185 kernel preemptions of target threads, **0** with a sleep state set, **0** gaps without a wakeup. The VM kernel runs `preempt=voluntary`, so the case cannot arise between `set_current_state()` and `schedule()`; the campaign data are unaffected. Identity on switched requests in the rerun: worst run 98.8%, median 100%. BPF now builds with `-mcpu=v3` (needed for the 32-bit CAS in `slot_ts()`).
- **H2:** 7 of 9 uncorrected, 5 of 9 after Holm (verified from `results.json:h2_tests`); now in abstract, results and conclusion (macros `hTwoRaw`, `hTwoHolm`). cpu flood +0.16 µs stated. "and only that component" removed from the intro.
- **bcc patch:** abstract says "prepare"; the preemption blind spot is documented in `opensource/PR_DESCRIPTION.md` as a known limitation for a separate PR (it would change the default output).
- **Overhead:** "therefore met" removed; per-request cost (0.2% cpu, 0.5% kv) is now the main evidence.
- **Wording:** H1-is-diagnosis sentence, one- vs two-sided note in the Fig. 5 caption, nested IRQs and loopback NET_RX in threats to validity.
- **Code notes:** header comment covers MODE_SHM; `ksnap_giveups` in `meta.json` (0 in all 18 reruns); CAS slot claim; comment on the `last_wake_delay` race.
- **Viva notes** for all of the above in `docs/LEARN.md` Part 9, item 10.
- Report still 6 pages; section lengths unchanged except results 2.50 pages.
