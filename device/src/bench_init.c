// SPDX-License-Identifier: GPL-2.0
/*
 * PID 1 for the pageout/refault benchmark guest.
 *
 * Mounts proc/sysfs/devtmpfs, turns /dev/vda into swap, then on CPU 1:
 *
 *   batch:  fill NPAGES with /corpus pages, MADV_PAGEOUT the lot, time it;
 *           then read one byte per page sequentially (seq refault), page the
 *           lot out again and read them back in a random order (rand refault).
 *   single: page out one random page and read it back, one at a time, and
 *           keep both latencies for percentiles.
 *
 * Only pages pagemap reports as swapped count, so a pageout that leaves
 * pages resident does not look fast.  Every round prints one BENCH line;
 * the host side takes medians over rounds and boots.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/swap.h>
#include <sys/syscall.h>
#include <sys/utsname.h>
#include <time.h>
#include <unistd.h>
#include <linux/fs.h>

#ifndef MADV_PAGEOUT
#define MADV_PAGEOUT 21
#endif
#define PG 4096
#define SWAPDEV "/dev/vda"
#define MCDIR "/sys/kernel/mm/memcomp/"

static unsigned long p_pages = 65536, p_rounds = 5, p_single = 20000;
static unsigned long p_cpu = 1;
/*
 * HUGE=1: let the buffer take 64K folios; HUGE=0: MADV_NOHUGEPAGE.  Either
 * way the buffer is mapped afresh, 64K aligned, every round, so each round
 * starts from first-touch faults rather than whatever the last refault left.
 * Unset: one MADV_NOHUGEPAGE mapping for all rounds, as before.
 */
static const char *p_huge;
/* KNOB=<file> KNOBVALS="a b": write a or b before each round. */
static const char *p_knob;
static char p_kval[2][64];

static void say(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void say(const char *fmt, ...)
{
	va_list ap;

	va_start(ap, fmt);
	vprintf(fmt, ap);
	va_end(ap);
	putchar('\n');
	fflush(stdout);
}

static uint64_t now_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000000000ull + ts.tv_nsec;
}

static uint64_t rng = 0x9e3779b97f4a7c15ull;

static uint64_t rnd(void)
{
	rng ^= rng << 13;
	rng ^= rng >> 7;
	rng ^= rng << 17;
	return rng;
}

static void dump(const char *path)
{
	char buf[8192];
	int fd = open(path, O_RDONLY);
	ssize_t n;

	if (fd < 0)
		return;
	n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (n <= 0)
		return;
	buf[n] = 0;
	printf("== %s\n%s", path, buf);
	if (buf[n - 1] != '\n')
		putchar('\n');
	fflush(stdout);
}

static int setup_swap(void)
{
	uint8_t *hdr = aligned_alloc(PG, PG);
	uint64_t size = 0;
	uint32_t v;
	int fd, i;

	for (i = 0; i < 50 && access(SWAPDEV, F_OK); i++)
		usleep(100000);
	fd = open(SWAPDEV, O_RDWR);
	if (fd < 0 || ioctl(fd, BLKGETSIZE64, &size) || size < 16 * PG)
		return -1;
	memset(hdr, 0, PG);
	v = 1;
	memcpy(hdr + 1024, &v, 4);
	v = (uint32_t)(size / PG - 1);
	memcpy(hdr + 1028, &v, 4);
	memcpy(hdr + PG - 10, "SWAPSPACE2", 10);
	if (pwrite(fd, hdr, PG, 0) != PG || fsync(fd))
		return -1;
	close(fd);
	free(hdr);
	return swapon(SWAPDEV, 0);
}

static unsigned long count_swapped(int pm, char *base, size_t n)
{
	static uint64_t *e;
	unsigned long s = 0;

	if (!e)
		e = malloc(p_pages * 8);
	if (pread(pm, e, n * 8, ((uintptr_t)base / PG) * 8) != (ssize_t)(n * 8))
		return 0;
	for (size_t i = 0; i < n; i++)
		s += (e[i] >> 62) & 1;
	return s;
}

#define MTHP "/sys/kernel/mm/transparent_hugepage/hugepages-64kB/stats/"
static const char *const mthp_stat[] = {
	"anon_fault_alloc", "anon_fault_fallback", "split", "split_deferred",
	"nr_anon",
};
#define NR_MTHP_STAT (sizeof(mthp_stat) / sizeof(mthp_stat[0]))

