# libbpf-tools/runqlat: add -S/--split to separate wakeup and preemption latency

**Target:** https://github.com/iovisor/bcc (libbpf-tools)
**Status:** drafted and built locally; not yet submitted (submit from your own GitHub account)

## Summary

`runqlat` records a task as runnable in two different situations:

1. on `sched_wakeup` / `sched_wakeup_new`, when a sleeping task is woken, and
2. on `sched_switch`, when `prev` is switched out while still `TASK_RUNNING`, i.e. it was preempted.

Both waits end up in the same histogram. They have different causes and
different fixes, though. Long waits *after wakeup* usually point at wakeup
placement or at no idle CPU being available when the task becomes runnable.
Long waits *after preemption* point at CPU contention from other runnable
tasks on the same CPU (time-slice sharing, a noisy neighbour).

This patch adds `-S` / `--split`, which keeps one histogram per cause:

```
# runqlat -S 5 1
Tracing run queue latency... Hit Ctrl-C to end.

run queue latency [after wakeup]
     usecs               : count    distribution
         ...

run queue latency [after preemption]
     usecs               : count    distribution
         ...
```

It combines with `-P` and `-L` (per process / per thread). In that case the
cause is encoded in the top bit of the histogram key
(`RUNQLAT_PREEMPT_BIT`). PIDs and TIDs are bounded by `PID_MAX_LIMIT`
(2^22), so the bit never collides with a real ID. `--pidnss` keys on
namespace inode numbers, which do not have that guarantee, so `-S` with
`--pidnss` is rejected with an error.

## Implementation notes

- The `start` map value changes from `u64 ts` to
  `struct enqueue_info { u64 ts; u64 preempted; }`. `preempted` is a `u64` so
  the struct has no padding holes (cf. #4688).
- `trace_enqueue()` takes a `preempted` flag: `false` from the wakeup
  handlers, `true` from the `TASK_RUNNING` branch of `handle_switch()`.
- Without `-S`, behaviour and output are unchanged. The extra cost is 8
  bytes per tracked task in `start`.

## Motivation / evidence

This came out of a course research project on attributing tail latency to
OS events with eBPF. There, separating the two kinds of runqueue wait was
needed to tell a co-located CPU hog (preemption waits grow) apart from
wakeup-path delays (wakeup waits grow). In our controlled experiments
(<fill in from results: e.g. "a CPU hog sharing the worker CPUs raised the
p99 preemption wait by N× while the wakeup wait changed by M×">).

## Testing

Built against upstream master `c398a9a7`, Ubuntu 24.04, kernel 6.8.0-142,
arm64 (see `opensource/build_and_test.sh`):

- `runqlat` without `-S`: output identical to before.
- `runqlat -S` with a CPU hog pinned to CPU 0: the preemption histogram is populated.
- `runqlat -S -P` / `-S -L`: per-pid / per-tid histograms carry the cause suffix.
- `runqlat -S --pidnss`: rejected.
