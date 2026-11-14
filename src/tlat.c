// SPDX-License-Identifier: GPL-2.0
/*
 * tlat - user-space collector for tlat.bpf.c
 *
 *   tlat -p PID -b BINARY [-m count|attrib|stream] [-o PREFIX] [-i MS]
 *        [-R READY_FILE] [-C CPU]
 *
 * Outputs (PREFIX defaults to "tlat"):
 *   PREFIX.req.csv   one row per request (attrib mode)
 *   PREFIX.agg.csv   global counters sampled every -i ms (all modes)
 *   PREFIX.raw.bin   raw events (stream mode)
 *   PREFIX.json      summary: drops, records, collector CPU, BPF run time
 *
 * Runs until the target process exits.
 */
#define _GNU_SOURCE
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <errno.h>
#include <getopt.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

#include "tlat.h"
#include "tlat.skel.h"

static struct {
	int pid;
	const char *binary;
	int mode;
	const char *prefix;
	int interval_ms;
	const char *ready_file;
	int cpu;
	const char *pin;
} A = { .mode = MODE_ATTRIB, .prefix = "tlat", .interval_ms = 10, .cpu = -1,
	.pin = "/sys/fs/bpf/tlat_slots" };

static FILE *f_req, *f_agg, *f_raw;
static unsigned long long n_req, n_raw, raw_bytes;
static volatile sig_atomic_t stopping;

static void on_sig(int s) { (void)s; stopping = 1; }

static unsigned long long mono_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000000000ull + ts.tv_nsec;
}

static int handle_rb(void *ctx, void *data, size_t sz)
{
	(void)ctx;
	if (A.mode == MODE_STREAM) {
		fwrite(data, sz, 1, f_raw);
		n_raw++;
		raw_bytes += sz;
		return 0;
	}
	const struct req_rec *r = data;
	if (sz < sizeof(*r))
		return 0;
	fprintf(f_req,
		"%llu,%u,%u,%u,%llu,%llu,%llu,%llu,%llu,%llu,%llu,%llu,%llu,"
		"%u,%u,%u,%u,%u\n",
		(unsigned long long)r->id, r->tid, r->cpu_begin, r->cpu_end,
		(unsigned long long)r->t_begin, (unsigned long long)r->t_end,
		(unsigned long long)r->pre_wake_ns, (unsigned long long)r->oncpu_ns,
		(unsigned long long)r->runq_ns, (unsigned long long)r->wake_ns,
		(unsigned long long)r->blocked_ns, (unsigned long long)r->softirq_ns,
		(unsigned long long)r->hardirq_ns, r->nvcsw, r->nivcsw, r->nmigr,
		r->nsoftirq, r->nhardirq);
	n_req++;
	return 0;
}

/* /proc/pressure/{cpu,io} "some ... total=<us>" */
static long long psi_total(const char *path)
{
	FILE *f = fopen(path, "r");
	if (!f)
		return -1;
	char line[256];
	long long tot = -1;
	if (fgets(line, sizeof(line), f)) {
		char *p = strstr(line, "total=");
		if (p)
			tot = atoll(p + 6);
	}
	fclose(f);
	return tot;
}

static int ncpus;

static void read_counters(int fd, struct gcounters *sum)
{
	struct gcounters vals[ncpus];
	__u32 k = 0;
	memset(sum, 0, sizeof(*sum));
	if (bpf_map_lookup_elem(fd, &k, vals))
		return;
	for (int c = 0; c < ncpus; c++) {
		__u64 *dst = (__u64 *)sum, *src = (__u64 *)&vals[c];
		for (size_t i = 0; i < sizeof(*sum) / sizeof(__u64); i++)
			dst[i] += src[i];
	}
}

