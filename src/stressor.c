// SPDX-License-Identifier: GPL-2.0
/*
 * stressor - one controlled interference source at a time.
 *
 *   stressor cpu  -c CPUS -n THREADS -d SEC   spin threads on CPUS
 *   stressor net  -c CPU  -n FLOWS   -d SEC   UDP loopback flood; with RPS
 *                                             steering on lo, receive
 *                                             softirqs land on other CPUs
 *   stressor io   -c CPU  -n THREADS -d SEC   1 MiB write + fdatasync loop
 *
 * Optional -D DUTY (0-100) makes cpu/net stressors bursty: active for
 * DUTY% of every 10 ms period, which creates episodic rather than constant
 * interference.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

static cpu_set_t cpus;
static double dur = 10;
static int duty = 100;
static uint64_t t_end;
static int nthreads = 1;
static const char *iofile = "/var/tmp/stressor.io";

static uint64_t now_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
}

/* returns 1 while inside the active part of the duty cycle */
static int active(uint64_t t)
{
	return duty >= 100 || (t % 10000000ull) < (uint64_t)duty * 100000ull;
}

static void pin(int idx)
{
	int cnt = CPU_COUNT(&cpus), seen = 0;
	if (!cnt)
		return;
	for (int c = 0; c < CPU_SETSIZE; c++) {
		if (!CPU_ISSET(c, &cpus))
			continue;
		if (seen++ == idx % cnt) {
			cpu_set_t one;
			CPU_ZERO(&one);
			CPU_SET(c, &one);
			pthread_setaffinity_np(pthread_self(), sizeof(one), &one);
			return;
		}
	}
}

static void *cpu_hog(void *arg)
{
	pin((int)(intptr_t)arg);
	volatile uint64_t x = 1;
	for (;;) {
		uint64_t t = now_ns();
		if (t > t_end)
			break;
		if (active(t)) {
			for (int i = 0; i < 20000; i++)
				x = x * 6364136223846793005ull + 1;
		} else {
			usleep(500);
		}
	}
	return NULL;
}

static void *net_flood(void *arg)
{
	int idx = (int)(intptr_t)arg;
	pin(idx);
	int rx = socket(AF_INET, SOCK_DGRAM, 0);
	int tx = socket(AF_INET, SOCK_DGRAM, 0);
	int big = 1 << 20;
	setsockopt(rx, SOL_SOCKET, SO_RCVBUF, &big, sizeof(big));
	struct sockaddr_in a = { .sin_family = AF_INET, .sin_port = 0,
				 .sin_addr.s_addr = htonl(INADDR_LOOPBACK) };
	bind(rx, (void *)&a, sizeof(a));
	socklen_t l = sizeof(a);
	getsockname(rx, (void *)&a, &l);
	char buf[256];
	memset(buf, idx, sizeof(buf));
	unsigned long n = 0;
	for (;;) {
		uint64_t t = now_ns();
		if (t > t_end)
			break;
		if (active(t)) {
			for (int i = 0; i < 64; i++)
				sendto(tx, buf, sizeof(buf), MSG_DONTWAIT, (void *)&a, sizeof(a));
			while (recv(rx, buf, sizeof(buf), MSG_DONTWAIT) > 0)
				n++;
		} else {
			usleep(500);
		}
	}
	close(rx);
	close(tx);
	return (void *)n;
}

static void *io_writer(void *arg)
{
	int idx = (int)(intptr_t)arg;
	pin(idx);
	char path[256];
	snprintf(path, sizeof(path), "%s.%d", iofile, idx);
	int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
	char *buf = malloc(1 << 20);
	memset(buf, 0xa5, 1 << 20);
	off_t off = 0;
	while (now_ns() < t_end) {
		if (pwrite(fd, buf, 1 << 20, off) < 0)
			break;
		fdatasync(fd);
		off = (off + (1 << 20)) % (64ll << 20);
	}
	close(fd);
	unlink(path);
	free(buf);
	return NULL;
}

int main(int argc, char **argv)
{
	if (argc < 2) {
		fprintf(stderr, "usage: %s cpu|net|io [-c CPUS] [-n N] [-d SEC] [-D DUTY]\n", argv[0]);
		return 2;
	}
	const char *kind = argv[1];
	CPU_ZERO(&cpus);
	int opt;
	optind = 2;
	while ((opt = getopt(argc, argv, "c:n:d:D:")) != -1) {
		switch (opt) {
		case 'c': {
			char b[64];
			snprintf(b, sizeof(b), "%s", optarg);
			for (char *t = strtok(b, ","); t; t = strtok(NULL, ",")) {
				int x, y;
				if (sscanf(t, "%d-%d", &x, &y) == 2)
					for (int i = x; i <= y; i++) CPU_SET(i, &cpus);
				else
					CPU_SET(atoi(t), &cpus);
			}
			break;
		}
		case 'n': nthreads = atoi(optarg); break;
		case 'd': dur = atof(optarg); break;
		case 'D': duty = atoi(optarg); break;
		default: return 2;
		}
	}
	void *(*fn)(void *) = !strcmp(kind, "cpu") ? cpu_hog :
			      !strcmp(kind, "net") ? net_flood :
			      !strcmp(kind, "io")  ? io_writer : NULL;
	if (!fn) {
		fprintf(stderr, "unknown stressor %s\n", kind);
		return 2;
	}
	t_end = now_ns() + (uint64_t)(dur * 1e9);
	pthread_t th[64];
	if (nthreads > 64)
		nthreads = 64;
	for (int i = 0; i < nthreads; i++)
		pthread_create(&th[i], NULL, fn, (void *)(intptr_t)i);
	for (int i = 0; i < nthreads; i++)
		pthread_join(th[i], NULL);
	return 0;
}
