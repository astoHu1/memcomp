/* SPDX-License-Identifier: GPL-2.0 */
/* Targeted runtime checks; included after the compatibility helpers. */
#include <poll.h>

#ifdef RT_WP_UNIT_TEST
extern int rt_wp_test_timedjoin(pthread_t, void **, const struct timespec *);
#define RT_WP_TIMEDJOIN rt_wp_test_timedjoin
#else
#define RT_WP_TIMEDJOIN pthread_timedjoin_np
#endif

static bool rt_wp_join_writer(pthread_t writer, const char *label)
{
	struct timespec deadline;
	int r;

	if (clock_gettime(CLOCK_REALTIME, &deadline)) {
		fail("runtime %s: join clock_gettime: %s", label, strerror(errno));
		return false;
	}
	deadline.tv_sec += 3;
	r = RT_WP_TIMEDJOIN(writer, NULL, &deadline);
	if (!r)
		return true;
	if (r == ETIMEDOUT)
		fail("runtime %s: writer join timed out", label);
	else
		fail("runtime %s: writer join: %s", label, strerror(r));
	return false;
}

static bool rt_wp_no_event(int fd, const char *label)
{
	struct pollfd pfd = { .fd = fd, .events = POLLIN };
	int r = poll(&pfd, 1, 100);

	if (r < 0) {
		fail("runtime %s: event poll: %s", label, strerror(errno));
		return false;
	}
	if (r != 0) {
		fail("runtime %s: unexpected duplicate WP event (revents=0x%x)",
		     label, pfd.revents);
		return false;
	}
	return true;
}

static bool rt_wp_validate_event(const struct uffd_msg *msg,
				 uintptr_t expected, const char *label)
{
	if (msg->event != UFFD_EVENT_PAGEFAULT ||
	    msg->arg.pagefault.flags !=
		(UFFD_PAGEFAULT_FLAG_WP | UFFD_PAGEFAULT_FLAG_WRITE) ||
	    msg->arg.pagefault.address != expected) {
		fail("runtime %s: wrong event/flags/address", label);
		return false;
	}
	return true;
}

static bool rt_wp_cleanup(int *fd, bool *registered,
			 struct uffdio_range *range, pthread_t *writer,
			 bool *started, const char *label)
{
	struct uffdio_writeprotect clear = {
		.range = *range,
		.mode = 0,
	};
	bool ok = true;

	if (*registered && *fd >= 0) {
		if (ioctl(*fd, UFFDIO_WRITEPROTECT, &clear)) {
			fail("runtime %s: cleanup unprotect: %s", label, strerror(errno));
			ok = false;
		}
		if (ioctl(*fd, UFFDIO_UNREGISTER, range)) {
			fail("runtime %s: cleanup unregister: %s", label, strerror(errno));
			ok = false;
		} else {
			*registered = false;
		}
	}
	if (*fd >= 0) {
		if (close(*fd)) {
			fail("runtime %s: cleanup close userfaultfd: %s", label,
			     strerror(errno));
			ok = false;
		}
		/* Do not retry close after an error; Linux may already have released it. */
		*fd = -1;
	}
	if (*started) {
		if (rt_wp_join_writer(*writer, label))
			*started = false;
		else
			ok = false;
	}
	return ok;
}

#ifndef RT_WP_UNIT_TEST

static bool rt_token(uint64_t e)
{
	/* v6.12 MAX_SWAPFILES reserves type 30 for the memcomp entry. */
	return !(e & (1ULL << 63)) && (e & (1ULL << 62)) && (e & 31) == 30;
}

static size_t runtime_token_count(int pm, char *p, size_t pages)
{
	size_t i, n = 0;

	for (i = 0; i < pages; i++)
		n += rt_token(pagemap_entry(pm, p + i * PG));
	return n;
}

