/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef MEMCOMP_COMPARE_COMMON_H
#define MEMCOMP_COMPARE_COMMON_H

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#ifndef _FILE_OFFSET_BITS
#define _FILE_OFFSET_BITS 64
#endif

#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <limits.h>
#include <poll.h>
#include <sched.h>
#include <signal.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <termios.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#include "corpus.h"

#define BENCH_MIB (UINT64_C(1) << 20)
#define BENCH_MAX_BYTES (UINT64_C(1024) * BENCH_MIB)
#define BENCH_MAX_PRESSURE_MIB 512

enum bench_content { CONTENT_MIXED, CONTENT_TEXT, CONTENT_RANDOM, CONTENT_SAME, CONTENT_CORPUS };
enum bench_kind { KIND_ZERO, KIND_SAME, KIND_TEXT, KIND_RECORD, KIND_ALPHA, KIND_RANDOM, KIND_REAL };
enum bench_class {
	CLASS_PRESENT,
	CLASS_MEMCOMP,
	CLASS_SWAPPED_NONRESIDENT,
	CLASS_OTHER_NONRESIDENT,
	CLASS_CACHED_NONPRESENT,
	CLASS_UNKNOWN,
	CLASS_COUNT
};

struct bench_options {
	size_t pages;
	size_t page_size;
	size_t bytes;
	uint64_t seed;
	enum bench_content content;
	bool random_order;
	int cpu;
	unsigned int settle_ms;
	unsigned int gap_us;
	const char *directory;
	unsigned int pressure_mib;
	unsigned int rounds, warmup, single;
	bool rewrite;
	size_t round_shift;
	const char *corpus_path, *corpus_source;
	uint64_t corpus_offset, corpus_first;
	struct bench_corpus *corpus;
	bool functional_synthetic, anonymous;
};

struct bench_observation {
	int present;
	int swapped;
	int resident;
	int swap_type;
	int memcomp;
	int real_swap;
};

struct bench_sample {
	size_t index;
	struct bench_observation snapshot;
	struct bench_observation before;
	uint64_t ns;
	uint64_t pageout_ns;
	int cpu;
	int verified;
};

struct bench_observer {
	int pagemap;
	bool mincore_available;
	int memcomp_type;
	int swap_type;
	unsigned int errors;
};

/* Drain diagnostics are per PAGEOUT/read phase, not per individual touch. */
struct bench_drain {
	uint64_t purge_ns;
	size_t remaining, remapped;
	unsigned int rounds;
	int timed_out;
};

struct bench_population {
	size_t eligible, counted, readahead;
};

struct bench_writeback {
	const char *source;
	uint64_t before, after, wait_ns, pressure_ready_ns;
	int gate_open;
	size_t pressure_bytes;
	unsigned int stable_polls;
	int timed_out, pressure_ready, explicit_idle, proven;
};

static volatile sig_atomic_t bench_interrupted;

static inline void bench_signal(int signo)
{
	bench_interrupted = signo;
}

static inline int bench_signals(void)
{
	struct sigaction action = { .sa_handler = bench_signal };

	if (sigemptyset(&action.sa_mask) || sigaction(SIGINT, &action, NULL) ||
	    sigaction(SIGTERM, &action, NULL))
		return -1;
	action.sa_handler = SIG_IGN;
	if (sigaction(SIGPIPE, &action, NULL))
		return -1;
	/* An explicitly ignored inherited SIGCHLD would auto-reap owned PIDs. */
	action.sa_handler = SIG_DFL;
	action.sa_flags = 0;
	return sigaction(SIGCHLD, &action, NULL);
}

/* stdout is a transport pipe, NOT a terminal. Never tcdrain(stdout). */
static inline int bench_console_drain(void)
{
	static int console = -1;
	int rc;

	if (bench_interrupted) {
		errno = EINTR;
		return -1;
	}
	if (console < 0) {
		console = open("/dev/console", O_WRONLY | O_NOCTTY | O_CLOEXEC);
		if (console < 0)
			return -1;
	}
	do {
		rc = tcdrain(console);
	} while (rc && errno == EINTR && !bench_interrupted);
	return rc;
}

static inline int bench_now(uint64_t *ns)
{
	struct timespec ts;

	if (clock_gettime(CLOCK_MONOTONIC_RAW, &ts))
		return -1;
	*ns = (uint64_t)ts.tv_sec * UINT64_C(1000000000) + (uint64_t)ts.tv_nsec;
	return 0;
}

/* Dedicated inherited pipe/stream endpoints, never the CSV/log descriptors. */
static inline int bench_phase_fd(const char *text, int *fd)
{
	unsigned int value = 0;

	if (!text || !*text)
		goto invalid;
	for (; *text; text++) {
		if (*text < '0' || *text > '9')
			goto invalid;
		value = value * 10 + (unsigned int)(*text - '0');
		if (value > 1023)
			goto invalid;
	}
	if (value < 3)
		goto invalid;
	*fd = (int)value;
	return 0;
invalid:
	errno = EINVAL;
	return -1;
}

