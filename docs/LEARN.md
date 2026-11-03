# Learning Guide: Every Concept This Project Uses

Read this top to bottom once and you'll be able to follow the code, the
experiments and the report. Each section starts simple and ends with how
the concept shows up in *our* project. Self-check questions are at the end,
so use them before the viva.

---

## Part 0: The one-paragraph version of the project

A server handles requests. Most finish fast (say 100 µs), but roughly 1 in 100
takes 10x longer. Those slow ones are the **tail**. We want to know *why* each
slow request was slow: was the thread waiting for a CPU? Was it interrupted
by the network card? Was it blocked on disk? Or was the app itself just
doing more work? We use **eBPF** to watch the Linux kernel's scheduler and
interrupt events, line them up with each request, and then (1) explain each
slow request and (2) train a small model that flags slow requests from
kernel signals alone. Finally we **deliberately cause** each problem (CPU
hog, interrupt flood, disk pressure) to check that our explanation changes
the way it should. That last step is what separates "these happened
together" from "this caused it".

---

## Part 1: Latency and the "tail"

### Latency vs throughput
- **Latency**: how long *one* request takes, from arrival to completion. Units: µs (microseconds, 10⁻⁶ s) or ms.
- **Throughput**: how many requests per second (req/s or ops/s) the system completes.
- They trade off. Pushing more load usually raises throughput until the system saturates, and latency climbs sharply as it approaches that point.

### Percentiles (p50, p95, p99, p99.9)
Sort all request latencies from fastest to slowest.
- **p50 (median)**: 50% of requests are faster than this.
- **p99**: 99% are faster, so 1% are *slower*. This is "the tail".
- **p99.9**: only 1 in 1000 is slower. To estimate it you need lots of samples (rule of thumb: at least ~10,000 requests, otherwise p99.9 rests on about 10 points).

**Why averages lie:** 99 requests at 100 µs plus 1 request at 50 ms average out to about 600 µs. That looks "fine" but hides a user who waited 50 ms.

**Tail at scale (Dean & Barroso, 2013):** if one page load fans out to 100 servers and waits for all of them, then *every* page load sees the p99 of at least one server. So the tail of each server becomes the *median* experience for users.

### Tail episode
A request (or a short time window) whose latency is above a threshold. We fix
that threshold **in advance**: the p99 of a separate **calibration run**. We
never compute it from the data we test on, because that would be cheating
(see *data leakage*, Part 6).

