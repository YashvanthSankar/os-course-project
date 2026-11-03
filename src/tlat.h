/* SPDX-License-Identifier: GPL-2.0 */
/* Shared definitions between tlat.bpf.c (kernel) and tlat.c (user space). */
#ifndef TLAT_H
#define TLAT_H

#define MODE_COUNT  0  /* global event counters only (baseline tracer)      */
#define MODE_ATTRIB 1  /* per-request in-kernel aggregation (proposed)      */
#define MODE_STREAM 2  /* stream every target event to user space (ablation)*/
#define MODE_SHM    3  /* per-thread counters in an mmap-able map, read by the
			* application itself at request boundaries (no traps) */

#define NSLOTS 4096    /* per-thread slots in the shared map, indexed tid % NSLOTS */

#define NSOFTIRQ 10

/*
 * Per-thread scheduler / interrupt accounting. In MODE_SHM this struct lives
 * in a BPF_F_MMAPABLE array that the application maps read-write; the kernel
 * side updates it and the application snapshots it with plain loads.
 */
struct tstate {
	__u64 wake_ts;
	__u64 runnable_ts;
	__u64 sleep_ts;
	__u64 oncpu_ts;
	__u64 oncpu_ns;
	__u64 runq_ns;
	__u64 wake_ns;
	__u64 blocked_ns;
	__u64 softirq_ns;
	__u64 hardirq_ns;
	__u64 last_wake_delay;  /* set at switch-in after a wakeup */
	__u32 nvcsw;
	__u32 nivcsw;
	__u32 nmigr;
	__u32 nsoftirq;
	__u32 nhardirq;
	__u32 on_cpu;
	__u32 tid;              /* owner of the slot (MODE_SHM) */
	__u32 pad;
};

/* one record per completed request, produced at tl_req_end() */
struct req_rec {
	__u64 id;
	__u64 t_begin;
	__u64 t_end;
	__u64 pre_wake_ns;   /* wakeup->run delay just before the request began */
	__u64 oncpu_ns;      /* on-CPU time inside the window (incl. irq time)  */
	__u64 runq_ns;       /* runnable-but-preempted wait inside the window   */
	__u64 wake_ns;       /* wakeup->run delay after blocking in the window  */
	__u64 blocked_ns;    /* sleeping (I/O, locks) inside the window         */
	__u64 softirq_ns;    /* softirq time that interrupted this thread       */
	__u64 hardirq_ns;    /* hardirq time that interrupted this thread       */
	__u32 tid;
	__u32 nvcsw;
	__u32 nivcsw;
	__u32 nmigr;
	__u32 nsoftirq;
	__u32 nhardirq;
	__u16 cpu_begin;
	__u16 cpu_end;
	__u32 pad;
};

/* global per-CPU counters (all modes) - the "event count" baseline */
struct gcounters {
	__u64 nswitch;
	__u64 ninvol;            /* switches where prev was still runnable */
	__u64 nwakeup;
	__u64 nmigr;
	__u64 nhardirq;
	__u64 hardirq_ns;
	__u64 nsoftirq[NSOFTIRQ];
	__u64 softirq_ns[NSOFTIRQ];
	__u64 rb_drops;          /* ring buffer reservations that failed */
	__u64 slot_collisions;   /* MODE_SHM: two target threads mapped to one slot */
	__u64 rb_records;
};

/* raw event, used only in MODE_STREAM */
enum ev_type { EV_SWITCH_IN = 1, EV_SWITCH_OUT, EV_WAKEUP, EV_MIGRATE,
	       EV_SOFTIRQ, EV_HARDIRQ, EV_REQ_BEGIN, EV_REQ_END };

struct raw_ev {
	__u64 ts;
	__u64 arg;     /* duration for irq events, request id, prev_state ... */
	__u32 tid;
	__u16 cpu;
	__u16 type;
};

#endif
