// SPDX-License-Identifier: GPL-2.0
/*
 * tlbench - open-loop request benchmark with stable per-request IDs.
 *
 * Three workload classes share one request path:
 *   cpu : workers are woken through a condition variable and run a fixed
 *         compute kernel (CPU-bound).
 *   kv  : hash-table lookups over a large table, a 4 KiB pread from a data
 *         file and a periodic pwrite+fdatasync (memory / I/O bound).
 *   net : requests travel over loopback TCP to epoll-driven server threads
 *         (event-driven, exercises the network softirq path).
 *
 * Every request carries an id. Worker threads call tl_req_begin(id) and
 * tl_req_end(id) around the service code; the tracer attaches uprobes to
 * these two symbols, so kernel-side telemetry can be joined to the request
 * on the exact thread and time window.
 *
 * Latency is measured from the *scheduled* arrival time, which avoids
 * coordinated omission in open-loop mode. rate=0 switches to closed loop,
 * used only to measure peak throughput for the overhead experiment.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <linux/bpf.h>
#include <linux/types.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <math.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#include "tlat.h"

#define MAX_WORKERS 64
#define QCAP (1u << 16)

enum mode { M_CPU, M_KV, M_NET };

struct cfg {
	enum mode mode;
	int workers;
	double rate;           /* req/s, 0 = closed loop */
	double duration;       /* measured seconds */
	double warmup;         /* seconds discarded at the start */
	uint64_t seed;
	double work_us;        /* compute per request */
	const char *out;
	const char *meta;
	const char *ready_file;
	const char *datafile;
	int sync_every;        /* kv: fdatasync every N requests per worker */
	int lookups;           /* kv: hash lookups per request */
	char worker_cpus[64];
	int gen_cpu;
	int port;
	int conns_per_worker;
	const char *slot_pin;  /* MODE_SHM pinned map; NULL = not used */
} C = {
	.mode = M_CPU, .workers = 3, .rate = 10000, .duration = 20, .warmup = 2,
	.seed = 1, .work_us = 100, .out = "bench.csv", .meta = NULL,
	.ready_file = NULL, .datafile = "/var/tmp/tlbench.dat",
	.sync_every = 200, .lookups = 8, .worker_cpus = "", .gen_cpu = -1,
	.port = 47011, .conns_per_worker = 4,
};

struct rec {
	uint64_t t_arr, t_enq, t_start, t_end, t_done;
	uint32_t tid;
	uint16_t cpu;
	uint16_t valid;
};

/* kernel-maintained per-thread counters, read from the shared map */
struct ksnap {
	uint64_t oncpu, runq, wake, blocked, soft, hard;
	uint32_t nvcsw, nivcsw, nmigr, nsoft, nhard;
};

struct kfeat {
	struct ksnap d;      /* end - begin */
	uint64_t pre_wake;
	uint32_t ok;
};

static struct rec *recs;
static struct kfeat *kf;               /* parallel to recs when slots used */
static volatile struct tstate *slots;
static uint64_t max_reqs;
static atomic_uint_fast64_t next_id;  /* closed loop id source */
static atomic_int stop_flag;
static uint64_t t_origin;              /* CLOCK_MONOTONIC ns at load start */
static uint64_t iters_per_us_x1000;    /* compute calibration */
static cpu_set_t worker_set;
static int have_worker_set;

/* ------------------------------------------------------------------ */
/* uprobe anchors: must stay out-of-line and must not be folded away.  */
__attribute__((noinline, used)) void tl_req_begin(uint64_t id)
{
	__asm__ volatile("" : : "r"(id) : "memory");
}

__attribute__((noinline, used)) void tl_req_end(uint64_t id)
{
	__asm__ volatile("" : : "r"(id) : "memory");
}
/* ------------------------------------------------------------------ */

static inline uint64_t now_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
}

static inline pid_t gettid_(void) { return (pid_t)syscall(SYS_gettid); }

