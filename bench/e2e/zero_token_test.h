/* SPDX-License-Identifier: GPL-2.0 */
#ifndef MCWB_ZERO_TOKEN_TEST_H
#define MCWB_ZERO_TOKEN_TEST_H

/*
 * Required J.3 phase, included after the mcwb helpers and before run_tests().
 * No SKIP_* override: absence of the required kernel ABI is a test failure.
 * All observations are real syscalls; no synthetic tokens or mirrored state.
 */
#include <linux/kernel-page-flags.h>
#include <linux/memfd.h>
#include <linux/userfaultfd.h>

#define ZT_PAGES 64UL
#define ZT_LEN (ZT_PAGES * PG)
#define ZT_ATTEMPTS 100U
#define ZT_MOVE_ATTEMPTS 32U
#define ZT_PRESENT (UINT64_C(1) << 63)
#define ZT_SWAP (UINT64_C(1) << 62)
#define ZT_FILE (UINT64_C(1) << 61)
#define ZT_WP (UINT64_C(1) << 57)
#define ZT_EXCLUSIVE (UINT64_C(1) << 56)
#define ZT_FRAME ((UINT64_C(1) << 55) - 1)
#define ZT_TYPE_MASK UINT64_C(31)
#define ZT_ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))

#if !defined(UFFDIO_MOVE) || !defined(UFFD_FEATURE_MOVE)
#error "J.3 requires the local Linux 6.12 tools UFFD MOVE UAPI"
#endif

struct zt_mapping {
	void *reservation;
	size_t reserved_len;
	unsigned char *addr;
};

struct zt_context {
	int pm;
	bool type_known;
	unsigned int type;
};

static bool zt_counter(uint64_t *value)
{
	char buf[16384], *p, *end;
	static const char key[] = "samefill_pages";
	size_t len = read_file(MCDIR "events", buf, sizeof(buf));

	if (!len || len == sizeof(buf) - 1) {
		fail("zero: missing/truncated required memcomp events");
		return false;
	}
	for (p = buf; p && *p; p = end ? end + 1 : NULL) {
		end = strchr(p, '\n');
		if (strncmp(p, key, sizeof(key) - 1) ||
		    p[sizeof(key) - 1] != ' ')
			continue;
		p += sizeof(key);
		while (*p == ' ' || *p == '\t')
			p++;
		if (*p < '0' || *p > '9')
			break;
		errno = 0;
		*value = strtoull(p, &end, 10);
		if (!errno && (*end == '\n' || *end == '\0'))
			return true;
		break;
	}
	fail("zero: required samefill_pages counter missing/invalid");
	return false;
}

static bool zt_count_is(const char *phase, uint64_t expected)
{
	uint64_t seen = 0;
	unsigned int n;

	for (n = 0; n < ZT_ATTEMPTS; n++) {
		if (!zt_counter(&seen))
			return false;
		if (seen == expected)
			return true;
		msleep(10);
	}
	fail("zero/%s: samefill_pages=%" PRIu64 ", expected=%" PRIu64,
	     phase, seen, expected);
	return false;
}