### Utilization and queueing
- **Utilization (ρ, "rho")** = arrival rate × average service time ÷ number of workers. At ρ = 0.5 the workers are busy half the time.
- **Queueing theory** says waiting time grows like 1/(1−ρ): at 50% utilization waits are small, at 90% they are about 10x bigger. Li et al. (SoCC'14) measured exactly this: p99 rose about 10x going from 50% to 95% utilization.
- This means part of the tail is "natural" queueing even on a perfect OS. Our job is to separate *that* from *OS-induced* delay.

### Open-loop vs closed-loop load generators (important!)
- **Closed loop**: a client sends a request, waits for the reply, then sends the next. If the server stalls, the client *stops sending*, so the stall hides itself. This is called **coordinated omission**.
- **Open loop**: requests arrive on a schedule, independent of replies, usually with random **Poisson** arrivals (random gaps with a fixed average rate, like customers walking into a shop). Latency is measured from the *scheduled* arrival time, so stalls are counted honestly.
- Treadmill (Zhang et al., ISCA'16) showed that badly designed load testers give wrong tail numbers for these kinds of reasons.
- **In our project:** `tlbench` is open-loop with Poisson arrivals for the latency experiments, and closed-loop only for measuring maximum throughput (overhead experiment).

---

## Part 2: Processes, threads and the scheduler

### Process, thread, PID, TID, TGID
- A **process** is a running program with its own memory. A **thread** is a separate line of execution *inside* a process, and threads share the process's memory.
- Linux calls every thread a **task** (`struct task_struct` in the kernel).
- In kernel terms:
  - **PID** (kernel meaning) = the ID of one *thread*. User space calls this the **TID**.
  - **TGID** (thread group ID) = the ID of the *process*. User space calls this the "PID".
- `bpf_get_current_pid_tgid()` returns both packed into one 64-bit number: upper 32 bits = TGID, lower 32 bits = TID.
- **PID reuse:** after a process exits its ID can be given to a new process, so an old ID might point at the wrong program. We avoid this by tracing one known benchmark process for a short, controlled run.

### Task states
- **Running**: currently on a CPU.
- **Runnable (ready)**: wants to run but is waiting in a queue for a CPU.
- **Sleeping / blocked**: waiting for something (disk, network, a lock, a timer). It doesn't want a CPU yet.

### The scheduler and runqueues
- The **scheduler** is the part of the kernel that decides which runnable task gets each CPU next.
- Each CPU has a **runqueue**: the list of runnable tasks waiting for that CPU.
- Linux's normal policy used to be **CFS** (Completely Fair Scheduler). Since kernel 6.6 it is **EEVDF** (Earliest Eligible Virtual Deadline First). Both try to share CPU time fairly. Ubuntu 24.04's 6.8 kernel uses EEVDF.
- **sched_ext** (mentioned in the assignment references) is a newer feature that lets you write a whole scheduling policy as an eBPF program. We *observe* the scheduler and don't replace it, but it's worth knowing that it exists.

### Runqueue delay (also "scheduling latency" or "runqlat")
The time a task spends **runnable but not running**. It has two flavours:
1. **Wakeup delay**: the task was asleep, something woke it (e.g., a new request arrived), and then it waited before actually getting a CPU.
2. **Preemption delay**: the task was running, got kicked off the CPU while still wanting to run (see *involuntary switch*), and waited to get back on.

This is the #1 OS-side cause of tail latency we expect to find under CPU contention.

### Context switch
The CPU stops running task A and starts running task B. The kernel fires a
`sched_switch` event each time, which tells us `prev` (who left), `next` (who
arrived) and `prev_state` (what state `prev` was in when it left).
- **Voluntary switch (nvcsw)**: `prev` gave up the CPU itself because it went to sleep (waiting for I/O or a lock). `prev_state` ≠ running.
- **Involuntary switch (nivcsw)**: `prev` was **preempted**, kicked off while it still wanted to run, because its time slice ended or a more deserving task woke up. `prev_state` == running (value 0).

### Wakeup
When a sleeping task becomes runnable, the kernel fires `sched_wakeup`.
**wakeup → switch-in** is the wakeup delay.

### CPU migration
Moving a task from one CPU's runqueue to another's (the `sched_migrate_task`
event). The load balancer does this to spread work. Each migration can cost
cache warmth, because the task's data is in the old CPU's cache. Lozi et al.
(EuroSys'16) found Linux load-balancing bugs that left cores idle while
threads waited, which is direct evidence that the scheduler causes delay.

### CPU affinity / pinning
Telling the kernel "this thread may only run on CPUs {0,1}" (`taskset`,
`sched_setaffinity`, `pthread_setaffinity_np`). We use it as an
**intervention**: pin the stressor to the same CPUs as our server (should hurt)
versus different CPUs (should not hurt).

### Nice value / priority
-20 (highest priority) to +19 (lowest). It affects how much CPU a task gets
under the fair scheduler.

### Off-CPU time
Time a thread is *not* on a CPU: blocked (sleeping) plus runnable-waiting.
Tools like Brendan Gregg's `offcputime` and the wPerf paper (OSDI'18) study
it. wPerf showed that analysing *waiting* events finds bottlenecks that
CPU profilers miss.

---

## Part 3: Interrupts and softirqs

### Hardware interrupt (hardirq / IRQ)
A device (network card, disk, timer) signals the CPU: "stop what you're doing
and handle me *now*". The CPU pauses the current thread (it does **not**
switch tasks; it borrows the thread's CPU time) and runs the **interrupt
handler**. Handlers must be very short.
- Kernel events: `irq_handler_entry` / `irq_handler_exit`.
- `/proc/interrupts` shows counts per IRQ per CPU.

### Softirq ("bottom half")
The urgent part of the work runs in the hardirq. The heavier follow-up work
is deferred to a **softirq**, which runs soon afterwards with interrupts
re-enabled. Types (vectors) include `NET_RX` (receive packets), `NET_TX`,
`TIMER`, `SCHED`, `RCU`, `BLOCK`, `TASKLET` and `HRTIMER`.
- Kernel events: `softirq_entry` / `softirq_exit` (with `vec` = type).
- `/proc/softirqs` shows counts.
- **Key point for us:** a softirq also runs "on top of" whatever thread was on the CPU, stealing its time. The thread's on-CPU clock keeps ticking, but no app code runs. In a BPF program running in softirq context, "current task" = the interrupted thread, so we can charge the stolen time to *that* thread.
- **ksoftirqd/N**: if softirqs pile up, the kernel hands them to a per-CPU kernel thread called `ksoftirqd`. That thread then competes with our server for the CPU and shows up as runqueue delay instead.

### Why interrupts cause tail latency
Li et al. (SoCC'14) found that *where* NIC interrupts land relative to server
threads (which cores) changes tail latency a lot. A burst of network
softirqs on the core running your request adds directly to its latency.

---

## Part 4: Other OS waits that can cause tails

- **Page fault**: the program touches memory whose page isn't in RAM / isn't mapped yet, and the kernel has to fix it. *Minor* = page is in memory, just needs mapping (µs). *Major* = must read from disk (ms).
- **I/O wait**: thread blocked on disk (`read`, `fsync`, `fdatasync`). `fdatasync` forces written data to stable storage and can take milliseconds.
- **Page cache**: the kernel keeps recently used file data in RAM, so a `pread` that hits the cache is fast and a miss goes to disk.
- **Lock contention**: threads waiting on a mutex. In Linux, user-space locks and condition variables are built on **futex** (fast user-space mutex). Waiting = voluntary switch (sleep); waking = `sched_wakeup`.
- **Condition variable (condvar)**: a "sleep until someone signals me" primitive. Our benchmark's workers sleep on one until a request is queued.
- **epoll**: Linux's mechanism to wait on many sockets at once. Event-driven servers (nginx, Redis) sit in `epoll_wait` until a socket has data.
- **Loopback (127.0.0.1)**: networking to yourself. Packets never leave the machine, but they still go through the kernel network stack *and NET_RX softirqs*. That's useful for causing softirq load in a controlled way.
- **CPU frequency scaling / idle states (C-states)**: a CPU waking from deep sleep is slow. In a VM we can't control this; we list it as a threat to validity.

---

## Part 5: eBPF, the tool we use

### What eBPF is
**eBPF** (extended Berkeley Packet Filter) lets you load small, safe programs
*into the running Linux kernel* without writing a kernel module or rebooting.
The programs run when a chosen kernel event happens. Originally it filtered
network packets (the BSD Packet Filter, McCanne & Jacobson, 1993). Now it is
used for tracing, networking (XDP), security and even schedulers.

### The verifier
Before loading, the kernel **verifier** checks the program: no infinite
loops, no out-of-bounds memory access, no crashing the kernel, bounded size.
If the verifier rejects your program, it won't load. This safety is why eBPF
can run in production.

### JIT
After verification, the BPF bytecode is **JIT-compiled** (Just-In-Time) to
native machine code (arm64 in our VM), so it runs fast.

### Hooks: where programs attach
- **Tracepoint**: a stable, named event the kernel developers placed on purpose (e.g., `sched:sched_switch`). Stable across versions, so it's the best choice. List them: `ls /sys/kernel/tracing/events/sched/`.
- **tp_btf / raw tracepoint**: same tracepoints, but your program receives the raw kernel arguments (like the actual `struct task_struct *`) with BTF type info. It's cheaper than classic tracepoints. **We use these.**
- **kprobe / kretprobe**: attach to almost *any* kernel function's entry or return. Powerful, but unstable because functions get renamed between versions.
- **fentry / fexit** (`BPF_PROG_TYPE_TRACING`): a faster, modern kprobe built on BPF trampolines.
- **uprobe / uretprobe**: attach to a function in a **user-space program**. **We use these** on two tiny functions in our benchmark, `tl_req_begin(id)` and `tl_req_end(id)`, so the kernel side knows exactly when each request starts and ends on which thread. Cost: each uprobe hit traps into the kernel (~1–3 µs), so it counts toward our overhead budget.
- **USDT**: static markers compiled into user programs (like tracepoints for apps). It's an alternative to uprobes.
- **perf_event**: run on a timer or hardware counter (used by profilers).

### Maps: how BPF programs store data
A **map** is a key/value store that lives in the kernel. Both BPF programs and
user space can read and write it.
- `BPF_MAP_TYPE_HASH`: general key → value. We use thread ID → per-thread counters.
- `BPF_MAP_TYPE_ARRAY` / `PERCPU_ARRAY`: fixed-size array. PERCPU keeps a separate copy per CPU, so there's no locking and it's very cheap. We use it for global counters and per-CPU softirq start timestamps.
- **In-kernel aggregation**: instead of sending every event to user space, add it up inside the map (count, total ns, histogram). That's much cheaper, and the assignment's ablation asks us to compare it with streaming.

### Getting data to user space
- **Perf buffer** (`PERF_EVENT_ARRAY`): older. One buffer per CPU, so events can arrive out of order across CPUs.
- **Ring buffer** (`BPF_MAP_TYPE_RINGBUF`, kernel ≥ 5.8): one shared buffer, ordered, more memory-efficient. **We use this.**
- **Dropped events**: if user space reads too slowly, the buffer fills and `bpf_ringbuf_reserve()` fails. We **count every failure** in a map and report it, as the assignment requires.

### Helpers
Kernel functions a BPF program may call:
`bpf_ktime_get_ns()` (monotonic clock in ns), `bpf_get_current_pid_tgid()`,
`bpf_get_smp_processor_id()` (which CPU), `bpf_map_lookup_elem()`,
`bpf_ringbuf_reserve/submit()`.

### BTF, CO-RE, vmlinux.h, libbpf, skeleton, bpftool
- **BTF** (BPF Type Format): compact type information about the kernel's structs, found at `/sys/kernel/btf/vmlinux`.
- **vmlinux.h**: one giant header generated from BTF (`bpftool btf dump file /sys/kernel/btf/vmlinux format c`) that holds every kernel type.
- **CO-RE** (Compile Once – Run Everywhere): struct field offsets are fixed up at load time using BTF, so one compiled program works on different kernel versions. `BPF_CORE_READ()` does this.
- **libbpf**: the C library that loads BPF programs, creates maps and attaches hooks.
- **Skeleton** (`*.skel.h`): a header generated by `bpftool gen skeleton` that gives typed C access to your BPF object (`tlat_bpf__open()`, `__load()`, `__attach()`).
- **bpftool**: CLI to inspect loaded programs and maps (`bpftool prog show`). With `sysctl kernel.bpf_stats_enabled=1` it also shows each program's **run time** and **run count**, which we use to measure in-kernel overhead.
- Other front-ends you'll see in papers and docs: **BCC** (Python + embedded C, compiles at runtime) and **bpftrace** (one-liner language, like awk for the kernel). We use plain libbpf + C because it's the lowest overhead and has the fewest dependencies.

### Clocks
- `CLOCK_MONOTONIC`: always moves forward, unaffected by date/time changes. `bpf_ktime_get_ns()` uses the same clock as `clock_gettime(CLOCK_MONOTONIC)`, so **user-space request timestamps and kernel event timestamps are directly comparable**. That shared clock is what makes the "join" possible.

### Observer effect / overhead
Tracing costs time. If our tracer slows the server by 20%, we've changed the
very thing we're measuring. The assignment wants a **budget**: we set **< 5%
throughput loss**. We measure overhead as:
- throughput loss (closed-loop max throughput, traced vs untraced),
- latency inflation (p50/p99 traced vs untraced),
- collector CPU time (`getrusage`),
- in-kernel BPF run time (`bpf_stats`),
- memory: map sizes plus ring buffer size.

### Older tracing relatives (for related work)
- **ftrace**: the kernel's built-in function/event tracer (`/sys/kernel/tracing`).
- **perf**: Linux profiler. `perf sched` records scheduler events (heavy).
- **DTrace** (Cantrill et al., USENIX ATC'04): dynamic instrumentation on Solaris, the ancestor of the "safe tracing in production" idea.
- **Distributed tracing** (Google's Dapper; Hindsight, NSDI'23): follows requests across *machines*. We do the same idea *inside one machine's kernel*.

---

## Part 6: The analysis side (statistics + ML)

### Features and labels
- A **feature** is a number describing a request, e.g. `runq_ns` (time spent runnable-waiting during the request) or `softirq_ns` (time stolen by softirqs).
- A **label** is the answer we want to predict: `is_tail` = 1 if latency > the calibrated p99 threshold, else 0.
- **Class imbalance**: only ~1% of requests are tails, so a model that always says "not tail" is 99% *accurate* and completely useless. That's why we use precision/recall, not accuracy.

### Our two kinds of features (this is hypothesis H1)
1. **Aligned per-request features** (our method): kernel events on *the exact thread* during *the exact request window* (plus the wakeup delay just before it).
2. **Aggregate counts** (the baseline that typical dashboards show): system-wide counts per 10 ms time bucket (total context switches, total softirqs, and so on), matched to the request by time only.

H1 says (1) beats (2). If it does, then "line up kernel events with each request" is worth the extra effort.

### Classifiers we compare
- **Rule / threshold**: "if runq_ns > X, flag it". Simple and fully interpretable. X is chosen on training data.
- **Logistic regression**: a weighted sum of features pushed through an S-curve to give a probability 0–1. The weights show which features matter.
- **Decision tree / random forest**: a tree of if/else splits learned from data. A forest averages many trees, so it's stronger but less interpretable. **Feature importance** shows which features it relied on.

### Metrics
Confusion matrix terms: **TP** (said tail, was tail), **FP** (false alarm), **FN** (missed tail), **TN**.
- **Precision** = TP / (TP + FP): of the requests we flagged, how many really were slow?
- **Recall** = TP / (TP + FN): of the really slow requests, how many did we catch?
- **F1** = harmonic mean of precision and recall. One number that balances both.
- **PR curve**: precision vs recall as you slide the decision threshold. **PR-AUC** = area under it (1.0 = perfect). With ~1% positives, a random guesser scores about 0.01, which makes PR-AUC more honest than ROC-AUC for rare events.
- **False alarms per minute**: FP ÷ run minutes. Operators care about this: a detector that pages you 50 times a minute is useless.
- **Calibration**: when the model says "70% chance of tail", is it right 70% of the time?

### Train/test split *by run* (avoid data leakage)
Requests from the same run share conditions (same moment's noise). If you
randomly split rows, the test set contains near-twins of training rows and
scores look great but mean nothing. That's **data leakage**. We train on some
complete runs and test on *other complete runs*. Stronger still: we pick
features using only the CPU and KV workloads, then test on the **held-out
network workload** the model never saw. That tests generalization, the gap
named in the assignment.

### Repeated runs, spread and confidence intervals
- Run each condition **≥ 5 times**. Report the **median** and the spread (min–max or IQR).
- **Confidence interval (CI)**: a range that likely contains the true value. We use the **bootstrap**: resample the runs many times with replacement, recompute the statistic, and take the 2.5th and 97.5th percentiles as the 95% CI.
- **Randomize condition order** so slow drift (the VM host getting busy, thermal throttling) doesn't line up with one condition.

### Ablation
Remove one piece and measure the drop. Ours:
- no scheduler features,
- no interrupt features,
- a different aggregation window for the count baseline (1 ms / 10 ms / 100 ms),
- per-event streaming vs in-kernel aggregation (overhead and drops).

### Correlation vs causation, and interventions
High softirq time *appearing with* slow requests doesn't prove softirqs *caused*
them. Something else (e.g., a burst of traffic) could cause both. So we
**intervene**: turn on *one* stressor at a time and check that
1. the latency tail gets worse, and
2. *the matching* feature moves (CPU hog → runq delay; interrupt flood → softirq time; disk writer → off-CPU blocked time) while the *others* don't.

That's hypothesis **H2**. We only call something a "cause" when an intervention backs it up, and even then we phrase it carefully ("is consistent with").

### Threats to validity
Honest reasons the results might be wrong or might not generalize: VM timer
noise, only 4 vCPUs, one kernel version, uprobe overhead, tracepoint
differences across kernels, a synthetic benchmark instead of a real
application. Listing these *strengthens* a paper.

### Reproducibility
Anyone should be able to re-run everything with one command and get similar
numbers: pinned versions, seeds, raw data plus a data dictionary, scripts
under git. The assignment has a checklist (Section 10) and we follow it.

---

## Part 7: How it all fits together in our code

```
 tlbench (user space, C)                     Linux kernel
 ┌──────────────────────────┐               ┌───────────────────────────────┐
 │ generator thread         │  condvar      │ sched_wakeup  ─┐              │
 │  Poisson arrivals ──────►│──signal──────►│ sched_switch  ─┤  tlat.bpf.c  │
 │ worker threads           │               │ sched_migrate ─┤  per-thread  │
 │  tl_req_begin(id) ◄──────┼── uprobe ────►│ softirq/irq   ─┤  counters    │
 │  do work (cpu / kv / net)│               │               (hash map)     │
 │  tl_req_end(id)   ◄──────┼── uprobe ────►│ end: delta of counters        │
 │ writes per-request CSV   │               │  → ring buffer record         │
 └──────────────────────────┘               │ global per-CPU counters       │
                                            └──────────────┬────────────────┘
 tlat (user space loader, C, libbpf)  ◄────── ring buffer ─┘
   writes per-request kernel features CSV + 10 ms aggregate counts CSV

 analysis/ (Python)
   join on request id → features → label with calibrated p99 →
   rule / logistic regression / random forest → PR-AUC, F1, false alarms/min
   + overhead, ablations, intervention signatures → figures for the report
```

**Per-request latency decomposition (the "explanation"):**
```
latency = queueing (waiting behind earlier requests — application/load)
        + wakeup delay (runnable, waiting for CPU after being signalled — OS scheduler)
        + runq delay inside the request (preempted — OS scheduler)
        + blocked/off-CPU time (I/O, locks — OS I/O path)
        + on-CPU time, of which:
              hardirq + softirq time (OS interrupts)
              the rest = application execution
```

---

## Part 8: Self-check questions (answer them out loud)

1. Why is p99 more useful than the mean for a server? Give a number example.
2. What's the difference between a voluntary and an involuntary context switch? Which `prev_state` value tells them apart?
3. A thread is runnable but not running. Name two reasons it might be waiting.
4. What does a softirq "steal", and why doesn't it show up as a context switch?
5. Why does an open-loop load generator give more honest tail numbers than a closed-loop one?
6. What does the eBPF verifier guarantee? Why does that matter?
7. Tracepoint vs kprobe vs uprobe: what does each attach to, and which is most stable?
8. Why do we aggregate in kernel maps instead of sending every event to user space?
9. Why can we compare `bpf_ktime_get_ns()` with `clock_gettime(CLOCK_MONOTONIC)`?
10. Why is accuracy a bad metric when only 1% of requests are tails? What do we use instead?
11. Why split train/test by *run* rather than by random rows?
12. What is H1? What is H2? Which experiment tests each?
13. Give one example of why correlation ≠ causation in this project, and how an intervention fixes it.
14. What is our overhead budget, and how do we measure it?
15. Name three threats to validity of our results.

---

## Glossary (quick lookup)

| Term | One-line meaning |
|---|---|
| ablation | remove one component and measure the effect |
| affinity | which CPUs a thread is allowed to run on |
| BTF | kernel type info used by CO-RE |
| CO-RE | compile once, run on many kernels |
| context switch | CPU changes from one task to another |
| coordinated omission | closed-loop testers hiding stalls |
| EEVDF / CFS | Linux's default fair schedulers (new / old) |
| futex | kernel primitive under mutexes and condvars |
| hardirq | device interrupt handler, runs immediately |
| ksoftirqd | per-CPU kernel thread that runs overflow softirqs |
| map | BPF key/value store in the kernel |
| migration | task moved to another CPU |
| nivcsw / nvcsw | involuntary / voluntary context switches |
| open loop | arrivals independent of responses |
| p99 | latency that 99% of requests beat |
| PR-AUC | area under precision-recall curve |
| ring buffer | ordered kernel→user event channel |
| runqueue delay | time runnable but not running |
| softirq | deferred interrupt work (NET_RX, TIMER, …) |
| tail episode | request/window above the pre-set p99 threshold |
| tracepoint | stable kernel event hook |
| uprobe | hook on a user-space function |
| verifier | kernel checker that makes BPF programs safe |
| wakeup delay | time from wakeup to actually running |

---

## Papers to read (all real; start with the first three)

1. Li, Sharma, Ports, Gribble. *Tales of the Tail: Hardware, OS, and Application-level Sources of Tail Latency.* ACM SoCC 2014. **The closest paper to our project. Read it fully.**
2. Zhang, Meisner, Mars. *Treadmill: Attributing the Source of Tail Latency through Precise Load Testing and Statistical Inference.* ISCA 2016. Covers how to measure tails correctly and how to attribute them with statistics.
3. Zhou, Gan, Ma, Wang. *wPerf: Generic Off-CPU Analysis to Identify Bottleneck Waiting Events.* OSDI 2018. Off-CPU and waiting analysis from scheduler events; it also reports ~5% tracing overhead, which is a good comparison point.
4. Lozi et al. *The Linux Scheduler: a Decade of Wasted Cores.* EuroSys 2016. Shows the scheduler itself causing delay.
5. Fried, Ruan, Ousterhout, Belay. *Caladan: Mitigating Interference at Microsecond Timescales.* OSDI 2020. Covers *fixing* interference by watching fast signals (queueing delay), which is the "early warning" idea in production form.
6. Zhang et al. *The Benefit of Hindsight: Tracing Edge-Cases in Distributed Systems.* NSDI 2023. Tracing that targets the rare slow requests.
7. Dean, Barroso. *The Tail at Scale.* CACM 2013. Short, foundational, and easy reading.

Technical references: Linux kernel BPF docs (kernel.org), docs.ebpf.io,
Brendan Gregg's *BPF Performance Tools* (2019), and bcc's `runqlat`,
`runqslower`, `softirqs` and `offcputime` tools.
