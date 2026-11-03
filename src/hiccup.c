// SPDX-License-Identifier: GPL-2.0
/*
 * hiccup - platform-noise meter. One spinning thread per CPU reads the
 * monotonic clock in a tight loop; any gap between consecutive reads larger
 * than the threshold means the thread did not run (host vCPU preemption,
 * guest interrupt, or a guest context switch). Reports, per CPU, the share
 * of time lost and the gap distribution.
 *
 *   hiccup [-d SEC] [-t THRESH_US] [-c CPUS]
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAXG 200000

static double dur = 5;
static uint64_t thresh_ns = 20000;

struct res {
	int cpu;
	uint64_t ngaps, lost_ns, max_ns;
	uint64_t *gaps;
};

static uint64_t now_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
}

static void *spin(void *arg)
{
	struct res *r = arg;
	cpu_set_t s;
	CPU_ZERO(&s);
	CPU_SET(r->cpu, &s);
	pthread_setaffinity_np(pthread_self(), sizeof(s), &s);
	uint64_t end = now_ns() + (uint64_t)(dur * 1e9), prev = now_ns();
	while (prev < end) {
		uint64_t t = now_ns();
		uint64_t g = t - prev;
		if (g > thresh_ns) {
			if (r->ngaps < MAXG)
				r->gaps[r->ngaps] = g;
			r->ngaps++;
			r->lost_ns += g;
			if (g > r->max_ns)
				r->max_ns = g;
		}
		prev = t;
	}
	return NULL;
}

static int cmp(const void *a, const void *b)
{
	uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
	return x < y ? -1 : x > y;
}

int main(int argc, char **argv)
{
	int opt;
	char cpus[64] = "";
	while ((opt = getopt(argc, argv, "d:t:c:")) != -1) {
		if (opt == 'd') dur = atof(optarg);
		else if (opt == 't') thresh_ns = (uint64_t)(atof(optarg) * 1000);
		else if (opt == 'c') snprintf(cpus, sizeof(cpus), "%s", optarg);
	}
	int n = (int)sysconf(_SC_NPROCESSORS_ONLN);
	struct res r[64];
	pthread_t th[64];
	int k = 0;
	for (int c = 0; c < n && c < 64; c++) {
		if (cpus[0] && !strchr(cpus, '0' + c))
			continue;
		r[k] = (struct res){ .cpu = c, .gaps = calloc(MAXG, sizeof(uint64_t)) };
		pthread_create(&th[k], NULL, spin, &r[k]);
		k++;
	}
	printf("{\"duration_s\":%.1f,\"thresh_us\":%.1f,\"cpus\":[", dur, thresh_ns / 1e3);
	for (int i = 0; i < k; i++) {
		pthread_join(th[i], NULL);
		uint64_t m = r[i].ngaps < MAXG ? r[i].ngaps : MAXG;
		qsort(r[i].gaps, m, sizeof(uint64_t), cmp);
		double p50 = m ? r[i].gaps[m / 2] / 1e3 : 0, p99 = m ? r[i].gaps[(m * 99) / 100] / 1e3 : 0;
		printf("%s{\"cpu\":%d,\"gaps\":%llu,\"gaps_per_s\":%.1f,\"lost_pct\":%.3f,"
		       "\"gap_p50_us\":%.1f,\"gap_p99_us\":%.1f,\"gap_max_us\":%.1f}",
		       i ? "," : "", r[i].cpu, (unsigned long long)r[i].ngaps, r[i].ngaps / dur,
		       100.0 * r[i].lost_ns / (dur * 1e9), p50, p99, r[i].max_ns / 1e3);
	}
	printf("]}\n");
	return 0;
}
