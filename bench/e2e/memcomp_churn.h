// SPDX-License-Identifier: GPL-2.0
/*
 * Include after mcwb_init.c's retained-span helpers, before run_tests().
 * Call churn_test() after retain_test(), before taking the fill baseline.
 * This header does not change load_params() or the existing test runners.
 */
#ifndef MEMCOMP_CHURN_H
#define MEMCOMP_CHURN_H

#include <limits.h>
#include <poll.h>
#include <sched.h>
#include <stdatomic.h>

#define CHURN_PAGES 256U
#define CHURN_BYTES (CHURN_PAGES * PG)
#define CHURN_WORKERS 5U
#define CHURN_SWP_MEMCOMP_TYPE 30U
#define CHURN_SWAP_TYPE_MASK UINT64_C(0x1f)
#define CHURN_SWAP_OFFSET_MASK ((UINT64_C(1) << 50) - 1)
#define CHURN_JOIN_GRACE_S 1
#define CHURN_JOIN_REAP_S 5

struct churn_ctx;

struct churn_events {
	int64_t owner, pending, store_ok, store_fail;
};

/* Pagemap bit 62 means swapped, not specifically a memcomp token. */
static bool churn_is_memcomp_token(uint64_t entry)
{
	uint64_t type = entry & CHURN_SWAP_TYPE_MASK;
	uint64_t offset = (entry >> 5) & CHURN_SWAP_OFFSET_MASK;

	return (entry & (UINT64_C(1) << 62)) &&
	       type == CHURN_SWP_MEMCOMP_TYPE && offset;
}

/* Each worker owns its counters; the coordinator reads them only after join. */
struct churn_worker {
	struct churn_ctx *ctx;
	unsigned int id;
	uint64_t calls, ok, pages, bad, tokens, token_max;
	uint64_t again, exists, lock_busy, directions[2];
	uint64_t faults, copies, copy_exists, fault_errors;
	unsigned char coverage[CHURN_PAGES];
	int error;
	size_t bad_page, bad_word;
	uint64_t actual, expected;
};

struct churn_ctx {
	char *base, *bank[2];
	size_t map_len;
	int uffd, pm;
	bool move;
	unsigned long round;
	atomic_bool start, stop, handler_stop;
	pthread_t fault_thread;
	bool fault_thread_started;
	pthread_rwlock_t valid[CHURN_PAGES];
	/* Only read/written while holding this page's valid lock. */
	/* 0/1 = active bank; 2 = unknown MOVE outcome, never dereference. */
	unsigned char slot[CHURN_PAGES];
};

static bool churn_stopped(struct churn_ctx *c)
{
	return atomic_load_explicit(&c->stop, memory_order_acquire);
}

static void churn_stop(struct churn_ctx *c)
{
	atomic_store_explicit(&c->stop, true, memory_order_release);
}

static bool churn_wait_start(struct churn_ctx *c)
{
	while (!atomic_load_explicit(&c->start, memory_order_acquire)) {
		if (churn_stopped(c))
			return false;
		msleep(1);
	}
	return !churn_stopped(c);
}

static uint64_t churn_seed(const struct churn_ctx *c, size_t page)
{
	return mix64((uint64_t)p_seed ^ UINT64_C(0x636875726e000000) ^
		     (uint64_t)c->round * UINT64_C(0x9e3779b97f4a7c15) ^
		     (uint64_t)page) | 1;
}

static uint64_t churn_word(uint64_t x)
{
	x ^= x << 13;
	x ^= x >> 7;
	x ^= x << 17;
	return x;
}

/* Fill only with workers stopped: there are no concurrent userspace writes. */
static uint64_t churn_next(uint64_t x, size_t page, size_t word)
{
	/* Alternating compressible and incompressible pages.  Successful
	 * stores permit repeated pageout/refault, rather than only exercising
	 * a once-marked incompressible page that reclaim subsequently skips.
	 */
	return (!(page & 1) && word >= 64) ? x : churn_word(x);
}

static void churn_fill(struct churn_ctx *c)
{
	size_t i, j;

	for (i = 0; i < CHURN_PAGES; i++) {
		uint64_t *p = (uint64_t *)(c->bank[0] + i * PG);
		uint64_t x = churn_seed(c, i);

		for (j = 0; j < PG / sizeof(*p); j++) {
			x = churn_next(x, i, j);
			p[j] = x;
		}
	}
}