static bool rt_pageout(int pm, char *p, size_t pages, const char *label)
{
	size_t n = 0;
	int attempt;

	for (attempt = 0; attempt < 50; attempt++) {
		if (madvise(p, pages * PG, MADV_PAGEOUT)) {
			fail("runtime %s: pageout: %s", label, strerror(errno));
			return false;
		}
		n = runtime_token_count(pm, p, pages);
		if (n == pages)
			break;
		msleep(20);
	}
	say("runtime %s: memcomp_tokens=%zu/%zu", label, n, pages);
	if (n != pages) {
		fail("runtime %s: not all target PTEs are memcomp tokens", label);
		return false;
	}
	return true;
}

static void runtime_fill(char *p)
{
	size_t i, j;

	for (i = 0; i < COMPAT_PAGES; i++)
		for (j = 0; j < PG; j++)
			p[i * PG + j] = compat_byte(i, j);
}

static void rt_free_case(const char *label, size_t first, size_t count,
			 bool shared, bool batch)
{
	const size_t bytes = COMPAT_PAGES * PG;
	char *base = MAP_FAILED, *p = MAP_FAILED;
	size_t maplen = bytes + 2 * MTHP_SZ, i, j, discarded = 0;
	int pm = -1, gate[2] = {-1, -1}, status;
	pid_t child = -1;
	bool child_cow = false;
	int64_t owner = EV("folio_owner_pages"), pending = EV("pending_pages");
	int64_t alloc0 = mthp_stat("anon_fault_alloc");

	pm = open("/proc/self/pagemap", O_RDONLY);
	base = mmap(NULL, maplen, PROT_READ | PROT_WRITE,
		    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (pm < 0 || base == MAP_FAILED) {
		fail("runtime %s: setup: %s", label, strerror(errno));
		goto out;
	}
	p = (char *)(((uintptr_t)base + MTHP_SZ - 1) & ~(MTHP_SZ - 1));
	if (batch) {
		if (mthp_set("always") || madvise(p, bytes, MADV_HUGEPAGE)) {
			fail("runtime %s: mTHP setup failed", label);
			goto out;
		}
	} else if (madvise(p, bytes, MADV_NOHUGEPAGE)) {
		fail("runtime %s: NOHUGEPAGE failed", label);
		goto out;
	}
	runtime_fill(p);
	if (batch) {
		int64_t delta = mthp_stat("anon_fault_alloc") - alloc0;

		say("runtime %s: %luK mTHP allocation witness=%" PRId64,
		    label, p_mthp_kb, delta);
		if (alloc0 < 0 || delta <= 0)
			fail("runtime %s: no mTHP allocation witness", label);
	}
	if (!rt_pageout(pm, p, COMPAT_PAGES, label))
		goto out;
	if (shared) {
		if (pipe(gate)) {
			fail("runtime %s: pipe: %s", label, strerror(errno));
			goto out;
		}
		child = fork();
		if (!child) {
			size_t discard_page = first;
			size_t keep_page = first ? 0 : first + count;
			char c;

			close(gate[1]);
			if (read(gate[0], &c, 1) != 1)
				_exit(2);
			/* Parent must not invalidate the fork's backing payload. */
			if (compat_bad(p) || count >= COMPAT_PAGES)
				_exit(3);
			*(volatile char *)(p + discard_page * PG) =
				compat_byte(discard_page, 0) ^ 0x5a;
			*(volatile char *)(p + keep_page * PG) =
				compat_byte(keep_page, 0) ^ 0xa5;
			if ((unsigned char)p[discard_page * PG] !=
				    (compat_byte(discard_page, 0) ^ 0x5a) ||
			    (unsigned char)p[keep_page * PG] !=
				    (compat_byte(keep_page, 0) ^ 0xa5))
				_exit(4);
			_exit(0);
		}
		if (child < 0) {
			fail("runtime %s: fork: %s", label, strerror(errno));
			goto out;
		}
	}
	if (madvise(p + first * PG, count * PG, MADV_FREE)) {
		fail("runtime %s: MADV_FREE: %s", label, strerror(errno));
		goto out;
	}
	for (i = 0; i < COMPAT_PAGES; i++) {
		uint64_t e = pagemap_entry(pm, p + i * PG);

		if (i >= first && i < first + count) {
			if (e & ((1ULL << 63) | (1ULL << 62)))
				fail("runtime %s: discarded PTE still mapped at %zu", label, i);
			else
				discarded++;
		} else if (!rt_token(e)) {
			fail("runtime %s: untouched token lost at %zu", label, i);
		}
	}
	if (child > 0) {
		if (write(gate[1], "x", 1) != 1 ||
		    waitpid(child, &status, 0) != child ||
		    !WIFEXITED(status) || WEXITSTATUS(status))
			fail("runtime %s: shared child partial COW corrupted", label);
		else
			child_cow = true;
		child = -1;
	}
	for (i = 0; i < COMPAT_PAGES; i++) {
		for (j = 0; j < PG; j++) {
			unsigned char want = (i >= first && i < first + count) ?
				0 : compat_byte(i, j);

			if ((unsigned char)p[i * PG + j] != want) {
				fail("runtime %s: zero-refault/untouched integrity page %zu", label, i);
				break;
			}
		}
	}
	say("runtime %s: discarded=%zu/%zu untouched=%zu shared=%u "
	    "child_cow=%u integrity checked", label, discarded, count,
	    COMPAT_PAGES - count, (unsigned int)shared, (unsigned int)child_cow);
out:
	if (child > 0) {
		kill(child, SIGKILL);
		waitpid(child, &status, 0);
	}
	if (gate[0] >= 0) close(gate[0]);
	if (gate[1] >= 0) close(gate[1]);
	if (base != MAP_FAILED) munmap(base, maplen);
	if (pm >= 0) close(pm);
	msleep(100);
	if (EV("folio_owner_pages") != owner || EV("pending_pages") != pending)
		fail("runtime %s: owner/pending residue", label);
}

struct rt_wp_writer {
	char *p;
	char *control;
	int done;
};

static void *rt_wp_write(void *arg)
{
	struct rt_wp_writer *w = arg;

	/* Page 1 is the explicit unprotected-token control. */
	*(volatile char *)w->control = compat_byte(1, 0) ^ 0xa5;
	*(volatile char *)w->p = compat_byte(0, 0) ^ 0x5a;
	__atomic_store_n(&w->done, 1, __ATOMIC_RELEASE);
	return NULL;
}

static void rt_wp_case(bool async)
{
	const size_t bytes = COMPAT_PAGES * PG;
	const char *label = async ? "UFFD-WP-async" : "UFFD-WP-sync";
	char *p = MAP_FAILED;
	int pm = -1, fd = -1, r, events = 0;
	bool started = false, registered = false, cleanup_ok;
	pthread_t writer;
	struct rt_wp_writer w = {0};
	struct uffdio_api api = { .api = UFFD_API,
		.features = UFFD_FEATURE_PAGEFAULT_FLAG_WP |
			(async ? UFFD_FEATURE_WP_ASYNC : 0) };
	struct uffdio_register reg = { .mode = UFFDIO_REGISTER_MODE_WP };
	struct uffdio_writeprotect wp = { .mode = UFFDIO_WRITEPROTECT_MODE_WP };
	struct uffdio_writeprotect control_clear;
	struct pollfd pollfd;
	struct uffd_msg msg;
	size_t i, protected = 0, preserved = 0, untouched = 0;
	int64_t owner = EV("folio_owner_pages"), pending = EV("pending_pages");

	pm = open("/proc/self/pagemap", O_RDONLY);
	fd = syscall(__NR_userfaultfd, O_CLOEXEC | O_NONBLOCK | UFFD_USER_MODE_ONLY);
	p = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (pm < 0 || p == MAP_FAILED || fd < 0) {
		fail("runtime %s: setup: %s", label, strerror(errno));
		goto out;
	}
	if (ioctl(fd, UFFDIO_API, &api)) {
		if (errno == EINVAL) coverage_skip(label, "requested WP feature unavailable");
		else fail("runtime %s: API: %s", label, strerror(errno));
		goto out;
	}
	if ((api.features & (UFFD_FEATURE_PAGEFAULT_FLAG_WP |
		(async ? UFFD_FEATURE_WP_ASYNC : 0))) !=
		(UFFD_FEATURE_PAGEFAULT_FLAG_WP |
		(async ? UFFD_FEATURE_WP_ASYNC : 0))) {
		coverage_skip(label, "feature negotiation did not provide WP");
		goto out;
	}
	if (madvise(p, bytes, MADV_NOHUGEPAGE)) {
		fail("runtime %s: NOHUGEPAGE", label);
		goto out;
	}
	runtime_fill(p);
	if (!rt_pageout(pm, p, COMPAT_PAGES, label))
		goto out;
	reg.range = (struct uffdio_range){ .start = (uintptr_t)p, .len = bytes };
	wp.range = reg.range;
	if (ioctl(fd, UFFDIO_REGISTER, &reg)) {
		fail("runtime %s: register: %s", label, strerror(errno));
		goto out;
	}
	registered = true;
	if (ioctl(fd, UFFDIO_WRITEPROTECT, &wp)) {
		fail("runtime %s: protect token PTEs: %s", label, strerror(errno));
		goto out;
	}
	control_clear.range = (struct uffdio_range){
		.start = (uintptr_t)(p + PG), .len = PG
	};
	control_clear.mode = 0;
	if (ioctl(fd, UFFDIO_WRITEPROTECT, &control_clear)) {
		fail("runtime %s: unprotect control token: %s", label,
		     strerror(errno));
		goto out;
	}
	for (i = 0; i < COMPAT_PAGES; i++) {
		bool wp_set = pagemap_entry(pm, p + i * PG) & (1ULL << 57);

		if (i == 1) {
			if (wp_set)
				fail("runtime %s: control token remained write-protected", label);
		} else if (!wp_set) {
			fail("runtime %s: token WP bit absent page %zu", label, i);
		} else {
			protected++;
		}
	}
	if (compat_bad(p))
		fail("runtime %s: read integrity", label);
	for (i = 0; i < COMPAT_PAGES; i++) {
		bool wp_set = pagemap_entry(pm, p + i * PG) & (1ULL << 57);

		if (i == 1) {
			if (wp_set)
				fail("runtime %s: read fault protected control token", label);
		} else if (!wp_set) {
			fail("runtime %s: read fault lost WP page %zu", label, i);
		} else {
			preserved++;
		}
	}
	/* Exercise write faults on compressed WP PTEs, not just resident pages. */
	if (!rt_pageout(pm, p, COMPAT_PAGES, label))
		goto out;
	for (i = 0; i < COMPAT_PAGES; i++) {
		bool wp_set = pagemap_entry(pm, p + i * PG) & (1ULL << 57);

		if (i == 1) {
			if (wp_set)
				fail("runtime %s: re-pageout protected control token", label);
		} else if (!wp_set) {
			fail("runtime %s: re-pageout lost WP page %zu", label, i);
		}
	}
	w.p = p;
	w.control = p + PG;
	r = pthread_create(&writer, NULL, rt_wp_write, &w);
	if (r) {
		fail("runtime %s: writer: %s", label, strerror(r));
		goto out;
	}
	started = true;
	pollfd = (struct pollfd){ .fd = fd, .events = POLLIN };
	if (!async) {
		ssize_t n;

		r = poll(&pollfd, 1, 3000);
		if (r != 1 || pollfd.revents != POLLIN) {
			fail("runtime %s: missing or non-exact real WP event", label);
			goto out;
		}
		n = read(fd, &msg, sizeof(msg));
		if (n != (ssize_t)sizeof(msg)) {
			fail("runtime %s: short WP event read: %s", label,
			     n < 0 ? strerror(errno) : "short read");
			goto out;
		}
		events++;
		if (!rt_wp_validate_event(&msg, (uintptr_t)p, label))
			goto out;
		if (__atomic_load_n(&w.done, __ATOMIC_ACQUIRE)) {
			fail("runtime %s: writer escaped WP before resolution", label);
			goto out;
		}
		wp.mode = 0;
		wp.range.len = PG;
		if (ioctl(fd, UFFDIO_WRITEPROTECT, &wp)) {
			fail("runtime %s: resolve event: %s", label, strerror(errno));
			goto out;
		}
	} else {
		for (i = 0; i < 300 &&
		     !__atomic_load_n(&w.done, __ATOMIC_ACQUIRE); i++)
			msleep(10);
		if (!__atomic_load_n(&w.done, __ATOMIC_ACQUIRE)) {
			fail("runtime %s: async writer did not resolve", label);
			goto out;
		}
	}
	if (!rt_wp_join_writer(writer, label))
		goto out;
	started = false;
	if (!rt_wp_no_event(fd, label))
		goto out;
	if (events != (async ? 0 : 1)) {
		fail("runtime %s: unexpected WP event count=%d", label, events);
		goto out;
	}
	{
		uint64_t e0 = pagemap_entry(pm, p);
		uint64_t e1 = pagemap_entry(pm, p + PG);

		if ((unsigned char)p[0] != (compat_byte(0, 0) ^ 0x5a) ||
		    !(e0 & (1ULL << 63)) || rt_token(e0) || (e0 & (1ULL << 57)))
			fail("runtime %s: protected token did not resolve to present page", label);
		if ((unsigned char)p[PG] != (compat_byte(1, 0) ^ 0xa5) ||
		    !(e1 & (1ULL << 63)) || rt_token(e1) || (e1 & (1ULL << 57)))
			fail("runtime %s: unprotected control token did not resolve", label);
	}
	for (i = 2; i < COMPAT_PAGES; i++) {
		if (!(pagemap_entry(pm, p + i * PG) & (1ULL << 57)))
			fail("runtime %s: untouched WP lost page %zu", label, i);
		else
			untouched++;
	}
	say("runtime %s: protected_tokens=%zu read-preserved=%zu events=%d "
	    "writer_done=%d resolved_present=%d control_present=%d untouched_wp=%zu",
	    label, protected, preserved, events, w.done,
	    !!(pagemap_entry(pm, p) & (1ULL << 63)),
	    !!(pagemap_entry(pm, p + PG) & (1ULL << 63)), untouched);
out:
	cleanup_ok = rt_wp_cleanup(&fd, &registered, &reg.range, &writer,
				   &started, label);
	if (!cleanup_ok && started) {
		fail("runtime %s: refusing unmap while writer is alive", label);
		fflush(NULL);
		_exit(1);
	}
	if (p != MAP_FAILED)
		munmap(p, bytes);
	if (pm >= 0)
		close(pm);
	msleep(100);
	if (EV("folio_owner_pages") != owner || EV("pending_pages") != pending)
		fail("runtime %s: owner/pending residue", label);
}

static void runtime_compat_test(void)
{
	char path[160], buf[256], saved[32];
	char *a, *b;

	header("targeted MADV_FREE and real UFFD-WP runtime");
	snprintf(path, sizeof(path), "/sys/kernel/mm/transparent_hugepage/"
		 "hugepages-%lukB/enabled", p_mthp_kb);
	if (!read_file(path, buf, sizeof(buf)) ||
	    !(a = strchr(buf, '[')) || !(b = strchr(a, ']')) ||
	    b - a - 1 >= (long)sizeof(saved)) {
		fail("runtime: cannot preserve selected mTHP mode");
		return;
	}
	memcpy(saved, a + 1, b - a - 1);
	saved[b - a - 1] = 0;
	rt_free_case("MADV_FREE-page", 0, 1, false, false);
	rt_free_case("MADV_FREE-batch", 0, COMPAT_PAGES, false, true);
	rt_free_case("MADV_FREE-partial", 17, 29, false, true);
	rt_free_case("MADV_FREE-fork-shared", 17, 29, true, true);
	rt_wp_case(false);
	rt_wp_case(true);
	if (mthp_set(saved)) fail("runtime: restore mTHP mode failed");
}

#endif /* !RT_WP_UNIT_TEST */

#undef RT_WP_TIMEDJOIN