static int bpf_obj_get_path(const char *path)
{
	union bpf_attr attr;
	memset(&attr, 0, sizeof(attr));
	attr.pathname = (__u64)(uintptr_t)path;
	return (int)syscall(SYS_bpf, BPF_OBJ_GET, &attr, sizeof(attr));
}

static void map_slots(const char *pin)
{
	int fd = bpf_obj_get_path(pin);
	if (fd < 0) {
		perror("bpf_obj_get slots");
		exit(1);
	}
	size_t len = NSLOTS * sizeof(struct tstate);
	long pg = sysconf(_SC_PAGESIZE);
	len = (len + pg - 1) / pg * pg;
	void *p = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (p == MAP_FAILED) {
		perror("mmap slots");
		exit(1);
	}
	slots = p;
}

static inline volatile struct tstate *my_slot(pid_t tid)
{
	return slots ? &slots[tid & (NSLOTS - 1)] : NULL;
}

static inline void ksnap_take(struct ksnap *k, volatile struct tstate *s)
{
	/* the thread is running, so its current on-CPU segment started at
	 * oncpu_ts (same clock as bpf_ktime_get_ns) */
	uint64_t ts = s->oncpu_ts, acc = s->oncpu_ns;
	k->oncpu = acc + (ts ? now_ns() - ts : 0);
	k->runq = s->runq_ns;
	k->wake = s->wake_ns;
	k->blocked = s->blocked_ns;
	k->soft = s->softirq_ns;
	k->hard = s->hardirq_ns;
	k->nvcsw = s->nvcsw;
	k->nivcsw = s->nivcsw;
	k->nmigr = s->nmigr;
	k->nsoft = s->nsoftirq;
	k->nhard = s->nhardirq;
}

/* begin/end of a request window as seen through the shared counters */
struct kwin {
	volatile struct tstate *s;
	struct ksnap a;
	uint64_t pre;
	int ok;
};

static inline void kwin_begin(struct kwin *w, pid_t tid)
{
	w->s = my_slot(tid);
	w->ok = w->s && w->s->tid == (uint32_t)tid;
	if (w->ok) {
		ksnap_take(&w->a, w->s);
		w->pre = w->s->last_wake_delay;
	} else {
		memset(&w->a, 0, sizeof(w->a));
		w->pre = 0;
	}
}

static inline void kwin_end(struct kwin *w, uint64_t id)
{
	if (!w->ok || !kf || id >= max_reqs)
		return;
	struct ksnap b;
	ksnap_take(&b, w->s);
	w->s->last_wake_delay = 0;
	struct kfeat *f = &kf[id];
	f->d.oncpu = b.oncpu - w->a.oncpu;
	f->d.runq = b.runq - w->a.runq;
	f->d.wake = b.wake - w->a.wake;
	f->d.blocked = b.blocked - w->a.blocked;
	f->d.soft = b.soft - w->a.soft;
	f->d.hard = b.hard - w->a.hard;
	f->d.nvcsw = b.nvcsw - w->a.nvcsw;
	f->d.nivcsw = b.nivcsw - w->a.nivcsw;
	f->d.nmigr = b.nmigr - w->a.nmigr;
	f->d.nsoft = b.nsoft - w->a.nsoft;
	f->d.nhard = b.nhard - w->a.nhard;
	f->pre_wake = w->pre;
	f->ok = 1;
}

/* splitmix64: small, seedable, good enough for arrivals and keys */
static inline uint64_t sm64(uint64_t *s)
{
	uint64_t z = (*s += 0x9e3779b97f4a7c15ull);
	z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ull;
	z = (z ^ (z >> 27)) * 0x94d049bb133111ebull;
	return z ^ (z >> 31);
}

static inline double urand(uint64_t *s)
{
	return (sm64(s) >> 11) * (1.0 / 9007199254740992.0);
}

/* ---------------- compute kernel ---------------------------------- */
static volatile uint64_t sink;

