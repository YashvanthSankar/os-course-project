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

Docs:

- [docs/LEARN.md](docs/LEARN.md): background concepts, from scratch
- [docs/REPRODUCE.md](docs/REPRODUCE.md): code guide, dependencies, one-command reproduction, data dictionary

Quick start (Ubuntu 24.04, inside the VM):

```bash
bash scripts/setup_vm.sh && make
sudo bash scripts/run_all.sh
```
