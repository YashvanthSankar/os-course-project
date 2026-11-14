// SPDX-License-Identifier: GPL-2.0
/*
 * tlat.bpf.c - kernel side of the tail-latency attribution tracer.
 *
 * Per-thread state machine driven by scheduler tracepoints:
 *
 *        sched_wakeup                 sched_switch (next == t)
 *  SLEEP ------------> WOKEN (wake_ts) ----------------------> ON-CPU
 *    ^                                                          |  |
 *    | sched_switch (prev == t, prev_state != RUNNING)          |  |
 *    +----------------------------------------------------------+  |
 *                                                                   |
 *  RUNNABLE (runnable_ts) <-- sched_switch (prev == t, RUNNING) ----+
 *       |                                                           ^
 *       +-------------- sched_switch (next == t) -------------------+
 *
 * Time in WOKEN is wakeup delay, time in RUNNABLE is preemption (runqueue)
 * delay, time in SLEEP is blocked time. Hard/soft IRQ time is charged to the
 * thread that was interrupted. A switch-out counts as RUNNING (involuntary)
 * when the kernel says it was a preemption or prev_state is TASK_RUNNING,
 * the same rule __schedule() uses for nivcsw: a thread preempted after it
 * set TASK_INTERRUPTIBLE stays on the runqueue.
 *
 * Request windows, two ways:
 *  MODE_SHM    the counters live in the mmap-able 'slots' array; the
 *              application snapshots its own slot at request begin/end with
 *              plain loads (no trap, no record per request).
 *  MODE_ATTRIB uprobes on tl_req_begin()/tl_req_end(); the end probe emits
 *              one record with the counter deltas through the ring buffer.
 * MODE_COUNT keeps only the global per-CPU counters, MODE_STREAM sends
 * every raw event to user space (overhead comparison only).
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>
#include "tlat.h"

char LICENSE[] SEC("license") = "GPL";

const volatile __u32 target_tgid = 0;
const volatile __u32 mode = MODE_ATTRIB;

struct begin_snap {
	__u64 id;
	__u64 ts;
	__u64 oncpu_now;
	__u64 pre_wake;
	struct tstate s;
	__u32 cpu;
};

struct irqstate {
	__u64 softirq_ts;
	__u64 hardirq_ts;
	__u32 vec;
};

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 4096);
	__type(key, __u32);
	__type(value, struct tstate);
} tstates SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 4096);
	__type(key, __u32);
	__type(value, struct begin_snap);
} begins SEC(".maps");

/* MODE_SHM: shared with the application through mmap() */
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(map_flags, BPF_F_MMAPABLE);
	__uint(max_entries, NSLOTS);
	__type(key, __u32);
	__type(value, struct tstate);
} slots SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct gcounters);
} gcnt SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct irqstate);
} irqst SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 16 << 20);
} rb SEC(".maps");

static __always_inline struct gcounters *gc(void)
{
	__u32 k = 0;
	return bpf_map_lookup_elem(&gcnt, &k);
}

static __always_inline int is_target(struct task_struct *p)
{
	return BPF_CORE_READ(p, tgid) == target_tgid;
}

static __always_inline struct tstate *slot_ts(__u32 tid, int claim)
{
	__u32 i = tid & (NSLOTS - 1);
	struct tstate *s = bpf_map_lookup_elem(&slots, &i);
	if (!s)
		return NULL;
	if (s->tid == tid)
		return s;
	if (s->tid == 0 && claim) {
		/* two CPUs may try to claim the same free slot at once */
		if (__sync_val_compare_and_swap(&s->tid, 0, tid) == 0 || s->tid == tid)
			return s;
	}
	if (claim) {
		struct gcounters *g = gc();
		if (g)
			g->slot_collisions++;
	}
	return NULL;
}

/* existing per-thread state, or NULL */
static __always_inline struct tstate *find_ts(__u32 tid)
{
	if (mode == MODE_SHM)
		return slot_ts(tid, 0);
	return bpf_map_lookup_elem(&tstates, &tid);
}

/* per-thread state, created on first use */
static __always_inline struct tstate *get_ts(__u32 tid)
{
	if (mode == MODE_SHM)
		return slot_ts(tid, 1);
	struct tstate *s = bpf_map_lookup_elem(&tstates, &tid);
	if (s)
		return s;
	struct tstate z = {};
	bpf_map_update_elem(&tstates, &tid, &z, BPF_NOEXIST);
	return bpf_map_lookup_elem(&tstates, &tid);
}

static __always_inline void stream(__u16 type, __u32 tid, __u64 arg, __u64 ts)
{
	struct gcounters *g = gc();
	struct raw_ev *e = bpf_ringbuf_reserve(&rb, sizeof(*e), 0);
	if (!e) {
		if (g)
			g->rb_drops++;
		return;
	}
	e->ts = ts;
	e->arg = arg;
	e->tid = tid;
	e->cpu = bpf_get_smp_processor_id();
	e->type = type;
	bpf_ringbuf_submit(e, 0);
	if (g)
		g->rb_records++;
}

/* ---------------- scheduler ------------------------------------------ */