static uint64_t spin_iters(uint64_t n, uint64_t x)
{
	for (uint64_t i = 0; i < n; i++) {
		x ^= x << 13;
		x ^= x >> 7;
		x ^= x << 17;
		x += i;
	}
	return x;
}

static void calibrate(void)
{
	const uint64_t n = 2000000;
	uint64_t best = UINT64_MAX;
	for (int k = 0; k < 7; k++) {
		uint64_t a = now_ns();
		sink = spin_iters(n, a | 1);
		uint64_t d = now_ns() - a;
		if (d < best)
			best = d;
	}
	/* iterations per microsecond, scaled by 1000 for precision */
	iters_per_us_x1000 = n * 1000000ull / best;
}

static void do_compute(double us, uint64_t salt)
{
	uint64_t n = (uint64_t)(us * iters_per_us_x1000 / 1000.0);
	sink = spin_iters(n, salt | 1);
}

/* ---------------- kv state ---------------------------------------- */
#define KV_BITS 22
#define KV_SIZE (1u << KV_BITS)
struct kv_slot { uint64_t key, val; };
static struct kv_slot *kv_tab;
static int kv_fd = -1;
static off_t kv_file_size = 256ll << 20;

static void kv_init(void)
{
	kv_tab = calloc(KV_SIZE, sizeof(*kv_tab));
	if (!kv_tab) { perror("calloc kv"); exit(1); }
	uint64_t s = C.seed ^ 0xabcdef;
	for (uint32_t i = 0; i < KV_SIZE / 2; i++) {
		uint64_t k = sm64(&s) | 1;
		uint32_t h = (uint32_t)(k * 0x9e3779b97f4a7c15ull >> (64 - KV_BITS));
		while (kv_tab[h].key)
			h = (h + 1) & (KV_SIZE - 1);
		kv_tab[h].key = k;
		kv_tab[h].val = i;
	}
	kv_fd = open(C.datafile, O_RDWR | O_CREAT, 0644);
	if (kv_fd < 0) { perror("open datafile"); exit(1); }
	struct stat st;
	fstat(kv_fd, &st);
	if (st.st_size < kv_file_size) {
		char *buf = malloc(1 << 20);
		memset(buf, 0x5a, 1 << 20);
		for (off_t o = 0; o < kv_file_size; o += 1 << 20)
			if (pwrite(kv_fd, buf, 1 << 20, o) < 0) { perror("pwrite"); exit(1); }
		free(buf);
		fsync(kv_fd);
	}
}

static void do_kv(uint64_t *s, uint64_t *wcount)
{
	uint64_t acc = 0;
	char buf[4096];
	for (int i = 0; i < C.lookups; i++) {
		uint64_t k = sm64(s) | 1;
		uint32_t h = (uint32_t)(k * 0x9e3779b97f4a7c15ull >> (64 - KV_BITS));
		for (int p = 0; p < 16 && kv_tab[h].key; p++) {
			if (kv_tab[h].key == k) { acc += kv_tab[h].val; break; }
			h = (h + 1) & (KV_SIZE - 1);
		}
	}
	off_t off = (off_t)(sm64(s) % (uint64_t)(kv_file_size / 4096)) * 4096;
	if (pread(kv_fd, buf, sizeof(buf), off) < 0)
		perror("pread");
	acc += (unsigned char)buf[acc & 4095];
	do_compute(C.work_us, acc);
	if (C.sync_every > 0 && ++*wcount % C.sync_every == 0) {
		memset(buf, (int)(acc & 0xff), sizeof(buf));
		if (pwrite(kv_fd, buf, sizeof(buf), off) < 0)
			perror("pwrite");
		fdatasync(kv_fd);
	}
}

/* ---------------- request queue (cpu / kv) ------------------------ */
struct qitem { uint64_t id, t_arr, t_enq; };
static struct qitem q[QCAP];
static uint32_t qhead, qtail;
static pthread_mutex_t qlock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t qcond = PTHREAD_COND_INITIALIZER;
static uint64_t q_overflow;