/* The caller excludes MOVE for this page, but NEVER excludes MADV_PAGEOUT. */
static bool churn_check_page(struct churn_worker *w, size_t i)
{
	struct churn_ctx *c = w->ctx;
	volatile const uint64_t *p;
	uint64_t x = churn_seed(c, i);
	size_t j;

	if (c->slot[i] > 1) {
		w->error = EPROTO;
		return false;
	}
	p = (volatile const uint64_t *)(c->bank[c->slot[i]] + i * PG);
	for (j = 0; j < PG / sizeof(*p); j++) {
		uint64_t actual = p[j];

		x = churn_next(x, i, j);
		if (actual != x) {
			w->bad++;
			w->bad_page = i;
			w->bad_word = j;
			w->actual = actual;
			w->expected = x;
			return false;
		}
	}
	w->pages++;
	return true;
}

static void *churn_refault(void *arg)
{
	struct churn_worker *w = arg;
	struct churn_ctx *c = w->ctx;
	size_t i;

	if (!churn_wait_start(c))
		return NULL;
	while (!churn_stopped(c)) {
		for (i = 0; i < CHURN_PAGES && !churn_stopped(c); i++) {
			bool good;
			int r;

			if (c->move) {
				r = pthread_rwlock_rdlock(&c->valid[i]);
				if (r) {
					w->error = r;
					churn_stop(c);
					return NULL;
				}
			}
			good = churn_check_page(w, i);
			if (c->move) {
				r = pthread_rwlock_unlock(&c->valid[i]);
				if (r)
					w->error = r;
			}
			if (!good || w->error) {
				churn_stop(c);
				return NULL;
			}
		}
		w->calls++;
	}
	return NULL;
}

static bool churn_read_pagemap(int fd, void *addr, uint64_t *entry)
{
	ssize_t n;
	off_t offset = (off_t)(((uintptr_t)addr / PG) * sizeof(*entry));

	do {
		n = pread(fd, entry, sizeof(*entry), offset);
	} while (n < 0 && errno == EINTR);
	if (n != sizeof(*entry)) {
		if (n >= 0)
			errno = EIO;
		return false;
	}
	return true;
}

static bool churn_pageout_tokens(struct churn_worker *w, unsigned int bank,
				 uint64_t *tokens)
{
	struct churn_ctx *c = w->ctx;
	size_t i;

	*tokens = 0;
	for (i = 0; i < CHURN_PAGES; i++) {
		uint64_t entry;

		if (!churn_read_pagemap(c->pm, c->bank[bank] + i * PG, &entry)) {
			w->error = errno;
			return false;
		}
		if (churn_is_memcomp_token(entry))
			(*tokens)++;
	}
	return true;
}

/* Use only after pageout/mover workers have stopped.  COPY resolves any
 * outstanding missing fault; EEXIST means another resolver already did so.
 */
static bool churn_wake_missing(struct churn_ctx *c)
{
	unsigned char page[PG];
	unsigned int pass, b;
	size_t i, j;
	int last_error = EIO;

	for (pass = 0; pass < 20; pass++) {
		bool retry = false;

		for (b = 0; b < (c->move ? 2U : 1U); b++) {
			for (i = 0; i < CHURN_PAGES; i++) {
				struct uffdio_copy cp = { 0 };
				uint64_t x = churn_seed(c, i);
				int err;

				for (j = 0; j < PG / sizeof(uint64_t); j++) {
					x = churn_next(x, i, j);
					((uint64_t *)page)[j] = x;
				}
				cp.dst = (uintptr_t)(c->bank[b] + i * PG);
				cp.src = (uintptr_t)page;
				cp.len = PG;
				if (!ioctl(c->uffd, UFFDIO_COPY, &cp) &&
				    cp.copy == (int64_t)PG)
					continue;
				err = errno;
				if (err == EEXIST)
					continue;
				last_error = err ? err : EPROTO;
				retry = true;
			}
		}
		if (!retry)
			return true;
		msleep(10);
	}
	errno = last_error;
	return false;
}

static int churn_timed_join(pthread_t thread, unsigned int seconds)
{
	struct timespec deadline;

	if (clock_gettime(CLOCK_REALTIME, &deadline))
		return errno;
	deadline.tv_sec += seconds;
	return pthread_timedjoin_np(thread, NULL, &deadline);
}

static void churn_join_fatal(const char *label, const char *what, int err)
{
	fail("churn %s: %s: %s; refusing to free live-thread mappings",
	     label, what, strerror(err));
	_exit(1);
}

