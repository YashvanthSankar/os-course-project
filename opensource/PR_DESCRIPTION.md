# libbpf-tools/runqlat: add -S/--split to separate wakeup and preemption latency

**Target:** https://github.com/iovisor/bcc (libbpf-tools)
**Status:** built and tested (2026-11-14, Linux 6.8, arm64 VM); applies cleanly to bcc master `34d9495a`; ready to be opened as a pull request upstream.

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
- Known limitation, kept on purpose: like upstream, the switch handler
  treats a task as still runnable only when `get_task_state(prev) ==
  TASK_RUNNING`. A task preempted after it set `TASK_INTERRUPTIBLE` stays on
  the runqueue too (`__schedule()` checks the `preempt` flag first), so its
  wait is missed, or labelled a wakeup if a wakeup arrives first. Using
  `preempt || state == TASK_RUNNING` would fix it but also changes the
  default output, so it belongs in a separate PR. In our 18 test runs on
  Linux 6.8 this case occurred 0 times.

## Motivation / evidence

This came out of a course research project on attributing tail latency to
OS events with eBPF. There, separating the two kinds of runqueue wait was
needed to tell a co-located CPU hog apart from wakeup-path delays. In our
controlled experiments (Linux 6.8, 5 runs per condition), a CPU hog sharing
the worker CPUs raised the mean per-request wait *after preemption* by
94–285× but the wait *after wakeup* only by 15–46×. Moving the workers off
the hog's CPU cut the preemption wait to 0.2–0.3× of its previous value.
The two waits respond differently to the same intervention, so a single
merged histogram hides which one moved.

## Testing

Built with `opensource/build_and_test.sh` against `c398a9a7` (the patch
also applies cleanly to master `34d9495a`), Ubuntu 24.04, Linux
6.8.0-142-generic, arm64:

- `runqlat` without `-S`: output unchanged (single histogram).
- `runqlat -S 3 1` with two CPU-bound threads pinned to CPU 0: the
  *after preemption* histogram holds 996 of 1001 waits at 2048-4095 us (the
  hogs' time slice), while *after wakeup* waits stay below 32 us (71 at 0-1 us).
- `runqlat -S -P`: per-process histograms carry the `[after preemption]` /
  `[after wakeup]` suffix.
- `runqlat -S --pidnss`: rejected with "--split cannot be used with --pidnss."