static void pin_self_to_set(void)
{
	if (have_worker_set)
		pthread_setaffinity_np(pthread_self(), sizeof(worker_set), &worker_set);
}

static void pin_self_to_cpu(int cpu)
{
	if (cpu < 0)
		return;
	cpu_set_t s;
	CPU_ZERO(&s);
	CPU_SET(cpu, &s);
	pthread_setaffinity_np(pthread_self(), sizeof(s), &s);
}

static void serve_one(uint64_t id, uint64_t t_arr, uint64_t t_enq, uint64_t *rng,
		      uint64_t *wcount, pid_t tid)
{
	struct kwin kw;
	uint64_t t0 = now_ns();
	tl_req_begin(id);
	kwin_begin(&kw, tid);
	if (C.mode == M_KV)
		do_kv(rng, wcount);
	else
		do_compute(C.work_us, id);
	kwin_end(&kw, id);
	tl_req_end(id);
	uint64_t t1 = now_ns();
	if (id < max_reqs) {
		struct rec *r = &recs[id];
		r->t_arr = t_arr;
		r->t_enq = t_enq;
		r->t_start = t0;
		r->t_end = t1;
		r->t_done = t1;
		r->tid = (uint32_t)tid;
		r->cpu = (uint16_t)sched_getcpu();
		r->valid = 1;
	}
}

static void *queue_worker(void *arg)
{
	uint64_t rng = C.seed * 7919 + (uintptr_t)arg;
	uint64_t wcount = 0;
	pid_t tid = gettid_();
	pin_self_to_set();
	for (;;) {
		pthread_mutex_lock(&qlock);
		while (qhead == qtail && !atomic_load(&stop_flag))
			pthread_cond_wait(&qcond, &qlock);
		if (qhead == qtail) {
			pthread_mutex_unlock(&qlock);
			break;
		}
		struct qitem it = q[qhead % QCAP];
		qhead++;
		pthread_mutex_unlock(&qlock);
		serve_one(it.id, it.t_arr, it.t_enq, &rng, &wcount, tid);
	}
	return NULL;
}

static void *closed_worker(void *arg)
{
	uint64_t rng = C.seed * 7919 + (uintptr_t)arg;
	uint64_t wcount = 0;
	pid_t tid = gettid_();
	pin_self_to_set();
	while (!atomic_load(&stop_flag)) {
		uint64_t id = atomic_fetch_add(&next_id, 1);
		if (id >= max_reqs)
			break;
		uint64_t t = now_ns();
		serve_one(id, t, t, &rng, &wcount, tid);
	}
	return NULL;
}

/*
 * Busy-wait until absolute time t. The generator never sleeps: in a VM an
 * idle vCPU is parked by the host and wakes late, which turns into bursty,
 * late arrivals (measured in the pilot as multi-millisecond lateness).
 */
static void wait_until(uint64_t t)
{
	while (now_ns() < t)
		__asm__ volatile("" ::: "memory");
}

static void run_generator_queue(void)
{
	uint64_t s = C.seed;
	double t = 0;
	uint64_t end = (uint64_t)((C.warmup + C.duration) * 1e9);
	pin_self_to_cpu(C.gen_cpu);
	for (uint64_t id = 0; id < max_reqs; id++) {
		t += -log(1.0 - urand(&s)) / C.rate * 1e9;
		if ((uint64_t)t >= end)
			break;
		uint64_t ta = t_origin + (uint64_t)t;
		wait_until(ta);
		pthread_mutex_lock(&qlock);
		if (qtail - qhead < QCAP) {
			q[qtail % QCAP] = (struct qitem){ id, ta, now_ns() };
			qtail++;
		} else {
			q_overflow++;
		}
		pthread_cond_signal(&qcond);
		pthread_mutex_unlock(&qlock);
	}
}

/* ---------------- net mode ---------------------------------------- */
struct msg { uint64_t id, t_arr; };

static int listen_fd;
static int *srv_epfd;
static int *cli_fds;
static int ncli;

