// SPDX-License-Identifier: GPL-2.0
/*
 * mcwb_init - static /init for an end-to-end memcomp swap WRITEBACK test
 * in a KVM guest.
 *
 * PID 1 mounts proc/sysfs/devtmpfs, writes a swap header on /dev/vda,
 * swapon()s it and forks a child that runs the tests (so an OOM kill or a
 * SIGBUS in a test does not take PID 1 down):
 *
 *   fill        big anon region A (FILL_MB) + pressure region B (PRESS_MB),
 *               deterministic per-page content, MADV_PAGEOUT of old chunks,
 *               then wait for kswapd scopes / the drain worker to write back
 *   verify      read back every page of A and B
 *   cycles      random-order refault subset, re-dirty some, MADV_PAGEOUT all,
 *               pressure via B, drain, re-verify (CYCLES times)
 *   conc        CONC_THREADS threads refault the same pages of A while a
 *               thread MADV_PAGEOUTs them and another sweeps B; then verify
 *   leak        munmap A/B; memcomp stat and swap must return near zero
 *   swapoff     region D partly written back, swapoff() pulls it back, verify
 *
 * Every env var below can be given as a kernel cmdline word (FOO=1).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/fs.h>
#include <linux/userfaultfd.h>
#include <pthread.h>
#include <signal.h>
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
#include <sys/syscall.h>
#include <sys/swap.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifndef MADV_PAGEOUT
#define MADV_PAGEOUT 21
#endif

#define PG 4096UL
#define PAGES_PER_MB 256UL
#define SWAPDEV "/dev/vda"
#define MCDIR "/sys/kernel/mm/memcomp/"
#define MCWB_PM_SOFT_DIRTY (UINT64_C(1) << 55)

/* ------------------------------------------------------------------ */
/* parameters                                                          */

static unsigned long envu(const char *n, unsigned long d)
{
	const char *v = getenv(n);

	if (!v || !*v)
		return d;
	return strtoul(v, NULL, 0);
}

static unsigned long p_fill_mb, p_press_mb, p_chunk_mb, p_lag, p_pace_us;
static unsigned long p_drain_ms, p_wb_wait_s, p_cycles, p_refault_pct;
static unsigned long p_redirty_pct, p_conc_secs, p_conc_threads, p_conc_k;
static unsigned long p_swapoff_mb, p_swapoff_press_mb, p_verify_threads;
static unsigned long p_seed, p_swap_tol_kb, p_mc_tol_kb, p_owner_fail;
static unsigned long p_owner_tol, p_mthp_kb, p_no_swap;
static unsigned long p_skip_cycles, p_skip_conc, p_skip_swapoff, p_skip_uffd;
static unsigned long p_skip_retain, p_retain_mb, p_retain_rounds;
static unsigned long p_retain_threads, p_retain_readers;
static unsigned long p_timeout_s, p_heartbeat_s, p_max_report;

static void load_params(void)
{
	p_fill_mb = envu("FILL_MB", 1280);
	p_press_mb = envu("PRESS_MB", 320);
	p_chunk_mb = envu("CHUNK_MB", 16);
	p_lag = envu("PAGEOUT_LAG", 4);	/* chunks; 0 = no MADV_PAGEOUT in fill */
	p_pace_us = envu("PACE_US", 0);	/* sleep after every chunk written */
	p_drain_ms = envu("DRAIN_MS", 2000);
	p_wb_wait_s = envu("WB_WAIT_S", 20);
	p_cycles = envu("CYCLES", 3);
	p_refault_pct = envu("REFAULT_PCT", 25);
	p_redirty_pct = envu("REDIRTY_PCT", 10);
	p_conc_secs = envu("CONC_SECS", 15);
	p_conc_threads = envu("CONC_THREADS", 4);
	p_conc_k = envu("CONC_K", 512);
	p_swapoff_mb = envu("SWAPOFF_MB", 1024);
	p_swapoff_press_mb = envu("SWAPOFF_PRESS_MB", 384);
	p_verify_threads = envu("VERIFY_THREADS", 4);
	p_seed = envu("SEED", 0x6d637762);
	p_swap_tol_kb = envu("SWAP_TOL_KB", 8192);
	p_mc_tol_kb = envu("MC_TOL_KB", 8192);
	p_owner_fail = envu("OWNER_LEAK_FAIL", 1);
	p_owner_tol = envu("OWNER_TOL", 64);
	p_mthp_kb = envu("MTHP_KB", 64);
	p_no_swap = envu("NO_SWAP", 0);
	if (p_no_swap) {
		p_fill_mb = envu("FILL_MB", 256);
		p_press_mb = envu("PRESS_MB", 64);
	}
	if (p_mthp_kb != 16 && p_mthp_kb != 32 && p_mthp_kb != 64)
		p_mthp_kb = 64;
	p_skip_cycles = envu("SKIP_CYCLES", 0);
	p_skip_conc = envu("SKIP_CONC", 0);
	p_skip_swapoff = envu("SKIP_SWAPOFF", 0);
	p_skip_uffd = envu("SKIP_UFFD", 0);
	p_skip_retain = envu("SKIP_RETAIN", 0);
	p_retain_mb = envu("RETAIN_MB", 16);
	p_retain_rounds = envu("RETAIN_ROUNDS", 8);
	p_retain_threads = envu("RETAIN_THREADS", 4);
	p_retain_readers = envu("RETAIN_READERS", 1);
	p_timeout_s = envu("TEST_TIMEOUT", 540);
	p_heartbeat_s = envu("HEARTBEAT_S", 30);
	p_max_report = envu("MAX_REPORT", 20);
	if (!p_chunk_mb)
		p_chunk_mb = 16;
	if (!p_conc_threads)
		p_conc_threads = 1;
	if (!p_verify_threads)
		p_verify_threads = 1;
	if (!p_conc_k)
		p_conc_k = 64;
	if (!p_retain_mb)
		p_retain_mb = 1;
	if (!p_retain_threads)
		p_retain_threads = 1;
	if (p_retain_threads > 64)
		p_retain_threads = 64;
}

/* ------------------------------------------------------------------ */
/* logging                                                             */

static struct timespec t0;
static pthread_mutex_t log_lock = PTHREAD_MUTEX_INITIALIZER;

static double now_s(void)
{
	struct timespec t;

	clock_gettime(CLOCK_MONOTONIC, &t);
	return (t.tv_sec - t0.tv_sec) + (t.tv_nsec - t0.tv_nsec) / 1e9;
}

static void say(const char *fmt, ...)
{
	va_list ap;

	pthread_mutex_lock(&log_lock);
	printf("[mcwb %7.2f] ", now_s());
	va_start(ap, fmt);
	vprintf(fmt, ap);
	va_end(ap);
	putchar('\n');
	fflush(stdout);
	pthread_mutex_unlock(&log_lock);
}

static void msleep(unsigned long ms)
{
	struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };

	while (nanosleep(&ts, &ts) && errno == EINTR)
		;
}

/* ------------------------------------------------------------------ */
/* stats                                                               */

static size_t read_file(const char *path, char *buf, size_t sz)
{
	int fd = open(path, O_RDONLY);
	ssize_t n, tot = 0;

	buf[0] = 0;
	if (fd < 0)
		return 0;
	while ((size_t)tot < sz - 1 &&
	       (n = read(fd, buf + tot, sz - 1 - tot)) > 0)
		tot += n;
	close(fd);
	buf[tot < 0 ? 0 : tot] = 0;
	return tot;
}

static void fail(const char *fmt, ...);

static uint64_t kv(const char *buf, const char *key)
{
	size_t kl = strlen(key);
	const char *p = buf;

	while (p && *p) {
		if (!strncmp(p, key, kl) &&
		    (p[kl] == ' ' || p[kl] == ':' || p[kl] == '\t')) {
			char *end;
			const char *v = p + kl + 1;
			uint64_t n;

			while (*v == ' ' || *v == '\t')
				v++;
			errno = 0;
			n = strtoull(v, &end, 10);
			if (*v >= '0' && *v <= '9' && end != v &&
			    !errno && (*end == 0 || *end == '\n' ||
				       *end == ' ' || *end == '\t'))
				return n;
			fail("counter %s: invalid value", key);
			return 0;
		}
		p = strchr(p, '\n');
		if (p)
			p++;
	}
	fail("counter %s: missing or unreadable; cannot assert zero", key);
	return 0;
}

struct snap {
	uint64_t stored, compressed, pool;
	uint64_t wb_payloads, wb_budget, wb_events, wb_scans, wb_zrecl;
	/* wb_scopes is instantaneous; wb_events counts scope openings. */
	uint64_t wb_noprog, wb_limited, wb_derr, wb_werr, wb_scopes;
	uint64_t sw_entries, sw_read_io, sw_read_err;
	uint64_t so_entries, so_read_io, so_fail;
	uint64_t owner, pending, samefill, raw, po_ok, po_fail;
	uint64_t pl_compress, pl_fallback;
	uint64_t memtotal, memfree, swaptotal, swapfree, anon;
	uint64_t pswpin, pswpout, oom_kill;
	uint64_t pgscan_kswapd, pgscan_direct, pgsteal_kswapd, pgsteal_direct;
	uint64_t allocstall;
};

static char sbuf[1 << 16];

static void take_snap(struct snap *s)
{
	const char *p;

	memset(s, 0, sizeof(*s));
	if (read_file(MCDIR "stat", sbuf, sizeof(sbuf)))
		{
		if (sscanf(sbuf, "%lu %lu %lu", &s->stored, &s->compressed,
			   &s->pool) != 3)
			fail("memcomp stat: malformed");
	} else {
		fail("memcomp stat: unreadable");
	}
	read_file(MCDIR "packed_swap_events", sbuf, sizeof(sbuf));
	s->wb_payloads = kv(sbuf, "writeback_payloads");
	s->wb_budget = kv(sbuf, "writeback_budget_granted");
	s->wb_events = kv(sbuf, "writeback_pressure_events");
	s->wb_scans = kv(sbuf, "writeback_pressure_scans");
	s->wb_zrecl = kv(sbuf, "writeback_zs_reclaimed");
	s->wb_noprog = kv(sbuf, "writeback_zs_no_progress");
	s->wb_limited = kv(sbuf, "writeback_limited_batches");
	s->wb_derr = kv(sbuf, "writeback_decompress_error");
	s->wb_werr = kv(sbuf, "writeback_write_error");
	s->wb_scopes = kv(sbuf, "writeback_pressure_scopes");
	read_file(MCDIR "events", sbuf, sizeof(sbuf));
	s->sw_entries = kv(sbuf, "swapped_entries");
	s->sw_read_io = kv(sbuf, "swapped_read_io");
	s->sw_read_err = kv(sbuf, "swapped_read_error");
	s->so_entries = kv(sbuf, "swapoff_entries");
	s->so_read_io = kv(sbuf, "swapoff_read_io");
	s->so_fail = kv(sbuf, "swapoff_fail");
	s->owner = kv(sbuf, "folio_owner_pages");
	s->pending = kv(sbuf, "pending_pages");
	s->samefill = kv(sbuf, "samefill_pages");
	s->raw = kv(sbuf, "raw_pages");
	s->po_ok = kv(sbuf, "pageout_store_ok");
	s->po_fail = kv(sbuf, "pageout_store_fail");
	read_file(MCDIR "placement_events", sbuf, sizeof(sbuf));
	s->pl_compress = kv(sbuf, "compress");
	s->pl_fallback = kv(sbuf, "normal_swap_fallback");
	read_file("/proc/meminfo", sbuf, sizeof(sbuf));
	s->memtotal = kv(sbuf, "MemTotal");
	s->memfree = kv(sbuf, "MemFree");
	s->swaptotal = kv(sbuf, "SwapTotal");
	s->swapfree = kv(sbuf, "SwapFree");
	s->anon = kv(sbuf, "AnonPages");
	read_file("/proc/vmstat", sbuf, sizeof(sbuf));
	s->pswpin = kv(sbuf, "pswpin");
	s->pswpout = kv(sbuf, "pswpout");
	s->oom_kill = kv(sbuf, "oom_kill");
	s->pgscan_kswapd = kv(sbuf, "pgscan_kswapd");
	s->pgscan_direct = kv(sbuf, "pgscan_direct");
	s->pgsteal_kswapd = kv(sbuf, "pgsteal_kswapd");
	s->pgsteal_direct = kv(sbuf, "pgsteal_direct");
	/* Zone names vary with the architecture and kernel configuration. */
	for (p = sbuf; p && *p; ) {
		if (!strncmp(p, "allocstall_", sizeof("allocstall_") - 1)) {
			char key[64];

			if (sscanf(p, "%63s", key) == 1)
				s->allocstall += kv(p, key);
		}
		p = strchr(p, '\n');
		if (p)
			p++;
	}
}

