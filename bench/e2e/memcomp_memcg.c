// SPDX-License-Identifier: GPL-2.0
/*
 * Standalone v6.12.23 memcg attribution evidence collector.
 * Build (header-only accounting parser): cc -std=gnu11 -O2 -Wall -Wextra -Werror -Wformat=2 -Wshadow
 *           -Wconversion memcomp_memcg.c -o <scratch>/memcomp-memcg
 * Requires --disposable-guest AND memcomp_memcg.disposable_guest=1 on
 * cmdline, QEMU/KVM, root/CAP_SYS_ADMIN, proc/sysfs/cgroup2 mounts and
 * root +memory delegation. Never changes root delegation/swap/limits.
 *
 * mm/memcomp.c:1433-1625 binds original folio objcg and separately
 * charges entry metadata/payload. 6.12 obj_cgroup_charge_pages updates
 * MEMCG_KMEM (memory.stat kernel); charge/uncharge use page rounding and
 * per-CPU stocks, NOT exact bytes, and do not update slab/zswap stats.
 * Global memcomp/stat is not per-cgroup; type30 can be backed by
 * writeback. Anon residues/kernel deltas/aggregate release are not exact
 * payload proof. Runtime acceptance requires loss-free memcomp_account trace.
 * Parser exit 0 is syntax success ONLY. --parse-evidence never PASSes.
 *
 * Catchable signals/errors/timeouts kill/reap our worker and remove only
 * our groups. PDEATHSIG protects lost supervisor. Supervisor SIGKILL or
 * power loss cannot guarantee removal of empty cgroup directories.
 */