SEC("tp_btf/sched_switch")
int BPF_PROG(on_switch, bool preempt, struct task_struct *prev,
	     struct task_struct *next, unsigned int prev_state)
{
	__u64 now = bpf_ktime_get_ns();
	struct gcounters *g = gc();
	/* same test as __schedule(): preempted, or still TASK_RUNNING */
	int involuntary = preempt || prev_state == 0;

	if (g) {
		g->nswitch++;
		g->ninvol += involuntary;
	}
	if (mode == MODE_COUNT)
		return 0;

	if (is_target(prev)) {
		__u32 tid = BPF_CORE_READ(prev, pid);
		if (mode == MODE_STREAM) {
			stream(EV_SWITCH_OUT, tid, prev_state, now);
		} else {
			struct tstate *s = get_ts(tid);
			if (g && preempt) {
				g->npreempt++;
				if (prev_state)
					g->preempt_sleeping++;
			}
			if (s) {
				if (s->oncpu_ts)
					s->oncpu_ns += now - s->oncpu_ts;
				s->oncpu_ts = 0;
				s->on_cpu = 0;
				if (involuntary) {
					s->nivcsw++;
					s->runnable_ts = now;
				} else {
					s->nvcsw++;
					s->sleep_ts = now;
				}
			}
		}
	}
	if (is_target(next)) {
		__u32 tid = BPF_CORE_READ(next, pid);
		if (mode == MODE_STREAM) {
			stream(EV_SWITCH_IN, tid, 0, now);
		} else {
			struct tstate *s = get_ts(tid);
			if (s) {
				if (s->runnable_ts) {
					s->runq_ns += now - s->runnable_ts;
					s->runnable_ts = 0;
				}
				if (s->wake_ts) {
					__u64 d = now - s->wake_ts;
					s->wake_ns += d;
					s->last_wake_delay = d;
					s->wake_ts = 0;
				}
				if (s->sleep_ts) {
					/* back on CPU with no wakeup seen: it never
					 * left the runqueue (e.g. a pending signal),
					 * so the gap was a runqueue wait */
					s->runq_ns += now - s->sleep_ts;
					if (g) {
						g->no_wake_gap++;
						g->no_wake_gap_ns += now - s->sleep_ts;
					}
					s->sleep_ts = 0;
				}
				s->oncpu_ts = now;
				s->on_cpu = 1;
			}
		}
	}
	return 0;
}

static __always_inline void handle_wakeup(struct task_struct *p)
{
	__u64 now = bpf_ktime_get_ns();
	struct gcounters *g = gc();
	if (g)
		g->nwakeup++;
	if (mode == MODE_COUNT || !is_target(p))
		return;
	__u32 tid = BPF_CORE_READ(p, pid);
	if (mode == MODE_STREAM) {
		stream(EV_WAKEUP, tid, 0, now);
		return;
	}
	struct tstate *s = get_ts(tid);
	if (!s || s->on_cpu || s->runnable_ts)
		return; /* already running or queued: not a real wakeup */
	if (s->sleep_ts) {
		s->blocked_ns += now - s->sleep_ts;
		s->sleep_ts = 0;
	}
	s->wake_ts = now;
}

SEC("tp_btf/sched_wakeup")
int BPF_PROG(on_wakeup, struct task_struct *p)
{
	handle_wakeup(p);
	return 0;
}

SEC("tp_btf/sched_wakeup_new")
int BPF_PROG(on_wakeup_new, struct task_struct *p)
{
	handle_wakeup(p);
	return 0;
}

SEC("tp_btf/sched_migrate_task")
int BPF_PROG(on_migrate, struct task_struct *p, int dest_cpu)
{
	struct gcounters *g = gc();
	if (g)
		g->nmigr++;
	if (mode == MODE_COUNT || !is_target(p))
		return 0;
	__u32 tid = BPF_CORE_READ(p, pid);
	if (mode == MODE_STREAM) {
		stream(EV_MIGRATE, tid, dest_cpu, bpf_ktime_get_ns());
		return 0;
	}
	struct tstate *s = get_ts(tid);
	if (s)
		s->nmigr++;
	return 0;
}

/* ---------------- interrupts ----------------------------------------- */

static __always_inline struct irqstate *irq_state(void)
{
	__u32 k = 0;
	return bpf_map_lookup_elem(&irqst, &k);
}

SEC("tp_btf/softirq_entry")
int BPF_PROG(on_softirq_entry, unsigned int vec_nr)
{
	struct irqstate *st = irq_state();
	if (st) {
		st->softirq_ts = bpf_ktime_get_ns();
		st->vec = vec_nr;
	}
	return 0;
}