static inline int bench_phase_wait(int fd, short events, uint64_t deadline)
{
	struct pollfd channel = { .fd = fd, .events = events };

	for (;;) {
		uint64_t now;
		int timeout, rc;

		if (bench_interrupted) {
			errno = EINTR;
			return -1;
		}
		if (bench_now(&now))
			return -1;
		if (now >= deadline) {
			errno = ETIMEDOUT;
			return -1;
		}
		timeout = (int)((deadline - now + 999999) / 1000000);
		rc = poll(&channel, 1, timeout);
		if (rc < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		if (!rc)
			continue;
		if (channel.revents & POLLNVAL) {
			errno = EBADF;
			return -1;
		}
		if (channel.revents & events)
			return 0;
		if (channel.revents & (POLLHUP | POLLERR)) {
			errno = EPIPE;
			return -1;
		}
	}
}

static inline int bench_phase_barrier(const char *name)
{
	const char *write_env = getenv("COMPARE_PHASE_WRITE_FD");
	const char *read_env = getenv("COMPARE_PHASE_READ_FD");
	int fds[2], flags[2], changed = 0, rc = -1, saved, i;
	uint64_t now, deadline;
	char message[64], ack;
	size_t sent = 0;
	int length;

	if (!write_env && !read_env)
		return 0;
	if (bench_phase_fd(write_env, &fds[0]) || bench_phase_fd(read_env, &fds[1]))
		return -1;
	if (fds[0] == fds[1]) {
		errno = EINVAL;
		return -1;
	}
	for (i = 0; i < 2; i++) {
		struct stat st;

		flags[i] = fcntl(fds[i], F_GETFL);
		if (flags[i] < 0 || fstat(fds[i], &st))
			return -1;
		if ((!S_ISFIFO(st.st_mode) && !S_ISSOCK(st.st_mode)) ||
		    (!i && (flags[i] & O_ACCMODE) == O_RDONLY) ||
		    (i && (flags[i] & O_ACCMODE) == O_WRONLY)) {
			errno = EINVAL;
			return -1;
		}
	}
	length = snprintf(message, sizeof(message), "%s\n", name);
	if (length < 0 || (size_t)length >= sizeof(message)) {
		errno = EMSGSIZE;
		return -1;
	}
	if (bench_now(&now))
		return -1;
	deadline = now + UINT64_C(30000000000);
	/* Nonblocking IO also bounds full-pipe writes and readiness races.
	 * Endpoints must be dedicated; F_SETFL is shared with duplicate fds.
	 * Restore the caller's flags on both success and error.
	 */
	for (i = 0; i < 2; i++) {
		if (fcntl(fds[i], F_SETFL, flags[i] | O_NONBLOCK))
			goto out;
		changed++;
	}
	while (sent < (size_t)length) {
		ssize_t count;

		if (bench_phase_wait(fds[0], POLLOUT, deadline))
			goto out;
		count = write(fds[0], message + sent, (size_t)length - sent);
		if (count < 0) {
			if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
				continue;
			goto out;
		}
		if (!count) {
			errno = EPIPE;
			goto out;
		}
		sent += (size_t)count;
	}
	for (;;) {
		ssize_t count;

		if (bench_phase_wait(fds[1], POLLIN, deadline))
			goto out;
		count = read(fds[1], &ack, 1);
		if (count < 0) {
			if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
				continue;
			goto out;
		}
		if (!count) {
			errno = EPIPE;
			goto out;
		}
		if (ack != '1') {
			errno = EPROTO;
			goto out;
		}
		rc = 0;
		break;
	}
out:
	saved = errno;
	for (i = changed - 1; i >= 0; i--) {
		if (fcntl(fds[i], F_SETFL, flags[i])) {
			int error = errno;

			fprintf(stderr, "ERROR phase fd=%d restore flags: %s\n", fds[i], strerror(error));
			if (!rc)
				saved = error;
			rc = -1;
		}
	}
	errno = saved;
	return rc;
}

static inline int bench_phase(const char *name)
{
	uint64_t ns;

	if (bench_now(&ns))
		return -1;
	if (fprintf(stderr, "PHASE name=%s monotonic_raw_ns=%" PRIu64 "\n", name, ns) < 0 ||
	    fflush(stderr))
		return -1;
	if (bench_phase_barrier(name)) {
		int saved = errno;

		fprintf(stderr, "ERROR phase_barrier name=%s errno=%d message=%s\n",
			name, saved, strerror(saved));
		errno = saved;
		return -1;
	}
	return 0;
}

static inline int bench_sleep_us(uint64_t us)
{
	struct timespec remaining = {
		.tv_sec = (time_t)(us / UINT64_C(1000000)),
		.tv_nsec = (long)(us % UINT64_C(1000000)) * 1000
	};

	if (bench_interrupted) {
		errno = EINTR;
		return -1;
	}
	if (!us)
		return 0;
	while (nanosleep(&remaining, &remaining)) {
		if (errno != EINTR || bench_interrupted)
			return -1;
	}
	return 0;
}

static inline int bench_number(const char *text, uint64_t max, uint64_t *value)
{
	char *end;
	unsigned long long number;

	if (text[0] < '0' || text[0] > '9')
		return -1;
	errno = 0;
	number = strtoull(text, &end, text[0] == '0' &&
			 (text[1] == 'x' || text[1] == 'X') ? 16 : 10);
	if (errno || *end || number > max)
		return -1;
	*value = (uint64_t)number;
	return 0;
}

static inline const char *bench_content_name(enum bench_content content)
{
	static const char *const names[] = { "mixed", "text", "random", "same", "corpus" };

	return names[content];
}

static inline void bench_usage(const char *name, bool shmem)
{
	fprintf(stderr,
		"Usage: %s [options]%s\n"
		"  --pages N          pages (default 4096; mapping <= 1 GiB)\n"
		"  --corpus PATH      required read-only real-page corpus device\n"
		"  --corpus-source S  source label or combined (default combined)\n"
		"  --corpus-offset N  first page within that source (default 0)\n"
		"  --functional-synthetic  TEST ONLY; never eligible for performance\n"
		"  --content TYPE     synthetic functional fixture only\n"
		"  --seed N           uint64 seed, decimal or 0x... (default 1)\n"
		"  --order TYPE       seq|rand (default seq)\n"
		"  --cpu N            optional CPU affinity; otherwise inherited\n"
		"  --settle-ms N      wait before snapshot/read (default 250, max 60000)\n"
		"  --gap-us N         gap before each observation/touch (default 0)\n"
		"  --help\n", name, shmem ? " [--anonymous | --dir /absolute/private/tmpfs/directory]" : "");
	if (!shmem)
		fputs("  --rewrite          disjoint real prime then target overwrite (W1)\n"
		      "  --rounds N         measured rounds (default 5, max 9)\n"
		      "  --warmup N         warmup rounds (default 1, max 1)\n"
		      "  --single N         single-page samples (default 4096, max pages)\n", stderr);
	if (shmem)
		fputs("  --anonymous        v4.10 explicit anonymous pressure workload\n"
		      "  --dir PATH         legacy functional shmem mode only\n"
		      "  --pressure-mib N   OPT IN: hold N MiB disjoint real anonymous pages\n"
		      "                     during readback (default 0, max 512)\n", stderr);
	fputs("CSV goes to stdout; configuration/phases/summary/errors to stderr.\n", stderr);
}

/* The reader owns only bounded aligned I/O scratch, not a page corpus cache. */
static struct bench_corpus bench_input = { .fd = -1 };

static void bench_input_cleanup(void)
{
	bench_corpus_close(&bench_input);
}

static inline int bench_input_open(struct bench_options *o)
{
	if (o->functional_synthetic) {
		if (o->corpus_path || o->corpus_offset || strcmp(o->corpus_source, "combined")) {
			errno = EINVAL;
			return -1;
		}
		return 0;
	}
	if (!o->corpus_path || o->content != CONTENT_MIXED) {
		errno = EINVAL;
		return -1;
	}
	if (bench_corpus_open(&bench_input, o->corpus_path))
		return -1;
	if (atexit(bench_input_cleanup)) {
		bench_corpus_close(&bench_input);
		errno = ENOMEM;
		return -1;
	}
	o->corpus = &bench_input;
	if (o->page_size != o->corpus->page_size ||
	    bench_corpus_range(o->corpus, o->corpus_source, o->corpus_offset,
			       o->pages, &o->corpus_first)) {
		errno = EINVAL;
		return -1;
	}
	o->content = CONTENT_CORPUS;
	return 0;
}

/* No wrap, padding, replication or generated fallback on corpus exhaustion. */
static inline int bench_input_extra(const struct bench_options *o, size_t pages,
				    uint64_t *first)
{
	if (o->corpus_offset > UINT64_MAX - o->pages) {
		errno = EOVERFLOW;
		return -1;
	}
	return bench_corpus_range(o->corpus, o->corpus_source,
				  o->corpus_offset + o->pages, pages, first);
}

static inline void bench_input_csv(const struct bench_options *o, size_t index)
{
	uint64_t page = o->corpus_first + index;

	if (!o->corpus) {
		return;
	}
	for (unsigned int i = 0; i < o->corpus->source_count; i++) {
		if (page >= o->corpus->sources[i].start_page &&
		    page - o->corpus->sources[i].start_page < o->corpus->sources[i].pages) {
			printf(",%s,%" PRIu64, o->corpus->sources[i].label, page);
			return;
		}
	}
	/* Parser/range validation makes this unreachable. Never print page data. */
	fputs(",unavailable,", stdout);
}

/* Returns 1 for --help, -1 for bad arguments, 0 for parsed options. */
static inline int bench_parse(int argc, char **argv, bool shmem,
			      struct bench_options *o)
{
	static const struct option options[] = {
		{ "corpus", required_argument, NULL, 1000 },
		{ "corpus-source", required_argument, NULL, 1001 },
		{ "corpus-offset", required_argument, NULL, 1002 },
		{ "functional-synthetic", no_argument, NULL, 1003 },
		{ "anonymous", no_argument, NULL, 1004 },
		{ "pages", required_argument, NULL, 'n' },
		{ "content", required_argument, NULL, 't' },
		{ "seed", required_argument, NULL, 's' },
		{ "order", required_argument, NULL, 'o' },
		{ "cpu", required_argument, NULL, 'c' },
		{ "settle-ms", required_argument, NULL, 'w' },
		{ "gap-us", required_argument, NULL, 'g' },
		{ "dir", required_argument, NULL, 'd' },
		{ "pressure-mib", required_argument, NULL, 'p' },
		{ "rounds", required_argument, NULL, 'r' },
		{ "rewrite", no_argument, NULL, 'R' },
		{ "warmup", required_argument, NULL, 'u' },
		{ "single", required_argument, NULL, 'l' },
		{ "help", no_argument, NULL, 'h' },
		{ NULL, 0, NULL, 0 }
	};
	long page_size = sysconf(_SC_PAGESIZE);
	uint64_t value;
	int option;

	if (page_size < 4096 || (page_size & (page_size - 1)) ||
	    (uint64_t)page_size > BENCH_MAX_BYTES) {
		fputs("ERROR unsupported page size\n", stderr);
		return -1;
	}
	*o = (struct bench_options) {
		.pages = 4096, .page_size = (size_t)page_size, .seed = 1,
		.content = CONTENT_MIXED, .cpu = -1, .settle_ms = 250,
		.rounds = 5, .warmup = 1, .single = 4096, .corpus_source = "combined"
	};
	while ((option = getopt_long(argc, argv, "n:t:s:o:c:w:g:d:p:r:u:l:Rh", options, NULL)) != -1) {
		switch (option) {
		case 1000:
			if (o->corpus_path || optarg[0] != '/')
				goto bad;
			o->corpus_path = optarg;
			break;
		case 1001:
			o->corpus_source = optarg;
			break;
		case 1002:
			if (bench_number(optarg, UINT64_MAX, &o->corpus_offset))
				goto bad;
			break;
		case 1003:
			o->functional_synthetic = true;
			break;
		case 1004:
			if (!shmem)
				goto bad;
			o->anonymous = true;
			break;
		case 'n':
			if (bench_number(optarg, BENCH_MAX_BYTES / o->page_size, &value) || !value)
				goto bad;
			o->pages = (size_t)value;
			break;
		case 't':
			if (!strcmp(optarg, "mixed"))
				o->content = CONTENT_MIXED;
			else if (!strcmp(optarg, "text"))
				o->content = CONTENT_TEXT;
			else if (!strcmp(optarg, "random"))
				o->content = CONTENT_RANDOM;
			else if (!strcmp(optarg, "same"))
				o->content = CONTENT_SAME;
			else
				goto bad;
			break;
		case 's':
			if (bench_number(optarg, UINT64_MAX, &o->seed))
				goto bad;
			break;
		case 'o':
			if (strcmp(optarg, "seq") && strcmp(optarg, "rand"))
				goto bad;
			o->random_order = !strcmp(optarg, "rand");
			break;
		case 'c':
			if (bench_number(optarg, CPU_SETSIZE - 1, &value))
				goto bad;
			o->cpu = (int)value;
			break;
		case 'w':
			if (bench_number(optarg, 60000, &value))
				goto bad;
			o->settle_ms = (unsigned int)value;
			break;
		case 'g':
			if (bench_number(optarg, 1000000, &value))
				goto bad;
			o->gap_us = (unsigned int)value;
			break;
		case 'd':
			if (!shmem || optarg[0] != '/')
				goto bad;
			o->directory = optarg;
			break;
		case 'p':
			if (!shmem || bench_number(optarg, BENCH_MAX_PRESSURE_MIB, &value))
				goto bad;
			o->pressure_mib = (unsigned int)value;
			break;
		case 'R':
			if (shmem || o->rewrite)
				goto bad;
			o->rewrite = true;
			break;
		case 'r':
			if (shmem || bench_number(optarg, 9, &value) || !value)
				goto bad;
			o->rounds = (unsigned int)value;
			break;
		case 'u':
			if (shmem || bench_number(optarg, 1, &value))
				goto bad;
			o->warmup = (unsigned int)value;
			break;
		case 'l':
			if (shmem || bench_number(optarg, BENCH_MAX_BYTES / o->page_size, &value))
				goto bad;
			o->single = (unsigned int)value;
			break;
		case 'h':
			bench_usage(argv[0], shmem);
			return 1;
		default:
			goto bad;
		}
	}
	if (optind != argc || (shmem && !o->anonymous && !o->directory) ||
	    o->pages > BENCH_MAX_BYTES / o->page_size)
		goto bad;
	o->bytes = o->pages * o->page_size;
	if (bench_input_open(o)) {
		fprintf(stderr, "ERROR corpus input unavailable errno=%d\n", errno);
		goto bad;
	}
	return 0;
bad:
	fputs("ERROR invalid arguments\n", stderr);
	bench_usage(argv[0], shmem);
	return -1;
}

static inline int bench_pin_cpu(const struct bench_options *o)
{
	cpu_set_t set;

	if (o->cpu < 0)
		return 0;
	CPU_ZERO(&set);
	CPU_SET(o->cpu, &set);
	return sched_setaffinity(0, sizeof(set), &set);
}

static inline void bench_config(const char *workload, const struct bench_options *o)
{
	fprintf(stderr,
		"CONFIG workload=%s input=%s measurement_eligible=%d pages=%zu page_size=%zu bytes=%zu "
		"content=%s seed=%" PRIu64 " order=%s cpu=%d settle_ms=%u gap_us=%u "
		"pressure_mib=%u corpus_source=%s corpus_first=%" PRIu64 "\n", workload,
		o->corpus ? "real-corpus-v1" : "synthetic-functional-only", !!o->corpus,
		o->pages, o->page_size, o->bytes,
		bench_content_name(o->content), o->seed, o->random_order ? "rand" : "seq",
		o->cpu, o->settle_ms, o->gap_us, o->pressure_mib,
		o->corpus_source, o->corpus_first);
}

static inline uint64_t bench_mix64(uint64_t z)
{
	z += UINT64_C(0x9e3779b97f4a7c15);
	z = (z ^ (z >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
	z = (z ^ (z >> 27)) * UINT64_C(0x94d049bb133111eb);
	return z ^ (z >> 31);
}

static inline uint64_t bench_rng(uint64_t *state)
{
	uint64_t x = *state;

	x ^= x >> 12;
	x ^= x << 25;
	x ^= x >> 27;
	*state = x;
	return x * UINT64_C(0x2545f4914f6cdd1d);
}

static inline void bench_le(unsigned char *dst, uint64_t value, unsigned int bytes)
{
	unsigned int i;

	for (i = 0; i < bytes; i++) {
		dst[i] = (unsigned char)value;
		value >>= 8;
	}
}

static inline size_t bench_content_index(const struct bench_options *o, size_t index)
{
	return (index + o->round_shift) % o->pages;
}

static inline uint64_t bench_page_seed(const struct bench_options *o, size_t index)
{
	return bench_mix64(o->seed ^ (UINT64_C(1) << 56) ^
			   ((uint64_t)bench_content_index(o, index) << 8));
}

static inline enum bench_kind bench_page_kind(const struct bench_options *o, size_t index)
{
	unsigned int r;

	if (o->content == CONTENT_CORPUS)
		return KIND_REAL;
	if (o->content == CONTENT_TEXT)
		return KIND_TEXT;
	if (o->content == CONTENT_RANDOM)
		return KIND_RANDOM;
	if (o->content == CONTENT_SAME)
		return KIND_SAME;
	r = (unsigned int)(bench_mix64(bench_page_seed(o, index) ^ UINT64_C(0x5bd1e995)) % 100);
	if (r < 5)
		return KIND_ZERO;
	if (r < 10)
		return KIND_SAME;
	if (r < 35)
		return KIND_TEXT;
	if (r < 65)
		return KIND_RECORD;
	if (r < 85)
		return KIND_ALPHA;
	return KIND_RANDOM;
}

/* Derived from e2e/mcwb_init.c's pure generator, rid=1, gen=0.
 * Explicit little-endian fields avoid native structure layout dependencies.
 * Zero and repeated-word pages have NO header and are never filtered out.
 */
static inline void bench_gen_page(unsigned char *dst, const struct bench_options *o,
				  size_t index)
{
	static const char *const words[64] = {
		"the", "of", "and", "memory", "page", "swap", "kernel", "to", "in",
		"is", "compress", "that", "for", "it", "with", "as", "was", "on", "be",
		"at", "by", "this", "had", "not", "are", "but", "from", "or", "have",
		"an", "they", "which", "one", "you", "were", "her", "all", "she",
		"there", "would", "their", "we", "him", "been", "has", "when", "who",
		"will", "more", "no", "if", "out", "so", "said", "what", "up", "its",
		"about", "into", "than", "them", "can", "only", "other"
	};
	static const char *const names[8] = {
		"alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"
	};
	uint64_t seed = bench_page_seed(o, index), state = seed | 1;
	enum bench_kind kind = bench_page_kind(o, index);
	unsigned char *p = dst, *end = dst + o->page_size;
	size_t i;
	unsigned int nw = 0;

	if (!o->functional_synthetic || kind == KIND_REAL)
		abort();
	if (kind == KIND_ZERO) {
		memset(dst, 0, o->page_size);
		return;
	}
	if (kind == KIND_SAME) {
		if (o->content == CONTENT_SAME) {
			memset(dst, (unsigned char)(seed | 1), o->page_size);
			return;
		}
		for (; p < end; p += 8)
			bench_le(p, seed | 1, 8);
		return;
	}
	bench_le(p, UINT64_C(0x4547415042574d43), 8);
	bench_le(p + 8, 1, 4);
	bench_le(p + 12, 0, 4);
	bench_le(p + 16, index, 8);
	bench_le(p + 24, seed, 8);
	p += 32;
	switch (kind) {
	case KIND_TEXT: {
		unsigned char *start = p;

		while (p < end) {
			uint64_t r = bench_rng(&state);
			unsigned int a = (unsigned int)(r & 63);
			unsigned int b = (unsigned int)((r >> 6) & 63);
			unsigned int c = (unsigned int)((r >> 24) & 63);
			unsigned int w = a < b ? a : b;
			size_t length;

			w = w < c ? w : c;
			if ((r & 3) == 0 && p - start > 128) {
				size_t back = 1 + (r >> 32) % (size_t)(p - start - 1);
				size_t k;

				length = 16 + (r >> 40) % 48;
				for (k = 0; k < length && p < end; k++, p++)
					*p = *(p - back);
				continue;
			}
			length = strlen(words[w]);
			if (length > (size_t)(end - p))
				length = (size_t)(end - p);
			memcpy(p, words[w], length);
			p += length;
			if (p < end && ((r >> 12) & 31) == 0) {
				unsigned int number = (unsigned int)((r >> 20) % 10000);
				char digits[8];
				int k, n = 0;

				*p++ = ' ';
				do {
					digits[n++] = (char)('0' + number % 10);
					number /= 10;
				} while (number);
				for (k = n - 1; k >= 0 && p < end; k--)
					*p++ = (unsigned char)digits[k];
			}
			if (p < end)
				*p++ = (++nw % 12) ? ' ' : '\n';
		}
		break;
	}
	case KIND_RECORD: {
		size_t records = (size_t)(end - p) / 64;

		memset(p, 0, (size_t)(end - p));
		for (i = 0; i < records; i++, p += 64) {
			uint64_t r = bench_rng(&state), id = (uint64_t)index * records + i;
			const char *name = names[(r >> 13) & 7];

			bench_le(p, id, 8);
			bench_le(p + 8, (uint32_t)seed + i * 1000 + (r & 15), 4);
			bench_le(p + 12, (r >> 8) & 7, 2);
			bench_le(p + 14, (r >> 11) & 3, 2);
			memcpy(p + 16, name, strlen(name));
			bench_le(p + 32, (r >> 16) & 255, 8);
			bench_le(p + 40, (uint32_t)id ^ UINT32_C(0x5a5a0000), 4);
		}
		break;
	}
	case KIND_ALPHA: {
		unsigned int symbols = 4 + (unsigned int)((seed >> 7) % 13);

		while (p < end) {
			uint64_t r = bench_rng(&state);
			unsigned int k;

			for (k = 0; k < 9 && p < end; k++) {
				unsigned int value = (unsigned int)((r >> (k * 7)) & 127);
				unsigned int run = 1 + (value & 3);
				unsigned char ch = (unsigned char)('a' + (value >> 2) % symbols);

				while (run-- && p < end)
					*p++ = ch;
			}
		}
		break;
	}
	case KIND_RANDOM:
		for (; p < end; p += 8)
			bench_le(p, bench_rng(&state), 8);
		break;
	default:
		break;
	}
}

static inline void bench_order(const struct bench_options *o, struct bench_sample *samples)
{
	uint64_t state = bench_mix64(o->seed ^ UINT64_C(0x72656661756c74)) | 1;
	size_t i;

	for (i = 0; i < o->pages; i++)
		samples[i].index = i;
	if (!o->random_order)
		return;
	for (i = o->pages - 1; i > 0; i--) {
		size_t j = (size_t)(bench_rng(&state) % (i + 1));
		size_t tmp = samples[i].index;

		samples[i].index = samples[j].index;
		samples[j].index = tmp;
	}
}

static inline void bench_observer_open(struct bench_observer *observer)
{
	const char *type = getenv("COMPARE_MEMCOMP_TYPE");
	const char *swap_type = getenv("COMPARE_SWAP_TYPE");
	uint64_t value;

	*observer = (struct bench_observer) {
		.pagemap = open("/proc/self/pagemap", O_RDONLY | O_CLOEXEC),
		.mincore_available = true, .memcomp_type = -1, .swap_type = -1
	};
	if (type) {
		if (bench_number(type, 31, &value))
			observer->errors++;
		else
			observer->memcomp_type = (int)value;
	}
	if (swap_type) {
		if (bench_number(swap_type, 31, &value))
			observer->errors++;
		else
			observer->swap_type = (int)value;
	}
	if (observer->memcomp_type >= 0 && observer->memcomp_type == observer->swap_type) {
		fprintf(stderr, "WARN memcomp type equals real swap type; identities unknown\n");
		observer->memcomp_type = observer->swap_type = -1;
		observer->errors++;
	}
	if (observer->pagemap < 0) {
		fprintf(stderr, "WARN pagemap unavailable: %s; PTE fields unknown\n", strerror(errno));
		observer->errors++;
	}
}

/* Both syscalls are outside the timed first-byte load. No PFNs/token IDs used. */
static inline void bench_observe(struct bench_observer *observer,
				 unsigned char *address, size_t page_size,
				 struct bench_observation *result)
{
	unsigned char vector;
	uint64_t entry;
	ssize_t got;
	int rc;

	*result = (struct bench_observation) { .present = -1, .swapped = -1,
		.resident = -1, .swap_type = -1, .memcomp = -1, .real_swap = -1 };
	if (observer->pagemap >= 0) {
		off_t offset = (off_t)(((uintptr_t)address / page_size) * sizeof(entry));

		do {
			got = pread(observer->pagemap, &entry, sizeof(entry), offset);
		} while (got < 0 && errno == EINTR && !bench_interrupted);
		if (got == (ssize_t)sizeof(entry)) {
			result->present = (int)((entry >> 63) & 1);
			result->swapped = (int)((entry >> 62) & 1);
			if (!result->present && result->swapped) {
				result->swap_type = (int)(entry & 31);
				result->memcomp = observer->memcomp_type >= 0 &&
					result->swap_type == observer->memcomp_type;
				result->real_swap = observer->swap_type >= 0 &&
					result->swap_type == observer->swap_type;
			}
		} else {
			fprintf(stderr, "WARN pagemap read bytes=%zd errno=%d; PTE fields unknown\n",
				got, got < 0 ? errno : 0);
			close(observer->pagemap);
			observer->pagemap = -1;
			observer->errors++;
		}
	}
	/* mincore reports memcomp tokens as resident, not swap-cache hits. */
	if (result->memcomp == 1 ||
	    (result->swapped == 1 && result->real_swap != 1))
		return;
	if (observer->mincore_available) {
		do {
			rc = mincore(address, page_size, &vector);
		} while (rc && errno == EINTR && !bench_interrupted);
		if (!rc)
			result->resident = vector & 1;
		else {
			fprintf(stderr, "WARN mincore: %s; residency unknown\n", strerror(errno));
			observer->mincore_available = false;
			observer->errors++;
		}
	}
}

static inline enum bench_class bench_classify(const struct bench_observation *o)
{
	if (o->present == 1)
		return CLASS_PRESENT;
	if (o->present == 0 && o->memcomp == 1)
		return CLASS_MEMCOMP;
	if (o->present == 0 && o->swapped == 1) {
		if (o->real_swap != 1)
			return CLASS_UNKNOWN;
		if (o->resident == 0)
			return CLASS_SWAPPED_NONRESIDENT;
		if (o->resident == 1)
			return CLASS_CACHED_NONPRESENT;
	}
	if (o->present == 0 && o->swapped == 0 && o->resident == 0)
		return CLASS_OTHER_NONRESIDENT;
	return CLASS_UNKNOWN;
}

static inline const char *bench_class_name(enum bench_class kind)
{
	static const char *const names[] = {
		"present", "memcomp", "swapped_nonresident", "other_nonresident",
		"cached_nonpresent", "unknown"
	};

	return names[kind];
}

/* Membership is frozen by the post-drain, pre-read snapshot. Unknown
 * special entries and present PTEs never become invented refaults.
 */
static inline int bench_refault_eligible(const struct bench_sample *sample)
{
	enum bench_class kind = bench_classify(&sample->snapshot);

	return kind == CLASS_MEMCOMP || kind == CLASS_SWAPPED_NONRESIDENT;
}

static inline int bench_refault_counted(const struct bench_sample *sample)
{
	enum bench_class kind = bench_classify(&sample->before);

	return bench_refault_eligible(sample) &&
		(kind == CLASS_MEMCOMP || kind == CLASS_SWAPPED_NONRESIDENT ||
		 kind == CLASS_CACHED_NONPRESENT);
}

static inline int bench_readahead_hit(const struct bench_sample *sample)
{
	return bench_refault_counted(sample) &&
		bench_classify(&sample->snapshot) == CLASS_SWAPPED_NONRESIDENT &&
		bench_classify(&sample->before) == CLASS_CACHED_NONPRESENT;
}

static inline struct bench_population bench_population(const struct bench_sample *samples,
						       size_t snapshot_pages, size_t done)
{
	struct bench_population population = { 0 };

	/* Allocation/setup failures still export an error/header safely. */
	if (!samples)
		return population;
	/* Eligibility is the entire frozen phase, even on a partial read. */
	for (size_t i = 0; i < snapshot_pages; i++)
		population.eligible += bench_refault_eligible(&samples[i]);
	for (size_t i = 0; i < done; i++) {
		population.counted += bench_refault_counted(&samples[i]);
		population.readahead += bench_readahead_hit(&samples[i]);
	}
	return population;
}

static inline void bench_sample_csv_header(void)
{
	fputs(",snapshot_swap_type,snapshot_classification,refault_eligible,"
	      "refault_counted,readahead_hit", stdout);
}

static inline void bench_sample_csv(const struct bench_sample *sample)
{
	printf(",%d,%s,%d,%d,%d", sample->snapshot.swap_type,
	       bench_class_name(bench_classify(&sample->snapshot)),
	       bench_refault_eligible(sample), bench_refault_counted(sample),
	       bench_readahead_hit(sample));
}

static inline void bench_drain_csv_header(void)
{
	fputs(",drain_rounds,drain_remaining_swapcache_pages,drain_remapped_pages,"
	      "refault_eligible_pages,refault_counted_pages,readahead_hit_pages,"
	      "readahead_hit_ratio", stdout);
}

static inline void bench_drain_csv(const struct bench_drain *drain,
				  const struct bench_population *population)
{
	printf(",%u,%zu,%zu,%zu,%zu,%zu,", drain->rounds, drain->remaining,
	       drain->remapped, population->eligible, population->counted,
	       population->readahead);
	/* An empty denominator is N/A, not an invented zero-percent hit rate. */
	if (population->counted)
		printf("%.9f", (double)population->readahead / population->counted);
}

static inline int bench_snapshot(unsigned char *map, const struct bench_options *o,
				 struct bench_observer *observer, struct bench_sample *samples)
{
	size_t i;

	for (i = 0; i < o->pages; i++) {
		if (bench_interrupted) {
			errno = EINTR;
			return -1;
		}
		bench_observe(observer, map + samples[i].index * o->page_size,
			      o->page_size, &samples[i].snapshot);
	}
	return 0;
}

static inline int bench_fill_page(unsigned char *dst, const struct bench_options *o,
				  size_t index)
{
	if (index >= o->pages) {
		errno = ERANGE;
		return -1;
	}
	if (o->corpus)
		return bench_corpus_load(o->corpus, o->corpus_first + index, dst);
	if (!o->functional_synthetic) {
		errno = EINVAL;
		return -1;
	}
	bench_gen_page(dst, o, index);
	return 0;
}

/* 0 match, 1 data mismatch, -1 I/O/format failure; outside touch timing. */
static inline int bench_verify_page(const unsigned char *page,
				    const struct bench_options *o, size_t index,
				    unsigned char *scratch)
{
	if (o->corpus) {
		if (!bench_corpus_verify(o->corpus, o->corpus_first + index, page))
			return 0;
		return errno == EBADMSG ? 1 : -1;
	}
	if (!o->functional_synthetic) {
		errno = EINVAL;
		return -1;
	}
	bench_gen_page(scratch, o, index);
	return memcmp(page, scratch, o->page_size) != 0;
}

/* Only the volatile first-byte load lies between the two clock reads.
 * Full-page verification is immediate, but outside that interval.
 */
static inline int bench_measure(unsigned char *map, const struct bench_options *o,
				struct bench_observer *observer, struct bench_sample *samples,
				unsigned char *expected, size_t *done, uint64_t *wall_ns,
				size_t *mismatches)
{
	uint64_t wall_start, wall_end;
	size_t i;

	if (bench_console_drain() || bench_now(&wall_start))
		return -1;
	for (i = 0; i < o->pages; i++) {
		struct bench_sample *s = &samples[i];
		unsigned char *page = map + s->index * o->page_size;
		uint64_t start, end;

		if (bench_sleep_us(o->gap_us) || bench_console_drain())
			return -1;
		bench_observe(observer, page, o->page_size, &s->before);
		s->cpu = sched_getcpu();
		if (bench_now(&start))
			return -1;
		(void)*(volatile unsigned char *)page;
		if (bench_now(&end))
			return -1;
		s->ns = end - start;
		int verification = bench_verify_page(page, o, s->index, expected);

		if (verification < 0)
			return -1;
		s->verified = verification == 0;
		if (!s->verified) {
			(*mismatches)++;
			if (*mismatches <= 8)
				fprintf(stderr, "DATA_MISMATCH page=%zu\n", s->index);
		}
		*done = i + 1;
	}
	if (bench_now(&wall_end))
		return -1;
	*wall_ns = wall_end - wall_start;
	return 0;
}

static inline int bench_csv(const char *workload, const struct bench_options *o,
			    const struct bench_sample *samples, size_t done,
			    const struct bench_drain *drain,
			    const struct bench_writeback *wb)
{
	static const char *const kinds[] = { "zero", "same", "text", "record", "alpha", "random", "real-unclassified" };
	struct bench_population population = bench_population(samples, o->pages, done);

	fputs("workload,content,seed,page_size,order,sequence,page_index,page_kind,"
	      "snapshot_present,snapshot_swapped,snapshot_resident,present_before,"
	      "swapped_before,resident_before,classification,touch_ns,verify_ok,cpu_before,swap_type_before",
	      stdout);
	bench_sample_csv_header();
	bench_drain_csv_header();
	fputs(",method,purge_ns,drain_timed_out,writeback_source,writeback_before,"
	     "writeback_after,writeback_delta,writeback_wait_ns,writeback_wait_timed_out,"
	     "writeback_stable_polls,pressure_bytes,pressure_ready,writeback_explicit_idle,writeback_proven", stdout);
	if (o->corpus)
		fputs(",corpus_source,corpus_page_index", stdout);
	putchar('\n');
	for (size_t i = 0; i < done; i++) {
		const struct bench_sample *sample = &samples[i];

		printf("%s,%s,%" PRIu64 ",%zu,%s,%zu,%zu,%s,%d,%d,%d,%d,%d,%d,%s,%" PRIu64 ",%d,%d,%d",
		       workload, bench_content_name(o->content), o->seed, o->page_size,
		       o->random_order ? "rand" : "seq", i, sample->index,
		       kinds[bench_page_kind(o, sample->index)], sample->snapshot.present,
		       sample->snapshot.swapped, sample->snapshot.resident, sample->before.present,
		       sample->before.swapped, sample->before.resident,
		       bench_class_name(bench_classify(&sample->before)), sample->ns,
		       sample->verified, sample->cpu, sample->before.swap_type);
		bench_sample_csv(sample);
		bench_drain_csv(drain, &population);
		printf(",v4.6,%" PRIu64 ",%d", drain->purge_ns, drain->timed_out);
		if (wb) {
			printf(",%s,%" PRIu64 ",%" PRIu64 ",", wb->source, wb->before, wb->after);
			if (wb->after >= wb->before)
				printf("%" PRIu64, wb->after - wb->before);
			printf(",%" PRIu64 ",%d,%u,%zu,%d,%d,%d", wb->wait_ns, wb->timed_out,
			       wb->stable_polls, wb->pressure_bytes, wb->pressure_ready,
			       wb->explicit_idle, wb->proven);
		} else {
			fputs(",,,,,,,,,,,", stdout);
		}
		bench_input_csv(o, sample->index);
		putchar('\n');
	}
	return fflush(stdout) || ferror(stdout) ? -1 : 0;
}

static inline int bench_meminfo(long *free_kib, long *available_kib)
{
	FILE *f = fopen("/proc/meminfo", "re");
	char line[256];

	*free_kib = *available_kib = -1;
	if (!f)
		return -1;
	while (fgets(line, sizeof(line), f)) {
		(void)sscanf(line, "MemFree: %ld kB", free_kib);
		(void)sscanf(line, "MemAvailable: %ld kB", available_kib);
	}
	int bad = ferror(f);
	if (fclose(f) || bad || *free_kib < 0 || *available_kib < 0) {
		errno = EIO;
		return -1;
	}
	return 0;
}

static inline int bench_counter(const char *path, const char *key, uint64_t *value)
{
	FILE *file = fopen(path, "re");
	char name[96];
	unsigned long long number;
	bool found = false;
	int bad;

	if (!file)
		return -1;
	while (fscanf(file, "%95s %llu", name, &number) == 2) {
		if (!strcmp(name, key)) {
			*value = (uint64_t)number;
			found = true;
			break;
		}
	}
	bad = ferror(file);
	if (fclose(file) || bad || !found) {
		errno = EIO;
		return -1;
	}
	return 0;
}

static inline int bench_wait_writeback(struct bench_drain *drain)
{
	uint64_t start, now, value;

	if (bench_now(&start))
		return -1;
	for (;;) {
		if (bench_counter("/proc/vmstat", "nr_writeback", &value))
			return -1;
		if (!value)
			return 0;
		if (bench_now(&now))
			return -1;
		if (now - start >= UINT64_C(10000000000)) {
			drain->timed_out = 1;
			errno = ETIMEDOUT;
			return -1;
		}
		if (bench_sleep_us(10000))
			return -1;
	}
}

static inline int bench_drain_pageout(unsigned char *map, const struct bench_options *o,
				     struct bench_observer *observer, struct bench_drain *drain,
				     bool retry_present)
{
	unsigned int errors = observer->errors;

	*drain = (struct bench_drain){ 0 };
	if (bench_wait_writeback(drain))
		return -1;
	for (;;) {
		uint64_t a, b;
		size_t present = 0;

		drain->remaining = 0;
		for (size_t i = 0; i < o->pages; i++) {
			struct bench_observation observation;

			if (bench_interrupted) {
				errno = EINTR;
				return -1;
			}
			bench_observe(observer, map + i * o->page_size, o->page_size, &observation);
			if (observer->errors != errors || observer->pagemap < 0) {
				errno = EIO;
				return -1;
			}
			/* A failed compression can put an unchanged, marked folio
			 * back on the LRU. A later PAGEOUT reaches real swap. W1
			 * drains these present pages too, without touching them.
			 */
			if (retry_present && observation.present == 1)
				present++;
			if (bench_classify(&observation) != CLASS_CACHED_NONPRESENT)
				continue;
			drain->remaining++;
			/* Real swap + cache only: never read a memcomp/unknown token.
			 * The minor-fault remap is deliberately outside every timer.
			 */
			if (drain->rounds < 4) {
				(void)*(volatile unsigned char *)(map + i * o->page_size);
				drain->remapped++;
			}
		}
		if ((!drain->remaining && !present) || drain->rounds == 4)
			break;
		/* PAGEOUT cannot free an unmapped swap-cache folio. Remapping
		 * above makes the clean folio reachable by this whole-range call.
		 */
		if (bench_console_drain() || bench_now(&a) ||
		    madvise(map, o->bytes, MADV_PAGEOUT) || bench_now(&b))
			return -1;
		drain->purge_ns += b - a;
		drain->rounds++;
		if (bench_wait_writeback(drain))
			return -1;
	}
	return 0;
}

struct bench_pressure_child {
	pid_t pid;
	int stop_fd;
};

static inline _Noreturn void bench_pressure_worker(const struct bench_options *o, size_t bytes,
						 int ready, int stop, pid_t parent)
{
	uint64_t state = bench_mix64(o->seed ^ UINT64_C(0x7072657373757265)) | 1;
	const char *workload = getenv("COMPARE_WORKLOAD_LABEL");
	bool w4 = workload && !strcmp(workload, "w4");
	unsigned char *map = MAP_FAILED;
	char command;
	ssize_t count;
	size_t offset, word, ramp_bytes = w4 ? bytes / o->page_size / 7 * 2 * o->page_size : 0;
	size_t mapped_bytes = bytes + ramp_bytes;
	struct bench_options input = *o;
	int rc = 1;

	if (prctl(PR_SET_PDEATHSIG, SIGKILL))
		goto error;
	if (getppid() != parent) {
		errno = ECHILD;
		goto error;
	}
	if (o->corpus) {
		if (bench_input_extra(o, mapped_bytes / o->page_size, &input.corpus_first))
			goto error;
		input.pages = mapped_bytes / o->page_size;
	}
	map = mmap(NULL, mapped_bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (map == MAP_FAILED || madvise(map, mapped_bytes, MADV_NOHUGEPAGE))
		goto error;
	if (w4)
		fprintf(stderr, "PRESSURE pattern=%s held_bytes=%zu ramp_pattern=%s ramp_bytes=%zu\n",
			o->corpus ? "real-corpus" : "functional-halfzero", bytes,
			o->corpus ? "real-corpus" : "functional-random", ramp_bytes);
	for (offset = 0; offset < mapped_bytes; offset += o->page_size) {
		volatile uint64_t *page = (volatile uint64_t *)(map + offset);
		bool half_zero = w4 && offset < bytes;

		if (bench_interrupted) {
			errno = EINTR;
			goto error;
		}
		/* Real runs retain the original held/ramp sizes, with disjoint
		 * unchanged corpus slices. Synthetic patterns below are reachable
		 * only for explicit host functional fixtures, never comparisons.
		 */
		if (o->corpus) {
			if (bench_fill_page(map + offset, &input, offset / o->page_size))
				goto error;
		} else if (o->functional_synthetic) {
			for (word = 0; word < o->page_size / sizeof(*page); word++) {
				if (half_zero && word >= o->page_size / (2 * sizeof(*page)))
					page[word] = 0;
				else
					page[word] = bench_rng(&state);
			}
		} else {
			errno = EINVAL;
			goto error;
		}
		/* Let kswapd enter moderate pressure scopes while filling, not
		 * only after a burst has crossed directly below the min mark.
		 * This untimed 1 ms/MiB pacing is fixed across W4 backends.
		 */
		if (w4 && !((offset + o->page_size) % (1024 * 1024)) &&
		    bench_sleep_us(1000))
			goto error;
	}
	if (ramp_bytes) {
		if (munmap(map + bytes, ramp_bytes))
			goto error;
		mapped_bytes = bytes;
		fprintf(stderr, "PRESSURE ramp_released_bytes=%zu held_bytes=%zu\n", ramp_bytes, bytes);
	}
	do {
		count = write(ready, "R", 1);
	} while (count < 0 && errno == EINTR && !bench_interrupted);
	if (count != 1) {
		if (count >= 0)
			errno = EIO;
		goto error;
	}
	close(ready);
	ready = -1;
	/* No churn or unbounded growth: hold this mapping until parent closes. */
	do {
		count = read(stop, &command, 1);
	} while (count < 0 && errno == EINTR && !bench_interrupted);
	if (count < 0)
		goto error;
	rc = 0;
	goto out;
error:
	fprintf(stderr, "ERROR pressure_child errno=%d message=%s\n", errno, strerror(errno));
out:
	if (ready >= 0)
		close(ready);
	close(stop);
	if (map != MAP_FAILED && munmap(map, mapped_bytes))
		rc = 1;
	_exit(rc);
}

static inline int bench_start_pressure(const struct bench_options *o, size_t bytes,
				       struct bench_pressure_child *child,
				       unsigned char *target, int file, int directory)
{
	int ready[2] = { -1, -1 }, stop[2] = { -1, -1 };
	pid_t parent = getpid();
	char message;
	ssize_t count;
	uint64_t now, deadline;
	int saved;

	if (!bytes)
		return 0;
	if (pipe2(ready, O_CLOEXEC | O_NONBLOCK) || pipe2(stop, O_CLOEXEC))
		goto error;
	child->pid = fork();
	if (child->pid < 0)
		goto error;
	if (!child->pid) {
		const char *phase_write = getenv("COMPARE_PHASE_WRITE_FD");
		const char *phase_read = getenv("COMPARE_PHASE_READ_FD");
		int phase_fd;

		close(ready[0]);
		close(stop[1]);
		/* Do not turn the target into an extra mapping held by the helper. */
		if (munmap(target, o->bytes)) {
			perror("pressure child unmap inherited target");
			_exit(1);
		}
		if (file >= 0)
			close(file);
		if (directory >= 0)
			close(directory);
		if (phase_write && !bench_phase_fd(phase_write, &phase_fd))
			close(phase_fd);
		if (phase_read && !bench_phase_fd(phase_read, &phase_fd))
			close(phase_fd);
		bench_pressure_worker(o, bytes, ready[1], stop[0], parent);
	}
	close(ready[1]);
	ready[1] = -1;
	close(stop[0]);
	stop[0] = -1;
	child->stop_fd = stop[1];
	stop[1] = -1;
	if (bench_now(&now))
		goto error;
	/* Real O_DIRECT loading is untimed setup, unlike the old generator. */
	deadline = now + (o->corpus ? UINT64_C(120000000000) : UINT64_C(30000000000));
	for (;;) {
		if (bench_phase_wait(ready[0], POLLIN, deadline))
			goto error;
		count = read(ready[0], &message, 1);
		if (count < 0 && (errno == EINTR || errno == EAGAIN))
			continue;
		break;
	}
	if (count != 1 || message != 'R') {
		if (count >= 0)
			errno = EIO;
		goto error;
	}
	close(ready[0]);
	return 0;
error:
	saved = errno;
	if (ready[0] >= 0)
		close(ready[0]);
	if (ready[1] >= 0)
		close(ready[1]);
	if (stop[0] >= 0)
		close(stop[0]);
	if (stop[1] >= 0)
		close(stop[1]);
	errno = saved;
	/* A forked child remains owned by main and is reaped on every exit. */
	return -1;
}

/* 1 reaped, 0 deadline, -1 error; never reuse a reaped/unknown PID. */
static inline int bench_wait_pressure(struct bench_pressure_child *child, int *status, bool interruptible)
{
	uint64_t now, deadline;

	if (bench_now(&now))
		return -1;
	deadline = now + UINT64_C(5000000000);
	for (;;) {
		pid_t got = waitpid(child->pid, status, WNOHANG);
		struct timespec delay = { .tv_nsec = 20000000 };

		if (got == child->pid) {
			child->pid = -1;
			return 1;
		}
		if (got < 0 && errno != EINTR) {
			if (errno == ECHILD)
				child->pid = -1;
			return -1;
		}
		if (bench_now(&now))
			return -1;
		if (now >= deadline || (interruptible && bench_interrupted))
			return 0;
		(void)nanosleep(&delay, NULL);
	}
}

static inline int bench_finish_pressure(struct bench_pressure_child *child, bool aborting)
{
	int status, result;
	bool forced = aborting;

	if (child->stop_fd >= 0) {
		close(child->stop_fd);
		child->stop_fd = -1;
	}
	if (child->pid <= 0)
		return 0;
	/* SIGCHLD is reset before fork; an unreaped child pins its PID. */
	if (aborting && kill(child->pid, SIGKILL) && errno != ESRCH)
		perror("kill owned pressure child");
	result = bench_wait_pressure(child, &status, !aborting);
	if (!result) {
		forced = true;
		if (kill(child->pid, SIGKILL) && errno != ESRCH)
			perror("kill owned pressure child after grace");
		result = bench_wait_pressure(child, &status, false);
	}
	if (result <= 0) {
		if (!result) {
			fprintf(stderr, "ERROR owned pressure child pid=%ld unreaped; VM supervisor required\n",
				(long)child->pid);
			errno = ETIMEDOUT;
		}
		return -1;
	}
	fprintf(stderr, "PRESSURE_EXIT aborted=%d forced=%d exited=%d code=%d signal=%d\n",
		aborting, forced, WIFEXITED(status), WIFEXITED(status) ? WEXITSTATUS(status) : -1,
		WIFSIGNALED(status) ? WTERMSIG(status) : 0);
	if (!aborting && (forced || !WIFEXITED(status) || WEXITSTATUS(status))) {
		errno = ECHILD;
		return -1;
	}
	return 0;
}

static inline int bench_compare_ns(const void *left, const void *right)
{
	uint64_t a = *(const uint64_t *)left, b = *(const uint64_t *)right;

	return (a > b) - (a < b);
}

static inline int bench_summary(const char *workload, const struct bench_options *o,
				const struct bench_sample *samples, size_t done,
				size_t mismatches, uint64_t wall_ns,
				unsigned int observation_errors, int rc)
{
	uint64_t *latencies = NULL;
	int group;
	size_t i;

	fprintf(stderr,
		"SUMMARY workload=%s status=%s completed=%zu requested=%zu mismatches=%zu "
		"observation_errors=%u read_phase_wall_ns=%" PRIu64 "\n",
		workload, rc ? "error" : "complete", done, o->pages, mismatches,
		observation_errors, wall_ns);
	if (done) {
		latencies = malloc(done * sizeof(*latencies));
		if (!latencies)
			return -1;
	}
	/* Anonymous swap-token eligibility is not a tmpfs refault definition.
	 * W3 uses the immediate pre-touch present bit, across every backend.
	 * W4 describes the whole segment, not a written-back-page subset.
	 */
	for (group = (!strcmp(workload, "w3") || !strcmp(workload, "w4")) ? -1 : -2;
	     group < CLASS_COUNT + !strcmp(workload, "w3"); group++) {
		const char *name = group == CLASS_COUNT ? "nonpresent_first_touch" :
			(group == -2 ? "refault_snapshot_eligible" :
			(group == -1 ? "all_first_touches" : bench_class_name((enum bench_class)group)));
		long double sum = 0;
		size_t n = 0;

		for (i = 0; i < done; i++) {
			if (group == -2 && !bench_refault_counted(&samples[i]))
				continue;
			if (group == CLASS_COUNT && samples[i].before.present != 0)
				continue;
			if (group >= 0 && group < CLASS_COUNT &&
			    bench_classify(&samples[i].before) != (enum bench_class)group)
				continue;
			latencies[n++] = samples[i].ns;
			sum += samples[i].ns;
		}
		if (group == CLASS_COUNT)
			fprintf(stderr, "NONPRESENT count=%zu requested=%zu fraction=%.9f\n",
				n, o->pages, (double)n / o->pages);
		if (!n) {
			fprintf(stderr, "LATENCY population=%s count=0\n", name);
			continue;
		}
		qsort(latencies, n, sizeof(*latencies), bench_compare_ns);
		fprintf(stderr,
			"LATENCY population=%s count=%zu mean_ns=%.0Lf min_ns=%" PRIu64
			" p50_ns=%" PRIu64 " p90_ns=%" PRIu64 " p99_ns=%" PRIu64 " max_ns=%" PRIu64 "\n",
			name, n, sum / n, latencies[0], latencies[(n * 50 + 99) / 100 - 1],
			latencies[(n * 90 + 99) / 100 - 1], latencies[(n * 99 + 99) / 100 - 1],
			latencies[n - 1]);
	}
	free(latencies);
	return fflush(stderr) || ferror(stderr) ? -1 : 0;
}

#endif /* MEMCOMP_COMPARE_COMMON_H */