static void write_agg(unsigned long long t, const struct gcounters *c,
		      const struct gcounters *p, long long pc, long long pi)
{
	/* softirq vectors: 1 TIMER, 2 NET_TX, 3 NET_RX, 4 BLOCK, 7 SCHED, 8 HRTIMER, 9 RCU */
	fprintf(f_agg, "%llu,%llu,%llu,%llu,%llu,%llu,%llu", t,
		c->nswitch - p->nswitch, c->ninvol - p->ninvol,
		c->nwakeup - p->nwakeup, c->nmigr - p->nmigr,
		c->nhardirq - p->nhardirq, c->hardirq_ns - p->hardirq_ns);
	for (int v = 0; v < NSOFTIRQ; v++)
		fprintf(f_agg, ",%llu", c->nsoftirq[v] - p->nsoftirq[v]);
	for (int v = 0; v < NSOFTIRQ; v++)
		fprintf(f_agg, ",%llu", c->softirq_ns[v] - p->softirq_ns[v]);
	fprintf(f_agg, ",%lld,%lld\n", pc, pi);
}

static unsigned long long prog_runtime(struct bpf_object *obj, unsigned long long *cnt)
{
	struct bpf_program *p;
	unsigned long long t = 0;
	*cnt = 0;
	bpf_object__for_each_program(p, obj) {
		int fd = bpf_program__fd(p);
		if (fd < 0)
			continue;
		struct bpf_prog_info info = {};
		__u32 len = sizeof(info);
		if (!bpf_prog_get_info_by_fd(fd, &info, &len)) {
			t += info.run_time_ns;
			*cnt += info.run_cnt;
		}
	}
	return t;
}

static int libbpf_print(enum libbpf_print_level lvl, const char *fmt, va_list ap)
{
	if (lvl == LIBBPF_DEBUG)
		return 0;
	return vfprintf(stderr, fmt, ap);
}