SEC("tp_btf/softirq_exit")
int BPF_PROG(on_softirq_exit, unsigned int vec_nr)
{
	struct irqstate *st = irq_state();
	struct gcounters *g = gc();
	if (!st || !st->softirq_ts)
		return 0;
	__u64 now = bpf_ktime_get_ns();
	__u64 d = now - st->softirq_ts;
	st->softirq_ts = 0;
	if (g && vec_nr < NSOFTIRQ) {
		g->nsoftirq[vec_nr]++;
		g->softirq_ns[vec_nr] += d;
	}
	if (mode == MODE_COUNT)
		return 0;
	__u64 pt = bpf_get_current_pid_tgid();
	if ((__u32)(pt >> 32) != target_tgid)
		return 0;
	__u32 tid = (__u32)pt;
	if (mode == MODE_STREAM) {
		stream(EV_SOFTIRQ, tid, d, now);
		return 0;
	}
	struct tstate *s = find_ts(tid);
	if (s) {
		s->softirq_ns += d;
		s->nsoftirq++;
	}
	return 0;
}

SEC("tp_btf/irq_handler_entry")
int BPF_PROG(on_irq_entry, int irq, struct irqaction *action)
{
	struct irqstate *st = irq_state();
	if (st)
		st->hardirq_ts = bpf_ktime_get_ns();
	return 0;
}

SEC("tp_btf/irq_handler_exit")
int BPF_PROG(on_irq_exit, int irq, struct irqaction *action, int ret)
{
	struct irqstate *st = irq_state();
	struct gcounters *g = gc();
	if (!st || !st->hardirq_ts)
		return 0;
	__u64 now = bpf_ktime_get_ns();
	__u64 d = now - st->hardirq_ts;
	st->hardirq_ts = 0;
	if (g) {
		g->nhardirq++;
		g->hardirq_ns += d;
	}
	if (mode == MODE_COUNT)
		return 0;
	__u64 pt = bpf_get_current_pid_tgid();
	if ((__u32)(pt >> 32) != target_tgid)
		return 0;
	__u32 tid = (__u32)pt;
	if (mode == MODE_STREAM) {
		stream(EV_HARDIRQ, tid, d, now);
		return 0;
	}
	struct tstate *s = find_ts(tid);
	if (s) {
		s->hardirq_ns += d;
		s->nhardirq++;
	}
	return 0;
}

/* ---------------- request windows (uprobes) -------------------------- */

SEC("uprobe")
int BPF_UPROBE(req_begin, __u64 id)
{
	__u64 now = bpf_ktime_get_ns();
	__u64 pt = bpf_get_current_pid_tgid();
	if ((__u32)(pt >> 32) != target_tgid)
		return 0;
	__u32 tid = (__u32)pt;
	if (mode == MODE_STREAM) {
		stream(EV_REQ_BEGIN, tid, id, now);
		return 0;
	}
	struct tstate *s = get_ts(tid);
	if (!s)
		return 0;
	struct begin_snap b = {};
	b.id = id;
	b.ts = now;
	b.cpu = bpf_get_smp_processor_id();
	b.pre_wake = s->last_wake_delay;
	b.oncpu_now = s->oncpu_ns + (s->oncpu_ts ? now - s->oncpu_ts : 0);
	__builtin_memcpy(&b.s, s, sizeof(b.s));
	s->last_wake_delay = 0;
	bpf_map_update_elem(&begins, &tid, &b, BPF_ANY);
	return 0;
}

SEC("uprobe")
int BPF_UPROBE(req_end, __u64 id)
{
	__u64 now = bpf_ktime_get_ns();
	__u64 pt = bpf_get_current_pid_tgid();
	if ((__u32)(pt >> 32) != target_tgid)
		return 0;
	__u32 tid = (__u32)pt;
	if (mode == MODE_STREAM) {
		stream(EV_REQ_END, tid, id, now);
		return 0;
	}
	struct tstate *s = find_ts(tid);
	struct begin_snap *b = bpf_map_lookup_elem(&begins, &tid);
	if (!s || !b || b->id != id)
		return 0;

	struct gcounters *g = gc();
	struct req_rec *r = bpf_ringbuf_reserve(&rb, sizeof(*r), 0);
	if (!r) {
		if (g)
			g->rb_drops++;
		return 0;
	}
	__u64 oncpu_now = s->oncpu_ns + (s->oncpu_ts ? now - s->oncpu_ts : 0);
	r->id = id;
	r->tid = tid;
	r->t_begin = b->ts;
	r->t_end = now;
	r->cpu_begin = b->cpu;
	r->cpu_end = bpf_get_smp_processor_id();
	r->pre_wake_ns = b->pre_wake;
	r->oncpu_ns = oncpu_now - b->oncpu_now;
	r->runq_ns = s->runq_ns - b->s.runq_ns;
	r->wake_ns = s->wake_ns - b->s.wake_ns;
	r->blocked_ns = s->blocked_ns - b->s.blocked_ns;
	r->softirq_ns = s->softirq_ns - b->s.softirq_ns;
	r->hardirq_ns = s->hardirq_ns - b->s.hardirq_ns;
	r->nvcsw = s->nvcsw - b->s.nvcsw;
	r->nivcsw = s->nivcsw - b->s.nivcsw;
	r->nmigr = s->nmigr - b->s.nmigr;
	r->nsoftirq = s->nsoftirq - b->s.nsoftirq;
	r->nhardirq = s->nhardirq - b->s.nhardirq;
	r->pad = 0;
	bpf_ringbuf_submit(r, 0);
	s->last_wake_delay = 0;
	if (g)
		g->rb_records++;
	return 0;
}
