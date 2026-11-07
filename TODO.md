# TODO: review fixes before submission

Collected from two external reviews and checked against the repo (2026-11-06).
Status updated 2026-11-07. Open items are marked **[ ]**.

## 1. Ask Yashvanth
- [x] **When was inherited delay finalised? Was net looked at?** Yes. The 10 µs floor and the queue-length control were designed on early *smoke-test* data (short, noisy runs) that included net, before the final campaign, so net was not fully unseen. Now stated in threats to validity. Re-tested on **9 fresh net runs** with unused seeds (`netfresh`): aligned PR-AUC **0.04** vs aggregate **0.10** (base rate 0.011). H1 did not hold there, and the report now says so (see §6).
- [x] **Where is the raw data?** In the Linux VM at `~/work/project/data/raw` (19 GB, 318 runs). Compressed archive: see §2.

## 2. Raw data
- [ ] **Upload the archive and put the link in README.md and `data/sample/README.md`** (needs your Drive/Zenodo account). The archive is `~/Study/OS/tailtag_raw_data.tar.zst` on Yashvanth's Mac (4.2 GB, all 318 runs, integrity checked), SHA-256 `a9bc7508e738b23361dcad6a8695022478e2869d33a5e7195ae597f6a18c0044`.
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
- [x] **Rotated the held-out workload** (`analysis/review_checks.py`). Held-out cpu: forest aligned 0.58 vs aggregate 0.33, but logistic 0.43 vs 0.56. Held-out kv: 0.61 vs 0.57, logistic 0.50 vs 0.63. Held-out net: 0.42/0.50 vs 0.25/0.26. **The net advantage does not generalize.** The report now says H1 is not supported as a general claim, and explains why (fresh-net tails are 69–99% client-side, outside the request window).
- [ ] **Extend the open-loop A/B latency test** beyond kv-mid. Not done (needs a quiet host for about 30 min).
- [ ] **One bare-metal run** to bound VM steal time. Not possible on this setup; listed as future work.

## Already done (no action)
- Overhead table (5 modes; ns/req includes BPF run time) and kv-mid A/B test.
- "We found no…" gap wording.
- Interrupt time "charged to the interrupted thread".
- Slot collisions counted (0 in all runs); TID reuse harmless since only deltas are used.
- References: 14 in total: 9 papers (8 peer-reviewed conference papers + 1 CACM article), 1 arXiv preprint, 1 conference talk, 3 docs/software/blog. All verified.
- "No signal" threshold stated (10 µs), now with 5/20 µs sensitivity.