#define MB(b) ((double)(b) / 1048576.0)
#define KMB(k) ((double)(k) / 1024.0)

static void print_snap(const char *label, const struct snap *s)
{
	say("[%s] stat stored=%.1fM compressed=%.1fM pool=%.1fM (%.1f%% of RAM)"
	    " | swap used=%.1fM/%.1fM | memfree=%.1fM anon=%.1fM",
	    label, MB(s->stored), MB(s->compressed), MB(s->pool),
	    s->memtotal ? 100.0 * s->pool / (s->memtotal * 1024.0) : 0.0,
	    KMB(s->swaptotal - s->swapfree), KMB(s->swaptotal),
	    KMB(s->memfree), KMB(s->anon));
	say("[%s] wb payloads=%lu budget=%lu events=%lu scans=%lu scopes=%lu "
	    "zs_reclaimed=%lu no_progress=%lu limited=%lu derr=%lu werr=%lu",
	    label, s->wb_payloads, s->wb_budget, s->wb_events, s->wb_scans,
	    s->wb_scopes, s->wb_zrecl, s->wb_noprog, s->wb_limited,
	    s->wb_derr, s->wb_werr);
	say("[%s] swapped entries=%lu read_io=%lu read_err=%lu | swapoff "
	    "entries=%lu read_io=%lu fail=%lu",
	    label, s->sw_entries, s->sw_read_io, s->sw_read_err,
	    s->so_entries, s->so_read_io, s->so_fail);
	say("[%s] owner=%lu pending=%lu samefill=%lu raw=%lu pageout_ok=%lu "
	    "pageout_fail=%lu compress=%lu normal_swap_fallback=%lu "
	    "pswpin=%lu pswpout=%lu oom_kill=%lu",
	    label, s->owner, s->pending, s->samefill, s->raw, s->po_ok,
	    s->po_fail, s->pl_compress, s->pl_fallback, s->pswpin,
	    s->pswpout, s->oom_kill);
	say("[%s] reclaim pgscan_kswapd=%lu pgsteal_kswapd=%lu "
	    "pgscan_direct=%lu pgsteal_direct=%lu allocstall=%lu",
	    label, s->pgscan_kswapd, s->pgsteal_kswapd,
	    s->pgscan_direct, s->pgsteal_direct, s->allocstall);
}

static void short_snap(const char *label)
{
	struct snap s;

	take_snap(&s);
	say("[%s] stored=%.0fM pool=%.0fM swap=%.0fM wb=%lu swapped=%lu "
	    "memfree=%.0fM scopes=%lu events=%lu budget=%lu scans=%lu "
	    "oom_kill=%lu", label, MB(s.stored), MB(s.pool),
	    KMB(s.swaptotal - s.swapfree), s.wb_payloads, s.sw_entries,
	    KMB(s.memfree), s.wb_scopes, s.wb_events, s.wb_budget,
	    s.wb_scans, s.oom_kill);
}

static void dump_file(const char *path)
{
	char *p, *nl;

	read_file(path, sbuf, sizeof(sbuf));
	say("---- %s ----", path);
	for (p = sbuf; *p; p = nl + 1) {
		nl = strchr(p, '\n');
		if (!nl) {
			say("  %s", p);
			break;
		}
		*nl = 0;
		say("  %s", p);
	}
}

/* ------------------------------------------------------------------ */
/* deterministic page content                                          */

static inline uint64_t mix64(uint64_t z)
{
	z += 0x9e3779b97f4a7c15ULL;
	z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
	z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
	return z ^ (z >> 31);
}

static inline uint64_t xs(uint64_t *st)
{
	uint64_t x = *st;

	x ^= x >> 12;
	x ^= x << 25;
	x ^= x >> 27;
	*st = x;
	return x * 0x2545F4914F6CDD1DULL;
}

enum { T_ZERO, T_FILL, T_TEXT, T_REC, T_ALPHA, T_RAND, T_NR };
static const char *const tname[T_NR] = {
	"zero", "fill", "text", "rec", "alpha", "rand"
};

#define PAGE_MAGIC 0x4547415042574d43ULL /* "CMWBPAGE" */

struct page_hdr {
	uint64_t magic;
	uint32_t rid;
	uint32_t gen;
	uint64_t idx;
	uint64_t seed;
};

static inline uint64_t page_seed(unsigned int rid, uint64_t idx,
				 unsigned int gen)
{
	return mix64(p_seed ^ ((uint64_t)rid << 56) ^ (idx << 8) ^ gen);
}

static inline int page_type(uint64_t seed)
{
	unsigned int r = (unsigned int)(mix64(seed ^ 0x5bd1e995ULL) % 100);

	if (r < 5)
		return T_ZERO;
	if (r < 10)
		return T_FILL;
	if (r < 35)
		return T_TEXT;
	if (r < 65)
		return T_REC;
	if (r < 85)
		return T_ALPHA;
	return T_RAND;
}

static const char *const words[64] = {
	"the", "of", "and", "memory", "page", "swap", "kernel", "to", "in",
	"is", "compress", "that", "for", "it", "with", "as", "was", "on", "be",
	"at", "by", "this", "had", "not", "are", "but", "from", "or", "have",
	"an", "they", "which", "one", "you", "were", "her", "all", "she",
	"there", "would", "their", "we", "him", "been", "has", "when", "who",
	"will", "more", "no", "if", "out", "so", "said", "what", "up", "its",
	"about", "into", "than", "them", "can", "only", "other",
};
static unsigned char wlen[64];

static const char *const rnames[8] = {
	"alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"
};

struct rec {
	uint64_t id;
	uint32_t ts;
	uint16_t kind;
	uint16_t flags;
	char name[16];
	uint64_t value;
	uint32_t chk;
	uint32_t pad;
	uint64_t z[2];
};

static void gen_init(void)
{
	int i;

	for (i = 0; i < 64; i++)
		wlen[i] = (unsigned char)strlen(words[i]);
}

static void gen_page(uint8_t *dst, unsigned int rid, uint64_t idx,
		     unsigned int gen)
{
	uint64_t seed = page_seed(rid, idx, gen);
	int type = page_type(seed);
	uint64_t st = seed | 1;
	struct page_hdr *h = (struct page_hdr *)dst;
	uint64_t *q = (uint64_t *)dst;
	uint8_t *p, *end = dst + PG;
	unsigned int i, nw = 0;

	switch (type) {
	case T_ZERO:
		memset(dst, 0, PG);
		return;
	case T_FILL: {
		uint64_t pat = seed | 1;

		for (i = 0; i < PG / 8; i++)
			q[i] = pat;
		return;
	}
	default:
		break;
	}

	h->magic = PAGE_MAGIC;
	h->rid = rid;
	h->gen = gen;
	h->idx = idx;
	h->seed = seed;
	p = dst + sizeof(*h);

	switch (type) {
	case T_TEXT: {
		uint8_t *start = p;

		while (p < end) {
			uint64_t r = xs(&st);
			unsigned int a = r & 63, b = (r >> 6) & 63;
			unsigned int c = (r >> 24) & 63;
			unsigned int w = a < b ? a : b;
			size_t l;

			w = w < c ? w : c;	/* Zipf-ish skew */
			/* Repeat an earlier phrase of this page now and then. */
			if ((r & 3) == 0 && p - start > 128) {
				size_t back = 1 + (r >> 32) % (p - start - 1);
				size_t k;

				l = 16 + (r >> 40) % 48;
				for (k = 0; k < l && p < end; k++, p++)
					*p = *(p - back);
				continue;
			}
			l = wlen[w];
			if ((size_t)(end - p) < l)
				l = end - p;
			memcpy(p, words[w], l);
			p += l;
			if (p < end && ((r >> 12) & 31) == 0) {
				unsigned int num = (r >> 20) % 10000;
				char nb[8];
				int k, n = 0;

				*p++ = ' ';
				do {
					nb[n++] = '0' + num % 10;
					num /= 10;
				} while (num);
				for (k = n - 1; k >= 0 && p < end; k--)
					*p++ = nb[k];
			}
			if (p < end)
				*p++ = (++nw % 12) ? ' ' : '\n';
		}
		break;
	}
	case T_REC: {
		struct rec *rc = (struct rec *)p;
		unsigned int n = (PG - sizeof(*h)) / sizeof(*rc);

		for (i = 0; i < n; i++) {
			uint64_t r = xs(&st);
			const char *nm = rnames[(r >> 13) & 7];

			memset(&rc[i], 0, sizeof(rc[i]));
			rc[i].id = idx * n + i;
			rc[i].ts = (uint32_t)seed + i * 1000 + (r & 15);
			rc[i].kind = (r >> 8) & 7;
			rc[i].flags = (r >> 11) & 3;
			memcpy(rc[i].name, nm, strlen(nm));
			rc[i].value = (r >> 16) & 0xff;
			rc[i].chk = (uint32_t)rc[i].id ^ 0x5a5a0000u;
		}
		p += n * sizeof(*rc);
		memset(p, 0, end - p);
		break;
	}
	case T_ALPHA: {
		unsigned int nsym = 4 + (unsigned int)((seed >> 7) % 13);

		while (p < end) {
			uint64_t r = xs(&st);
			int k;

			/* Short runs of a small alphabet. */
			for (k = 0; k < 9 && p < end; k++) {
				unsigned int v = (r >> (k * 7)) & 127;
				unsigned int run = 1 + (v & 3);
				char ch = 'a' + (v >> 2) % nsym;

				while (run-- && p < end)
					*p++ = ch;
			}
		}
		break;
	}
	case T_RAND:
		for (q = (uint64_t *)p; (uint8_t *)q < end; q++)
			*q = xs(&st);
		break;
	}
}

/* ------------------------------------------------------------------ */
/* regions                                                             */

struct region {
	const char *name;
	unsigned int id;
	uint8_t *base;
	size_t npages;
	uint8_t *gen;
};