static bool zt_map(struct zt_mapping *m, size_t alignment, bool huge)
{
	uintptr_t a;

	memset(m, 0, sizeof(*m));
	/* At most 64 accessible pages, with inaccessible ends/alignment slack. */
	m->reserved_len = ZT_LEN + alignment + 2 * PG;
	m->reservation = mmap(NULL, m->reserved_len, PROT_NONE,
			      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (m->reservation == MAP_FAILED) {
		m->reservation = NULL;
		fail("zero: mmap failed: %s", strerror(errno));
		return false;
	}
	a = ((uintptr_t)m->reservation + PG + alignment - 1) &
	    ~(uintptr_t)(alignment - 1);
	m->addr = (unsigned char *)a;
	if (mprotect(m->addr, ZT_LEN, PROT_READ | PROT_WRITE) ||
	    madvise(m->addr, ZT_LEN, huge ? MADV_HUGEPAGE : MADV_NOHUGEPAGE)) {
		fail("zero: required mapping policy failed: %s", strerror(errno));
		munmap(m->reservation, m->reserved_len);
		memset(m, 0, sizeof(*m));
		return false;
	}
	return true;
}

static void zt_unmap(struct zt_mapping *m)
{
	if (m->reservation && munmap(m->reservation, m->reserved_len))
		fail("zero: munmap failed: %s", strerror(errno));
	memset(m, 0, sizeof(*m));
}

static void zt_fill_zero(unsigned char *p)
{
	volatile unsigned char *v = p;
	size_t i;

	/* Real writes allocate private pages; a read-only ZERO_PAGE is no proof. */
	for (i = 0; i < ZT_LEN; i++)
		v[i] = 0;
}

static bool zt_bytes(const char *phase, unsigned char *p, unsigned char byte)
{
	volatile unsigned char *v = p;
	size_t i;

	for (i = 0; i < ZT_LEN; i++) {
		if (v[i] != byte) {
			fail("zero/%s: byte mismatch at page=%zu byte=%zu",
			     phase, i / PG, i % PG);
			return false;
		}
	}
	return true;
}

static bool zt_visible_present(struct zt_context *c, unsigned char *p)
{
	size_t i;

	for (i = 0; i < ZT_PAGES; i++) {
		uint64_t e = pagemap_entry(c->pm, p + i * PG);

		if (!(e & ZT_PRESENT) || (e & ZT_SWAP) || !(e & ZT_FRAME)) {
			fail("zero: page=%zu not resident with a visible nonzero PFN; "
			     "CAP_SYS_ADMIN in init userns/pagemap visibility required "
			     "(entry=%016" PRIx64 ")", i, e);
			return false;
		}
	}
	return true;
}

static uint64_t zt_offset(uint64_t e)
{
	return (e & ZT_FRAME) >> 5;
}

static bool zt_is_token(struct zt_context *c, uint64_t e, bool positive)
{
	if (!(e & ZT_SWAP) || (e & ZT_PRESENT) ||
	    (positive ? !zt_offset(e) : !!zt_offset(e)))
		return false;
	if (c->type_known && (e & ZT_TYPE_MASK) != c->type)
		return false;
	return true;
}

static bool zt_pageout(struct zt_context *c, unsigned char *p, bool positive,
		       const char *phase, uint64_t entries[ZT_PAGES])
{
	unsigned int n;
	size_t i, got = 0;

	for (n = 0; n < ZT_ATTEMPTS; n++) {
		if (madvise(p, ZT_LEN, MADV_PAGEOUT)) {
			fail("zero/%s: required MADV_PAGEOUT: %s",
			     phase, strerror(errno));
			return false;
		}
		got = 0;
		for (i = 0; i < ZT_PAGES; i++) {
			entries[i] = pagemap_entry(c->pm, p + i * PG);
			if (!zt_is_token(c, entries[i], positive))
				continue;
			if (!c->type_known && !positive) {
				c->type = entries[i] & ZT_TYPE_MASK;
				c->type_known = true;
			}
			got++;
		}
		if (got == ZT_PAGES) {
			say("zero/%s: observed %zu/%lu actual swapped %s tokens, "
			    "type=%u (no present/none)", phase, got, ZT_PAGES,
			    positive ? "positive ZERO" : "offset-0", c->type);
			return true;
		}
		msleep(10);
	}
	fail("zero/%s: only %zu/%lu actual %s tokens after %u pageouts; "
	     "required zero path did not complete (last=%016" PRIx64 ")",
	     phase, got, ZT_PAGES, positive ? "positive" : "offset-0",
	     ZT_ATTEMPTS, entries[ZT_PAGES - 1]);
	return false;
}

static bool zt_prepare(struct zt_context *c, struct zt_mapping *m,
		       const char *phase, uint64_t *base,
		       uint64_t entries[ZT_PAGES])
{
	if (!zt_counter(base) || !zt_map(m, PG, false))
		return false;
	zt_fill_zero(m->addr);
	return zt_visible_present(c, m->addr) &&
	       zt_pageout(c, m->addr, false, phase, entries) &&
	       zt_count_is(phase, *base + ZT_PAGES);
}

static bool zt_wait_child(pid_t pid)
{
	unsigned int n;
	int status = 0;

	for (n = 0; n < 500; n++) {
		pid_t r = waitpid(pid, &status, WNOHANG);

		if (r == pid) {
			if (WIFEXITED(status) && !WEXITSTATUS(status))
				return true;
			fail("zero/fork: child failed, wait status=%#x", status);
			return false;
		}
		if (r < 0 && errno != EINTR) {
			fail("zero/fork: waitpid: %s", strerror(errno));
			return false;
		}
		msleep(10);
	}
	fail("zero/fork: child exceeded bounded 5s wait");
	if (kill(pid, SIGKILL) && errno != ESRCH)
		fail("zero/fork: SIGKILL: %s", strerror(errno));
	for (n = 0; n < 100; n++) {
		pid_t r = waitpid(pid, &status, WNOHANG);

		if (r == pid || (r < 0 && errno == ECHILD))
			return false;
		msleep(10);
	}
	fail("zero/fork: killed child not reaped within 1s");
	return false;
}

static void zt_anon_fork(struct zt_context *c)
{
	struct zt_mapping m = { 0 };
	uint64_t e[ZT_PAGES], base = 0;
	pid_t pid;
	size_t i;

	header("J.3 zero: order0, fork and COW");
	if (!zt_prepare(c, &m, "anon/fork", &base, e))
		goto out;
	/* Fork the actual nonpresent tokens, before the parent reads any byte. */
	pid = fork();
	if (pid < 0) {
		fail("zero/fork: fork: %s", strerror(errno));
		goto out;
	}
	if (!pid) {
		volatile unsigned char *v = m.addr;
		int child_pm;

		alarm(4);
		/* An inherited pagemap fd would inspect the parent's mm, not ours. */
		child_pm = open("/proc/self/pagemap", O_RDONLY | O_CLOEXEC);
		if (child_pm < 0)
			_exit(33);
		for (i = 0; i < ZT_PAGES; i++) {
			uint64_t entry = 0;
			off_t off = ((uintptr_t)(m.addr + i * PG) / PG) * sizeof(entry);

			if (pread(child_pm, &entry, sizeof(entry), off) != sizeof(entry) ||
			    (entry & (ZT_PRESENT | ZT_SWAP)) != ZT_SWAP ||
			    zt_offset(entry) || (entry & ZT_TYPE_MASK) != c->type)
				_exit(34);
		}
		if (close(child_pm))
			_exit(35);
		for (i = 0; i < ZT_LEN; i++)
			if (v[i])
				_exit(31);
		for (i = 0; i < ZT_LEN; i++)
			v[i] = 0xa7;
		for (i = 0; i < ZT_LEN; i++)
			if (v[i] != 0xa7)
				_exit(32);
		_exit(0);
	}
	if (!zt_wait_child(pid))
		goto out;
	for (i = 0; i < ZT_PAGES; i++) {
		uint64_t after = pagemap_entry(c->pm, m.addr + i * PG);

		if (!zt_is_token(c, after, false)) {
			fail("zero/fork: parent token changed before read page=%zu", i);
			goto out;
		}
	}
	if (zt_count_is("fork child exit", base + ZT_PAGES) &&
	    zt_bytes("parent after child COW", m.addr, 0) &&
	    zt_count_is("parent refault", base))
		say("zero/anon+fork: all %lu pages/%lu bytes verified, child COW isolated",
		    ZT_PAGES, ZT_LEN);
out:
	zt_unmap(&m);
}

static int zt_uffd(uint64_t features, const char *phase)
{
	struct uffdio_api api = { .api = UFFD_API, .features = features };
	int fd = syscall(__NR_userfaultfd, O_CLOEXEC | O_NONBLOCK);

	if (fd < 0) {
		fail("zero/%s: required userfaultfd syscall: %s", phase, strerror(errno));
		return -1;
	}
	if (ioctl(fd, UFFDIO_API, &api) || (api.features & features) != features) {
		fail("zero/%s: required UFFD feature negotiation failed "
		     "(requested=%" PRIx64 ", returned=%" PRIx64 "): %s",
		     phase, features, (uint64_t)api.features, strerror(errno));
		close(fd);
		return -1;
	}
	return fd;
}

static bool zt_register(int fd, unsigned char *p, uint64_t mode,
			uint64_t required, const char *phase)
{
	struct uffdio_register r = {
		.range = { .start = (uintptr_t)p, .len = ZT_LEN }, .mode = mode,
	};

	if (ioctl(fd, UFFDIO_REGISTER, &r) || (r.ioctls & required) != required) {
		fail("zero/%s: required UFFD registration/ioctls failed: %s "
		     "(available=%" PRIx64 ", required=%" PRIx64 ")",
		     phase, strerror(errno), (uint64_t)r.ioctls, required);
		return false;
	}
	return true;
}

static bool zt_wp(int fd, unsigned char *p, size_t len)
{
	struct uffdio_writeprotect wp = {
		.range = { .start = (uintptr_t)p, .len = len },
		.mode = UFFDIO_WRITEPROTECT_MODE_WP,
	};

	if (ioctl(fd, UFFDIO_WRITEPROTECT, &wp)) {
		fail("zero: required UFFDIO_WRITEPROTECT: %s", strerror(errno));
		return false;
	}
	return true;
}

static void zt_uffd_wp(struct zt_context *c)
{
	struct zt_mapping m = { 0 };
	uint64_t e[ZT_PAGES], base = 0;
	size_t i, swapped_wp = 0, present_wp = 0;
	int fd = -1;

	header("J.3 zero: UFFD-WP set on token before read");
	if (!zt_prepare(c, &m, "WP", &base, e))
		goto out;
	fd = zt_uffd(UFFD_FEATURE_PAGEFAULT_FLAG_WP, "WP");
	/* WP-only: reads must use the real swap fault path, not a missing handler. */
	if (fd < 0 || !zt_register(fd, m.addr, UFFDIO_REGISTER_MODE_WP,
		UINT64_C(1) << _UFFDIO_WRITEPROTECT, "WP") ||
	    !zt_wp(fd, m.addr, ZT_LEN))
		goto out;
	for (i = 0; i < ZT_PAGES; i++) {
		e[i] = pagemap_entry(c->pm, m.addr + i * PG);
		swapped_wp += zt_is_token(c, e[i], false) && !!(e[i] & ZT_WP);
	}
	if (swapped_wp != ZT_PAGES) {
		fail("zero/WP: before-read swapped+WP=%zu/%lu", swapped_wp, ZT_PAGES);
		goto out;
	}
	if (!zt_bytes("WP read", m.addr, 0))
		goto out;
	for (i = 0; i < ZT_PAGES; i++) {
		e[i] = pagemap_entry(c->pm, m.addr + i * PG);
		present_wp += (e[i] & (ZT_PRESENT | ZT_SWAP | ZT_WP)) ==
			      (ZT_PRESENT | ZT_WP);
	}
	if (present_wp != ZT_PAGES)
		fail("zero/WP: read lost WP, present+WP=%zu/%lu", present_wp, ZT_PAGES);
	else if (zt_count_is("WP refault", base))
		say("zero/WP: swapped+WP=%zu, refault-present+WP=%zu, all bytes zero",
		    swapped_wp, present_wp);
out:
	if (fd >= 0 && close(fd))
		fail("zero/WP: close: %s", strerror(errno));
	zt_unmap(&m);
}

static void zt_move(struct zt_context *c)
{
	struct zt_mapping src = { 0 }, dst = { 0 };
	uint64_t before[ZT_PAGES], after[ZT_PAGES], base = 0;
	static const uint64_t bits[] = {
		ZT_PRESENT, ZT_SWAP, ZT_FILE, ZT_WP, ZT_EXCLUSIVE, MCWB_PM_SOFT_DIRTY,
	};
	unsigned long count[ZT_ARRAY_SIZE(bits)] = { 0 };
	bool softdirty_ok;
	unsigned int retry = 0;
	size_t i, j, moved = 0, equal = 0, holes = 0;
	int fd = -1;

	header("J.3 zero: actual UFFDIO_MOVE and all exposed PTE bits");
	if (!zt_prepare(c, &src, "MOVE", &base, before) ||
	    !zt_map(&dst, PG, false))
		goto out;
	fd = zt_uffd(UFFD_FEATURE_MOVE | UFFD_FEATURE_PAGEFAULT_FLAG_WP, "MOVE");
	if (fd < 0 ||
	    !zt_register(fd, src.addr, UFFDIO_REGISTER_MODE_WP,
		UINT64_C(1) << _UFFDIO_WRITEPROTECT, "MOVE src") ||
	    !zt_register(fd, dst.addr,
		UFFDIO_REGISTER_MODE_MISSING | UFFDIO_REGISTER_MODE_WP,
		(UINT64_C(1) << _UFFDIO_MOVE) |
		(UINT64_C(1) << _UFFDIO_WRITEPROTECT), "MOVE dst"))
		goto out;
	/* Mix WP-set and WP-clear swap PTEs, keeping both VMAs registered. */
	for (i = 0; i < ZT_PAGES; i += 2)
		if (!zt_wp(fd, src.addr + i * PG, PG))
			goto out;
	for (i = 0; i < ZT_PAGES; i++) {
		uint64_t d = pagemap_entry(c->pm, dst.addr + i * PG);

		before[i] = pagemap_entry(c->pm, src.addr + i * PG);
		if (!zt_is_token(c, before[i], false) ||
		    !!(before[i] & ZT_WP) != !(i & 1) ||
		    (d & (ZT_PRESENT | ZT_SWAP | ZT_WP))) {
			fail("zero/MOVE: invalid pre-move token/WP/hole page=%zu", i);
			goto out;
		}
	}
	for (i = 0; i < ZT_PAGES; i++) {
		unsigned int n;
		bool done = false;

		for (n = 0; n < ZT_MOVE_ATTEMPTS; n++) {
			struct uffdio_move mv = {
				.dst = (uintptr_t)(dst.addr + i * PG),
				.src = (uintptr_t)(src.addr + i * PG), .len = PG,
			};
			int rc, error;

			errno = 0;
			rc = ioctl(fd, UFFDIO_MOVE, &mv);
			error = errno;

			if (!rc && mv.move == (int64_t)PG) {
				done = true;
				break;
			}
			/*
			 * One-page requests cannot partially succeed. Early
			 * mmap_changing EAGAIN can leave the output field 0.
			 */
			if (rc == -1 && (error == EAGAIN || error == EEXIST) &&
			    (mv.move == 0 || mv.move == -EAGAIN ||
			     mv.move == -EEXIST)) {
				uint64_t s = pagemap_entry(c->pm, src.addr + i * PG);
				uint64_t d = pagemap_entry(c->pm, dst.addr + i * PG);

				if (s != before[i] || (d & (ZT_PRESENT | ZT_SWAP | ZT_WP)))
					break;
				retry++;
				msleep(1);
				continue;
			}
			fail("zero/MOVE: ioctl page=%zu rc=%d errno=%d move=%" PRId64,
			     i, rc, error, (int64_t)mv.move);
			break;
		}
		if (!done) {
			fail("zero/MOVE: page=%zu did not move within %u attempts",
			     i, ZT_MOVE_ATTEMPTS);
			goto out;
		}
		moved++;
	}
	for (i = 0; i < ZT_PAGES; i++) {
		uint64_t s = pagemap_entry(c->pm, src.addr + i * PG);

		after[i] = pagemap_entry(c->pm, dst.addr + i * PG);
		equal += after[i] == before[i] && zt_is_token(c, after[i], false);
		holes += !(s & (ZT_PRESENT | ZT_SWAP | ZT_WP));
		for (j = 0; j < ZT_ARRAY_SIZE(bits); j++)
			count[j] += !!(after[i] & bits[j]);
	}
	if (check_soft_dirty("zero/MOVE soft-dirty bit")) {
		say("zero/MOVE: moved=%zu/%lu raw64_equal=%zu holes=%zu retries=%u; "
		    "present=%lu swap=%lu file=%lu WP=%lu exclusive=%lu softdirty=%lu",
		    moved, ZT_PAGES, equal, holes, retry, count[0], count[1], count[2],
		    count[3], count[4], count[5]);
		softdirty_ok = count[5] == ZT_PAGES;
	} else {
		say("zero/MOVE: moved=%zu/%lu raw64_equal=%zu holes=%zu retries=%u; "
		    "present=%lu swap=%lu file=%lu WP=%lu exclusive=%lu softdirty=%lu (unsupported)",
		    moved, ZT_PAGES, equal, holes, retry, count[0], count[1], count[2],
		    count[3], count[4], count[5]);
		softdirty_ok = count[5] == 0;
	}
	if (equal != ZT_PAGES || holes != ZT_PAGES ||
	    count[0] || count[1] != ZT_PAGES || count[3] != ZT_PAGES / 2 ||
	    !softdirty_ok) {
		fail("zero/MOVE: lost token or exposed PTE bits");
		goto out;
	}
	if (!zt_count_is("MOVE net-zero ownership", base + ZT_PAGES))
		goto out;
	/* Unregister before reading: this test never fabricates missing pages. */
	if (close(fd))
		fail("zero/MOVE: close: %s", strerror(errno));
	fd = -1;
	if (zt_bytes("MOVE destination", dst.addr, 0))
		zt_count_is("MOVE refault", base);
out:
	if (fd >= 0 && close(fd))
		fail("zero/MOVE: close cleanup: %s", strerror(errno));
	zt_unmap(&dst);
	zt_unmap(&src);
}

static void zt_madv_free(struct zt_context *c)
{
	struct zt_mapping m = { 0 };
	uint64_t e[ZT_PAGES], base = 0;
	size_t i, cleared = 0;

	header("J.3 zero: MADV_FREE clears real zero tokens");
	if (!zt_prepare(c, &m, "MADV_FREE", &base, e))
		goto out;
	if (madvise(m.addr, ZT_LEN, MADV_FREE)) {
		fail("zero/MADV_FREE: required syscall: %s", strerror(errno));
		goto out;
	}
	for (i = 0; i < ZT_PAGES; i++) {
		e[i] = pagemap_entry(c->pm, m.addr + i * PG);
		cleared += !(e[i] & (ZT_PRESENT | ZT_SWAP | ZT_WP));
	}
	if (cleared != ZT_PAGES) {
		fail("zero/MADV_FREE: cleared=%zu/%lu before read", cleared, ZT_PAGES);
		goto out;
	}
	if (zt_count_is("MADV_FREE clear", base) &&
	    zt_bytes("MADV_FREE refault", m.addr, 0))
		say("zero/MADV_FREE: %zu tokens cleared, full zero refault verified", cleared);
out:
	zt_unmap(&m);
}

/* cachestat(2) UAPI; local copies keep the static guest off <linux/mman.h>. */
struct zt_cachestat_range {
	uint64_t off;
	uint64_t len;
};

struct zt_cachestat {
	uint64_t nr_cache;
	uint64_t nr_dirty;
	uint64_t nr_writeback;
	uint64_t nr_evicted;
	uint64_t nr_recently_evicted;
};

#ifndef __NR_cachestat
#define __NR_cachestat 451
#endif

static bool zt_cachestat(int fd, struct zt_cachestat *cs)
{
	struct zt_cachestat_range range = { .off = 0, .len = ZT_LEN };

	if (syscall(__NR_cachestat, fd, &range, cs, 0)) {
		fail("zero/shmem: required cachestat: %s", strerror(errno));
		return false;
	}
	return true;
}

static void zt_shmem(struct zt_context *c)
{
	unsigned char *a = MAP_FAILED, *b = MAP_FAILED;
	unsigned char vec[ZT_PAGES];
	struct zt_cachestat cs = { 0 };
	uint64_t base = 0, count = 0;
	size_t i, resident = 0, absent = 0;
	unsigned int n;
	int fd = -1;
	bool stored = false;

	header("J.3 zero: shmem cache eviction, roundtrip and aliases");
	if (!zt_counter(&base))
		return;
	fd = syscall(__NR_memfd_create, "mcwb-j3-zero", MFD_CLOEXEC);
	if (fd < 0 || ftruncate(fd, ZT_LEN)) {
		fail("zero/shmem: required memfd/ftruncate: %s", strerror(errno));
		goto out;
	}
	a = mmap(NULL, ZT_LEN, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	b = mmap(NULL, ZT_LEN, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (a == MAP_FAILED || b == MAP_FAILED ||
	    madvise(a, ZT_LEN, MADV_NOHUGEPAGE) ||
	    madvise(b, ZT_LEN, MADV_NOHUGEPAGE)) {
		fail("zero/shmem: required shared/nohuge mappings: %s", strerror(errno));
		goto out;
	}
	zt_fill_zero(a);
	if (!zt_visible_present(c, a))
		goto out;
	/* b remains unfaulted: a is the only mapping to reclaim, not an mlock. */
	for (n = 0; n < ZT_ATTEMPTS; n++) {
		if (madvise(a, ZT_LEN, MADV_PAGEOUT) || mincore(a, ZT_LEN, vec)) {
			fail("zero/shmem: required pageout/mincore: %s", strerror(errno));
			goto out;
		}
		if (!zt_cachestat(fd, &cs))
			goto out;
		resident = absent = 0;
		for (i = 0; i < ZT_PAGES; i++) {
			uint64_t ea = pagemap_entry(c->pm, a + i * PG);
			uint64_t eb = pagemap_entry(c->pm, b + i * PG);

			resident += vec[i] & 1;
			absent += !((ea | eb) & (ZT_PRESENT | ZT_SWAP));
		}
		if (!zt_counter(&count))
			goto out;
		/* A zero token needs no I/O, so mincore reports it resident. */
		if (resident == ZT_PAGES && absent == ZT_PAGES &&
		    !cs.nr_cache && cs.nr_evicted == ZT_PAGES &&
		    count == base + ZT_PAGES) {
			stored = true;
			break;
		}
		msleep(10);
	}
	if (!stored) {
		fail("zero/shmem: no actual cache ZERO proof; resident=%zu absent=%zu "
		     "cache=%" PRIu64 " evicted=%" PRIu64
		     " samefill=%" PRIu64 " baseline=%" PRIu64,
		     resident, absent, cs.nr_cache, cs.nr_evicted, count, base);
		goto out;
	}
	say("zero/shmem: mincore-resident=%zu/%lu, cache folios=%" PRIu64
	    ", evicted entries=%" PRIu64 ", absent aliases=%zu, "
	    "samefill delta=%lu (xa token ID not exposed by pagemap)",
	    resident, ZT_PAGES, cs.nr_cache, cs.nr_evicted, absent, ZT_PAGES);
	if (!zt_bytes("shmem alias A zero", a, 0) ||
	    !zt_bytes("shmem alias B zero", b, 0) ||
	    !zt_count_is("shmem cache refault", base))
		goto out;
	/* Whole-range writes/reads prove page-cache alias coherence, not COW. */
	memset(b, 0x53, ZT_LEN);
	if (!zt_bytes("shmem B->A", a, 0x53))
		goto out;
	memset(a, 0xc9, ZT_LEN);
	if (zt_bytes("shmem A->B", b, 0xc9))
		say("zero/shmem: full-byte ZERO roundtrip and bidirectional alias coherence");
out:
	if (b != MAP_FAILED && munmap(b, ZT_LEN))
		fail("zero/shmem: unmap B: %s", strerror(errno));
	if (a != MAP_FAILED && munmap(a, ZT_LEN))
		fail("zero/shmem: unmap A: %s", strerror(errno));
	if (fd >= 0 && close(fd))
		fail("zero/shmem: close: %s", strerror(errno));
}

static bool zt_mthp_stat(const char *key, int64_t *value)
{
	/* Validate the text too: the legacy helper accepts strtoll errors as 0. */
	char path[192], buf[64], *end;
	long long n;

	snprintf(path, sizeof(path), "/sys/kernel/mm/transparent_hugepage/"
		 "hugepages-%lukB/stats/%s", p_mthp_kb, key);
	if (!read_file(path, buf, sizeof(buf))) {
		fail("zero/mTHP: required stat unavailable: %s", path);
		return false;
	}
	errno = 0;
	n = strtoll(buf, &end, 10);
	if (errno || end == buf || n < 0 || (*end != '\n' && *end != '\0')) {
		fail("zero/mTHP: malformed required stat %s", path);
		return false;
	}
	*value = n;
	return true;
}

static bool zt_compound(struct zt_context *c, unsigned char *p, size_t step)
{
	uint64_t entries[ZT_PAGES], flags, head = 0;
	const uint64_t compound = (UINT64_C(1) << KPF_COMPOUND_HEAD) |
				  (UINT64_C(1) << KPF_COMPOUND_TAIL);
	size_t i;
	int fd = open("/proc/kpageflags", O_RDONLY | O_CLOEXEC);
	bool ok = false;

	if (fd < 0) {
		fail("zero/mTHP: required kpageflags: %s", strerror(errno));
		return false;
	}
	for (i = 0; i < ZT_PAGES; i++) {
		uint64_t pfn, expected;

		entries[i] = pagemap_entry(c->pm, p + i * PG);
		pfn = entries[i] & ZT_FRAME;
		if (!i || !(i % step))
			head = pfn;
		expected = UINT64_C(1) << (i % step ? KPF_COMPOUND_TAIL :
						     KPF_COMPOUND_HEAD);
		if ((entries[i] & (ZT_PRESENT | ZT_SWAP)) != ZT_PRESENT ||
		    !pfn || pfn != head + i % step ||
		    pread(fd, &flags, sizeof(flags), (off_t)(pfn * sizeof(flags))) !=
			(ssize_t)sizeof(flags)) {
			fail("zero/mTHP: unreadable/noncontiguous resident PFN page=%zu", i);
			goto out;
		}
		if ((flags & compound) != expected ||
		    !(flags & (UINT64_C(1) << KPF_THP)) ||
		    !(flags & (UINT64_C(1) << KPF_ANON)) ||
		    (flags & (UINT64_C(1) << KPF_ZERO_PAGE))) {
			fail("zero/mTHP: page=%zu not the selected real compound "
			     "head/tail (kpageflags=%016" PRIx64 ")", i, flags);
			goto out;
		}
	}
	for (i = 0; i < ZT_PAGES; i++) {
		if (entries[i] != pagemap_entry(c->pm, p + i * PG)) {
			fail("zero/mTHP: mapping migrated/changed during compound observation");
			goto out;
		}
	}
	ok = true;
	say("zero/mTHP: %lu PFNs verified as %zu real %luK compound head/tail groups",
	    ZT_PAGES, ZT_PAGES / step, p_mthp_kb);
out:
	if (close(fd))
		fail("zero/mTHP: kpageflags close: %s", strerror(errno));
	return ok;
}

static void zt_mthp(struct zt_context *c)
{
	struct zt_mapping m = { 0 };
	char path[192], buf[256], saved[32], *a, *b;
	uint64_t entries[ZT_PAGES], base = 0;
	int64_t alloc0, alloc1, nr0, nr1, split0, split1;
	int64_t swp0, swp1;
	size_t i, step, groups;
	bool changed = false;

	header("J.3 zero: real mTHP retains positive ZERO offsets");
	if (p_mthp_kb != 16 && p_mthp_kb != 32 && p_mthp_kb != 64) {
		fail("zero/mTHP: required selected size must be 16/32/64K");
		return;
	}
	step = (p_mthp_kb << 10) / PG;
	groups = ZT_PAGES / step;
	snprintf(path, sizeof(path), "/sys/kernel/mm/transparent_hugepage/"
		 "hugepages-%lukB/enabled", p_mthp_kb);
	read_file(path, buf, sizeof(buf));
	a = strchr(buf, '[');
	b = a ? strchr(a, ']') : NULL;
	if (!a || !b || b - a <= 1 ||
	    (size_t)(b - a - 1) >= sizeof(saved)) {
		fail("zero/mTHP: cannot save required selected policy %s", path);
		return;
	}
	memcpy(saved, a + 1, b - a - 1);
	saved[b - a - 1] = '\0';
	if (!zt_counter(&base) || !zt_mthp_stat("anon_fault_alloc", &alloc0) ||
	    !zt_mthp_stat("nr_anon", &nr0) || !zt_mthp_stat("split", &split0) ||
	    !zt_mthp_stat("swpout", &swp0))
		return;
	/* Restore even if a policy write reports an error after taking effect. */
	changed = true;
	if (mthp_set("always")) {
		fail("zero/mTHP: required selected-size always policy: %s", strerror(errno));
		goto out;
	}
	if (!zt_map(&m, p_mthp_kb << 10, true))
		goto out;
	zt_fill_zero(m.addr);
	if (!zt_visible_present(c, m.addr) || !zt_compound(c, m.addr, step) ||
	    !zt_mthp_stat("anon_fault_alloc", &alloc1) ||
	    !zt_mthp_stat("nr_anon", &nr1))
		goto out;
	if (alloc1 - alloc0 < (int64_t)groups || nr1 - nr0 < (int64_t)groups) {
		fail("zero/mTHP: requested policy is NOT proof; alloc delta=%" PRId64
		     " nr_anon delta=%" PRId64 " require >=%zu actual %luK folios",
		     alloc1 - alloc0, nr1 - nr0, groups, p_mthp_kb);
		goto out;
	}
	if (!c->type_known) {
		fail("zero/mTHP: missing actual offset-0 memcomp type control");
		goto out;
	}
	if (!zt_pageout(c, m.addr, true, "mTHP", entries) ||
	    !zt_count_is("positive ZERO mTHP", base + ZT_PAGES) ||
	    !zt_mthp_stat("split", &split1) || !zt_mthp_stat("swpout", &swp1))
		goto out;
	if (split1 != split0) {
		fail("zero/mTHP: selected-size split changed %" PRId64 "->%" PRId64,
		     split0, split1);
		goto out;
	}
	for (i = 0; i < ZT_PAGES; i++) {
		if (zt_offset(entries[i]) != zt_offset(entries[i - i % step]) + i % step) {
			fail("zero/mTHP: lost retained contiguous span at page=%zu", i);
			goto out;
		}
	}
	say("zero/mTHP: actual %luK alloc delta=%" PRId64 " nr_anon delta=%" PRId64
	    ", split=%" PRId64 "->%" PRId64 ", swpout=%" PRId64 "->%" PRId64
	    "; %lu/%lu positive ZERO offsets retained across %zu spans",
	    p_mthp_kb, alloc1 - alloc0, nr1 - nr0, split0, split1, swp0, swp1,
	    ZT_PAGES, ZT_PAGES, groups);
	/* memcomp is not conventional swap: swpout need not increase here. */
	if (zt_bytes("positive ZERO mTHP refault", m.addr, 0))
		zt_count_is("mTHP ZERO refault", base);
out:
	zt_unmap(&m);
	if (changed && mthp_set(saved))
		fail("zero/mTHP: failed restoring selected policy '%s': %s",
		     saved, strerror(errno));
}

static void zero_token_test(void)
{
	struct zt_context c = { .pm = -1 };
	uint64_t base = 0;

	header("J.3 required zero-token userspace phase");
	if (sysconf(_SC_PAGESIZE) != (long)PG) {
		fail("zero: this harness requires the local 4096-byte base-page ABI");
		return;
	}
	c.pm = open("/proc/self/pagemap", O_RDONLY | O_CLOEXEC);
	if (c.pm < 0) {
		fail("zero: required pagemap open: %s", strerror(errno));
		return;
	}
	if (!zt_counter(&base))
		goto out;
	/* Serial, before guest workloads: exact global counter deltas are required. */
	zt_anon_fork(&c);
	zt_count_is("anon cleanup", base);
	zt_uffd_wp(&c);
	zt_count_is("WP cleanup", base);
	zt_move(&c);
	zt_count_is("MOVE cleanup", base);
	zt_madv_free(&c);
	zt_count_is("MADV_FREE cleanup", base);
	zt_shmem(&c);
	zt_count_is("shmem cleanup", base);
	zt_mthp(&c);
	zt_count_is("mTHP cleanup", base);
	say("zero: required phases returned; consult FAIL records/final RESULT, "
	    "not an unconditional runtime PASS");
out:
	if (close(c.pm))
		fail("zero: pagemap close: %s", strerror(errno));
}

#endif /* MCWB_ZERO_TOKEN_TEST_H */