int main(int argc, char **argv)
{
	int opt;
	while ((opt = getopt(argc, argv, "p:b:m:o:i:R:C:K:")) != -1) {
		switch (opt) {
		case 'p': A.pid = atoi(optarg); break;
		case 'b': A.binary = optarg; break;
		case 'm':
			A.mode = !strcmp(optarg, "count") ? MODE_COUNT :
				 !strcmp(optarg, "stream") ? MODE_STREAM :
				 !strcmp(optarg, "shm") ? MODE_SHM : MODE_ATTRIB;
			break;
		case 'o': A.prefix = optarg; break;
		case 'i': A.interval_ms = atoi(optarg); break;
		case 'R': A.ready_file = optarg; break;
		case 'C': A.cpu = atoi(optarg); break;
		case 'K': A.pin = optarg; break;
		default:
			fprintf(stderr, "usage: %s -p PID -b BINARY [-m count|attrib|stream] "
					"[-o PREFIX] [-i MS] [-R READY] [-C CPU] [-K PIN]\n", argv[0]);
			return 2;
		}
	}
	int need_uprobes = A.mode == MODE_ATTRIB || A.mode == MODE_STREAM;
	if (!A.pid || (need_uprobes && !A.binary)) {
		fprintf(stderr, "need -p PID and -b BINARY\n");
		return 2;
	}
	if (A.cpu >= 0) {
		cpu_set_t s;
		CPU_ZERO(&s);
		CPU_SET(A.cpu, &s);
		sched_setaffinity(0, sizeof(s), &s);
	}
	libbpf_set_print(libbpf_print);
	ncpus = libbpf_num_possible_cpus();

	struct tlat_bpf *skel = tlat_bpf__open();
	if (!skel) { fprintf(stderr, "open failed\n"); return 1; }
	skel->rodata->target_tgid = A.pid;
	skel->rodata->mode = A.mode;
	if (!need_uprobes) {
		bpf_program__set_autoload(skel->progs.req_begin, false);
		bpf_program__set_autoload(skel->progs.req_end, false);
		/* the ring buffer is unused; keep it tiny */
		bpf_map__set_max_entries(skel->maps.rb, 4096);
	}
	if (tlat_bpf__load(skel)) { fprintf(stderr, "load failed\n"); return 1; }
	if (tlat_bpf__attach(skel)) { fprintf(stderr, "attach failed\n"); return 1; }

	if (A.mode == MODE_SHM) {
		unlink(A.pin);
		if (bpf_map__pin(skel->maps.slots, A.pin)) {
			fprintf(stderr, "pin %s failed: %s\n", A.pin, strerror(errno));
			return 1;
		}
	}
	if (need_uprobes) {
		LIBBPF_OPTS(bpf_uprobe_opts, ob, .func_name = "tl_req_begin");
		LIBBPF_OPTS(bpf_uprobe_opts, oe, .func_name = "tl_req_end");
		skel->links.req_begin = bpf_program__attach_uprobe_opts(
			skel->progs.req_begin, A.pid, A.binary, 0, &ob);
		skel->links.req_end = bpf_program__attach_uprobe_opts(
			skel->progs.req_end, A.pid, A.binary, 0, &oe);
		if (!skel->links.req_begin || !skel->links.req_end) {
			fprintf(stderr, "uprobe attach failed: %s\n", strerror(errno));
			return 1;
		}
	}

	char path[512];
	snprintf(path, sizeof(path), "%s.agg.csv", A.prefix);
	f_agg = fopen(path, "w");
	setvbuf(f_agg, NULL, _IOFBF, 1 << 20);
	fprintf(f_agg, "t,nswitch,ninvol,nwakeup,nmigr,nhardirq,hardirq_ns");
	for (int v = 0; v < NSOFTIRQ; v++)
		fprintf(f_agg, ",nsoft%d", v);
	for (int v = 0; v < NSOFTIRQ; v++)
		fprintf(f_agg, ",softns%d", v);
	fprintf(f_agg, ",psi_cpu_us,psi_io_us\n");
	if (A.mode == MODE_ATTRIB) {
		snprintf(path, sizeof(path), "%s.req.csv", A.prefix);
		f_req = fopen(path, "w");
		setvbuf(f_req, NULL, _IOFBF, 1 << 22);
		fprintf(f_req, "id,ktid,cpu_begin,cpu_end,k_begin,k_end,pre_wake_ns,"
			       "oncpu_ns,runq_ns,wake_ns,blocked_ns,softirq_ns,hardirq_ns,"
			       "nvcsw,nivcsw,nmigr,nsoftirq,nhardirq\n");
	} else if (A.mode == MODE_STREAM) {
		snprintf(path, sizeof(path), "%s.raw.bin", A.prefix);
		f_raw = fopen(path, "wb");
		setvbuf(f_raw, NULL, _IOFBF, 1 << 22);
	}

	struct ring_buffer *rb = NULL;
	if (need_uprobes) {
		rb = ring_buffer__new(bpf_map__fd(skel->maps.rb), handle_rb, NULL, NULL);
		if (!rb) { fprintf(stderr, "ring buffer failed\n"); return 1; }
	}
	signal(SIGINT, on_sig);
	signal(SIGTERM, on_sig);

	if (A.ready_file) {
		FILE *rf = fopen(A.ready_file, "w");
		if (rf)
			fclose(rf);
	}
	fprintf(stderr, "tlat: tracing pid %d mode %d\n", A.pid, A.mode);

	struct rusage ru0, ru1;
	getrusage(RUSAGE_SELF, &ru0);
	int gfd = bpf_map__fd(skel->maps.gcnt);
	struct gcounters prev, cur;
	read_counters(gfd, &prev);
	unsigned long long next_tick = mono_ns() + A.interval_ms * 1000000ull;
	unsigned long long t_start = mono_ns();

	while (!stopping) {
		unsigned long long n = mono_ns();
		if (next_tick > n) {
			if (rb) {
				/* round up so we never spin on a 0 ms timeout */
				int to = (int)((next_tick - n + 999999) / 1000000);
				ring_buffer__poll(rb, to);
			} else {
				struct timespec ts = { .tv_sec = next_tick / 1000000000ull,
						       .tv_nsec = next_tick % 1000000000ull };
				clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL);
			}
		}
		n = mono_ns();
		if (n >= next_tick) {
			read_counters(gfd, &cur);
			write_agg(n, &cur, &prev, psi_total("/proc/pressure/cpu"),
				  psi_total("/proc/pressure/io"));
			prev = cur;
			next_tick += A.interval_ms * 1000000ull;
			if (next_tick < n)
				next_tick = n + A.interval_ms * 1000000ull;
			if (kill(A.pid, 0) && errno == ESRCH)
				break;
		}
	}
	if (rb)
		ring_buffer__consume(rb);
	unsigned long long t_end = mono_ns();
	getrusage(RUSAGE_SELF, &ru1);

	read_counters(gfd, &cur);
	unsigned long long run_cnt = 0;
	unsigned long long run_ns = prog_runtime(skel->obj, &run_cnt);
	double ucpu = (ru1.ru_utime.tv_sec - ru0.ru_utime.tv_sec) +
		      (ru1.ru_utime.tv_usec - ru0.ru_utime.tv_usec) / 1e6;
	double scpu = (ru1.ru_stime.tv_sec - ru0.ru_stime.tv_sec) +
		      (ru1.ru_stime.tv_usec - ru0.ru_stime.tv_usec) / 1e6;
	size_t map_bytes = 0;
	struct bpf_map *m;
	bpf_object__for_each_map(m, skel->obj) {
		size_t per = bpf_map__key_size(m) + bpf_map__value_size(m);
		if (bpf_map__type(m) == BPF_MAP_TYPE_RINGBUF)
			map_bytes += bpf_map__max_entries(m);
		else if (bpf_map__type(m) == BPF_MAP_TYPE_PERCPU_ARRAY)
			map_bytes += (size_t)bpf_map__value_size(m) * ncpus;
		else
			map_bytes += per * bpf_map__max_entries(m);
	}

	snprintf(path, sizeof(path), "%s.json", A.prefix);
	FILE *fj = fopen(path, "w");
	fprintf(fj,
		"{\"mode\":%d,\"wall_s\":%.3f,\"req_records\":%llu,\"raw_events\":%llu,"
		"\"raw_bytes\":%llu,\"rb_drops\":%llu,\"rb_submitted\":%llu,"
		"\"collector_user_s\":%.4f,\"collector_sys_s\":%.4f,\"collector_maxrss_kb\":%ld,"
		"\"bpf_run_ns\":%llu,\"bpf_run_cnt\":%llu,\"map_bytes_max\":%zu,"
		"\"slot_collisions\":%llu,\"preempt_sleeping\":%llu,\"npreempt\":%llu,\"no_wake_gap\":%llu,\"no_wake_gap_ns\":%llu,\"nswitch\":%llu}\n",
		A.mode, (t_end - t_start) / 1e9, n_req, n_raw, raw_bytes,
		(unsigned long long)cur.rb_drops, (unsigned long long)cur.rb_records,
		ucpu, scpu, ru1.ru_maxrss, run_ns, run_cnt, map_bytes,
		(unsigned long long)cur.slot_collisions,
		(unsigned long long)cur.preempt_sleeping, (unsigned long long)cur.npreempt,
		(unsigned long long)cur.no_wake_gap, (unsigned long long)cur.no_wake_gap_ns,
		(unsigned long long)cur.nswitch);
	fclose(fj);
	if (f_req) fclose(f_req);
	if (f_raw) fclose(f_raw);
	fclose(f_agg);
	ring_buffer__free(rb);
	if (A.mode == MODE_SHM)
		unlink(A.pin);
	tlat_bpf__destroy(skel);
	fprintf(stderr, "tlat: %llu requests, %llu raw events, %llu drops\n",
		n_req, n_raw, (unsigned long long)cur.rb_drops);
	return 0;
}