#define _GNU_SOURCE
#define _FILE_OFFSET_BITS 64
#include <ctype.h>
#include <errno.h>
#include <dirent.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <linux/magic.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include "memcomp_account_trace.h"
#define TEXT_MAX 65536U
#define KV_MAX 256U
#define PHASES 5U
#define MAX_PAGES 65536U
#define CGROOT "/sys/fs/cgroup"
#define MCDIR "/sys/kernel/mm/memcomp"
#define PM_PRESENT (UINT64_C(1) << 63)
#define PM_SWAP (UINT64_C(1) << 62)
#define PM_LOW ((UINT64_C(1) << 55) - 1)
#define BOOT_OPT "memcomp_memcg.disposable_guest=1"
struct kv { char key[96]; uint64_t value; };
struct table { struct kv item[KV_MAX]; size_t count; };
struct group_sample {
	uint64_t current, anon, kernel;
	struct table stat;
	char current_raw[64], stat_raw[TEXT_MAX], events_raw[4096];
	char swap_raw[64], pressure_raw[4096], cg_events_raw[1024];
};
struct snapshot {
	struct group_sample a, b;
	uint64_t stored, compressed, pool, tokens, present;
	char mc_raw[256];
	uint64_t start_ms, end_ms;
};
struct evidence { struct snapshot s[PHASES]; uint64_t pages, page_size; };
static const char *const phases[] = {
	"baseline", "allocated", "migrated", "compressed", "released"
};
static volatile sig_atomic_t stopped;
static uint64_t deadline;
static int rootfd = -1, parentfd = -1, afd = -1, bfd = -1;
static bool made_parent, made_a, made_b;
static char group_name[96];
static pid_t worker_pid = -1;
static int command_fd = -1, reply_fd = -1, pmfd = -1;
static struct mat_trace account;
static int tracefd = -1, tracepipe = -1, instancesfd = -1;
static bool made_instance;
static char trace_name[128], trace_partial[2048];
static size_t trace_used;
static unsigned int trace_cpus;
static bool trace_pending;
static int trace_stdout_flags = -1;
static uint64_t trace_cpu_ids;
static int trace_pump(void);
static int fail(const char *what)
{
	fprintf(stderr, "FAIL %s: %s\n", what, strerror(errno));
	return -1;
}
static uint64_t now_ms(void)
{
	struct timespec ts;
	if (clock_gettime(CLOCK_MONOTONIC, &ts)) return 0;
	return (uint64_t)ts.tv_sec * 1000 + (uint64_t)ts.tv_nsec / 1000000;
}
static bool expired(void)
{
	uint64_t now;
	if (trace_pump()) { errno = EPROTO; return true; }
	/* Pumping and raw serial output consume real time; use a fresh clock. */
	now = now_ms();
	if (stopped) { errno = EINTR; return true; }
	if (!now || now >= deadline) { errno = ETIMEDOUT; return true; }
	return false;
}
static void on_signal(int sig) { stopped = sig; }
static int decimal(const char *p, uint64_t *value)
{
	uint64_t n = 0;
	if (!*p) return -1;
	for (; *p; p++) {
		unsigned int digit;
		if (*p < '0' || *p > '9') return -1;
		digit = (unsigned int)(*p - '0');
		if (n > (UINT64_MAX - digit) / 10) return -1;
		n = n * 10 + digit;
	}
	*value = n;
	return 0;
}
static int key_value(const struct table *t, const char *key, uint64_t *out)
{
	size_t i;
	for (i = 0; i < t->count; i++)
		if (!strcmp(t->item[i].key, key)) {
			*out = t->item[i].value;
			return 0;
		}
	return -1;
}
static int parse_table(const char *raw, struct table *t)
{
	const char *p = raw;
	memset(t, 0, sizeof(*t));
	while (*p) {
		char key[96], number[32];
		size_t k = 0, n = 0;
		uint64_t ignored, value;
		while (*p && (isalnum((unsigned char)*p) || *p == '_')) {
			if (k == sizeof(key) - 1) return -1;
			key[k++] = *p++;
		}
		key[k] = 0;
		if (!k || (*p != ' ' && *p != '\t')) return -1;
		while (*p == ' ' || *p == '\t') p++;
		while (*p >= '0' && *p <= '9') {
			if (n == sizeof(number) - 1) return -1;
			number[n++] = *p++;
		}
		number[n] = 0;
		if (*p != '\n' || decimal(number, &value) ||
		    t->count == KV_MAX || !key_value(t, key, &ignored)) return -1;
		p++;
		strcpy(t->item[t->count].key, key);
		t->item[t->count++].value = value;
	}
	return t->count ? 0 : -1;
}
static int parse_numbers(const char *raw, uint64_t *v, size_t wanted)
{
	const char *p = raw;
	size_t i;
	for (i = 0; i < wanted; i++) {
		char number[32];
		size_t n = 0;
		while (*p >= '0' && *p <= '9') {
			if (n == sizeof(number) - 1) return -1;
			number[n++] = *p++;
		}
		number[n] = 0;
		if (decimal(number, &v[i])) return -1;
		if (i + 1 < wanted) {
			if (*p != ' ' && *p != '\t') return -1;
			while (*p == ' ' || *p == '\t') p++;
		}
	}
	return *p == '\n' && !p[1] ? 0 : -1;
}
static int memory_stat(const char *raw, struct table *t)
{
	uint64_t dummy;
	return parse_table(raw, t) || key_value(t, "anon", &dummy) ||
		key_value(t, "file", &dummy) || key_value(t, "kernel", &dummy) ||
		key_value(t, "pagetables", &dummy) ||
		key_value(t, "slab_reclaimable", &dummy) ||
		key_value(t, "slab_unreclaimable", &dummy) ? -1 : 0;
}
static int pagemap_state(uint64_t entry, uint64_t *present, uint64_t *tokens)
{
	bool resident = !!(entry & PM_PRESENT), swapped = !!(entry & PM_SWAP);
	if (resident == swapped || (entry & (UINT64_C(15) << 58))) return -1;
	if (resident) { (*present)++; return 0; }
	if ((entry & 31) == 31) return -1;
	if ((entry & 31) == 30) {
		if (!(entry & (PM_LOW & ~UINT64_C(31)))) return -1;
		(*tokens)++;
	}
	return 0;
}
static int read_text(int fd, char *buf, size_t capacity)
{
	size_t used = 0;
	ssize_t n;
	while (used + 1 < capacity) {
		n = read(fd, buf + used, capacity - used - 1);
		if (n < 0 && errno == EINTR && !stopped) continue;
		if (n < 0) return -1;
		if (!n) break;
		if (memchr(buf + used, 0, (size_t)n)) { errno = EINVAL; return -1; }
		used += (size_t)n;
	}
	if (used + 1 == capacity) {
		char extra;
		if (read(fd, &extra, 1) != 0) { errno = EOVERFLOW; return -1; }
	}
	buf[used] = 0;
	return 0;
}
static int read_at(int dir, const char *name, char *raw, size_t cap)
{
	int fd = openat(dir, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	int r, saved;
	if (fd < 0) return -1;
	r = read_text(fd, raw, cap);
	saved = errno;
	if (close(fd) && !r) return -1;
	errno = saved;
	return r;
}
static int write_at(int dir, const char *name, const char *text)
{
	int fd = openat(dir, name, O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
	size_t size = strlen(text);
	ssize_t n;
	int saved;
	if (fd < 0) return -1;
	do { n = write(fd, text, size); }
	while (n < 0 && errno == EINTR && !stopped);
	saved = errno;
	if (close(fd) && n == (ssize_t)size) return -1;
	errno = n >= 0 && n != (ssize_t)size ? EIO : saved;
	return n == (ssize_t)size ? 0 : -1;
}
static bool word(const char *raw, const char *wanted)
{
	size_t n = strlen(wanted);
	const char *p = raw;
	while (*p) {
		const char *start;
		while (isspace((unsigned char)*p)) p++;
		start = p;
		while (*p && !isspace((unsigned char)*p)) p++;
		if ((size_t)(p - start) == n && !memcmp(start, wanted, n)) return true;
	}
	return false;
}
static int optional_at(int dir, const char *name, char *raw, size_t cap)
{
	if (!read_at(dir, name, raw, cap)) return 0;
	if (errno != ENOENT) return -1;
	snprintf(raw, cap, "UNAVAILABLE ENOENT\n");
	return 0;
}
static int sample_group(int dir, struct group_sample *s)
{
	struct table events;
	if (read_at(dir, "memory.current", s->current_raw, sizeof(s->current_raw)) ||
	    parse_numbers(s->current_raw, &s->current, 1) ||
	    read_at(dir, "memory.stat", s->stat_raw, sizeof(s->stat_raw)) ||
	    memory_stat(s->stat_raw, &s->stat) ||
	    key_value(&s->stat, "anon", &s->anon) ||
	    key_value(&s->stat, "kernel", &s->kernel) ||
	    read_at(dir, "memory.events", s->events_raw, sizeof(s->events_raw)) ||
	    parse_table(s->events_raw, &events) ||
	    read_at(dir, "cgroup.events", s->cg_events_raw, sizeof(s->cg_events_raw)) ||
	    optional_at(dir, "memory.swap.current", s->swap_raw, sizeof(s->swap_raw)) ||
	    optional_at(dir, "memory.pressure", s->pressure_raw, sizeof(s->pressure_raw))) {
		errno = EINVAL;
		return fail("sampling unreadable/malformed cgroup data");
	}
	return 0;
}
static int snapshot(struct snapshot *s)
{
	uint64_t v[3];
	s->start_ms = now_ms();
	if (expired()) return fail("sampling deadline/signal");
	if (sample_group(afd, &s->a) || sample_group(bfd, &s->b)) return -1;
	if (read_at(AT_FDCWD, MCDIR "/stat", s->mc_raw, sizeof(s->mc_raw)))
		return fail("missing global memcomp stat");
	if (parse_numbers(s->mc_raw, v, 3)) {
		errno = EINVAL;
		return fail("malformed global memcomp stat");
	}
	s->stored = v[0]; s->compressed = v[1]; s->pool = v[2];
	s->end_ms = now_ms();
	return 0;
}
static void raw_block(const char *phase, const char *group,
		      const char *name, const char *raw)
{
	printf("RAW_BEGIN phase=%s group=%s file=%s\n%s", phase, group, name, raw);
	if (*raw && raw[strlen(raw) - 1] != '\n') putchar('\n');
	puts("RAW_END");
}
static void report(const struct snapshot *s, unsigned int phase)
{
	const struct group_sample *g[2] = { &s->a, &s->b };
	unsigned int i;
	printf("SAMPLE phase=%s from_ms=%" PRIu64 " to_ms=%" PRIu64
	       " sequential_not_atomic=1 present=%" PRIu64 " type30=%" PRIu64 "\n",
	       phases[phase], s->start_ms, s->end_ms, s->present, s->tokens);
	for (i = 0; i < 2; i++) {
		const char *group = i ? "B" : "A";
		raw_block(phases[phase], group, "memory.current", g[i]->current_raw);
		raw_block(phases[phase], group, "memory.stat", g[i]->stat_raw);
		raw_block(phases[phase], group, "memory.events", g[i]->events_raw);
		raw_block(phases[phase], group, "cgroup.events", g[i]->cg_events_raw);
		raw_block(phases[phase], group, "memory.swap.current", g[i]->swap_raw);
		raw_block(phases[phase], group, "memory.pressure", g[i]->pressure_raw);
	}
	raw_block(phases[phase], "global", "memcomp/stat", s->mc_raw);
	fflush(stdout);
}
/* Equality is NOT a stock drain/RCU grace period. Never use it as proof. */
static bool same_counts(const struct snapshot *x, const struct snapshot *y)
{
	return x->a.current == y->a.current && x->a.anon == y->a.anon &&
		x->a.kernel == y->a.kernel && x->b.current == y->b.current &&
		x->b.anon == y->b.anon && x->b.kernel == y->b.kernel &&
		x->stored == y->stored && x->compressed == y->compressed &&
		x->pool == y->pool;
}
static int settled(struct snapshot *out, unsigned int phase)
{
	struct snapshot *last = calloc(1, sizeof(*last));
	unsigned int equal = 0, reads = 0;
	int r = -1;
	if (!last) return fail("sampling allocation");
	while (!expired()) {
		if (snapshot(out)) break;
		equal = reads && same_counts(out, last) ? equal + 1 : 0;
		reads++;
		if (equal == 2) { r = 0; break; }
		memcpy(last, out, sizeof(*last));
		usleep(100000);
	}
	printf("SETTLE phase=%s reads=%u stable=%d stock_drained=0 rcu_barrier=0\n",
	       phases[phase], reads, !r);
	report(out, phase);
	free(last);
	return r ? fail("sample settling deadline/error") : 0;
}
static void delta(const char *name, uint64_t before, uint64_t after,
		  uint64_t pages)
{
	uint64_t n = after >= before ? after - before : before - after;
	printf("DELTA %s=%s%" PRIu64 " per_test_page=%s%" PRIu64 "/%" PRIu64 "\n",
	       name, after >= before ? "+" : "-", n,
	       after >= before ? "+" : "-", n, pages);
}
static bool release_counts_at_baseline(const struct evidence *e)
{
	const struct snapshot *base = &e->s[0], *released = &e->s[4];

	return !released->tokens && !released->present &&
		released->compressed <= base->compressed &&
		released->stored <= base->stored &&
		released->a.anon <= base->a.anon &&
		released->a.kernel <= base->a.kernel &&
		released->b.kernel <= base->b.kernel;
}

/* Equal adjacent samples do not prove stocks/rstat reached their baseline. */
static int wait_release_counts(struct evidence *e)
{
	unsigned int reads = 0;
	while (!expired()) {
		if (snapshot(&e->s[4])) return -1;
		reads++;
		if (release_counts_at_baseline(e)) {
			printf("RELEASE_BASELINE reads=%u exact_original_gates=1\n", reads);
			return settled(&e->s[4], 4);
		}
		usleep(100000);
	}
	printf("RELEASE_BASELINE reads=%u exact_original_gates=0\n", reads);
	report(&e->s[4], 4);
	return fail("release counters did not reach original baseline");
}

static int assess(const struct evidence *e, bool real_trace)
{
	const struct snapshot *base = &e->s[0], *allocated = &e->s[1];
	const struct snapshot *migrated = &e->s[2], *compressed = &e->s[3];
	uint64_t bytes = e->pages * e->page_size;
	bool allocation, migration, tokens, anon_drop, payload, release;
	unsigned int i;
	allocation = allocated->a.anon >= base->a.anon &&
		allocated->a.anon - base->a.anon >= bytes &&
		allocated->present == e->pages && !allocated->tokens;
	migration = migrated->a.anon == allocated->a.anon &&
		migrated->b.anon == base->b.anon &&
		migrated->present == e->pages && !migrated->tokens;
	tokens = compressed->tokens == e->pages && !compressed->present;
	anon_drop = migrated->a.anon >= compressed->a.anon &&
		migrated->a.anon - compressed->a.anon >= bytes;
	payload = compressed->compressed > migrated->compressed &&
		compressed->pool > 0 && compressed->stored > migrated->stored;
	release = release_counts_at_baseline(e);
	printf("SUPPORT source_fault=%d unchanged_source_after_migration=%d "
	       "own_type30=%d source_anon_removed=%d global_payload_growth=%d "
	       "release_returned_to_baseline=%d\n", allocation, migration, tokens,
	       anon_drop, payload, release);
	for (i = 1; i < PHASES; i++) {
		char label[80];
		const struct snapshot *before = &e->s[i - 1], *after = &e->s[i];
#define DIFF(field, suffix) do { \
	snprintf(label, sizeof(label), "%s_" suffix, phases[i]); \
	delta(label, before->field, after->field, e->pages); \
} while (0)
		DIFF(a.current, "A_current"); DIFF(a.anon, "A_anon");
		DIFF(a.kernel, "A_kernel"); DIFF(b.current, "B_current");
		DIFF(b.anon, "B_anon"); DIFF(b.kernel, "B_kernel");
		DIFF(compressed, "global_compressed"); DIFF(pool, "global_pool");
#undef DIFF
	}
	if (real_trace) return allocation && migration && tokens && anon_drop && payload && release ? 0 : 1;
	puts("GATE exact_resident_payload FAIL covered=0 reason=real_accounting_trace_required");
	puts("GATE source_A_payload_charge FAIL covered=0 reason=real_accounting_trace_required");
	puts("GATE destination_B_no_payload_charge FAIL covered=0 reason=real_accounting_trace_required");
	puts("GATE payload_charge_release FAIL covered=0 reason=real_accounting_trace_required");
	return 1;
}
static void final_marker(bool cleanup_ok, const char *reason, bool synthetic)
{
	printf("MEMCOMP_MEMCG_RESULT {\"status\":\"FAIL\",\"exit_code\":1,"
	       "\"skip\":false,\"payload_source_covered\":false,"
	       "\"cleanup_ok\":%s,\"synthetic\":%s,\"reason\":\"%s\"}\n",
	       cleanup_ok ? "true" : "false", synthetic ? "true" : "false", reason);
}
static int parse_evidence(const char *raw, struct evidence *e)
{
	struct table t;
	unsigned int i;
	if (parse_table(raw, &t) || t.count != 2 + PHASES * 11 ||
	    key_value(&t, "pages", &e->pages) ||
	    key_value(&t, "page_size", &e->page_size) ||
	    !e->pages || e->pages > MAX_PAGES ||
	    e->page_size < 4096 || e->page_size > 65536 ||
	    (e->page_size & (e->page_size - 1))) return -1;
	for (i = 0; i < PHASES; i++) {
		char key[96];
		struct snapshot *s = &e->s[i];
#define GET(field, suffix) do { \
	snprintf(key, sizeof(key), "%s_" suffix, phases[i]); \
	if (key_value(&t, key, &s->field)) return -1; \
} while (0)
		GET(a.current, "a_current"); GET(a.anon, "a_anon");
		GET(a.kernel, "a_kernel"); GET(b.current, "b_current");
		GET(b.anon, "b_anon"); GET(b.kernel, "b_kernel");
		GET(stored, "stored"); GET(compressed, "compressed");
		GET(pool, "pool"); GET(tokens, "tokens"); GET(present, "present");
#undef GET
		if (s->tokens > e->pages || s->present > e->pages ||
		    s->tokens > e->pages - s->present) return -1;
	}
	return 0;
}
static int parser_mode(const char *mode)
{
	char raw[TEXT_MAX];
	struct table t;
	uint64_t v[3];
	int r = -1;
	if (read_text(STDIN_FILENO, raw, sizeof(raw))) goto bad;
	if (!strcmp(mode, "--parse-memory-stat")) {
		r = memory_stat(raw, &t);
		if (!r) printf("PARSED memory.stat fields=%zu proof=none\n", t.count);
	} else if (!strcmp(mode, "--parse-scalar")) {
		r = parse_numbers(raw, v, 1);
		if (!r) printf("PARSED scalar=%" PRIu64 " proof=none\n", v[0]);
	} else if (!strcmp(mode, "--parse-memcomp-stat")) {
		r = parse_numbers(raw, v, 3);
		if (!r) printf("PARSED stored=%" PRIu64 " compressed=%" PRIu64
			       " pool=%" PRIu64 " proof=global-only\n", v[0], v[1], v[2]);
	} else if (!strcmp(mode, "--parse-pagemap")) {
		const char *p = raw;
		uint64_t present = 0, tokens = 0;
		size_t pages = 0;
		while (*p) {
			uint64_t entry = 0;
			unsigned int n;
			for (n = 0; n < 16; n++) {
				unsigned int digit;
				char c = *p++;
				if (c >= '0' && c <= '9') digit = (unsigned int)(c - '0');
				else if (c >= 'a' && c <= 'f') digit = (unsigned int)(c - 'a') + 10;
				else goto bad;
				entry = (entry << 4) | digit;
			}
			if (*p != '\n' || pagemap_state(entry, &present, &tokens)) goto bad;
			p++; pages++;
		}
		if (!pages) goto bad;
		printf("PARSED pagemap pages=%zu present=%" PRIu64 " type30=%" PRIu64
		       " proof=token-only\n", pages, present, tokens);
		r = 0;
	} else if (!strcmp(mode, "--parse-evidence")) {
		struct evidence *e = calloc(1, sizeof(*e));
		if (!e) goto bad;
		r = parse_evidence(raw, e);
		if (!r) {
			puts("PARSED synthetic_evidence not_guest_execution=1");
			r = assess(e, false);
			final_marker(true, "synthetic_evidence_without_real_trace", true);
			free(e);
			return r;
		}
		free(e);
	}
	if (!r) return 0;
bad:
	fprintf(stderr, "FAIL parser malformed/incomplete input mode=%s\n", mode);
	return 2;
}
/* Private instance, no owner/PID filter, continuously consumed trace_pipe.
 * Buffer loss is never excused even if every workload charge appears paired.
 */