static void mthp_read(long *v)
{
	char path[128], b[32];

	for (unsigned int i = 0; i < NR_MTHP_STAT; i++) {
		int fd;
		ssize_t n;

		snprintf(path, sizeof(path), MTHP "%s", mthp_stat[i]);
		fd = open(path, O_RDONLY);
		n = fd < 0 ? -1 : read(fd, b, sizeof(b) - 1);
		if (fd >= 0)
			close(fd);
		b[n > 0 ? n : 0] = 0;
		v[i] = strtol(b, NULL, 10);
	}
}

static char *map_buf(void)
{
	size_t len = p_pages * PG, al = 16 * PG;
	char *raw = mmap(NULL, len + al, PROT_READ | PROT_WRITE,
			 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	char *buf;

	if (raw == MAP_FAILED)
		return NULL;
	buf = (char *)(((uintptr_t)raw + al - 1) & ~(uintptr_t)(al - 1));
	if (buf > raw)
		munmap(raw, buf - raw);
	munmap(buf + len, raw + al - buf);
	if (!p_huge || *p_huge != '1')
		madvise(buf, len, MADV_NOHUGEPAGE);
	return buf;
}

static int cmp_u32(const void *a, const void *b)
{
	uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b;

	return x < y ? -1 : x > y;
}

static volatile unsigned long sink;

static void bench(const char *corpus, size_t cpages)
{
	char *buf = map_buf();
	uint32_t *perm = malloc(p_pages * 4);
	uint32_t *lpo = malloc(p_single * 4), *lrf = malloc(p_single * 4);
	int pm = open("/proc/self/pagemap", O_RDONLY);
	unsigned long r, i;

	for (i = 0; i < p_pages; i++)
		perm[i] = i;

	for (r = 0; r < p_rounds; r++) {
		uint64_t t0, t1, t2, t3, t4, t4b, t5, po1;
		unsigned long sw1, sw2, sw3, sw4, k, npo = 0;

		const char *kv = "-";
		long m0[NR_MTHP_STAT], m1[NR_MTHP_STAT], m2[NR_MTHP_STAT];

		if (p_huge && r) {
			munmap(buf, p_pages * PG);
			buf = map_buf();
		}
		if (p_knob) {
			int kfd = open(p_knob, O_WRONLY);

			/* Round 0 warms up; then pairs (1,2), (3,4)... as AB, BA. */
			kv = p_kval[r ? ((r - 1) % 2) ^ ((r - 1) / 2 % 2) : 0];
			if (kfd < 0 || write(kfd, kv, strlen(kv)) < 0)
				say("knob %s=%s: %s", p_knob, kv, strerror(errno));
			if (kfd >= 0)
				close(kfd);
		}
		mthp_read(m0);
		/* Fresh contents each round: the corpus, rotated. */
		for (i = 0; i < p_pages; i++)
			memcpy(buf + i * PG,
			       corpus + ((i + r * 7919) % cpages) * PG, PG);
		for (i = p_pages - 1; i > 0; i--) {
			uint32_t j = rnd() % (i + 1), t = perm[i];

			perm[i] = perm[j];
			perm[j] = t;
		}

		mthp_read(m1);
		t0 = now_ns();
		madvise(buf, p_pages * PG, MADV_PAGEOUT);
		po1 = now_ns() - t0;
		sw1 = count_swapped(pm, buf, p_pages);
		if (!r)
			dump(MCDIR "stat");
		t1 = now_ns();
		for (i = 0; i < p_pages; i++)
			sink += buf[i * PG + 64];
		t2 = now_ns();
		sw2 = count_swapped(pm, buf, p_pages);

		t3 = now_ns();
		madvise(buf, p_pages * PG, MADV_PAGEOUT);
		t4 = now_ns();
		sw3 = count_swapped(pm, buf, p_pages);
		t4b = now_ns();
		for (i = 0; i < p_pages; i++)
			sink += buf[(size_t)perm[i] * PG + 64];
		t5 = now_ns();
		sw4 = count_swapped(pm, buf, p_pages);

		/* One page at a time. */
		for (k = 0; k < p_single; k++) {
			char *p = buf + (size_t)perm[k] * PG;
			uint64_t a, po, b, c;
			uint64_t e;

			a = now_ns();
			madvise(p, PG, MADV_PAGEOUT);
			po = now_ns();
			if (pread(pm, &e, 8, ((uintptr_t)p / PG) * 8) != 8)
				e = 0;
			b = now_ns();
			sink += p[64];
			c = now_ns();
			if (!((e >> 62) & 1))
				continue;
			lpo[npo] = po - a;
			lrf[npo] = c - b;
			npo++;
		}
		mthp_read(m2);
		qsort(lpo, npo, 4, cmp_u32);
		qsort(lrf, npo, 4, cmp_u32);

		say("BENCH knob=%s round=%lu pageout_ns=%.1f seqrf_ns=%.1f "
		    "pageout2_ns=%.1f randrf_ns=%.1f swapped=%lu/%lu/%lu/%lu "
		    "single_n=%lu spo_p50=%u spo_p99=%u srf_p50=%u srf_p99=%u",
		    kv, r, (double)po1 / (sw1 ? sw1 : 1),
		    (double)(t2 - t1) / (sw1 ? sw1 - sw2 : 1),
		    (double)(t4 - t3) / (sw3 ? sw3 : 1),
		    (double)(t5 - t4b) / (sw3 ? sw3 - sw4 : 1),
		    sw1, sw2, sw3, sw4, npo,
		    npo ? lpo[npo / 2] : 0, npo ? lpo[npo * 99 / 100] : 0,
		    npo ? lrf[npo / 2] : 0, npo ? lrf[npo * 99 / 100] : 0);
		/* Fill: 64K folios the first touch made; rest: the whole round. */
		say("MTHP round=%lu huge=%s fill_alloc=%ld fallback=%ld "
		    "alloc=%ld split=%ld split_deferred=%ld",
		    r, p_huge ?: "-", m1[0] - m0[0], m2[1] - m0[1],
		    m2[0] - m1[0], m2[2] - m0[2], m2[3] - m0[3]);
	}
	close(pm);
}

static char *load_corpus(const char *path, size_t *pages)
{
	struct stat st;
	char *c;
	int fd = open(path, O_RDONLY);

	if (fd < 0 || fstat(fd, &st))
		return NULL;
	*pages = st.st_size / PG;
	c = aligned_alloc(PG, *pages * PG);
	if (read(fd, c, *pages * PG) != (ssize_t)(*pages * PG))
		return NULL;
	close(fd);
	/* Keep the corpus out of reclaim's way. */
	mlock(c, *pages * PG);
	return c;
}

static void param(const char *name, unsigned long *v)
{
	const char *s = getenv(name);

	if (s && *s)
		*v = strtoul(s, NULL, 0);
}

int main(int argc, char **argv)
{
	const char *cpath = argc > 1 ? argv[1] : "/corpus";
	bool pid1 = getpid() == 1;
	struct utsname u;
	cpu_set_t set;
	size_t cpages;
	char *corpus;
	int fd;

	if (!pid1)
		goto tool;
	mount("proc", "/proc", "proc", 0, NULL);
	mount("sysfs", "/sys", "sysfs", 0, NULL);
	mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);
	/* GKI's built-in console=ttynull may own /dev/console. */
	fd = open("/dev/ttyS0", O_WRONLY);
	if (fd >= 0) {
		dup2(fd, 1);
		dup2(fd, 2);
		close(fd);
	}
tool:
	/* An A/B module for KNOB, if the initramfs carries one. */
	fd = open("/knob_toggle.ko", O_RDONLY);
	if (fd >= 0) {
		if (syscall(SYS_finit_module, fd, "", 0))
			printf("finit_module: %s\n", strerror(errno));
		close(fd);
	}
	setvbuf(stdout, NULL, _IOLBF, 0);
	param("NPAGES", &p_pages);
	param("ROUNDS", &p_rounds);
	param("SINGLE", &p_single);
	param("CPU", &p_cpu);
	p_huge = getenv("HUGE");
	if (p_single > p_pages)
		p_single = p_pages;
	/* "a b", or "a,b" where a kernel command line carries it. */
	p_knob = getenv("KNOB");
	if (p_knob && sscanf(getenv("KNOBVALS") ?: "1 0", "%63[^ ,]%*[ ,]%63s",
			     p_kval[0], p_kval[1]) != 2)
		p_knob = NULL;
	uname(&u);
	say("bench_init: %s %s", u.release, u.version);
	dump("/proc/cmdline");
	dump(MCDIR "compressor");
	if (pid1 && setup_swap()) {
		say("RESULT: FAIL swap: %s", strerror(errno));
		goto out;
	}
	corpus = load_corpus(cpath, &cpages);
	if (!corpus) {
		say("RESULT: FAIL corpus");
		goto out;
	}
	CPU_ZERO(&set);
	CPU_SET(p_cpu, &set);
	sched_setaffinity(0, sizeof(set), &set);
	say("pages=%lu rounds=%lu single=%lu corpus=%zu cpu=%lu huge=%s",
	    p_pages, p_rounds, p_single, cpages, p_cpu, p_huge ?: "-");
	bench(corpus, cpages);
	dump(MCDIR "stat");
	say("RESULT: DONE");
out:
	if (!pid1)
		return 0;
	sync();
	reboot(RB_POWER_OFF);
	return 0;
}