static void set_nodelay(int fd)
{
	int one = 1;
	setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
}

static int read_full(int fd, void *p, size_t n)
{
	size_t got = 0;
	while (got < n) {
		ssize_t r = read(fd, (char *)p + got, n - got);
		if (r == 0)
			return 0;
		if (r < 0) {
			if (errno == EINTR)
				continue;
			if (errno == EAGAIN && got > 0)
				continue;
			return -1;
		}
		got += r;
	}
	return 1;
}

static void *net_server_worker(void *arg)
{
	int w = (int)(uintptr_t)arg;
	int ep = srv_epfd[w];
	pid_t tid = gettid_();
	struct epoll_event evs[64];
	pin_self_to_set();
	while (!atomic_load(&stop_flag)) {
		int n = epoll_wait(ep, evs, 64, 100);
		for (int i = 0; i < n; i++) {
			int fd = evs[i].data.fd;
			struct msg m;
			for (;;) {
				ssize_t r = recv(fd, &m, sizeof(m), MSG_DONTWAIT | MSG_PEEK);
				if (r < (ssize_t)sizeof(m))
					break;
				if (read_full(fd, &m, sizeof(m)) <= 0)
					break;
				struct kwin kw;
				uint64_t t0 = now_ns();
				tl_req_begin(m.id);
				kwin_begin(&kw, tid);
				do_compute(C.work_us, m.id);
				kwin_end(&kw, m.id);
				tl_req_end(m.id);
				uint64_t t1 = now_ns();
				if (m.id < max_reqs) {
					struct rec *r = &recs[m.id];
					r->t_start = t0;
					r->t_end = t1;
					r->tid = (uint32_t)tid;
					r->cpu = (uint16_t)sched_getcpu();
				}
				if (write(fd, &m, sizeof(m)) != sizeof(m))
					break;
			}
		}
	}
	return NULL;
}

static void *net_client_receiver(void *arg)
{
	(void)arg;
	int ep = epoll_create1(0);
	for (int i = 0; i < ncli; i++) {
		struct epoll_event ev = { .events = EPOLLIN, .data.fd = cli_fds[i] };
		epoll_ctl(ep, EPOLL_CTL_ADD, cli_fds[i], &ev);
	}
	pin_self_to_cpu(C.gen_cpu);
	struct epoll_event evs[64];
	while (!atomic_load(&stop_flag)) {
		int n = epoll_wait(ep, evs, 64, 100);
		for (int i = 0; i < n; i++) {
			int fd = evs[i].data.fd;
			struct msg m;
			for (;;) {
				ssize_t r = recv(fd, &m, sizeof(m), MSG_DONTWAIT | MSG_PEEK);
				if (r < (ssize_t)sizeof(m))
					break;
				if (read_full(fd, &m, sizeof(m)) <= 0)
					break;
				uint64_t t = now_ns();
				if (m.id < max_reqs) {
					recs[m.id].t_arr = m.t_arr;
					recs[m.id].t_done = t;
					recs[m.id].valid = 1;
				}
			}
		}
	}
	close(ep);
	return NULL;
}

static void net_setup(pthread_t *srv)
{
	listen_fd = socket(AF_INET, SOCK_STREAM, 0);
	int one = 1;
	setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
	struct sockaddr_in a = { .sin_family = AF_INET, .sin_port = htons(C.port),
				 .sin_addr.s_addr = htonl(INADDR_LOOPBACK) };
	if (bind(listen_fd, (void *)&a, sizeof(a)) || listen(listen_fd, 1024)) {
		perror("bind/listen");
		exit(1);
	}
	ncli = C.workers * C.conns_per_worker;
	cli_fds = calloc(ncli, sizeof(int));
	srv_epfd = calloc(C.workers, sizeof(int));
	for (int w = 0; w < C.workers; w++)
		srv_epfd[w] = epoll_create1(0);
	for (int i = 0; i < ncli; i++) {
		int c = socket(AF_INET, SOCK_STREAM, 0);
		if (connect(c, (void *)&a, sizeof(a))) { perror("connect"); exit(1); }
		set_nodelay(c);
		int s = accept(listen_fd, NULL, NULL);
		set_nodelay(s);
		cli_fds[i] = c;
		struct epoll_event ev = { .events = EPOLLIN, .data.fd = s };
		epoll_ctl(srv_epfd[i % C.workers], EPOLL_CTL_ADD, s, &ev);
	}
	for (int w = 0; w < C.workers; w++)
		pthread_create(&srv[w], NULL, net_server_worker, (void *)(uintptr_t)w);
}