static void *churn_pageout(void *arg)
{
	struct churn_worker *w = arg;
	struct churn_ctx *c = w->ctx;
	unsigned int turn = w->id;

	if (!churn_wait_start(c))
		return NULL;
	while (!churn_stopped(c)) {
		unsigned int b = c->move ? (turn++ & 1U) : 0;
		uint64_t tokens = 0;

		/* Fixed RW VMAs; no validity lock and no access to slot[]. */
		w->calls++;
		if (madvise(c->bank[b], CHURN_BYTES, MADV_PAGEOUT)) {
			w->error = errno;
			churn_stop(c);
			break;
		}
		w->ok++;
		if (!churn_pageout_tokens(w, b, &tokens)) {
			churn_stop(c);
			break;
		}
		w->tokens += tokens;
		if (tokens > w->token_max)
			w->token_max = tokens;
		/* Keep pressure active without monopolizing a guest vCPU. */
		msleep(1);
	}
	return NULL;
}

static bool churn_fault_address(struct churn_ctx *c, uintptr_t addr,
				 size_t *page)
{
	unsigned int b;

	for (b = 0; b < 2; b++) {
		uintptr_t start = (uintptr_t)c->bank[b];
		uintptr_t end = start + CHURN_BYTES;

		if (addr >= start && addr < end &&
		    ((addr - start) % PG) == 0) {
			*page = (addr - start) / PG;
			return *page < CHURN_PAGES;
		}
	}
	return false;
}

/* Resolve UFFD missing faults with the immutable logical page contents. */
static void *churn_fault_handler(void *arg)
{
	struct churn_worker *w = arg;
	struct churn_ctx *c = w->ctx;
	unsigned char page[PG];
	struct pollfd pfd = { .fd = c->uffd, .events = POLLIN };

	if (!churn_wait_start(c))
		return NULL;
	while (!atomic_load_explicit(&c->handler_stop, memory_order_acquire)) {
		struct uffd_msg msg;
		ssize_t n;
		int pr;

		pr = poll(&pfd, 1, 100);
		if (pr < 0) {
			if (errno == EINTR)
				continue;
			w->fault_errors++;
			w->error = errno;
			churn_stop(c);
			break;
		}
		if (!pr)
			continue;
		n = read(c->uffd, &msg, sizeof(msg));
		if (n < 0 && (errno == EAGAIN || errno == EINTR))
			continue;
		if (n != (ssize_t)sizeof(msg)) {
			if (n < 0)
				w->error = errno;
			else
				w->error = EPROTO;
			w->fault_errors++;
			churn_stop(c);
			break;
		}
		if (msg.event != UFFD_EVENT_PAGEFAULT) {
			w->error = EPROTO;
			w->fault_errors++;
			churn_stop(c);
			break;
		}
		{
			size_t page_no;
			uintptr_t addr = msg.arg.pagefault.address & ~(uintptr_t)(PG - 1);
			uint64_t x;
			size_t j;
			struct uffdio_copy cp;
			int err;

			if (!churn_fault_address(c, addr, &page_no)) {
				w->error = EFAULT;
				w->fault_errors++;
				churn_stop(c);
				break;
			}
			/* Active slots must never be missing.  Refill only to unblock
			 * the worker after recording a failure, not to hide data loss.
			 */
			w->fault_errors++;
			w->error = ENODATA;
			churn_stop(c);
			x = churn_seed(c, page_no);
			for (j = 0; j < PG / sizeof(uint64_t); j++) {
				x = churn_next(x, page_no, j);
				((uint64_t *)page)[j] = x;
			}
			memset(&cp, 0, sizeof(cp));
			cp.dst = addr;
			cp.src = (uintptr_t)page;
			cp.len = PG;
			if (ioctl(c->uffd, UFFDIO_COPY, &cp)) {
				err = errno;
				if (err == EEXIST) {
					w->copy_exists++;
					continue;
				}
				w->fault_errors++;
				w->error = err;
				churn_stop(c);
				break;
			}
			if (cp.copy != (int64_t)PG) {
				w->fault_errors++;
				w->error = EPROTO;
				churn_stop(c);
				break;
			}
			w->faults++;
			w->copies++;
		}
	}
	return NULL;
}