static struct region *region_new(const char *name, unsigned int id,
				 unsigned long mb)
{
	struct region *r = calloc(1, sizeof(*r));
	size_t len = mb * PAGES_PER_MB * PG;
	uint8_t *m;

	r->name = name;
	r->id = id;
	r->npages = mb * PAGES_PER_MB;
	/* PROT_NONE fences keep this VMA from merging with a neighbour. */
	m = mmap(NULL, len + 2 * PG, PROT_NONE,
		 MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
	if (m == MAP_FAILED) {
		say("mmap %s %lu MB failed: %s", name, mb, strerror(errno));
		exit(2);
	}
	if (mprotect(m + PG, len, PROT_READ | PROT_WRITE)) {
		say("mprotect %s failed: %s", name, strerror(errno));
		exit(2);
	}
	r->base = m + PG;
	r->gen = calloc(r->npages, 1);
	if (!r->gen) {
		say("gen array %s failed", name);
		exit(2);
	}
	return r;
}

static void region_free(struct region *r)
{
	munmap(r->base - PG, r->npages * PG + 2 * PG);
	free(r->gen);
	free(r);
}

static struct region *all_regions[8];
static unsigned int nr_all_regions;

static struct region *region_add(const char *name, unsigned int id,
				 unsigned long mb)
{
	struct region *r = region_new(name, id, mb);

	all_regions[nr_all_regions++] = r;
	return r;
}

static void region_drop(struct region *r)
{
	unsigned int i;

	for (i = 0; i < nr_all_regions; i++) {
		if (all_regions[i] == r) {
			all_regions[i] = all_regions[--nr_all_regions];
			break;
		}
	}
	region_free(r);
}

static void pageout(struct region *r, size_t first, size_t n)
{
	if (first >= r->npages)
		return;
	if (first + n > r->npages)
		n = r->npages - first;
	madvise(r->base + first * PG, n * PG, MADV_PAGEOUT);
}

static void pageout_all(struct region *r)
{
	size_t i, step = 512; /* 2 MiB */

	for (i = 0; i < r->npages; i += step)
		pageout(r, i, step);
}

/* ------------------------------------------------------------------ */
/* verification                                                        */

static uint64_t g_mismatch;	/* total mismatching pages (atomic) */
static uint64_t g_reports;

static void hex16(char *out, const uint8_t *p, size_t n)
{
	size_t i;

	for (i = 0; i < n; i++)
		sprintf(out + 2 * i, "%02x", p[i]);
}

static void report_mismatch(struct region *r, size_t idx, unsigned int gen,
			    const uint8_t *pg, const uint8_t *exp,
			    const char *ctx)
{
	uint64_t n = __atomic_fetch_add(&g_reports, 1, __ATOMIC_RELAXED);
	uint8_t alt[PG] __attribute__((aligned(64)));
	char he[40], ha[40];
	size_t off = 0, ndiff = 0, i, len;
	int zero = 1;
	const struct page_hdr *h = (const struct page_hdr *)pg;
	const char *other = "";

	if (n >= p_max_report) {
		if (n == p_max_report)
			say("MISMATCH: further reports suppressed");
		return;
	}
	for (i = 0; i < PG; i++) {
		if (pg[i] != exp[i]) {
			if (!ndiff)
				off = i;
			ndiff++;
		}
		if (pg[i])
			zero = 0;
	}
	len = PG - off < 16 ? PG - off : 16;
	hex16(he, exp + off, len);
	hex16(ha, pg + off, len);
	gen_page(alt, r->id, idx, (gen - 1) & 0xff);
	if (!memcmp(alt, pg, PG))
		other = " [content == previous generation: stale]";
	gen_page(alt, r->id, idx, (gen + 1) & 0xff);
	if (!memcmp(alt, pg, PG))
		other = " [content == next generation]";
	say("MISMATCH %s: region %s page %zu gen %u type %s: %zu bytes "
	    "differ, first at %zu exp %s act %s%s%s", ctx, r->name, idx, gen,
	    tname[page_type(page_seed(r->id, idx, gen))], ndiff, off, he, ha,
	    zero ? " [actual all zero]" : "", other);
	if (h->magic == PAGE_MAGIC)
		say("MISMATCH   actual header: rid %u idx %lu gen %u", h->rid,
		    (unsigned long)h->idx, h->gen);
}

static int check_page(struct region *r, size_t idx, uint8_t *tmp,
		      const char *ctx)
{
	unsigned int gen = __atomic_load_n(&r->gen[idx], __ATOMIC_RELAXED);
	const uint8_t *pg = r->base + idx * PG;

	gen_page(tmp, r->id, idx, gen);
	if (!memcmp(pg, tmp, PG))
		return 0;
	__atomic_fetch_add(&g_mismatch, 1, __ATOMIC_RELAXED);
	report_mismatch(r, idx, gen, pg, tmp, ctx);
	return 1;
}

static void write_page(struct region *r, size_t idx, unsigned int gen)
{
	__atomic_store_n(&r->gen[idx], (uint8_t)gen, __ATOMIC_RELAXED);
	gen_page(r->base + idx * PG, r->id, idx, gen);
}

struct vjob {
	struct region *r;
	size_t lo, hi;
	uint64_t bad;
	const char *ctx;
};

static void *verify_thread(void *arg)
{
	struct vjob *j = arg;
	uint8_t *tmp = aligned_alloc(PG, PG);
	size_t i;

	for (i = j->lo; i < j->hi; i++)
		j->bad += check_page(j->r, i, tmp, j->ctx);
	free(tmp);
	return NULL;
}

static uint64_t verify_region(struct region *r, const char *ctx)
{
	unsigned int nt = p_verify_threads, t;
	pthread_t th[64];
	struct vjob job[64];
	uint64_t bad = 0;
	double ts = now_s();

	if (nt > 64)
		nt = 64;
	for (t = 0; t < nt; t++) {
		job[t].r = r;
		job[t].lo = r->npages * t / nt;
		job[t].hi = r->npages * (t + 1) / nt;
		job[t].bad = 0;
		job[t].ctx = ctx;
		pthread_create(&th[t], NULL, verify_thread, &job[t]);
	}
	for (t = 0; t < nt; t++) {
		pthread_join(th[t], NULL);
		bad += job[t].bad;
	}
	say("verify %s (%s): %zu pages, %lu mismatches, %.1fs", r->name, ctx,
	    r->npages, bad, now_s() - ts);
	return bad;
}

/* ------------------------------------------------------------------ */
/* workload helpers                                                    */

static void fill_region(struct region *r, unsigned long lag)
{
	size_t chunk = p_chunk_mb * PAGES_PER_MB, c, i;
	size_t nchunks = (r->npages + chunk - 1) / chunk;
	size_t report = 256 * PAGES_PER_MB / chunk;
	double ts = now_s();
	char label[64];

	if (!report)
		report = 1;
	for (c = 0; c < nchunks; c++) {
		size_t lo = c * chunk, hi = lo + chunk;

		if (hi > r->npages)
			hi = r->npages;
		for (i = lo; i < hi; i++)
			write_page(r, i, 0);
		if (lag && c >= lag)
			pageout(r, (c - lag) * chunk, chunk);
		if (p_pace_us)
			usleep(p_pace_us);
		if ((c + 1) % report == 0 || c + 1 == nchunks) {
			snprintf(label, sizeof(label), "fill %s %zuM",
				 r->name, hi / PAGES_PER_MB);
			short_snap(label);
		}
	}
	say("fill %s: %zu MB in %.1fs", r->name, r->npages / PAGES_PER_MB,
	    now_s() - ts);
}

/* Sweep a region reading every page: refaults it and creates pressure. */
static uint64_t sweep_region(struct region *r, const char *ctx)
{
	uint8_t *tmp = aligned_alloc(PG, PG);
	uint64_t bad = 0;
	size_t i;

	for (i = 0; i < r->npages; i++)
		bad += check_page(r, i, tmp, ctx);
	free(tmp);
	return bad;
}

static uint64_t wait_drain(const char *label, unsigned long ms)
{
	struct snap a, b;

	take_snap(&a);
	msleep(ms);
	take_snap(&b);
	say("[%s] drain wait %lums: +%lu payloads written back, swapped "
	    "entries %lu -> %lu, pool %.1fM -> %.1fM", label, ms,
	    b.wb_payloads - a.wb_payloads, a.sw_entries, b.sw_entries,
	    MB(a.pool), MB(b.pool));
	return b.wb_payloads - a.wb_payloads;
}

static uint64_t rng_state = 88172645463325252ULL;

static uint64_t rnd(void)
{
	return xs(&rng_state);
}

/* ------------------------------------------------------------------ */
/* result bookkeeping                                                  */

static int g_fail;
static unsigned int g_skips;
static pthread_mutex_t failure_lock = PTHREAD_MUTEX_INITIALIZER;
static char g_fail_reasons[2048];

static void fail(const char *fmt, ...)
{
	va_list ap;
	size_t l;
	char msg[256];

	pthread_mutex_lock(&failure_lock);
	l = strlen(g_fail_reasons);
	va_start(ap, fmt);
	vsnprintf(msg, sizeof(msg), fmt, ap);
	va_end(ap);
	say("FAIL: %s", msg);
	g_fail = 1;
	if (l < sizeof(g_fail_reasons) - 2)
		snprintf(g_fail_reasons + l, sizeof(g_fail_reasons) - l,
			 "%s%s", l ? "; " : "", msg);
	pthread_mutex_unlock(&failure_lock);
}

static void coverage_skip(const char *what, const char *why)
{
	g_skips++;
	say("COVERAGE: SKIP %s: %s", what, why);
}

static void coverage_expected_skip(const char *what, const char *why)
{
	say("COVERAGE: SKIP (expected) %s: %s", what, why);
}

static bool check_soft_dirty(const char *what)
{
	/* The arm64 guest kernel lacks HAVE_ARCH_SOFT_DIRTY. Keep x86 strict. */
#if defined(__aarch64__)
	coverage_skip(what, "arm64 guest kernel lacks soft-dirty PTE support");
	return false;
#else
	(void)what;
	return true;
#endif
}

static void header(const char *name)
{
	say("");
	say("== test %s", name);
}

/* ------------------------------------------------------------------ */
/* UFFDIO_MOVE of a 64K mTHP that reclaim unmapped in part            */

#define MTHP_SZ		(p_mthp_kb << 10)
#define UFFD_REGION	(1UL << 20)

static int mthp_set(const char *mode)
{
	char path[128];
	int fd;

	snprintf(path, sizeof(path), "/sys/kernel/mm/transparent_hugepage/"
		 "hugepages-%lukB/enabled", p_mthp_kb);
	fd = open(path, O_WRONLY);
	int r;

	if (fd < 0)
		return -1;
	r = write(fd, mode, strlen(mode)) == (ssize_t)strlen(mode) ? 0 : -1;
	close(fd);
	return r;
}

static uint64_t pagemap_entry(int fd, void *addr)
{
	uint64_t e = 0;

	if (pread(fd, &e, sizeof(e),
		  ((uintptr_t)addr / PG) * sizeof(e)) != sizeof(e)) {
		fail("pagemap: unreadable entry at %p", addr);
		return 0;
	}
	return e;
}

/*
 * Fill 64K folios with incompressible data and page them out: reclaim
 * unmaps each into resident tokens, fails to store it and keeps the span.
 * Then UFFDIO_MOVE the first page of each: the folio has to be split for
 * that, which used to fail with -EBUSY while the rest of its pages were
 * still tokens - and ART's GC aborts on -EBUSY.
 */
static int64_t mthp_stat(const char *name);

static void uffd_mthp_move_test(void)
{
	size_t npages = UFFD_REGION / 4096, i, folios = UFFD_REGION / MTHP_SZ;
	struct uffdio_api api = { .api = UFFD_API, .features = UFFD_FEATURE_MOVE };
	struct uffdio_register reg = { 0 };
	unsigned long tokens = 0, moved = 0, bad = 0, ebusy = 0;
	size_t pick[UFFD_REGION / MTHP_SZ];
	bool done[UFFD_REGION / MTHP_SZ];
	uint64_t *seeds = NULL;
	char *base, *src, *dst;
	char modepath[160], modebuf[256], saved[32] = "never";
	int uffd = -1, pm = -1;
	int64_t alloc0 = mthp_stat("anon_fault_alloc");

	snprintf(modepath, sizeof(modepath),
		 "/sys/kernel/mm/transparent_hugepage/hugepages-%lukB/enabled",
		 p_mthp_kb);
	if (read_file(modepath, modebuf, sizeof(modebuf))) {
		char *a = strchr(modebuf, '['), *b = a ? strchr(a, ']') : NULL;
		if (a && b && b - a - 1 < (long)sizeof(saved)) {
			memcpy(saved, a + 1, b - a - 1);
			saved[b - a - 1] = 0;
		}
	}

	header("uffd move of a partly reclaimed mTHP");
	if (mthp_set("always")) {
		coverage_skip("mTHP UFFD MOVE", "selected mTHP mode unavailable");
		return;
	}
	/* guard | src | guard | dst | guard, so nothing merges or spills */
	base = mmap(NULL, 3 * MTHP_SZ + 2 * UFFD_REGION + MTHP_SZ, PROT_NONE,
		    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (base == MAP_FAILED) {
		fail("uffd: mmap: %s", strerror(errno));
		goto out_mthp;
	}
	src = (char *)(((uintptr_t)base + 2 * MTHP_SZ - 1) & ~(MTHP_SZ - 1));
	dst = src + UFFD_REGION + MTHP_SZ;
	if (mprotect(src, UFFD_REGION, PROT_READ | PROT_WRITE) ||
	    mprotect(dst, UFFD_REGION, PROT_READ | PROT_WRITE)) {
		fail("uffd: mprotect: %s", strerror(errno));
		goto out_unmap;
	}
	seeds = calloc(npages, sizeof(*seeds));
	pm = open("/proc/self/pagemap", O_RDONLY);
	if (!seeds || pm < 0) {
		fail("uffd: setup: %s", strerror(errno));
		goto out_close;
	}
	for (i = 0; i < npages; i++) {
		uint64_t *w = (uint64_t *)(src + i * 4096), x;
		size_t j;

		seeds[i] = x = mix64(p_seed ^ (0x75666664ULL << 32) ^ i) | 1;
		for (j = 0; j < 4096 / 8; j++) {
			x ^= x << 13;
			x ^= x >> 7;
			x ^= x << 17;
			w[j] = x;
		}
	}
	say("uffd: requested=%lukB anon_fault_alloc_delta=%" PRId64,
	    p_mthp_kb, mthp_stat("anon_fault_alloc") - alloc0);
	if (alloc0 < 0 || mthp_stat("anon_fault_alloc") <= alloc0)
		coverage_skip("mTHP allocation", "no selected-size allocation witness");
	if (madvise(src, UFFD_REGION, MADV_PAGEOUT)) {
		fail("uffd: MADV_PAGEOUT: %s", strerror(errno));
		goto out_close;
	}
	for (i = 0; i < npages; i++)
		if ((pagemap_entry(pm, src + i * 4096) >> 62) & 1)
			tokens++;
	say("uffd: %lu of %zu pages unmapped by MADV_PAGEOUT", tokens, npages);

	/*
	 * Move the first page of half of the folios and the sixth of the
	 * rest, so that the folio's first page is still a token for those.
	 * Of each half, fault the page back from its resident token first
	 * for every other folio, leaving the folio partly mapped; the others
	 * are moved from the token itself.
	 */
	for (i = 0; i < folios; i++) {
		pick[i] = (i & 2) ? ((MTHP_SZ / PG > 5) ? 5 : 3) : 0;
		done[i] = false;
		if (!(i & 1))
			(void)*(volatile char *)(src + i * MTHP_SZ +
						 pick[i] * 4096);
	}

	uffd = syscall(__NR_userfaultfd, O_CLOEXEC | O_NONBLOCK);
	if (uffd < 0) {
		if (errno == ENOSYS || errno == EPERM || errno == EACCES)
			coverage_skip("mTHP UFFD MOVE", strerror(errno));
		else
			fail("uffd: userfaultfd: %s", strerror(errno));
		goto out_close;
	}
	if (ioctl(uffd, UFFDIO_API, &api)) {
		if (errno == EINVAL || errno == ENOTTY || errno == ENOSYS)
			coverage_skip("mTHP UFFD MOVE feature", strerror(errno));
		else
			fail("uffd: UFFDIO_API: %s", strerror(errno));
		goto out_uffd;
	}
	if (!(api.features & UFFD_FEATURE_MOVE)) {
		coverage_skip("mTHP UFFD MOVE", "feature not negotiated");
		goto out_uffd;
	}
	reg.range.start = (uintptr_t)dst;
	reg.range.len = UFFD_REGION;
	reg.mode = UFFDIO_REGISTER_MODE_MISSING;
	if (ioctl(uffd, UFFDIO_REGISTER, &reg)) {
		fail("uffd: UFFDIO_REGISTER: %s", strerror(errno));
		goto out_uffd;
	}
	if (!(reg.ioctls & (UINT64_C(1) << _UFFDIO_MOVE))) {
		coverage_skip("mTHP UFFD MOVE", "range ioctl unsupported");
		goto out_uffd;
	}
	for (i = 0; i < folios; i++) {
		struct uffdio_move mv = {
			.dst = (uintptr_t)(dst + i * MTHP_SZ + pick[i] * 4096),
			.src = (uintptr_t)(src + i * MTHP_SZ + pick[i] * 4096),
			.len = 4096,
		};
		int r, tries = 0;

		do {
			r = ioctl(uffd, UFFDIO_MOVE, &mv);
		} while (r && errno == EAGAIN && ++tries < 100);
		if (!r && mv.move == 4096) {
			done[i] = true;
			moved++;
		} else if (errno == EBUSY) {
			say("uffd: folio %zu page %zu (%s): -EBUSY", i, pick[i],
			    (i & 1) ? "token" : "mapped");
			ebusy++;
		} else {
			fail("uffd: UFFDIO_MOVE of folio %zu page %zu: %s", i,
			     pick[i], strerror(errno));
			break;
		}
	}
	say("uffd: moved %lu of %zu, -EBUSY %lu", moved, folios, ebusy);
	if (moved != folios)
		fail("uffd: incomplete MOVE coverage: %lu/%zu", moved, folios);
	if (ebusy)
		fail("uffd: UFFDIO_MOVE returned -EBUSY for %lu of %zu "
		     "partly reclaimed mTHPs", ebusy, folios);

	/* Every page reads back what was written, moved or not. */
	for (i = 0; i < npages; i++) {
		size_t f = i / (MTHP_SZ / 4096), off = i % (MTHP_SZ / 4096);
		char *p = src + i * 4096;
		uint64_t x = seeds[i], *w;
		size_t j;

		if (done[f] && off == pick[f])
			p = dst + i * 4096;
		w = (uint64_t *)p;
		for (j = 0; j < 4096 / 8; j++) {
			x ^= x << 13;
			x ^= x >> 7;
			x ^= x << 17;
			if (w[j] != x) {
				bad++;
				break;
			}
		}
	}
	if (bad)
		fail("uffd: %lu pages read back wrong", bad);
	else
		say("uffd: all %zu pages intact", npages);
out_uffd:
	if (uffd >= 0)
		close(uffd);
out_close:
	if (pm >= 0)
		close(pm);
	free(seeds);
out_unmap:
	munmap(base, 3 * MTHP_SZ + 2 * UFFD_REGION + MTHP_SZ);
out_mthp:
	if (mthp_set(saved)) fail("uffd: restore mTHP mode: %s", strerror(errno));
}

/* ------------------------------------------------------------------ */
/* failed-store rollback and marked-folio splitting                    */

/*
 * The rollback counters of events and execution_events, printed at
 * the end of each phase.  Gauges are signed.
 */
static const char *const rstat_ev_keys[] = {
	"folio_owner_pages", "pending_pages",
	"pageout_store_ok", "pageout_store_fail",
	NULL,
};

static const char *const rstat_ex_keys[] = {
	"restore_spans", "restore_ptes", "restore_missed_pages",
	"restore_rewalks", "restore_ns", "restore_max_ns",
	"split_refused", "split_refused_pages",
	NULL,
};

static void rstat_line(const char *label, const char *path,
		       const char *const *keys)
{
	char line[1024];
	unsigned int i, n = 0;
	size_t l = 0;

	read_file(path, sbuf, sizeof(sbuf));
	for (i = 0; keys[i]; i++) {
		l += snprintf(line + l, sizeof(line) - l, " %s=%" PRId64,
			      keys[i], (int64_t)kv(sbuf, keys[i]));
		if (++n == 5 || !keys[i + 1]) {
			say("[%s]%s", label, line);
			l = 0;
			n = 0;
		}
	}
}

static void rstat(const char *label)
{
	rstat_line(label, MCDIR "events", rstat_ev_keys);
	rstat_line(label, MCDIR "execution_events", rstat_ex_keys);
}

static int64_t rget(const char *path, const char *key)
{
	read_file(path, sbuf, sizeof(sbuf));
	return (int64_t)kv(sbuf, key);
}

static void write_str(const char *path, const char *s);

#define EV(k)	rget(MCDIR "events", k)
#define EX(k)	rget(MCDIR "execution_events", k)

#ifndef MPOL_MF_MOVE
#define MPOL_MF_MOVE	(1 << 1)
#endif

struct rt {
	char *p;
	size_t npages;
	size_t seed_base;
	pthread_barrier_t bar;
	volatile int stop;
	volatile int fstop;	/* faulters loop until set, if floop */
	int floop;
	uint64_t smaps_reads, pagemap_reads;
};

static uint64_t rt_seed(size_t i)
{
	return mix64(p_seed ^ (0x72746e64ULL << 32) ^ i) | 1;
}

/* Incompressible, so every store of it fails and the span is retained. */
static void rt_fill(char *p, uint64_t x)
{
	uint64_t *w = (uint64_t *)p;
	size_t j;

	for (j = 0; j < PG / 8; j++) {
		x ^= x << 13;
		x ^= x >> 7;
		x ^= x << 17;
		w[j] = x;
	}
}

static int rt_bad(const char *p, uint64_t x)
{
	const uint64_t *w = (const uint64_t *)p;
	size_t j;

	for (j = 0; j < PG / 8; j++) {
		x ^= x << 13;
		x ^= x >> 7;
		x ^= x << 17;
		if (w[j] != x)
			return 1;
	}
	return 0;
}

static uint64_t rt_verify(struct rt *r, size_t first, size_t n)
{
	uint64_t bad = 0;
	size_t i;

	for (i = first; i < first + n; i++)
		bad += rt_bad(r->p + i * PG, rt_seed(r->seed_base + i));
	return bad;
}

/*
 * Every thread faults every page, in the same order, all at once; with
 * floop set, over and over until fstop, so that faults race the pageout.
 */
static void *rt_faulter(void *arg)
{
	struct rt *r = arg;
	size_t i;

	pthread_barrier_wait(&r->bar);
	do {
		for (i = 0; i < r->npages; i++)
			(void)*(volatile char *)(r->p + i * PG);
	} while (r->floop && !r->fstop);
	return NULL;
}

/* smaps looks up every token PTE it walks. */
static void *rt_smaps(void *arg)
{
	struct rt *r = arg;
	char *buf = malloc(1 << 16);
	int fd;

	while (buf && !r->stop) {
		fd = open("/proc/self/smaps", O_RDONLY);
		if (fd < 0)
			break;
		while (read(fd, buf, 1 << 16) > 0)
			;
		close(fd);
		__atomic_fetch_add(&r->smaps_reads, 1, __ATOMIC_RELAXED);
	}
	free(buf);
	return NULL;
}

static void *rt_pagemap(void *arg)
{
	struct rt *r = arg;
	uint64_t *ents = malloc(r->npages * sizeof(*ents));
	int fd = open("/proc/self/pagemap", O_RDONLY);

	while (ents && fd >= 0 && !r->stop) {
		if (pread(fd, ents, r->npages * sizeof(*ents),
			  ((uintptr_t)r->p / PG) * sizeof(*ents)) < 0)
			break;
		__atomic_fetch_add(&r->pagemap_reads, 1, __ATOMIC_RELAXED);
	}
	if (fd >= 0)
		close(fd);
	free(ents);
	return NULL;
}

static size_t rt_tokens(int pm, char *p, size_t n)
{
	size_t i, t = 0;

	for (i = 0; i < n; i++)
		if ((pagemap_entry(pm, p + i * PG) >> 62) & 1)
			t++;
	return t;
}

static bool rt_folio_mapped(int pm, char *p, size_t n)
{
	size_t i;

	for (i = 0; i < n; i++) {
		uint64_t entry = pagemap_entry(pm, p + i * PG);

		if (!((entry >> 63) & 1) || ((entry >> 62) & 1))
			return false;
	}
	return true;
}

static void mthp_stats(const char *label)
{
	static const char *const names[] = {
		"nr_anon", "nr_anon_partially_mapped", "split", "split_failed",
		"split_deferred", "anon_fault_alloc", "swpout",
		"swpout_fallback", NULL,
	};
	char path[128], buf[32], line[512];
	size_t l = 0;
	unsigned int i;

	for (i = 0; names[i]; i++) {
		snprintf(path, sizeof(path), "/sys/kernel/mm/transparent_hugepage/"
			 "hugepages-%lukB/stats/%s", p_mthp_kb, names[i]);
		read_file(path, buf, sizeof(buf));
		buf[strcspn(buf, "\n")] = 0;
		l += snprintf(line + l, sizeof(line) - l, " %s=%s", names[i],
			      buf);
	}
	say("[%s] %luK mTHP:%s", label, p_mthp_kb, line);
}

static int64_t mthp_stat(const char *name)
{
	char path[128], buf[32];

	snprintf(path, sizeof(path), "/sys/kernel/mm/transparent_hugepage/"
		 "hugepages-%lukB/stats/%s", p_mthp_kb, name);
	if (!read_file(path, buf, sizeof(buf)))
		return -1;
	return strtoll(buf, NULL, 10);
}

static void rt_split_delta(const char *what, int64_t *last)
{
	int64_t now = EX("split_refused");

	say("retain: %s: split_refused +%" PRId64,
	    what, now - last[0]);
	last[0] = now;
	mthp_stats(what);
}

/*
 * R2: split retained 64K mTHPs whose first page faulted back, from each
 * caller we can reach from userspace, and see who gets refused.  A fresh
 * region, so that none of its folios was split before.
 *
 * Rollback no longer keeps a span, so each failed store leaves the folio
 * mapped and marked incompressible in folio->swap, and nothing may refuse
 * to split it: the mark is not a reservation.  Fail if the folios did not
 * get the mark, if any split is refused, or if the MADV_PAGEOUT and
 * debugfs splits did not split every folio they aimed at.
 */
static void rt_split_callers(int pm, size_t folios)
{
	size_t npages = folios * (MTHP_SZ / PG), i, n = 0, tokens;
	size_t folio_pages = MTHP_SZ / PG;
	int64_t last[1] = { EX("split_refused") };
	int64_t refused0 = last[0], marked0 = EV("incompressible_marked_pages");
	int64_t fail0 = EV("pageout_store_fail");
	int64_t split0, marked, failed;
	size_t aimed = 0;
	void **pages = calloc(folios, sizeof(*pages));
	int *nodes = calloc(folios, sizeof(*nodes));
	int *status = calloc(folios, sizeof(*status));
	struct rt r = { .npages = npages, .seed_base = 1UL << 24 };
	char cmd[128], *base;
	uint64_t bad;
	long ret;
	int fd;

	header("retained mTHP splits");
	base = mmap(NULL, npages * PG + 3 * MTHP_SZ, PROT_NONE,
		    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (!pages || !nodes || !status || base == MAP_FAILED) {
		fail("split: setup: %s", strerror(errno));
		goto out;
	}
	r.p = (char *)(((uintptr_t)base + 2 * MTHP_SZ - 1) & ~(MTHP_SZ - 1));
	if (mprotect(r.p, npages * PG, PROT_READ | PROT_WRITE) ||
	    mthp_set("always")) {
		fail("split: mprotect/mTHP: %s", strerror(errno));
		goto out_unmap;
	}
	if (!access("/sys/devices/system/node/node1", F_OK)) {
		unsigned long mask = 1;
		if (syscall(__NR_mbind, r.p, npages * PG, 2, &mask,
			    sizeof(mask) * 8, 0)) {
			fail("split: bind source to node0: %s", strerror(errno));
			goto out_unmap;
		}
	}
	for (i = 0; i < npages; i++)
		rt_fill(r.p + i * PG, rt_seed(r.seed_base + i));
	mthp_set("never");
	mthp_stats("split filled");
	madvise(r.p, npages * PG, MADV_PAGEOUT);
	msleep(100);
	failed = EV("pageout_store_fail") - fail0;
	marked = EV("incompressible_marked_pages") - marked0;
	if (failed < (int64_t)npages)
		fail("split: only %" PRId64 " of %zu pages recorded store failure",
		     failed, npages);
	if (marked < (int64_t)npages)
		fail("split: %" PRId64 " of %zu pages marked incompressible, "
		     "so the splits do not test the mark", marked, npages);
	tokens = rt_tokens(pm, r.p, npages);
	for (i = 0; i < folios; i++)
		(void)*(volatile char *)(r.p + i * MTHP_SZ);
	msleep(100);
	say("split: %zu of %zu pages were tokens, first page of each of %zu "
	    "folios faulted back, %zu tokens left", tokens, npages, folios,
	    rt_tokens(pm, r.p, npages));
	mthp_stats("split faulted");
	split0 = mthp_stat("split");

	/*
	 * Move still-retained, marked folios before any explicit split request.
	 * Fault every page in each witness folio back first, then require the
	 * pagemap to show ordinary present PTEs rather than retained tokens.
	 * This makes the witness order: store failure -> restore/refault ->
	 * retained mark -> move_pages(2).
	 */
	if (access("/sys/devices/system/node/node1", F_OK)) {
		coverage_expected_skip("marked mTHP NUMA move_pages", "single-node guest");
		if (envu("EXPECT_NUMA", 0))
			fail("split: EXPECT_NUMA=1 but node1 unavailable");
	} else {
		int hist[6] = { 0 };

		for (i = 2; i < folios; i += 5) {
			size_t j;

			for (j = 0; j < folio_pages; j++)
				(void)*(volatile char *)(r.p + i * MTHP_SZ + j * PG);
			if (!rt_folio_mapped(pm, r.p + i * MTHP_SZ, folio_pages))
				fail("split: witness folio %zu was not fully mapped back", i);
			pages[n] = r.p + i * MTHP_SZ;
			nodes[n] = 1;
			status[n] = -1000;
			n++;
		}
		if (!n)
			fail("split: no marked mTHP witness folios");
		ret = syscall(__NR_move_pages, 0, n, pages, NULL, status, 0);
		for (i = 0; i < n; i++)
			if (status[i] != 0)
				fail("split: migration source[%zu] is node/error %d, not node0",
				     i, status[i]);
		if (ret)
			fail("split: source query failed: %s", strerror(errno));
		ret = syscall(__NR_move_pages, 0, n, pages, nodes, status,
			      MPOL_MF_MOVE);
		for (i = 0; i < n; i++) {
			if (status[i] == 1)
				hist[0]++;
			else if (status[i] == 0)
				hist[1]++;
			else if (status[i] == -EBUSY)
				hist[2]++;
			else if (status[i] == -EAGAIN)
				hist[3]++;
			else if (status[i] == -ENOMEM)
				hist[4]++;
			else
				hist[5]++;
		}
		say("split: move_pages of %zu: ret=%ld (%s) node1=%d node0=%d "
		    "-EBUSY=%d -EAGAIN=%d -ENOMEM=%d other=%d", n, ret,
		    ret < 0 ? strerror(errno) : "ok", hist[0], hist[1],
		    hist[2], hist[3], hist[4], hist[5]);
		if (ret || hist[0] != (int)n)
			fail("split: not all %zu marked folios migrated to node1", n);
		ret = syscall(__NR_move_pages, 0, n, pages, NULL, status, 0);
		if (ret)
			fail("split: destination query failed");
		for (i = 0; i < n; i++)
			if (status[i] != 1)
				fail("split: destination[%zu] is not node1: %d", i, status[i]);
		if (EX("split_refused") != refused0)
			fail("split: move_pages changed split_refused by %" PRId64,
			     EX("split_refused") - refused0);
	}
	rt_split_delta("move_pages", last);

	/* madvise: a range that does not cover the folio splits it. */
	for (i = 0; i < folios; i += 5, aimed++)
		madvise(r.p + i * MTHP_SZ, PG, MADV_PAGEOUT);
	rt_split_delta("MADV_PAGEOUT of one page", last);

	/* The debugfs knob splits what it finds mapped in the range. */
	mount("debugfs", "/sys/kernel/debug", "debugfs", 0, NULL);
	fd = open("/sys/kernel/debug/split_huge_pages", O_WRONLY);
	if (fd < 0) {
		coverage_skip("debugfs marked mTHP split", strerror(errno));
	} else {
		for (i = 1; i < folios; i += 5, aimed++) {
			int l = snprintf(cmd, sizeof(cmd), "%d,%#lx,%#lx",
					 getpid(),
					 (unsigned long)(r.p + i * MTHP_SZ),
					 (unsigned long)(r.p + (i + 1) *
							 MTHP_SZ));

			if (write(fd, cmd, l) < 0)
				say("split: split_huge_pages: %s",
				    strerror(errno));
		}
		close(fd);
	}
	rt_split_delta("debugfs split_huge_pages", last);
	if (fd >= 0 && mthp_stat("split") - split0 < (int64_t)aimed)
		fail("split: %" PRId64 " of the %zu marked folios aimed at "
		     "were split", mthp_stat("split") - split0, aimed);

	/* The deferred split queue has the partly mapped ones. */
	write_str("/proc/sys/vm/drop_caches", "2");
	rt_split_delta("drop_caches=2 (deferred split shrinker)", last);
	write_str("/proc/sys/vm/compact_memory", "1");
	rt_split_delta("compact_memory", last);
	if (EX("split_refused") != refused0)
		fail("split: %" PRId64 " splits of marked folios refused",
		     EX("split_refused") - refused0);
	bad = rt_verify(&r, 0, npages);
	if (bad)
		fail("split: %" PRIu64 " pages read back wrong", bad);
	else
		say("split: all %zu pages intact", npages);
	rstat("after split");
out_unmap:
	mthp_set("never");
	if (base != MAP_FAILED)
		munmap(base, npages * PG + 3 * MTHP_SZ);
out:
	free(pages);
	free(nodes);
	free(status);
}

static void thp_top(char *save, size_t sz, const char *set)
{
	char buf[128], *a, *b;

	if (save) {
		read_file("/sys/kernel/mm/transparent_hugepage/enabled", buf,
			  sizeof(buf));
		a = strchr(buf, '[');
		b = a ? strchr(a, ']') : NULL;
		if (a && b && (size_t)(b - a) < sz) {
			memcpy(save, a + 1, b - a - 1);
			save[b - a - 1] = 0;
		} else {
			snprintf(save, sz, "always");
		}
	}
	write_str("/sys/kernel/mm/transparent_hugepage/enabled", set);
}

/*
 * Page out incompressible memory, half 64K mTHPs and half small folios,
 * so that every store fails and reclaim keeps the span, then have several
 * threads fault the same pages at once while smaps and pagemap look the
 * tokens up.  The counters say who kept each span, as named by a PTE or
 * only by a lookup, and how it went; a span a lookup kept with every page
 * mapped again is one nothing queues for the reaper any more.
 */
static void retain_test(void)
{
	size_t npages = p_retain_mb * PAGES_PER_MB, mthp_pages, i, j, tokens;
	unsigned int T = p_retain_threads, t;
	int64_t owner0, pending0;
	pthread_t th[64], sm, pmt;
	char thp_saved[32];
	struct rt r;
	uint64_t bad;
	char *base;
	bool mthp;
	int pm;

	header("retained spans");
	memset(&r, 0, sizeof(r));
	owner0 = EV("folio_owner_pages");
	pending0 = EV("pending_pages");
	thp_top(thp_saved, sizeof(thp_saved), "never");
	mthp = !mthp_set("always");
	npages &= ~(MTHP_SZ / PG - 1);
	mthp_pages = mthp ? npages / 2 : 0;
	mthp_pages &= ~(MTHP_SZ / PG - 1);
	base = mmap(NULL, npages * PG + 3 * MTHP_SZ, PROT_NONE,
		    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (base == MAP_FAILED) {
		fail("retain: mmap: %s", strerror(errno));
		goto out_thp;
	}
	r.p = (char *)(((uintptr_t)base + 2 * MTHP_SZ - 1) & ~(MTHP_SZ - 1));
	r.npages = npages;
	pm = open("/proc/self/pagemap", O_RDONLY);
	if (pm < 0 || mprotect(r.p, npages * PG, PROT_READ | PROT_WRITE)) {
		fail("retain: setup: %s", strerror(errno));
		goto out_unmap;
	}
	for (i = 0; i < npages; i++) {
		if (i == mthp_pages && mthp)
			mthp_set("never");
		rt_fill(r.p + i * PG, rt_seed(i));
	}
	mthp_set("never");
	say("retain: %zu pages, %zu requested mTHP pages, %u faulting threads, "
	    "%lu rounds, readers %s", npages, mthp_pages, T, p_retain_rounds,
	    p_retain_readers ? "on" : "off");
	rstat("retain start");
	mthp_stats("retain start");

	if (p_retain_readers) {
		pthread_create(&sm, NULL, rt_smaps, &r);
		pthread_create(&pmt, NULL, rt_pagemap, &r);
	}
	for (i = 0; i < p_retain_rounds; i++) {
		madvise(r.p, npages * PG, MADV_PAGEOUT);
		tokens = rt_tokens(pm, r.p, npages);
		pthread_barrier_init(&r.bar, NULL, T);
		for (t = 0; t < T; t++)
			pthread_create(&th[t], NULL, rt_faulter, &r);
		for (t = 0; t < T; t++)
			pthread_join(th[t], NULL);
		pthread_barrier_destroy(&r.bar);
		msleep(100);
		/*
		 * Then with the faults racing the pageout, so that a fault
		 * holds a lookup of a token while reclaim rolls its span back.
		 * New contents first: the old ones are marked incompressible
		 * now, and reclaim would not try to store them again.
		 */
		r.seed_base = (i + 1) << 20;
		for (j = 0; j < npages; j++)
			rt_fill(r.p + j * PG, rt_seed(r.seed_base + j));
		r.floop = 1;
		r.fstop = 0;
		pthread_barrier_init(&r.bar, NULL, T + 1);
		for (t = 0; t < T; t++)
			pthread_create(&th[t], NULL, rt_faulter, &r);
		pthread_barrier_wait(&r.bar);
		for (t = 0; t < 4; t++)
			madvise(r.p, npages * PG, MADV_PAGEOUT);
		r.fstop = 1;
		for (t = 0; t < T; t++)
			pthread_join(th[t], NULL);
		pthread_barrier_destroy(&r.bar);
		r.floop = 0;
		msleep(100);
		say("retain: round %zu: %zu tokens after pageout, %zu after "
		    "faults; pending=%" PRId64, i, tokens,
		    rt_tokens(pm, r.p, npages), EV("pending_pages") - pending0);
	}
	r.stop = 1;
	if (p_retain_readers) {
		pthread_join(sm, NULL);
		pthread_join(pmt, NULL);
	}
	say("retain: %" PRIu64 " smaps reads, %" PRIu64 " pagemap reads",
	    r.smaps_reads, r.pagemap_reads);
	bad = rt_verify(&r, 0, npages);
	if (bad)
		fail("retain: %" PRIu64 " pages read back wrong", bad);
	msleep(500);
	say("retain: with every page mapped: pending %+" PRId64
	    ", owner %+" PRId64, EV("pending_pages") - pending0,
	    EV("folio_owner_pages") - owner0);
	rstat("retain after rounds");
	/*
	 * R1: rollback drops the span whatever references remain, so with
	 * every page mapped nothing may still own or pend for these folios.
	 */
	if (EV("pending_pages") - pending0 > (int64_t)p_owner_tol ||
	    EV("folio_owner_pages") - owner0 > (int64_t)p_owner_tol)
		fail("retain: spans kept with every page mapped: pending %+"
		     PRId64 ", owner %+" PRId64, EV("pending_pages") - pending0,
		     EV("folio_owner_pages") - owner0);

out_unmap:
	munmap(base, npages * PG + 3 * MTHP_SZ);
	msleep(1000);
	say("retain: after munmap: pending %+" PRId64 ", owner %+" PRId64,
	    EV("pending_pages") - pending0, EV("folio_owner_pages") - owner0);
	if (EV("pending_pages") - pending0 > (int64_t)p_owner_tol ||
	    EV("folio_owner_pages") - owner0 > (int64_t)p_owner_tol)
		fail("retain: owner/pending residue after munmap");
	rstat("retain after munmap");
	if (mthp_pages && pm >= 0)
		rt_split_callers(pm, mthp_pages / (MTHP_SZ / PG));
	if (pm >= 0)
		close(pm);
out_thp:
	mthp_set("never");
	thp_top(NULL, 0, thp_saved);
}

/* ------------------------------------------------------------------ */
/* concurrency test                                                    */

struct conc {
	struct region *a, *b;
	size_t half;
	size_t *list;
	unsigned long k;
	unsigned int nthreads;
	pthread_barrier_t bar;
	volatile int stop;
	double deadline;
	uint64_t rounds, reads, writes, pageouts, sweeps;
};

struct cthr {
	struct conc *c;
	unsigned int t;
	uint64_t bad;
};

static void *conc_reader(void *arg)
{
	struct cthr *me = arg;
	struct conc *c = me->c;
	uint8_t *tmp = aligned_alloc(PG, PG);
	uint64_t st = mix64(p_seed ^ (0x1000 + me->t)) | 1;
	unsigned int T = c->nthreads;

	for (;;) {
		unsigned long i, start;
		int w;

		if (me->t == 0) {
			/* leader: publish the next list of shared pages */
			if (now_s() > c->deadline)
				c->stop = 1;
			for (i = 0; i < c->k; i++)
				__atomic_store_n(&c->list[i],
						 xs(&st) % c->half,
						 __ATOMIC_RELAXED);
			c->rounds++;
		}
		pthread_barrier_wait(&c->bar);
		if (c->stop)
			break;
		/* Everybody refaults the same pages, from different starts. */
		start = (c->k * me->t) / T;
		for (i = 0; i < c->k; i++) {
			unsigned long j = (me->t & 1) ?
				(start + c->k - i) % c->k : (start + i) % c->k;

			me->bad += check_page(c->a, c->list[j], tmp, "conc-read");
		}
		__atomic_fetch_add(&c->reads, c->k, __ATOMIC_RELAXED);
		/* Then re-dirty some pages of the thread's own stripe. */
		for (w = 0; w < 16; w++) {
			size_t n2 = (c->a->npages - c->half) / T;
			size_t idx;

			if (!n2)
				break;
			idx = c->half + (xs(&st) % n2) * T + me->t;
			if (idx >= c->a->npages)
				continue;
			me->bad += check_page(c->a, idx, tmp, "conc-own");
			write_page(c->a, idx, c->a->gen[idx] + 1);
		}
		__atomic_fetch_add(&c->writes, 16, __ATOMIC_RELAXED);
		pthread_barrier_wait(&c->bar);
	}
	free(tmp);
	return NULL;
}

static void *conc_pageout(void *arg)
{
	struct cthr *me = arg;
	struct conc *c = me->c;
	uint64_t st = mix64(p_seed ^ 0x2000) | 1;

	while (!c->stop) {
		unsigned long i;
		int n;

		/* The pages the readers are about to fight over... */
		for (i = 0; i < c->k && !c->stop; i++) {
			size_t idx = __atomic_load_n(&c->list[i],
						     __ATOMIC_RELAXED);

			pageout(c->a, idx, 1);
		}
		/* ...and random 256 KiB chunks anywhere in the region. */
		for (n = 0; n < 8; n++)
			pageout(c->a, (xs(&st) % c->a->npages) & ~63UL, 64);
		__atomic_fetch_add(&c->pageouts, 1, __ATOMIC_RELAXED);
	}
	return NULL;
}

static void *conc_sweeper(void *arg)
{
	struct cthr *me = arg;
	struct conc *c = me->c;
	uint8_t *tmp = aligned_alloc(PG, PG);
	size_t i;

	while (!c->stop) {
		for (i = 0; i < c->b->npages && !c->stop; i++)
			me->bad += check_page(c->b, i, tmp, "conc-sweep");
		/* Push what was just faulted back so the next sweep refaults. */
		pageout_all(c->b);
		c->sweeps++;
	}
	free(tmp);
	return NULL;
}

static uint64_t conc_test(struct region *a, struct region *b)
{
	struct conc c;
	unsigned int T = p_conc_threads, t;
	pthread_t th[70];
	struct cthr ct[70];
	uint64_t bad = 0;

	if (T > 64)
		T = 64;
	memset(&c, 0, sizeof(c));
	c.a = a;
	c.b = b;
	c.half = a->npages / 2;
	c.k = p_conc_k;
	c.nthreads = T;
	c.list = calloc(c.k, sizeof(*c.list));
	c.deadline = now_s() + p_conc_secs;
	pthread_barrier_init(&c.bar, NULL, T);
	for (t = 0; t < T + 2; t++) {
		ct[t].c = &c;
		ct[t].t = t;
		ct[t].bad = 0;
	}
	for (t = 0; t < T; t++)
		pthread_create(&th[t], NULL, conc_reader, &ct[t]);
	pthread_create(&th[T], NULL, conc_pageout, &ct[T]);
	pthread_create(&th[T + 1], NULL, conc_sweeper, &ct[T + 1]);
	for (t = 0; t < T; t++)
		pthread_join(th[t], NULL);
	c.stop = 1;
	pthread_join(th[T], NULL);
	pthread_join(th[T + 1], NULL);
	for (t = 0; t < T + 2; t++)
		bad += ct[t].bad;
	say("conc: %u readers, %lu rounds, %lu shared refaults, %lu owned "
	    "rewrites, %lu pageout passes, %lu sweeps of %s, %lu mismatches",
	    T, c.rounds, c.reads, c.writes, c.pageouts, c.sweeps, b->name, bad);
	pthread_barrier_destroy(&c.bar);
	free(c.list);
	return bad;
}

/* ------------------------------------------------------------------ */
/* SIGBUS/SIGSEGV in the test child                                    */

static void fault_handler(int sig, siginfo_t *si, void *uc)
{
	char buf[256];
	unsigned int i;
	int n;

	(void)uc;
	n = snprintf(buf, sizeof(buf), "\n[mcwb] FATAL signal %d at %p\n",
		     sig, si->si_addr);
	{ ssize_t ignored = write(1, buf, n); (void)ignored; }
	for (i = 0; i < nr_all_regions; i++) {
		struct region *r = all_regions[i];
		uint8_t *a = si->si_addr;

		if (a >= r->base && a < r->base + r->npages * PG) {
			n = snprintf(buf, sizeof(buf),
				     "[mcwb] fault is in region %s page %lu\n",
				     r->name,
				     (unsigned long)((a - r->base) / PG));
			{ ssize_t ignored = write(1, buf, n); (void)ignored; }
		}
	}
	_exit(128 + sig);
}

/* ------------------------------------------------------------------ */
/* the tests                                                           */

#include "memcomp_compat.h"
#include "memcomp_mincore_writeback.h"
#include "memcomp_runtime.h"
#include "memcomp_churn.h"
#include "zero_token_test.h"

static void guest_ksm(void);
static void guest_selftest(void);
static void guest_hmm(void);

static int run_tests(void)
{
	struct snap s0, s1, s2;
	struct region *A, *B, *D, *E;
	uint64_t bad, wb_total;
	double ts;
	unsigned long c;
	struct sigaction sa;

	memset(&sa, 0, sizeof(sa));
	sa.sa_sigaction = fault_handler;
	sa.sa_flags = SA_SIGINFO;
	sigaction(SIGBUS, &sa, NULL);
	sigaction(SIGSEGV, &sa, NULL);
	read_file(MCDIR "events", sbuf, sizeof(sbuf));
	if (!strstr(sbuf, "folio_owner_pages ") ||
	    !strstr(sbuf, "pending_pages ") ||
	    !strstr(sbuf, "pageout_store_ok ")) {
		say("RESULT: FAIL missing required debug counters");
		return 1;
	}
	header("initial memcomp counters");
	dump_file(MCDIR "events");

	zero_token_test();
	rstat("after zero-token");
	guest_ksm();
	guest_selftest();
	guest_hmm();
	rng_state = mix64(p_seed) | 1;
	if (p_skip_uffd) coverage_skip("mTHP UFFD MOVE", "SKIP_UFFD=1");
	if (p_skip_retain) coverage_skip("rollback/marked mTHP", "SKIP_RETAIN=1");
	if (envu("SKIP_COMPAT", 0)) coverage_skip("compat", "SKIP_COMPAT=1");
	if (p_skip_cycles) coverage_skip("cycles", "SKIP_CYCLES=1");
	if (p_skip_conc) coverage_skip("concurrency", "SKIP_CONC=1");
	if (!p_no_swap && p_skip_swapoff) coverage_skip("swapoff", "SKIP_SWAPOFF=1");

	if (!p_skip_uffd) {
		uffd_mthp_move_test();
		rstat("after uffd");
	}
	if (!p_skip_retain)
		retain_test();
	churn_test();
	if (!envu("SKIP_COMPAT", 0)) {
		compat_test();
		runtime_compat_test();
	}

	take_snap(&s0);
	print_snap("baseline", &s0);

	/* ---------------------------------------------------------- */
	header("fill (writeback trigger)");
	say("A=%lu MB B=%lu MB chunk=%lu MB pageout_lag=%lu pace=%luus",
	    p_fill_mb, p_press_mb, p_chunk_mb, p_lag, p_pace_us);
	A = region_add("A", 1, p_fill_mb);
	B = region_add("B", 2, p_press_mb);
	fill_region(A, p_lag);
	fill_region(B, 0);
	take_snap(&s1);
	print_snap("after fill", &s1);
	/* Keep kswapd busy until the writeback counters move. */
	ts = now_s();
	for (c = 0; now_s() - ts < p_wb_wait_s; c++) {
		uint64_t moved;

		sweep_region(B, "fill-sweep-B");
		pageout_all(B);
		moved = wait_drain("fill", p_drain_ms);
		take_snap(&s1);
		if (s1.wb_payloads > s0.wb_payloads && !moved && c >= 1)
			break;
	}
	take_snap(&s1);
	print_snap("after fill+pressure", &s1);
	rstat("after fill+pressure");
	dump_file(MCDIR "packed_swap_events");
	wb_total = s1.wb_payloads - s0.wb_payloads;
	if (p_no_swap)
		coverage_expected_skip("writeback/refault/swapoff", "NO_SWAP=1; resident integrity only");
	else if (!wb_total)
		fail("no writeback: writeback_payloads did not move");
	else if (s1.swaptotal - s1.swapfree == 0)
		fail("writeback_payloads=%lu but swap used is 0", wb_total);
	else if (!s1.sw_entries)
		fail("writeback_payloads=%lu but swapped_entries is 0",
		     wb_total);
	else
		say("writeback OK: %lu payloads written back, %lu swapped "
		    "entries, swap used %.1fM, pool %.1fM", wb_total,
		    s1.sw_entries, KMB(s1.swaptotal - s1.swapfree),
		    MB(s1.pool));

	/* ---------------------------------------------------------- */
	compat_mincore_writeback(A, B);

	header("verify all");
	bad = verify_region(A, "verify-A");
	bad += verify_region(B, "verify-B");
	take_snap(&s2);
	print_snap("after verify", &s2);
	say("verify: swapped reads during verify=%lu read errors=%lu",
	    s2.sw_read_io - s1.sw_read_io, s2.sw_read_err - s1.sw_read_err);
	if (bad)
		fail("verify all: %lu mismatching pages", bad);
	if (s2.sw_read_io == s1.sw_read_io && s1.sw_entries)
		say("note: verify did not read any swapped entry");

	/* ---------------------------------------------------------- */
	if (!p_skip_cycles) {
		header("refault/rewrite cycles");
		for (c = 0; c < p_cycles; c++) {
			uint8_t *tmp = aligned_alloc(PG, PG);
			size_t n = A->npages * p_refault_pct / 100, i;
			size_t nd = A->npages * p_redirty_pct / 100;
			uint64_t cb = 0;
			char label[32];

			snprintf(label, sizeof(label), "cycle %lu", c);
			for (i = 0; i < n; i++)
				cb += check_page(A, rnd() % A->npages, tmp,
						 "cycle-refault");
			for (i = 0; i < nd; i++) {
				size_t idx = rnd() % A->npages;

				write_page(A, idx, A->gen[idx] + 1);
			}
			free(tmp);
			pageout_all(A);
			cb += sweep_region(B, "cycle-sweep-B");
			pageout_all(B);
			wait_drain(label, p_drain_ms);
			say("%s: refaulted %zu, re-dirtied %zu, %lu mismatches",
			    label, n, nd, cb);
			if (cb)
				fail("%s: %lu mismatches", label, cb);
			short_snap(label);
		}
		bad = verify_region(A, "post-cycles-A");
		bad += verify_region(B, "post-cycles-B");
		if (bad)
			fail("post-cycles verify: %lu mismatching pages", bad);
		take_snap(&s2);
		print_snap("after cycles", &s2);
	}

	/* ---------------------------------------------------------- */
	if (!p_skip_conc) {
		header("concurrent refault vs MADV_PAGEOUT");
		take_snap(&s1);
		bad = conc_test(A, B);
		take_snap(&s2);
		say("conc: +%lu swapped reads, +%lu payloads written back, "
		    "+%lu pageout stores", s2.sw_read_io - s1.sw_read_io,
		    s2.wb_payloads - s1.wb_payloads, s2.po_ok - s1.po_ok);
		if (bad)
			fail("conc: %lu mismatches during the test", bad);
		bad = verify_region(A, "post-conc-A");
		bad += verify_region(B, "post-conc-B");
		if (bad)
			fail("post-conc verify: %lu mismatching pages", bad);
		print_snap("after conc", &s2);
		rstat("after conc");
	}

	/* ---------------------------------------------------------- */
	header("munmap leak check");
	take_snap(&s1);
	print_snap("before munmap", &s1);
	region_drop(A);
	region_drop(B);
	msleep(1500);
	take_snap(&s2);
	print_snap("after munmap", &s2);
	rstat("after munmap");
	{
		int64_t swap_res = (int64_t)(s2.swaptotal - s2.swapfree) -
				   (int64_t)(s0.swaptotal - s0.swapfree);
		int64_t mc_res = ((int64_t)s2.stored - (int64_t)s0.stored) /
				 1024;
		int64_t pool_res = ((int64_t)s2.pool - (int64_t)s0.pool) / 1024;

		say("residue after munmap: swap %ld kB, swapped_entries %lu, "
		    "stored %ld kB, pool %ld kB, owner %lu, pending %lu",
		    (long)swap_res, s2.sw_entries, (long)mc_res,
		    (long)pool_res, s2.owner, s2.pending);
		if (swap_res > (int64_t)p_swap_tol_kb)
			fail("swap-slot leak: %ld kB of swap still used after "
			     "munmap (tolerance %lu kB)", (long)swap_res,
			     p_swap_tol_kb);
		/*
		 * What MC_SWAPPED entries remain belong to pages still in
		 * use: the initramfs files in rootfs and this process.  In
		 * a 1 GiB guest that is a couple of hundred.  Entries owned
		 * by freed folios used to leave hundreds more, which only
		 * swapoff cleared.
		 */
		if ((int64_t)s2.sw_entries > (int64_t)(p_swap_tol_kb / 4))
			fail("swapped-entry leak: %lu MC_SWAPPED entries after "
			     "munmap (tolerance %lu)", s2.sw_entries,
			     p_swap_tol_kb / 4);
		/*
		 * A folio owns its entries only while reclaim holds it, so
		 * what is left after munmap is reclaim in flight elsewhere.
		 * Entries owned by folios that were freed never go away.
		 */
		if (s2.owner > s0.owner + p_owner_tol && p_owner_fail)
			fail("owner-entry leak: %lu folio-owned entries after "
			     "munmap, %lu before the test (tolerance %lu)",
			     s2.owner, s0.owner, p_owner_tol);
		if (mc_res > (int64_t)p_mc_tol_kb) {
			/* Does it survive pressure? */
			struct region *P = region_new("P", 7, p_press_mb);
			struct snap s3;

			fill_region(P, 0);
			region_free(P);
			msleep(1000);
			take_snap(&s3);
			print_snap("after munmap+pressure", &s3);
			mc_res = ((int64_t)s3.stored - (int64_t)s0.stored) /
				 1024;
			say("LEAK: memcomp residue %ld kB stored / %ld kB "
			    "pool after munmap+pressure, owner=%lu pending=%lu "
			    "swapped_entries=%lu%s", (long)mc_res,
			    (long)(((int64_t)s3.pool - (int64_t)s0.pool) / 1024),
			    s3.owner, s3.pending, s3.sw_entries,
			    s3.sw_entries ? "" :
			    " (no swapped entries: owner-entry leak class, "
			    "not a swap-slot leak)");
			if (p_owner_fail)
				fail("memcomp residue %ld kB after munmap",
				     (long)mc_res);
		} else {
			say("no memcomp residue beyond %lu kB tolerance",
			    p_mc_tol_kb);
		}
	}

	/* ---------------------------------------------------------- */
	if (!p_no_swap && !p_skip_swapoff) {
		header("swapoff with partly swapped region mapped");
		D = region_add("D", 3, p_swapoff_mb);
		fill_region(D, p_lag);
		pageout_all(D);
		for (c = 0; c < 4; c++) {
			E = region_add("E", 4, p_swapoff_press_mb);
			fill_region(E, 0);
			pageout_all(E);
			region_drop(E);
			wait_drain("swapoff-prep", p_drain_ms);
			take_snap(&s1);
			if (s1.sw_entries > (uint64_t)D->npages / 20)
				break;
			pageout_all(D);
		}
		print_snap("before swapoff", &s1);
		if (!s1.sw_entries)
			fail("swapoff: D has no swapped entries before swapoff");
		/* Refault a slice first so D mixes resident/stored/swapped. */
		{
			uint8_t *tmp = aligned_alloc(PG, PG);
			size_t i, n = D->npages / 10;

			bad = 0;
			for (i = 0; i < n; i++)
				bad += check_page(D, rnd() % D->npages, tmp,
						  "pre-swapoff");
			free(tmp);
			if (bad)
				fail("pre-swapoff refault: %lu mismatches", bad);
		}
		take_snap(&s1);
		ts = now_s();
		if (swapoff(SWAPDEV)) {
			fail("swapoff(%s) failed: %s", SWAPDEV,
			     strerror(errno));
		} else {
			say("swapoff took %.2fs", now_s() - ts);
		}
		take_snap(&s2);
		print_snap("after swapoff", &s2);
		say("swapoff: +%lu entries restored, +%lu reads, +%lu fail",
		    s2.so_entries - s1.so_entries, s2.so_read_io - s1.so_read_io,
		    s2.so_fail - s1.so_fail);
		if (s2.sw_entries)
			fail("swapoff left %lu MC_SWAPPED entries",
			     s2.sw_entries);
		if (s2.so_fail != s1.so_fail)
			fail("swapoff_fail moved by %lu",
			     s2.so_fail - s1.so_fail);
		if (s1.sw_entries && s2.so_entries == s1.so_entries)
			fail("swapoff restored no memcomp entries");
		bad = verify_region(D, "post-swapoff-D");
		if (bad)
			fail("post-swapoff verify: %lu mismatching pages", bad);
		region_drop(D);
		msleep(1000);
		take_snap(&s2);
		print_snap("final", &s2);
	}

	header("summary");
	dump_file(MCDIR "stat");
	dump_file(MCDIR "packed_swap_events");
	dump_file(MCDIR "placement_events");
	dump_file(MCDIR "events");
	dump_file(MCDIR "execution_events");
	take_snap(&s2);
	if (s2.oom_kill)
		fail("oom_kill=%lu", s2.oom_kill);
	say("RESULT: %s mismatches=%lu writeback_payloads=%lu "
	    "swapped_read_io=%lu swapoff_entries=%lu runtime=%.0fs "
	    "coverage=%s skips=%u%s%s",
	    g_fail ? "FAIL" : "PASS", g_mismatch,
	    s2.wb_payloads - s0.wb_payloads, s2.sw_read_io - s0.sw_read_io,
	    s2.so_entries - s0.so_entries, now_s(),
	    g_skips ? "PARTIAL" : "COMPLETE", g_skips,
	    g_fail ? " reasons: " : "", g_fail ? g_fail_reasons : "");
	return g_fail ? 1 : 0;
}

/* ------------------------------------------------------------------ */
/* PID 1                                                               */

static int setup_swap(void)
{
	uint8_t *hdr = aligned_alloc(PG, PG);
	uint64_t size = 0;
	uint32_t v;
	int fd, i;

	for (i = 0; i < 50 && access(SWAPDEV, F_OK); i++)
		msleep(100);
	fd = open(SWAPDEV, O_RDWR);
	if (fd < 0) {
		say("open %s: %s", SWAPDEV, strerror(errno));
		return -1;
	}
	if (ioctl(fd, BLKGETSIZE64, &size) || size < 16 * PG) {
		say("BLKGETSIZE64 %s: %s size=%lu", SWAPDEV, strerror(errno),
		    size);
		close(fd);
		return -1;
	}
	memset(hdr, 0, PG);
	v = 1;				/* info.version */
	memcpy(hdr + 1024, &v, 4);
	v = (uint32_t)(size / PG - 1);	/* info.last_page */
	memcpy(hdr + 1028, &v, 4);
	v = 0;				/* info.nr_badpages */
	memcpy(hdr + 1032, &v, 4);
	memcpy(hdr + PG - 10, "SWAPSPACE2", 10);
	if (pwrite(fd, hdr, PG, 0) != (ssize_t)PG || fsync(fd)) {
		say("write swap header: %s", strerror(errno));
		close(fd);
		return -1;
	}
	close(fd);
	free(hdr);
	if (swapon(SWAPDEV, 0)) {
		say("swapon %s: %s", SWAPDEV, strerror(errno));
		return -1;
	}
	say("swapon %s: %lu MB, last_page %lu", SWAPDEV, size >> 20,
	    size / PG - 1);
	return 0;
}

static void write_str(const char *path, const char *s)
{
	int fd = open(path, O_WRONLY);

	if (fd < 0)
		return;
	if (write(fd, s, strlen(s)) < 0)
		say("write %s: %s", path, strerror(errno));
	close(fd);
}

static void poweroff(void)
{
	sync();
	reboot(RB_POWER_OFF);
	for (;;)
		pause();
}

/* Host-side helper: mcwb_init --dump N writes N pages of region 1. */
static int dump_pages(unsigned long n)
{
	uint8_t *buf = aligned_alloc(PG, PG);
	unsigned long i;

	load_params();
	gen_init();
	for (i = 0; i < n; i++) {
		gen_page(buf, 1, i, 0);
		if (fwrite(buf, PG, 1, stdout) != 1)
			return 1;
	}
	return 0;
}

/* Exit zero is not proof of complete TAP coverage: preserve every SKIP. */
static void guest_binary(const char *name, const char *path, bool hmm)
{
    pid_t pid;
    int status, rc, fd;
    unsigned int passes = 0, skips = 0, failures = 0;
    char logpath[128], line[1024];
    FILE *log;

    if (access(path, X_OK)) {
        fail("%s: requested binary unavailable", name);
        return;
    }
    snprintf(logpath, sizeof(logpath), "/tmp/%s.log", name);
    fd = open(logpath, O_CREAT | O_TRUNC | O_RDWR, 0600);
    if (fd < 0) {
        fail("%s: capture open: %s", name, strerror(errno));
        return;
    }
    say("%s: BEGIN mode=%s swapoff_optin=%d", name,
        p_no_swap ? "no-swap" : "swap", !hmm && !p_no_swap);
    fflush(NULL);
    pid = fork();
    if (!pid) {
        if (dup2(fd, STDOUT_FILENO) < 0 || dup2(fd, STDERR_FILENO) < 0)
            _exit(126);
        close(fd);
        if (hmm)
            execl(path, "hmm-tests", "-r",
                  "hmm.hmm_device_private.memcomp_read_write", (char *)NULL);
        else if (!p_no_swap)
            execl(path, "memcomp_selftest", "--swap-device", SWAPDEV,
                  "--allow-swapoff", "--swapon-flags", "0", (char *)NULL);
        else
            execl(path, "memcomp_selftest", (char *)NULL);
        _exit(127);
    }
    close(fd);
    if (pid < 0 || waitpid(pid, &status, 0) != pid ||
        !WIFEXITED(status)) {
        fail("%s: child did not exit normally", name);
        return;
    }
    rc = WEXITSTATUS(status);
    log = fopen(logpath, "r");
    if (!log) {
        fail("%s: cannot read TAP capture", name);
        return;
    }
    while (fgets(line, sizeof(line), log)) {
        fputs(line, stdout);
        if (strstr(line, "# SKIP"))
            skips++;
        else if (!strncmp(line, "ok ", 3))
            passes++;
        if (!strncmp(line, "not ok ", 7))
            failures++;
    }
    fclose(log);
    say("%s: END exit=%d pass=%u skip=%u fail=%u result=%s", name,
        rc, passes, skips, failures,
        rc == 4 ? "SKIP" : rc || failures ? "FAIL" :
        skips ? "PARTIAL" : "PASS");
    if (skips || rc == 4)
        coverage_skip(name, "TAP reports skipped coverage (see captured results)");
    if ((rc && rc != 4) || failures || (!passes && !skips && rc != 4))
        fail("%s: exit=%d pass=%u skip=%u fail=%u", name,
             rc, passes, skips, failures);
    if (hmm && rc == 0 && passes != 1)
        fail("%s: expected exactly one filtered HMM test; got %u", name, passes);
}

static void guest_ksm(void)
{
    const char *path = "/ksm_memcomp_test";
    pid_t pid;
    int status, rc, fd;
    unsigned int passes = 0, skips = 0, failures = 0;
    char logpath[128], line[1024];
    FILE *log;

    if (!envu("RUN_KSM", 0))
        return;
    if (access(path, X_OK)) {
        fail("KSM: requested binary unavailable");
        return;
    }
    snprintf(logpath, sizeof(logpath), "/tmp/KSM.log");
    fd = open(logpath, O_CREAT | O_TRUNC | O_RDWR, 0600);
    if (fd < 0) {
        fail("KSM: capture open: %s", strerror(errno));
        return;
    }
    say("KSM: BEGIN");
    fflush(NULL);
    pid = fork();
    if (!pid) {
        if (dup2(fd, STDOUT_FILENO) < 0 || dup2(fd, STDERR_FILENO) < 0)
            _exit(126);
        close(fd);
        execl(path, "ksm_memcomp_test", (char *)NULL);
        _exit(127);
    }
    close(fd);
    if (pid < 0 || waitpid(pid, &status, 0) != pid ||
        !WIFEXITED(status)) {
        fail("KSM: child did not exit normally");
        return;
    }
    rc = WEXITSTATUS(status);
    log = fopen(logpath, "r");
    if (!log) {
        fail("KSM: cannot read captured output");
        return;
    }
    while (fgets(line, sizeof(line), log)) {
        fputs(line, stdout);
        if (!strncmp(line, "PASS:", 5))
            passes++;
        else if (!strncmp(line, "SKIP:", 5))
            skips++;
        else if (!strncmp(line, "FAIL:", 5))
            failures++;
    }
    fclose(log);
    say("KSM: END exit=%d pass=%u skip=%u fail=%u result=%s", rc,
        passes, skips, failures,
        rc == 4 ? "SKIP" : rc || failures || skips || passes != 5 ? "FAIL" :
        "PASS");
    if (skips || rc == 4)
        coverage_skip("KSM memcomp integration",
                      "KSM harness reports skipped coverage");
    if ((rc && rc != 4) || failures || (rc == 0 && skips) ||
        (rc == 0 && passes != 5))
        fail("KSM: exit=%d pass=%u skip=%u fail=%u (expected pass=5, skip=0, fail=0)",
             rc, passes, skips, failures);
}

static void guest_selftest(void)
{
    if (envu("RUN_SELFTEST", 0))
        guest_binary("SELFTEST", "/memcomp_selftest", false);
}

#include "memcomp_hmm_module.h"

static void guest_hmm(void)
{
    if (!envu("RUN_HMM", 0))
        return;
    if (envu("HMM_MODULE", 0) && !memcomp_hmm_load_module("/test_hmm.ko"))
        return;
    guest_binary("HMM", "/hmm-tests", true);
}

int main(int argc, char **argv)
{
	struct utsname u;
	const char *thp;
	pid_t pid;
	int st, fd;
	double last_hb = 0;

	if (getpid() != 1 && argc == 3 && !strcmp(argv[1], "--dump"))
		return dump_pages(strtoul(argv[2], NULL, 0));
	if (getpid() != 1) {
		fprintf(stderr, "This harness must run as guest PID 1.\n");
		return 4;
	}
	clock_gettime(CLOCK_MONOTONIC, &t0);
	mount("proc", "/proc", "proc", 0, NULL);
	mount("sysfs", "/sys", "sysfs", 0, NULL);
	mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);
	if (fcntl(1, F_GETFD) < 0) {
		fd = open("/dev/console", O_RDWR);
		if (fd >= 0) {
			dup2(fd, 0);
			dup2(fd, 1);
			dup2(fd, 2);
			if (fd > 2)
				close(fd);
		}
	}
	setvbuf(stdout, NULL, _IOLBF, 0);
	load_params();
	gen_init();
	uname(&u);
	say("mcwb_init: %s %s", u.release, u.version);
	thp = getenv("THP");
	if (thp && *thp)
		write_str("/sys/kernel/mm/transparent_hugepage/enabled", thp);
	read_file("/sys/kernel/mm/transparent_hugepage/enabled", sbuf,
		  sizeof(sbuf));
	say("thp: %.*s", (int)strcspn(sbuf, "\n"), sbuf);
	read_file("/sys/kernel/mm/lru_gen/enabled", sbuf, sizeof(sbuf));
	say("lru_gen/enabled: %.*s", (int)strcspn(sbuf, "\n"), sbuf);
	read_file(MCDIR "compressor", sbuf, sizeof(sbuf));
	say("memcomp compressor: %.*s", (int)strcspn(sbuf, "\n"), sbuf);
	if (access(MCDIR "packed_swap_events", R_OK))
		say("TEST CONFIG ERROR: no " MCDIR "packed_swap_events "
		    "(CONFIG_MEMCOMP_DEBUG_STATS off?)");
	read_file("/proc/sys/vm/min_free_kbytes", sbuf, sizeof(sbuf));
	say("min_free_kbytes: %.*s", (int)strcspn(sbuf, "\n"), sbuf);
	dump_file("/proc/zoneinfo");
	say("params: FILL_MB=%lu PRESS_MB=%lu CHUNK_MB=%lu PAGEOUT_LAG=%lu "
	    "PACE_US=%lu DRAIN_MS=%lu WB_WAIT_S=%lu CYCLES=%lu REFAULT_PCT=%lu "
	    "REDIRTY_PCT=%lu CONC_SECS=%lu CONC_THREADS=%lu CONC_K=%lu "
	    "SWAPOFF_MB=%lu SWAPOFF_PRESS_MB=%lu VERIFY_THREADS=%lu SEED=%#lx "
	    "RETAIN_MB=%lu RETAIN_ROUNDS=%lu RETAIN_THREADS=%lu RETAIN_READERS=%lu",
	    p_fill_mb, p_press_mb, p_chunk_mb, p_lag, p_pace_us, p_drain_ms,
	    p_wb_wait_s, p_cycles, p_refault_pct, p_redirty_pct, p_conc_secs,
	    p_conc_threads, p_conc_k, p_swapoff_mb, p_swapoff_press_mb,
	    p_verify_threads, p_seed, p_retain_mb, p_retain_rounds,
	    p_retain_threads, p_retain_readers);

	if (!p_no_swap && setup_swap()) {
		say("RESULT: FAIL swap setup failed");
		poweroff();
	}

	pid = fork();
	if (pid == 0)
		_exit(run_tests());
	if (pid < 0) {
		say("RESULT: FAIL fork: %s", strerror(errno));
		poweroff();
	}
	for (;;) {
		pid_t w = waitpid(pid, &st, WNOHANG);

		if (w == pid)
			break;
		if (now_s() > p_timeout_s) {
			struct snap s;

			say("TIMEOUT after %lus: dumping blocked tasks",
			    p_timeout_s);
			write_str("/proc/sys/kernel/sysrq", "1");
			write_str("/proc/sysrq-trigger", "w");
			take_snap(&s);
			print_snap("timeout", &s);
			kill(pid, SIGKILL);
			say("RESULT: FAIL timeout after %lus", p_timeout_s);
			poweroff();
		}
		if (p_heartbeat_s && now_s() - last_hb >= p_heartbeat_s) {
			if (last_hb)
				short_snap("hb");
			last_hb = now_s();
		}
		msleep(200);
	}
	if (WIFSIGNALED(st) || (WIFEXITED(st) && WEXITSTATUS(st) > 1)) {
		struct snap s;

		take_snap(&s);
		print_snap("child died", &s);
		if (WIFSIGNALED(st))
			say("RESULT: FAIL test child killed by signal %d "
			    "(oom_kill=%lu)", WTERMSIG(st), s.oom_kill);
		else
			say("RESULT: FAIL test child exit %d",
			    WEXITSTATUS(st));
	}
	say("mcwb_init: done, powering off");
	poweroff();
	return 0;
}