static void run_generator_net(void)
{
	uint64_t s = C.seed;
	double t = 0;
	uint64_t end = (uint64_t)((C.warmup + C.duration) * 1e9);
	pin_self_to_cpu(C.gen_cpu);
	for (uint64_t id = 0; id < max_reqs; id++) {
		t += -log(1.0 - urand(&s)) / C.rate * 1e9;
		if ((uint64_t)t >= end)
			break;
		uint64_t ta = t_origin + (uint64_t)t;
		wait_until(ta);
		struct msg m = { id, ta };
		int fd = cli_fds[id % ncli];
		if (id < max_reqs)
			recs[id].t_enq = now_ns();
		if (write(fd, &m, sizeof(m)) != sizeof(m))
			perror("client write");
	}
}

/* ---------------- main -------------------------------------------- */
static int parse_cpus(const char *s, cpu_set_t *set)
{
	CPU_ZERO(set);
	char buf[64];
	snprintf(buf, sizeof(buf), "%s", s);
	for (char *tok = strtok(buf, ","); tok; tok = strtok(NULL, ",")) {
		int a, b;
		if (sscanf(tok, "%d-%d", &a, &b) == 2) {
			for (int i = a; i <= b; i++)
				CPU_SET(i, set);
		} else if (sscanf(tok, "%d", &a) == 1) {
			CPU_SET(a, set);
		} else {
			return -1;
		}
	}
	return 0;
}

static void usage(const char *p)
{
	fprintf(stderr,
		"usage: %s [options]\n"
		"  -m cpu|kv|net    workload class (cpu)\n"
		"  -w N             worker threads (3)\n"
		"  -r RATE          open-loop arrival rate req/s, 0 = closed loop (10000)\n"
		"  -d SEC           measured duration (20)\n"
		"  -W SEC           warmup discarded (2)\n"
		"  -s SEED          random seed (1)\n"
		"  -u US            compute per request in microseconds (100)\n"
		"  -c CPUS          worker cpu list, e.g. 0-2\n"
		"  -g CPU           generator / client cpu\n"
		"  -o FILE          per-request csv (bench.csv)\n"
		"  -M FILE          run metadata json\n"
		"  -R FILE          wait for FILE to exist before starting load\n"
		"  -f FILE          kv data file (/var/tmp/tlbench.dat)\n"
		"  -S N             kv: fdatasync every N requests per worker (200)\n"
		"  -L N             kv: lookups per request (8)\n"
		"  -K PIN           read per-thread kernel counters from pinned map PIN\n", p);
}