static void *churn_mover(void *arg)
{
	struct churn_worker *w = arg;
	struct churn_ctx *c = w->ctx;
	size_t i = 0;

	if (!churn_wait_start(c))
		return NULL;
	while (!churn_stopped(c)) {
		struct uffdio_move mv = { 0 };
		unsigned int src;
		int r, err;

		/* Per-page locking leaves reads of all other pages concurrent. */
		r = pthread_rwlock_trywrlock(&c->valid[i]);
		if (r == EBUSY) {
			w->lock_busy++;
			goto next;
		}
		if (r) {
			w->error = r;
			churn_stop(c);
			break;
		}
		src = c->slot[i];
		mv.src = (uintptr_t)(c->bank[src] + i * PG);
		mv.dst = (uintptr_t)(c->bank[src ^ 1U] + i * PG);
		mv.len = PG;
		/* No ALLOW_SRC_HOLES: a missing source must not count as MOVE. */
		w->calls++;
		r = ioctl(c->uffd, UFFDIO_MOVE, &mv);
		err = r < 0 ? errno : 0;
		if (!r && mv.move == (int64_t)PG) {
			c->slot[i] = src ^ 1U;
			w->ok++;
			w->directions[src]++;
			w->coverage[i] |= 1U << src;
		} else if (r < 0 && mv.move <= 0 &&
			   (err == EAGAIN || err == EEXIST)) {
			/* A one-page request cannot have a positive short MOVE. */
			if (err == EAGAIN)
				w->again++;
			else
				w->exists++;
		} else {
			w->error = err ? err : EPROTO;
			/* Do not read a potentially emptied source after a bad result. */
			c->slot[i] = 2;
			churn_stop(c);
		}
		r = pthread_rwlock_unlock(&c->valid[i]);
		if (r) {
			w->error = r;
			churn_stop(c);
		}
		if (w->error)
			break;
next:
		i = (i + 1) % CHURN_PAGES;
		if (!i)
			sched_yield();
	}
	return NULL;
}

/* Missing facility is SKIP; negotiated MOVE errors are test failures. */
static int churn_uffd(struct churn_ctx *c)
{
	struct uffdio_api api = { .api = UFFD_API,
				  .features = UFFD_FEATURE_MOVE };
	unsigned int b;
	int flags = O_CLOEXEC | O_NONBLOCK;

#ifdef UFFD_USER_MODE_ONLY
	flags |= UFFD_USER_MODE_ONLY;
#endif
	c->uffd = syscall(__NR_userfaultfd, flags);
#ifdef UFFD_USER_MODE_ONLY
	if (c->uffd < 0 && errno == EINVAL)
		c->uffd = syscall(__NR_userfaultfd, O_CLOEXEC | O_NONBLOCK);
#endif
	if (c->uffd < 0) {
		int err = errno;

		if (err == ENOSYS || err == EPERM || err == EACCES) {
			say("churn MOVE: SKIP userfaultfd unavailable: %s; "
			    "attempts=0 moved=0", strerror(err));
			return 1;
		}
		fail("churn MOVE: userfaultfd: %s", strerror(err));
		return -1;
	}
	if (ioctl(c->uffd, UFFDIO_API, &api)) {
		int err = errno;

		if (err == EINVAL || err == ENOTTY || err == ENOSYS) {
			say("churn MOVE: SKIP UFFD_FEATURE_MOVE unavailable: %s; "
			    "attempts=0 moved=0", strerror(err));
			return 1;
		}
		fail("churn MOVE: UFFDIO_API: %s", strerror(err));
		return -1;
	}
	if (!(api.features & UFFD_FEATURE_MOVE)) {
		say("churn MOVE: SKIP no UFFD_FEATURE_MOVE; attempts=0 moved=0");
		return 1;
	}
	for (b = 0; b < 2; b++) {
		struct uffdio_register reg = {
			.range = { .start = (uintptr_t)c->bank[b],
				   .len = CHURN_BYTES },
			.mode = UFFDIO_REGISTER_MODE_MISSING,
		};

		if (ioctl(c->uffd, UFFDIO_REGISTER, &reg)) {
			fail("churn MOVE: register bank %u: %s", b,
			     strerror(errno));
			return -1;
		}
		if (!(reg.ioctls & (UINT64_C(1) << _UFFDIO_MOVE)) ||
		    !(reg.ioctls & (UINT64_C(1) << _UFFDIO_COPY))) {
			say("churn MOVE: SKIP range lacks MOVE/COPY ioctl; "
			    "attempts=0 moved=0");
			return 1;
		}
	}
	return 0;
}

/* Read and validate every counter needed by churn; never turn read failure
 * into a numeric zero that can make a negative delta look like coverage.
 */