/* Nonblocking serial output with its own clock checks, no expired() recursion. */
static int trace_output(const char *line, size_t len)
{
	size_t used = 0;

	if (ferror(stdout) || fflush(stdout))
		return mat_bad(&account, "stdout failed before raw trace write");
	while (used < len) {
		struct pollfd pfd = { .fd = STDOUT_FILENO, .events = POLLOUT };
		uint64_t now = now_ms();
		ssize_t n;
		int ready;

		if (stopped || !now || now >= deadline)
			return mat_bad(&account, "raw trace output deadline/signal");
		ready = poll(&pfd, 1, 100);
		if (ready < 0 && errno == EINTR) continue;
		if (ready < 0 || (ready && (pfd.revents & (POLLERR | POLLHUP | POLLNVAL))))
			return mat_bad(&account, "raw trace output poll failed");
		if (!ready) continue;
		n = write(STDOUT_FILENO, line + used, len - used);
		if (n < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK))
			continue;
		if (n <= 0) return mat_bad(&account, "raw trace output write failed");
		used += (size_t)n;
	}
	return 0;
}

static int trace_pump(void)
{
	char chunk[16384];
	ssize_t n;
	size_t i;
	unsigned int batches;

	trace_pending = false;
	if (tracepipe < 0) return 0;
	if (account.error[0]) return -1;
	for (batches = 0; batches < 8; batches++) {
		uint64_t now = now_ms();

		if (stopped || !now || now >= deadline)
			return mat_bad(&account, "trace capture deadline/signal");
		n = read(tracepipe, chunk, sizeof(chunk));
		if (n < 0 && errno == EINTR && !stopped) continue;
		if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return 0;
		if (n < 0) return mat_bad(&account, "trace_pipe read failed");
		if (!n) return 0;
		for (i = 0; i < (size_t)n; i++) {
			if (!chunk[i] || trace_used + 1 >= sizeof(trace_partial))
				return mat_bad(&account, "NUL/oversize trace line");
			trace_partial[trace_used++] = chunk[i];
			if (chunk[i] != '\n') continue;
			trace_partial[trace_used] = 0;
			/* Original, unfiltered evidence; backpressure cannot bypass timeout. */
			if (trace_output(trace_partial, trace_used)) return -1;
			if (mat_line(&account, trace_partial)) return -1;
			trace_used = 0;
		}
	}
	trace_pending = true;
	return 0;
}
static int trace_drain(void)
{
	do {
		if (trace_pump()) return -1;
	} while (trace_pending);
	return 0;
}
static int trace_loss(bool initial)
{
	int fd = openat(tracefd, "per_cpu", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	DIR *dir;
	struct dirent *de;
	unsigned int cpus = 0;
	uint64_t ids = 0;
	int result = 0;
	if (fd < 0) return mat_bad(&account, "missing per_cpu stats directory");
	dir = fdopendir(fd);
	if (!dir) { close(fd); return mat_bad(&account, "open CPU stats directory"); }
	for (;;) {
		char path[512], raw[4096];
		const char *p;
		uint64_t cpu;
		errno = 0;
		de = readdir(dir);
		if (!de) { if (errno) result = -1; break; }
		if (de->d_name[0] == '.') continue;
		p = de->d_name;
		if (strncmp(p, "cpu", 3)) { result = -1; break; }
		p += 3;
		if (mat_number(&p, UINT32_MAX, &cpu) || *p) { result = -1; break; }
		snprintf(path, sizeof(path), "%s/stats", de->d_name);
		if (read_at(fd, path, raw, sizeof(raw))) { result = -1; break; }
		printf("TRACE_CPU_STATS stage=%s cpu=%" PRIu64 "\n%s",
		       initial ? "initial" : "final", cpu, raw);
		if (mat_stats(&account, raw)) { result = -1; break; }
		cpus++; ids ^= (cpu + 1) * UINT64_C(0x9e3779b97f4a7c15);
	}
	if (closedir(dir)) result = -1;
	if (result || !cpus) return mat_bad(&account, "incomplete/invalid CPU loss audit");
	if (initial) { trace_cpus = cpus; trace_cpu_ids = ids; }
	else if (trace_cpus != cpus || trace_cpu_ids != ids)
		return mat_bad(&account, "CPU inventory changed during capture");
	return 0;
}
static int trace_start(void)
{
	struct stat a, b;
	struct statfs fs;
	char raw[256];
	uint64_t metadata_bytes;

	if (read_at(AT_FDCWD, "/sys/kernel/slab/memcomp_entry/object_size", raw, sizeof(raw)) ||
	    parse_numbers(raw, &metadata_bytes, 1) || !metadata_bytes || metadata_bytes > UINT32_MAX)
		return fail("missing/invalid entry slab object size witness");
	printf("TRACE_METADATA object_size=%" PRIu64 " independent_slab_witness=1\n", metadata_bytes);
	if (fstat(afd, &a) || fstat(bfd, &b) ||
	    mat_init(&account, (uint64_t)a.st_ino, (uint64_t)b.st_ino, (uint32_t)metadata_bytes))
		return fail("cgroup inode/trace allocation");
	printf("TRACE_OWNERS source=%" PRIu64 " destination=%" PRIu64
	       " scope=migration-release offline_reparent=not_tested\n",
	       account.source, account.destination);
	instancesfd = open("/sys/kernel/tracing/instances",
			   O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
	if (instancesfd < 0) return fail("tracefs instances unavailable");
	/* Tracefs is mandatory: never modify the shared/global tracing instance. */
	if (fstatfs(instancesfd, &fs) ||
	    (unsigned long)fs.f_type != (unsigned long)TRACEFS_MAGIC) {
		errno = ENODEV; return fail("not tracefs");
	}
	snprintf(trace_name, sizeof(trace_name), "account-%s", group_name);
	if (mkdirat(instancesfd, trace_name, 0700)) return fail("create tracefs instance");
	made_instance = true;
	tracefd = openat(instancesfd, trace_name,
			 O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
	if (tracefd < 0 || write_at(tracefd, "tracing_on", "0\n") ||
	    write_at(tracefd, "current_tracer", "nop\n") ||
	    write_at(tracefd, "events/enable", "0\n") ||
	    write_at(tracefd, "trace_clock", "global\n") ||
	    write_at(tracefd, "buffer_size_kb", "4096\n") ||
	    write_at(tracefd, "options/raw", "0\n") ||
	    write_at(tracefd, "options/hex", "0\n") ||
	    write_at(tracefd, "options/bin", "0\n") ||
	    write_at(tracefd, "options/latency-format", "0\n") ||
	    write_at(tracefd, "options/context-info", "1\n") ||
	    write_at(tracefd, "options/markers", "1\n") ||
	    write_at(tracefd, "trace", "\n") ||
	    read_at(tracefd, "events/memcomp/memcomp_account/filter", raw, sizeof(raw)) ||
	    (strcmp(raw, "none\n") && strcmp(raw, "0\n")) ||
	    write_at(tracefd, "events/memcomp/memcomp_account/enable", "1\n") ||
	    trace_loss(true)) return fail("configure unfiltered accounting trace");
	if (ferror(stdout) || fflush(stdout)) return fail("stdout before trace capture");
	trace_stdout_flags = fcntl(STDOUT_FILENO, F_GETFL);
	if (trace_stdout_flags < 0 ||
	    fcntl(STDOUT_FILENO, F_SETFL, trace_stdout_flags | O_NONBLOCK))
		return fail("nonblocking trace stdout");
	tracepipe = openat(tracefd, "trace_pipe", O_RDONLY | O_NONBLOCK | O_CLOEXEC);
	if (tracepipe < 0 || write_at(tracefd, "tracing_on", "1\n"))
		return fail("enable accounting trace before workload");
	puts("TRACE_CAPTURE enabled=1 instance=private owner_filter=none");
	return 0;
}
static int trace_mark(const char *phase)
{
	char marker[64];
	bool *seen = !strcmp(phase, "compressed") ? &account.compressed_seen : &account.released_seen;
	snprintf(marker, sizeof(marker), "memcomp_phase=%s\n", phase);
	if (write_at(tracefd, "trace_marker", marker)) return fail("write accounting phase marker");
	while (!*seen && !expired()) usleep(1000);
	if (!*seen) return fail("accounting phase marker not captured");
	return 0;
}
static int trace_finish(void)
{
	/* No tracing_on toggle at compressed. Wait for every payload and the RCU
	 * metadata release, then mark released, stop, drain and audit all CPUs.
	 */
	while (!mat_balanced(&account) && !expired()) usleep(10000);
	if (!mat_balanced(&account) || trace_mark("released") ||
	    write_at(tracefd, "tracing_on", "0\n") || trace_drain() ||
	    trace_used || trace_loss(false) || mat_finish(&account, true)) {
		fprintf(stderr, "FAIL accounting trace: %s\n",
			account.error[0] ? account.error : "incomplete release/capture");
		return -1;
	}
	if (close(tracepipe)) return fail("close completed accounting trace");
	tracepipe = -1;
	printf("TRACE_VERIFIED charges=%zu all_cpu_loss_zero=1 "
	       "source_payload_outstanding_at_compressed=1 released_balanced=1\n",
	       account.count);
	return 0;
}
static int trace_cleanup(void)
{
	int result = 0;
	if (tracefd >= 0) {
		if (write_at(tracefd, "tracing_on", "0\n")) result = -1;
		if (write_at(tracefd, "events/memcomp/memcomp_account/enable", "0\n")) result = -1;
	}
	if (tracepipe >= 0 && close(tracepipe)) result = -1;
	tracepipe = -1;
	if (tracefd >= 0 && close(tracefd)) result = -1;
	tracefd = -1;
	if (made_instance && unlinkat(instancesfd, trace_name, AT_REMOVEDIR)) result = -1;
	if (instancesfd >= 0 && close(instancesfd)) result = -1;
	instancesfd = -1;
	if (ferror(stdout) || fflush(stdout)) result = -1;
	if (trace_stdout_flags >= 0 &&
	    fcntl(STDOUT_FILENO, F_SETFL, trace_stdout_flags)) result = -1;
	trace_stdout_flags = -1;
	mat_destroy(&account);
	return result;
}
static bool path_contains(const char *path, const char *value)
{
	char raw[4096];
	return !read_at(AT_FDCWD, path, raw, sizeof(raw)) && strstr(raw, value);
}
static int guest_guard(void)
{
	char raw[TEXT_MAX];
	struct utsname uts;
	struct statfs fs;
	FILE *status;
	char *line = NULL;
	size_t cap = 0;
	bool admin = false;
	if (geteuid() || uname(&uts) || strstr(uts.release, "icrosoft") ||
	    read_at(AT_FDCWD, "/proc/cmdline", raw, sizeof(raw)) ||
	    !word(raw, BOOT_OPT) ||
	    !(path_contains("/sys/class/dmi/id/sys_vendor", "QEMU") ||
	      path_contains("/sys/class/dmi/id/product_name", "QEMU") ||
	      path_contains("/sys/class/dmi/id/product_name", "KVM"))) {
		errno = EPERM;
		return fail("disposable QEMU guest boot opt-in/root required");
	}
	status = fopen("/proc/self/status", "re");
	if (!status) return fail("read capabilities");
	while (getline(&line, &cap, status) >= 0) {
		unsigned long long bits;
		if (sscanf(line, "CapEff: %llx", &bits) == 1)
			admin = !!(bits & (1ULL << 21));
	}
	free(line);
	if (fclose(status) || !admin) {
		errno = EPERM;
		return fail("CAP_SYS_ADMIN required for pagemap types");
	}
	if (read_at(AT_FDCWD, "/proc/swaps", raw, sizeof(raw)) ||
	    strncmp(raw, "Filename", 8) || !strchr(raw, '\n') ||
	    strchr(raw, '\n')[1]) {
		errno = EPERM; return fail("guest must have no active swap");
	}
	rootfd = open(CGROOT, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
	if (rootfd < 0 || fstatfs(rootfd, &fs) ||
	    (unsigned long)fs.f_type != (unsigned long)CGROUP2_SUPER_MAGIC ||
	    read_at(rootfd, "cgroup.subtree_control", raw, sizeof(raw)) ||
	    !word(raw, "memory")) {
		errno = ENODEV;
		return fail("existing cgroup2 root +memory required");
	}
	return 0;
}
static int setup_groups(void)
{
	uint64_t nonce;
	if (getrandom(&nonce, sizeof(nonce), 0) != (ssize_t)sizeof(nonce))
		return fail("cgroup name randomness");
	snprintf(group_name, sizeof(group_name), "memcomp-memcg-%ld-%016" PRIx64,
		 (long)getpid(), nonce);
	if (mkdirat(rootfd, group_name, 0700)) return fail("create private root");
	made_parent = true;
	parentfd = openat(rootfd, group_name,
			  O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
	if (parentfd < 0 || write_at(parentfd, "cgroup.subtree_control", "+memory\n"))
		return fail("private root memory delegation");
	if (mkdirat(parentfd, "source", 0700)) return fail("create source");
	made_a = true;
	if (mkdirat(parentfd, "destination", 0700)) return fail("create destination");
	made_b = true;
	afd = openat(parentfd, "source", O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
	bfd = openat(parentfd, "destination", O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
	if (afd < 0 || bfd < 0) return fail("open sibling groups");
	printf("GROUPS root=%s/%s A=source B=destination\n", CGROOT, group_name);
	return 0;
}
struct reply { uint64_t address, checksum; int error; };
static int io_exact(int fd, void *buffer, size_t size, bool writing)
{
	char *p = buffer;
	while (size) {
		struct pollfd f = { .fd = fd, .events = writing ? POLLOUT : POLLIN };
		ssize_t n;
		int r;
		if (expired()) return -1;
		r = poll(&f, 1, 100);
		if (r < 0 && errno == EINTR) continue;
		if (r < 0) return -1;
		if (!r) continue;
		n = writing ? write(fd, p, size) : read(fd, p, size);
		if (n < 0 && errno == EINTR) continue;
		if (n <= 0) { if (!n) errno = EPIPE; return -1; }
		p += (size_t)n; size -= (size_t)n;
	}
	return 0;
}
/* Repeated random 256-byte pattern, not constant 32/64-bit words. */
static uint64_t fill_data(unsigned char *p, size_t pages, size_t page_size)
{
	uint64_t sum = UINT64_C(1469598103934665603);
	size_t i, j;
	for (i = 0; i < pages; i++) {
		uint32_t state = (uint32_t)i ^ UINT32_C(0xa713928b);
		unsigned char pattern[256];
		for (j = 0; j < sizeof(pattern); j++) {
			state ^= state << 13; state ^= state >> 17; state ^= state << 5;
			pattern[j] = (unsigned char)state;
		}
		pattern[0] = 0x19; pattern[8] = 0xe6;
		for (j = 0; j < page_size; j++) {
			unsigned char value = pattern[j % sizeof(pattern)];
			p[i * page_size + j] = value;
			sum = (sum ^ value) * UINT64_C(1099511628211);
		}
	}
	return sum;
}
static uint64_t checksum_data(const unsigned char *p, size_t bytes)
{
	uint64_t sum = UINT64_C(1469598103934665603);
	size_t i;
	for (i = 0; i < bytes; i++) sum = (sum ^ p[i]) * UINT64_C(1099511628211);
	return sum;
}
static void worker(int input, int output, size_t pages, size_t page_size,
		   pid_t supervisor)
{
	unsigned char *map = MAP_FAILED;
	struct reply r = { 0 };
	size_t bytes = pages * page_size;
	char cmd;
	if (prctl(PR_SET_PDEATHSIG, SIGKILL) || getppid() != supervisor) _exit(1);
	for (;;) {
		if (io_exact(input, &cmd, 1, false)) _exit(1);
		r.error = 0;
		switch (cmd) {
		case 'W': /* Warm stack/IPC in A before baseline. */
			r.checksum = 0;
			break;
		case 'F':
			map = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
				   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
			if (map == MAP_FAILED || madvise(map, bytes, MADV_NOHUGEPAGE)) {
				r.error = errno; break;
			}
			r.checksum = fill_data(map, pages, page_size);
			if (checksum_data(map, bytes) != r.checksum) { r.error = EILSEQ; break; }
			r.address = (uint64_t)(uintptr_t)map;
			break;
		case 'C':
			if (map == MAP_FAILED || madvise(map, bytes, MADV_PAGEOUT))
				r.error = map == MAP_FAILED ? EINVAL : errno;
			break;
		case 'U':
			if (map == MAP_FAILED || munmap(map, bytes))
				r.error = map == MAP_FAILED ? EINVAL : errno;
			map = MAP_FAILED;
			break;
		default: r.error = EINVAL;
		}
		if (io_exact(output, &r, sizeof(r), true) || r.error) _exit(1);
		if (cmd == 'U') _exit(0);
	}
}
static int start_worker(size_t pages, size_t page_size)
{
	int in[2], out[2];
	pid_t parent = getpid();
	if (pipe2(in, O_CLOEXEC)) return fail("command pipe");
	if (pipe2(out, O_CLOEXEC)) {
		close(in[0]); close(in[1]); return fail("reply pipe");
	}
	fflush(NULL);
	worker_pid = fork();
	if (!worker_pid) {
		close(in[1]); close(out[0]); close(rootfd); close(parentfd);
		close(afd); close(bfd);
		if (tracepipe >= 0) close(tracepipe);
		if (tracefd >= 0) close(tracefd);
		if (instancesfd >= 0) close(instancesfd);
		/* expired() is also used by worker IPC; only the parent records. */
		tracepipe = -1;
		tracefd = -1;
		instancesfd = -1;
		worker(in[0], out[1], pages, page_size, parent);
		_exit(1);
	}
	close(in[0]); close(out[1]);
	command_fd = in[1]; reply_fd = out[0];
	return worker_pid < 0 ? fail("fork worker") : 0;
}
static int attach_worker(int dir)
{
	char pid[32], raw[256];
	snprintf(pid, sizeof(pid), "%ld\n", (long)worker_pid);
	if (write_at(dir, "cgroup.procs", pid))
		return fail("write worker cgroup.procs");
	if (read_at(dir, "cgroup.procs", raw, sizeof(raw)))
		return fail("read worker cgroup.procs");
	if (strcmp(raw, pid)) {
		fprintf(stderr, "FAIL worker membership expected=%s observed=%s", pid, raw);
		errno = EPROTO; return fail("worker migration/membership");
	}
	return 0;
}
static int command(char cmd, struct reply *r)
{
	if (io_exact(command_fd, &cmd, 1, true) ||
	    io_exact(reply_fd, r, sizeof(*r), false)) return fail("worker IPC deadline/exit");
	if (r->error) { errno = r->error; return fail("worker reported error"); }
	return 0;
}
static int scan(uint64_t address, struct evidence *e, unsigned int phase,
		bool print_entries)
{
	uint64_t i, present = 0, tokens = 0;
	for (i = 0; i < e->pages; i++) {
		uint64_t entry, index = address / e->page_size + i;
		ssize_t n;
		if (expired()) return fail("pagemap deadline");
		if (index > (uint64_t)INT64_MAX / sizeof(entry)) {
			errno = EOVERFLOW; return fail("pagemap offset overflow");
		}
		do { n = pread(pmfd, &entry, sizeof(entry), (off_t)(index * sizeof(entry))); }
		while (n < 0 && errno == EINTR && !stopped);
		if (n != (ssize_t)sizeof(entry) || pagemap_state(entry, &present, &tokens)) {
			errno = EINVAL; return fail("pagemap short read/invalid page state");
		}
		if (print_entries)
			printf("PAGEMAP phase=%s page=%" PRIu64 " raw=%016" PRIx64 "\n",
			       phases[phase], i, entry);
	}
	e->s[phase].present = present; e->s[phase].tokens = tokens;
	return 0;
}
static int reap_worker(bool force)
{
	int status;
	pid_t pid;
	uint64_t limit = now_ms() + 3000;
	if (worker_pid <= 0) return 0;
	if (force && kill(worker_pid, SIGKILL) && errno != ESRCH) return fail("kill own worker");
	for (;;) {
		pid = waitpid(worker_pid, &status, WNOHANG);
		if (pid == worker_pid) {
			worker_pid = -1;
			if (force || (WIFEXITED(status) && !WEXITSTATUS(status))) return 0;
			errno = ECHILD; return fail("worker exit status");
		}
		if (pid < 0 && errno != EINTR) return fail("reap worker");
		if (!force && expired()) return fail("worker exit timeout");
		if (force && now_ms() >= limit) {
			errno = ETIMEDOUT; return fail("forced cleanup reap timeout");
		}
		usleep(10000);
	}
}
/* Establish a real post-release stock baseline without limit writes or a
 * tolerance. Warmup is fully captured before the measured workload, and all
 * its payload and RCU metadata charges must be paired before the baseline.
 */
static int warmup_account_stocks(size_t pages, size_t page_size)
{
	struct reply r;
	struct snapshot *sample;
	int result = -1;

	if (start_worker(pages, page_size) || attach_worker(afd) ||
	    command('W', &r) || command('F', &r) || attach_worker(bfd) ||
	    command('C', &r) || command('U', &r) || reap_worker(false))
		return -1;
	while (!expired()) {
		if (trace_drain()) return -1;
		if (mat_balanced(&account)) break;
		usleep(10000);
	}
	if (!mat_balanced(&account)) return fail("warmup accounting release incomplete");
	if (close(command_fd)) return fail("close warmup command pipe");
	command_fd = -1;
	if (close(reply_fd)) return fail("close warmup reply pipe");
	reply_fd = -1;
	sample = calloc(1, sizeof(*sample));
	if (!sample) return fail("warmup snapshot allocation");
	if (!snapshot(sample) && !sample->a.anon && !sample->b.anon &&
	    !sample->stored && !sample->compressed && account.count) {
		puts("WARMUP_RELEASE full_workload=1 payload_metadata_paired=1 anon_global_zero=1 baseline_before_measured_workload=1");
		result = 0;
	} else {
		fail("warmup retained data or no accounting coverage");
	}
	free(sample);
	return result;
}

static int cleanup(void)
{
	int result = 0;
	if (reap_worker(true)) result = -1;
	if (trace_cleanup()) result = fail("remove own trace instance");
	if (command_fd >= 0 && close(command_fd)) result = -1;
	if (reply_fd >= 0 && close(reply_fd)) result = -1;
	if (pmfd >= 0 && close(pmfd)) result = -1;
	if (afd >= 0 && close(afd)) result = -1;
	if (bfd >= 0 && close(bfd)) result = -1;
	if (made_a && unlinkat(parentfd, "source", AT_REMOVEDIR))
		result = fail("remove own source cgroup");
	if (made_b && unlinkat(parentfd, "destination", AT_REMOVEDIR))
		result = fail("remove own destination cgroup");
	if (parentfd >= 0 && close(parentfd)) result = -1;
	if (made_parent && unlinkat(rootfd, group_name, AT_REMOVEDIR))
		result = fail("remove own private cgroup root");
	if (rootfd >= 0 && close(rootfd)) result = -1;
	printf("CLEANUP own_worker_and_cgroups=%s\n", result ? "FAIL" : "removed");
	return result;
}
static int guest_run(uint64_t requested_pages, uint64_t timeout)
{
	struct evidence *e = calloc(1, sizeof(*e));
	struct reply r;
	struct sigaction sa = { .sa_handler = on_signal };
	long page_size = sysconf(_SC_PAGESIZE), available = sysconf(_SC_AVPHYS_PAGES);
	char path[96], raw[256];
	uint64_t address;
	const char *reason = "runtime_error_or_incomplete_observation";
	bool cleanup_ok, verified = false;
	deadline = now_ms() + timeout * 1000;
	if (!e || page_size < 4096 || page_size > 65536 ||
	    ((unsigned long)page_size & ((unsigned long)page_size - 1)) || available < 1) {
		errno = EINVAL; fail("guest allocation/page geometry"); goto out;
	}
	e->page_size = (uint64_t)page_size;
	e->pages = requested_pages ? requested_pages : (uint64_t)available / 64;
	if (!requested_pages && e->pages > 8192) e->pages = 8192;
	if (!e->pages || e->pages > MAX_PAGES || e->pages > (uint64_t)available / 8 ||
	    e->pages > SIZE_MAX / e->page_size) {
		errno = ENOMEM; fail("unsafe/empty page batch"); goto out;
	}
	printf("CONFIG pages=%" PRIu64 " page_size=%" PRIu64 " timeout_s=%" PRIu64
	       " non_samefill_pattern_bytes=256\n", e->pages, e->page_size, timeout);
	sigemptyset(&sa.sa_mask);
	if (sigaction(SIGTERM, &sa, NULL) || sigaction(SIGINT, &sa, NULL) ||
	    sigaction(SIGHUP, &sa, NULL) || signal(SIGPIPE, SIG_IGN) == SIG_ERR ||
	    guest_guard() || setup_groups() || trace_start() ||
	    warmup_account_stocks((size_t)e->pages, (size_t)e->page_size) ||
	    start_worker((size_t)e->pages, (size_t)e->page_size) || attach_worker(afd) ||
	    command('W', &r) || settled(&e->s[0], 0) || command('F', &r)) goto out;
	address = r.address;
	if (!address || address % e->page_size) {
		errno = EINVAL; fail("worker mapping address"); goto out;
	}
	printf("FAULT source=A address=%" PRIu64 " checksum=%016" PRIx64
	       " prefault_data_verified=1\n", address, r.checksum);
	snprintf(path, sizeof(path), "/proc/%ld/pagemap", (long)worker_pid);
	pmfd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (pmfd < 0 || scan(address, e, 1, false) || settled(&e->s[1], 1) ||
	    e->s[1].present != e->pages || attach_worker(bfd) ||
	    read_at(afd, "cgroup.procs", raw, sizeof(raw)) || *raw ||
	    scan(address, e, 2, false) || settled(&e->s[2], 2) ||
	    e->s[2].present != e->pages) {
		errno = EINVAL; fail("allocation/migration observation incomplete"); goto out;
	}
	while (!expired()) {
		if (command('C', &r) || scan(address, e, 3, false)) goto out;
		if (e->s[3].tokens == e->pages) break;
		usleep(100000);
	}
	if (expired() || scan(address, e, 3, true) || settled(&e->s[3], 3) ||
	    scan(address, e, 3, false) || e->s[3].tokens != e->pages) {
		fail("MADV_PAGEOUT type30 coverage deadline/unstable"); goto out;
	}
	if (trace_mark("compressed") || command('U', &r) || reap_worker(false) ||
	    trace_finish() || wait_release_counts(e)) goto out;
	if (assess(e, true)) { reason = "supporting_workload_gates_failed"; goto out; }
	verified = true;
out:
	cleanup_ok = !cleanup();
	free(e);
	if (verified && cleanup_ok) {
		puts("RESULT source_payload_charge PASS");
		puts("RESULT destination_no_payload_charge PASS");
		puts("RESULT source_release PASS");
		puts("PASS real_memcg_attribution");
		return 0;
	}
	if (account.error[0]) fprintf(stderr, "FAIL accounting trace: %s\n", account.error);
	final_marker(cleanup_ok, cleanup_ok ? reason : "cleanup_failed", false);
	return 1;
}
int main(int argc, char **argv)
{
	uint64_t pages = 0, timeout = 30;
	bool have_pages = false, have_timeout = false;
	int i;
	if (argc == 2 && !strncmp(argv[1], "--parse-", 8)) return parser_mode(argv[1]);
	if (argc == 2 && !strcmp(argv[1], "--help")) {
		puts("Usage: memcomp-memcg --disposable-guest [--pages N] [--timeout SEC]");
		puts("Requires QEMU, root/CAP_SYS_ADMIN and " BOOT_OPT);
		puts("Requires proc/sysfs/cgroup2 mounts and root +memory delegation.");
		puts("Parsers: --parse-memory-stat --parse-scalar --parse-memcomp-stat "
		     "--parse-pagemap --parse-evidence (stdin only)");
		puts("Real acceptance requires loss-free memcomp_account trace lifecycle evidence.");
		return 0;
	}
	if (argc < 2 || strcmp(argv[1], "--disposable-guest")) goto bad;
	for (i = 2; i < argc; i += 2) {
		uint64_t value;
		if (i + 1 >= argc || decimal(argv[i + 1], &value) || !value) goto bad;
		if (!strcmp(argv[i], "--pages") && !have_pages && value <= MAX_PAGES) {
			pages = value; have_pages = true;
		} else if (!strcmp(argv[i], "--timeout") && !have_timeout && value <= 300) {
			timeout = value; have_timeout = true;
		} else goto bad;
	}
	return guest_run(pages, timeout);
bad:
	fputs("FAIL arguments; use --help (guest execution is opt-in)\n", stderr);
	return 2;
}