int main(int argc, char **argv)
{
	int opt;
	while ((opt = getopt(argc, argv, "m:w:r:d:W:s:u:c:g:o:M:R:f:S:L:p:K:h")) != -1) {
		switch (opt) {
		case 'm':
			C.mode = !strcmp(optarg, "kv") ? M_KV : !strcmp(optarg, "net") ? M_NET : M_CPU;
			break;
		case 'w': C.workers = atoi(optarg); break;
		case 'r': C.rate = atof(optarg); break;
		case 'd': C.duration = atof(optarg); break;
		case 'W': C.warmup = atof(optarg); break;
		case 's': C.seed = strtoull(optarg, NULL, 10); break;
		case 'u': C.work_us = atof(optarg); break;
		case 'c': snprintf(C.worker_cpus, sizeof(C.worker_cpus), "%s", optarg); break;
		case 'g': C.gen_cpu = atoi(optarg); break;
		case 'o': C.out = optarg; break;
		case 'M': C.meta = optarg; break;
		case 'R': C.ready_file = optarg; break;
		case 'f': C.datafile = optarg; break;
		case 'S': C.sync_every = atoi(optarg); break;
		case 'L': C.lookups = atoi(optarg); break;
		case 'p': C.port = atoi(optarg); break;
		case 'K': C.slot_pin = optarg; break;
		default: usage(argv[0]); return 2;
		}
	}
	if (C.workers < 1 || C.workers > MAX_WORKERS) { usage(argv[0]); return 2; }
	if (C.mode == M_NET && C.rate <= 0) {
		fprintf(stderr, "net mode needs an open-loop rate\n");
		return 2;
	}
	if (C.worker_cpus[0]) {
		if (parse_cpus(C.worker_cpus, &worker_set)) { usage(argv[0]); return 2; }
		have_worker_set = 1;
	}

	double tot = C.warmup + C.duration;
	max_reqs = C.rate > 0 ? (uint64_t)(C.rate * tot * 1.3) + 1024
			      : (uint64_t)(160000 * tot) + 1024;
	recs = calloc(max_reqs, sizeof(*recs));
	if (!recs) { perror("calloc recs"); return 1; }
	if (C.slot_pin) {
		kf = calloc(max_reqs, sizeof(*kf));
		if (!kf) { perror("calloc kf"); return 1; }
	}

	calibrate();
	if (C.mode == M_KV)
		kv_init();

	pthread_t th[MAX_WORKERS], rcv;
	if (C.mode == M_NET) {
		net_setup(th);
		pthread_create(&rcv, NULL, net_client_receiver, NULL);
	} else if (C.rate > 0) {
		for (int i = 0; i < C.workers; i++)
			pthread_create(&th[i], NULL, queue_worker, (void *)(uintptr_t)i);
	}

	printf("tlbench pid=%d mode=%d ready\n", getpid(), C.mode);
	fflush(stdout);
	if (C.ready_file) {
		struct stat st;
		while (stat(C.ready_file, &st) != 0)
			usleep(10000);
	}
	if (C.slot_pin)
		map_slots(C.slot_pin);

	struct rusage ru0, ru1;
	getrusage(RUSAGE_SELF, &ru0);
	t_origin = now_ns() + 1000000; /* 1 ms headroom */

	if (C.mode == M_NET) {
		run_generator_net();
		usleep(200000); /* drain in-flight replies */
	} else if (C.rate > 0) {
		run_generator_queue();
		/* let the queue drain */
		for (;;) {
			pthread_mutex_lock(&qlock);
			int empty = qhead == qtail;
			pthread_mutex_unlock(&qlock);
			if (empty)
				break;
			usleep(1000);
		}
	} else {
		for (int i = 0; i < C.workers; i++)
			pthread_create(&th[i], NULL, closed_worker, (void *)(uintptr_t)i);
		wait_until(t_origin + (uint64_t)(tot * 1e9));
	}
	atomic_store(&stop_flag, 1);
	pthread_mutex_lock(&qlock);
	pthread_cond_broadcast(&qcond);
	pthread_mutex_unlock(&qlock);
	for (int i = 0; i < C.workers; i++)
		if (C.mode == M_NET || C.rate >= 0)
			pthread_join(th[i], NULL);
	if (C.mode == M_NET)
		pthread_join(rcv, NULL);
	uint64_t t_stop = now_ns();
	getrusage(RUSAGE_SELF, &ru1);

	/* dump per-request records */
	FILE *f = fopen(C.out, "w");
	if (!f) { perror("fopen out"); return 1; }
	setvbuf(f, NULL, _IOFBF, 1 << 22);
	fprintf(f, "id,tid,cpu,t_arr,t_enq,t_start,t_end,t_done,warm%s\n",
		kf ? ",pre_wake_ns,oncpu_ns,runq_ns,wake_ns,blocked_ns,softirq_ns,hardirq_ns,"
		     "nvcsw,nivcsw,nmigr,nsoftirq,nhardirq,kvalid" : "");
	uint64_t n_done = 0, n_meas = 0, n_lost = 0;
	uint64_t warm_end = t_origin + (uint64_t)(C.warmup * 1e9);
	uint64_t lim = C.rate > 0 ? max_reqs : atomic_load(&next_id);
	if (lim > max_reqs)
		lim = max_reqs;
	for (uint64_t i = 0; i < lim; i++) {
		struct rec *r = &recs[i];
		if (!r->valid) {
			if (r->t_start)
				n_lost++;
			continue;
		}
		int warm = r->t_arr < warm_end;
		n_done++;
		n_meas += !warm;
		fprintf(f, "%llu,%u,%u,%llu,%llu,%llu,%llu,%llu,%d",
			(unsigned long long)i, r->tid, r->cpu,
			(unsigned long long)r->t_arr, (unsigned long long)r->t_enq,
			(unsigned long long)r->t_start,
			(unsigned long long)r->t_end, (unsigned long long)r->t_done, warm);
		if (kf) {
			struct kfeat *k = &kf[i];
			fprintf(f, ",%llu,%llu,%llu,%llu,%llu,%llu,%llu,%u,%u,%u,%u,%u,%u",
				(unsigned long long)k->pre_wake, (unsigned long long)k->d.oncpu,
				(unsigned long long)k->d.runq,
				(unsigned long long)k->d.wake, (unsigned long long)k->d.blocked,
				(unsigned long long)k->d.soft, (unsigned long long)k->d.hard,
				k->d.nvcsw, k->d.nivcsw, k->d.nmigr, k->d.nsoft, k->d.nhard, k->ok);
		}
		fputc('\n', f);
	}
	fclose(f);

	double cpu_s = (ru1.ru_utime.tv_sec - ru0.ru_utime.tv_sec) +
		       (ru1.ru_stime.tv_sec - ru0.ru_stime.tv_sec) +
		       ((ru1.ru_utime.tv_usec - ru0.ru_utime.tv_usec) +
			(ru1.ru_stime.tv_usec - ru0.ru_stime.tv_usec)) / 1e6;
	double wall = (t_stop - t_origin) / 1e9;
	const char *mname[] = { "cpu", "kv", "net" };
	FILE *m = C.meta ? fopen(C.meta, "w") : stdout;
	fprintf(m,
		"{\"mode\":\"%s\",\"workers\":%d,\"rate\":%.1f,\"duration\":%.2f,"
		"\"warmup\":%.2f,\"seed\":%llu,\"work_us\":%.2f,\"worker_cpus\":\"%s\","
		"\"gen_cpu\":%d,\"sync_every\":%d,\"lookups\":%d,"
		"\"iters_per_us\":%.3f,\"t_origin\":%llu,\"wall_s\":%.4f,"
		"\"completed\":%llu,\"measured\":%llu,\"lost\":%llu,\"queue_overflow\":%llu,"
		"\"throughput\":%.2f,\"cpu_s\":%.4f,\"maxrss_kb\":%ld,"
		"\"nvcsw\":%ld,\"nivcsw\":%ld}\n",
		mname[C.mode], C.workers, C.rate, C.duration, C.warmup,
		(unsigned long long)C.seed, C.work_us, C.worker_cpus, C.gen_cpu,
		C.sync_every, C.lookups, iters_per_us_x1000 / 1000.0,
		(unsigned long long)t_origin, wall,
		(unsigned long long)n_done, (unsigned long long)n_meas,
		(unsigned long long)n_lost, (unsigned long long)q_overflow,
		n_done / wall, cpu_s, ru1.ru_maxrss,
		ru1.ru_nvcsw - ru0.ru_nvcsw, ru1.ru_nivcsw - ru0.ru_nivcsw);
	if (m != stdout)
		fclose(m);
	return 0;
}