static bool churn_parse_event(const char *buf, const char *key, int64_t *value)
{
	size_t key_len = strlen(key);
	const char *p = buf;

	while (p && *p) {
		const char *end;
		char *value_end;
		unsigned long long parsed;

		end = strchr(p, '\n');
		if (!end)
			end = p + strlen(p);
		if ((size_t)(end - p) > key_len &&
		    !memcmp(p, key, key_len) &&
		    (p[key_len] == ' ' || p[key_len] == ':' ||
		     p[key_len] == '\t')) {
			const char *v = p + key_len + 1;

			while (v < end && (*v == ' ' || *v == '\t'))
				v++;
			if (v == end || *v < '0' || *v > '9')
				return false;
			errno = 0;
			parsed = strtoull(v, &value_end, 10);
			if (errno || value_end == v || value_end > end ||
			    parsed > INT64_MAX)
				return false;
			while (value_end < end &&
			       (*value_end == ' ' || *value_end == '\t'))
				value_end++;
			if (value_end != end)
				return false;
			*value = (int64_t)parsed;
			return true;
		}
		p = *end ? end + 1 : NULL;
	}
	return false;
}

static bool churn_read_events(struct churn_events *events)
{
	char buf[8192];
	int fd;
	size_t used = 0;
	static const char * const keys[] = {
		"folio_owner_pages", "pending_pages",
		"pageout_store_ok", "pageout_store_fail",
	};
	int64_t * const values[] = {
		&events->owner, &events->pending,
		&events->store_ok, &events->store_fail,
	};
	size_t i;

	fd = open(MCDIR "events", O_RDONLY | O_CLOEXEC);
	if (fd < 0) {
		fail("churn: open memcomp events: %s", strerror(errno));
		return false;
	}
	while (used < sizeof(buf) - 1) {
		ssize_t n = read(fd, buf + used, sizeof(buf) - 1 - used);

		if (n < 0 && errno == EINTR)
			continue;
		if (n < 0) {
			int err = errno;

			close(fd);
			fail("churn: read memcomp events: %s", strerror(err));
			return false;
		}
		if (!n)
			break;
		used += n;
	}
	if (close(fd)) {
		fail("churn: close memcomp events: %s", strerror(errno));
		return false;
	}
	if (used == sizeof(buf) - 1) {
		fail("churn: memcomp events exceed buffer");
		return false;
	}
	buf[used] = 0;
	for (i = 0; i < sizeof(keys) / sizeof(keys[0]); i++) {
		if (!churn_parse_event(buf, keys[i], values[i])) {
			fail("churn: events lacks valid %s counter", keys[i]);
			return false;
		}
	}
	return true;
}

static bool churn_residue(const char *label, unsigned long round,
			  int64_t owner0, int64_t pending0)
{
	double deadline = now_s() + (double)p_drain_ms / 1000.0;
	struct churn_events events;
	int64_t owner, pending;

	do {
		if (!churn_read_events(&events))
			return false;
		owner = events.owner - owner0;
		pending = events.pending - pending0;
		if (owner <= (int64_t)p_owner_tol &&
		    pending <= (int64_t)p_owner_tol)
			break;
		if (now_s() >= deadline)
			break;
		msleep(50);
	} while (true);
	say("churn %s round %lu after munmap: owner=%+" PRId64
	    " pending=%+" PRId64 " tolerance=%lu", label, round + 1,
	    owner, pending, p_owner_tol);
	if (owner > (int64_t)p_owner_tol || pending > (int64_t)p_owner_tol) {
		fail("churn %s round %lu: owner/pending residue after munmap",
		     label, round + 1);
		return false;
	}
	return true;
}

/* 0 = completed, 1 = explicit UFFD SKIP, -1 = failed. */
static int churn_round(bool move, unsigned long round, unsigned long secs)
{
	struct churn_ctx c = { .uffd = -1, .pm = -1, .move = move,
			       .round = round };
	struct churn_worker w[CHURN_WORKERS] = { 0 };
	pthread_t threads[CHURN_WORKERS];
	unsigned int n = move ? 4 : 3, created = 0, locks = 0, t;
	const char *label = move ? "MOVE" : "plain";
	struct churn_events baseline, current;
	int64_t owner0, pending0, store_ok0, store_fail0;
	uint64_t tokens = 0, bad = 0, covered[2] = { 0, 0 };
	bool mapped = false;
	double start = 0;
	int status = -1, r;
	size_t i;

	atomic_init(&c.start, false);
	atomic_init(&c.stop, false);
	atomic_init(&c.handler_stop, false);
	if (!churn_read_events(&baseline))
		return -1;
	owner0 = baseline.owner;
	pending0 = baseline.pending;
	store_ok0 = baseline.store_ok;
	store_fail0 = baseline.store_fail;
	/* guard | aligned bank A | guard | bank B | guard/alignment slack */
	c.map_len = 2 * CHURN_BYTES + 4 * MTHP_SZ;
	c.base = mmap(NULL, c.map_len, PROT_NONE,
		      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (c.base == MAP_FAILED) {
		fail("churn %s: mmap: %s", label, strerror(errno));
		return -1;
	}
	mapped = true;
	c.bank[0] = (char *)(((uintptr_t)c.base + 2 * MTHP_SZ - 1) &
			    ~(MTHP_SZ - 1));
	c.bank[1] = c.bank[0] + CHURN_BYTES + MTHP_SZ;
	for (t = 0; t < (move ? 2U : 1U); t++) {
		if (mprotect(c.bank[t], CHURN_BYTES, PROT_READ | PROT_WRITE) ||
		    madvise(c.bank[t], CHURN_BYTES, MADV_DONTFORK)) {
			fail("churn %s: bank setup: %s", label, strerror(errno));
			goto out;
		}
	}
	c.pm = open("/proc/self/pagemap", O_RDONLY | O_CLOEXEC);
	if (c.pm < 0) {
		fail("churn %s: pagemap: %s", label, strerror(errno));
		goto out;
	}
	/* Populate source BEFORE missing registration; no UFFD handler is needed. */
	churn_fill(&c);
	if (move) {
		r = churn_uffd(&c);
		if (r) {
			status = r;
			goto out;
		}
		for (i = 0; i < CHURN_PAGES; i++) {
			r = pthread_rwlock_init(&c.valid[i], NULL);
			if (r) {
				fail("churn MOVE: rwlock init: %s", strerror(r));
				goto out;
			}
			locks++;
		}
	}
	{
		uint64_t entry;

		if (!churn_read_pagemap(c.pm, c.bank[0], &entry)) {
			fail("churn %s: read populated pagemap entry: %s", label,
			     strerror(errno));
			goto out;
		}
		if (!(entry & (UINT64_C(1) << 63))) {
			fail("churn %s: pagemap cannot observe populated resident page",
			     label);
			goto out;
		}
	}
	/* Start from reclaim too, before a reader can fault everything back. */
	if (madvise(c.bank[0], CHURN_BYTES, MADV_PAGEOUT)) {
		fail("churn %s: initial pageout: %s", label, strerror(errno));
		goto out;
	}
	{
		struct churn_worker sample = { .ctx = &c };

		if (!churn_pageout_tokens(&sample, 0, &tokens)) {
			fail("churn %s: read pageout pagemap entries: %s", label,
			     strerror(sample.error));
			goto out;
		}
	}
	if (move) {
		w[4].ctx = &c;
		w[4].id = 4;
		r = pthread_create(&c.fault_thread, NULL, churn_fault_handler, &w[4]);
		if (r) {
			fail("churn MOVE: fault handler create: %s", strerror(r));
			goto join;
		}
		c.fault_thread_started = true;
	}
	for (t = 0; t < n; t++) {
		void *(*fn)(void *) = t < 2 ? churn_pageout :
				     t == 2 ? churn_refault : churn_mover;

		w[t].ctx = &c;
		w[t].id = t;
		r = pthread_create(&threads[t], NULL, fn, &w[t]);
		if (r) {
			fail("churn %s: pthread_create %u: %s", label, t,
			     strerror(r));
			goto join;
		}
		created++;
	}
	start = now_s();
	atomic_store_explicit(&c.start, true, memory_order_release);
	while (!churn_stopped(&c) && now_s() - start < (double)secs)
		msleep(10);
join:
	churn_stop(&c);

	/* Reclaim/mover never wait on a missing fault.  Stop them first, but
	 * leave the refault worker joined only after its UFFD wait is resolved.
	 */
	for (t = 0; t < created; t++) {
		if (move && t == 2)
			continue;
		r = churn_timed_join(threads[t], CHURN_JOIN_REAP_S);
		if (r)
			churn_join_fatal(label, "worker timed join", r);
	}
	if (move && created > 2) {
		if (w[4].error || w[4].fault_errors) {
			if (!churn_wake_missing(&c))
				churn_join_fatal(label, "UFFD recovery COPY", errno);
		}
		r = churn_timed_join(threads[2], CHURN_JOIN_GRACE_S);
		if (r == ETIMEDOUT) {
			if (!churn_wake_missing(&c))
				churn_join_fatal(label, "UFFD wake after join timeout",
						 errno);
			r = churn_timed_join(threads[2], CHURN_JOIN_REAP_S);
		}
		if (r)
			churn_join_fatal(label, "refault worker timed join", r);
	}
	if (c.fault_thread_started) {
		atomic_store_explicit(&c.handler_stop, true, memory_order_release);
		r = churn_timed_join(c.fault_thread, CHURN_JOIN_REAP_S);
		if (r)
			churn_join_fatal("MOVE", "handler timed join", r);
		if (w[4].error || w[4].fault_errors)
			fail("churn MOVE: unexpected missing fault; refill is not integrity PASS");
	}

	for (t = 0; t < created; t++) {
		if (w[t].error)
			fail("churn %s: worker %u: %s", label, t,
			     strerror(w[t].error));
		if (w[t].bad)
			fail("churn %s: page %zu word %zu: got %016" PRIx64
			     " expected %016" PRIx64, label, w[t].bad_page,
			     w[t].bad_word, w[t].actual, w[t].expected);
	}
	say("churn %s round %lu: %.3fs initial_tokens=%" PRIu64
	    " pageout0=%" PRIu64 "/%" PRIu64 " pageout1=%" PRIu64
	    "/%" PRIu64 " refault_sweeps=%" PRIu64 " checked_pages=%" PRIu64,
	    label, round + 1, start ? now_s() - start : 0.0, tokens,
	    w[0].ok, w[0].calls, w[1].ok, w[1].calls, w[2].calls, w[2].pages);
	if (!churn_read_events(&current))
		goto out;
	say("churn %s: token_samples=%" PRIu64 "/%" PRIu64
	    " token_max=%" PRIu64 "/%" PRIu64
	    " store_ok=%+" PRId64 " store_fail=%+" PRId64,
	    label, w[0].tokens, w[1].tokens, w[0].token_max, w[1].token_max,
	    current.store_ok - store_ok0, current.store_fail - store_fail0);
	if (move) {
		for (i = 0; i < CHURN_PAGES; i++) {
			covered[0] += !!(w[3].coverage[i] & 1);
			covered[1] += !!(w[3].coverage[i] & 2);
		}
		say("churn MOVE: faults=%" PRIu64 " copies=%" PRIu64
		    " copy_EEXIST=%" PRIu64 " fault_errors=%" PRIu64,
		    w[4].faults, w[4].copies, w[4].copy_exists,
		    w[4].fault_errors);
		say("churn MOVE: attempts=%" PRIu64 " moved=%" PRIu64
		    " bytes=%" PRIu64 " A->B=%" PRIu64 " B->A=%" PRIu64
		    " covered=%" PRIu64 "/%" PRIu64
		    " EAGAIN=%" PRIu64 " EEXIST=%" PRIu64
		    " validity_lock_busy=%" PRIu64, w[3].calls, w[3].ok,
		    w[3].ok * (uint64_t)PG, w[3].directions[0],
		    w[3].directions[1], covered[0], covered[1], w[3].again,
		    w[3].exists, w[3].lock_busy);
		if (created == n && (covered[0] != CHURN_PAGES ||
				     covered[1] != CHURN_PAGES))
			fail("churn MOVE: incomplete bidirectional page coverage");
	}
	if (created == n && (current.store_ok <= store_ok0 ||
			    current.store_fail <= store_fail0 ||
			    !(tokens || w[0].tokens || w[1].tokens)))
		fail("churn %s: missing actual memcomp store/rollback/token coverage", label);

	/* Workers have joined; inspect all surviving data, not inactive holes. */
	{
		struct churn_worker verify = { .ctx = &c };

		for (i = 0; i < CHURN_PAGES; i++)
			if (!churn_check_page(&verify, i))
				bad++;
	}
	say("churn %s: final_attempted=%u bad_or_invalid_pages=%" PRIu64
	    " live_bad_pages=%" PRIu64, label, CHURN_PAGES, bad, w[2].bad);
	if (bad)
		fail("churn %s: final data integrity failed", label);
	if (created == n && (!w[0].ok || !w[1].ok ||
			    w[2].pages < CHURN_PAGES))
		fail("churn %s: insufficient pageout/refault coverage", label);
	if (created == n && !bad && !w[0].error && !w[1].error &&
	    !w[2].error && !w[2].bad &&
	    (!move || (!w[3].error && covered[0] == CHURN_PAGES &&
		       covered[1] == CHURN_PAGES && !w[4].error &&
		       !w[4].fault_errors)) && w[0].ok && w[1].ok &&
	    w[2].pages >= CHURN_PAGES)
		status = 0;
out:
	if (c.uffd >= 0)
		close(c.uffd);
	if (c.pm >= 0)
		close(c.pm);
	for (t = 0; t < locks; t++) {
		r = pthread_rwlock_destroy(&c.valid[t]);
		if (r) {
			fail("churn MOVE: rwlock destroy: %s", strerror(r));
			status = -1;
		}
	}
	if (mapped) {
		if (munmap(c.base, c.map_len)) {
			fail("churn %s: munmap: %s", label, strerror(errno));
			status = -1;
		} else if (!churn_residue(label, round, owner0, pending0)) {
			status = -1;
		}
	}
	return status;
}

/* Save and restore the selected mode rather than leaving global THP changed. */
struct churn_mode {
	char path[192], saved[32];
	bool changed;
};

static bool churn_write_mode(const char *path, const char *value)
{
	int fd = open(path, O_WRONLY | O_CLOEXEC);
	size_t len = strlen(value);
	ssize_t n;
	int err;

	if (fd < 0)
		return false;
	do {
		n = write(fd, value, len);
	} while (n < 0 && errno == EINTR);
	err = errno;
	if (close(fd) && n == (ssize_t)len)
		return false;
	errno = err;
	return n == (ssize_t)len;
}

static void churn_set_mode(struct churn_mode *m, const char *value)
{
	char buf[256], *p, *q;
	size_t len;

	if (!read_file(m->path, buf, sizeof(buf)))
		return;
	p = strchr(buf, '[');
	q = p ? strchr(p, ']') : NULL;
	if (!q)
		return;
	len = (size_t)(q - ++p);
	if (!len || len >= sizeof(m->saved))
		return;
	memcpy(m->saved, p, len);
	m->saved[len] = 0;
	if (!strcmp(m->saved, value))
		return;
	if (churn_write_mode(m->path, value))
		m->changed = true;
	else
		say("churn: THP mode unchanged: %s", m->path);
}

static void churn_test(void)
{
	unsigned long rounds = envu("CHURN_ROUNDS", 4);
	unsigned long secs = envu("CHURN_SECS", 15), i;
	struct churn_mode top = {
		.path = "/sys/kernel/mm/transparent_hugepage/enabled",
	};
	struct churn_mode small = { 0 };
	unsigned long plain_done = 0, move_done = 0;
	bool move_skip = false;
	int failures = g_fail;
	struct churn_events baseline;
	int64_t owner0, pending0;

	header("256-page pageout/refault and ART MOVE churn");
	if (envu("SKIP_CHURN", 0)) {
		coverage_skip("churn plain/MOVE", "SKIP_CHURN=1; zero rounds executed");
		return;
	}
	if (!rounds || !secs || p_owner_tol > (unsigned long)INT64_MAX) {
		fail("churn: rounds/seconds must be positive; OWNER_TOL <= INT64_MAX");
		return;
	}
	if (sysconf(_SC_PAGESIZE) != (long)PG) {
		say("churn: SKIP needs %lu-byte pages; plain_rounds=0 MOVE_rounds=0",
		    PG);
		return;
	}
	if (!churn_read_events(&baseline))
		return;
	owner0 = baseline.owner;
	pending0 = baseline.pending;
	say("churn: pages=%u rounds=%lu seconds=%lu, 2 pageout + 1 refault; "
	    "MOVE adds 1 mover", CHURN_PAGES, rounds, secs);
	snprintf(small.path, sizeof(small.path),
		 "/sys/kernel/mm/transparent_hugepage/hugepages-%lukB/enabled",
		 p_mthp_kb);
	churn_set_mode(&top, "never");
	churn_set_mode(&small, "always");
	say("churn: selected mTHP=%lukB mode=%s (allocation not guaranteed)",
	    p_mthp_kb, small.changed ? "always" :
	    small.saved[0] ? small.saved : "unavailable/base-page fallback");
	for (i = 0; i < rounds; i++) {
		if (churn_round(false, i, secs))
			goto restore;
		plain_done++;
	}
	for (i = 0; i < rounds; i++) {
		int r = churn_round(true, i, secs);

		if (r) {
			move_skip = r > 0;
			if (move_skip) coverage_skip("churn MOVE", "UFFD MOVE unavailable");
			goto restore;
		}
		move_done++;
	}
restore:
	/* Also catch sub-tolerance residue accumulating over multiple rounds. */
	if (!churn_residue("aggregate", 0, owner0, pending0))
		fail("churn: aggregate event verification failed");
	if (small.changed && !churn_write_mode(small.path, small.saved))
		fail("churn: cannot restore mTHP mode: %s", strerror(errno));
	if (top.changed && !churn_write_mode(top.path, top.saved))
		fail("churn: cannot restore top THP mode: %s", strerror(errno));
	/* Report progress, never a synthetic PASS (including a skipped MOVE). */
	say("churn: plain_rounds=%lu/%lu MOVE_rounds=%lu/%lu MOVE_skipped=%u "
	    "harness_failure=%d failure_before=%d", plain_done, rounds,
	    move_done, rounds, (unsigned int)move_skip, g_fail, failures);
}

#endif /* MEMCOMP_CHURN_H */
