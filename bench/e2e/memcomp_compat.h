/* SPDX-License-Identifier: GPL-2.0 */
/* Included by the PID-1 harness after its pagemap/stat helpers. */
#include <sys/uio.h>

#define COMPAT_PAGES 64UL
#define COMPAT_MINCORE_ROUNDS 8U

static unsigned char compat_byte(size_t page, size_t off)
{
	return (unsigned char)(page * 7 + (off % 128) * 3 + 19);
}

static unsigned long compat_bad(char *p)
{
	unsigned long bad = 0;
	size_t i, j;

	for (i = 0; i < COMPAT_PAGES; i++)
		for (j = 0; j < PG; j++)
			if ((unsigned char)p[i * PG + j] != compat_byte(i, j)) {
				bad++;
				break;
			}
	return bad;
}

/* Pagemap swap type 30 is SWP_MEMCOMP in the guest kernel. */
static bool compat_token(uint64_t e)
{
	return !(e & (1ULL << 63)) && (e & (1ULL << 62)) &&
	       (e & 31) == 30;
}

static bool compat_pageout(int pm, char *p, const char *label)
{
	uint64_t entries[COMPAT_PAGES];
	size_t i, tokens = 0;
	int attempt;

	for (attempt = 0; attempt < 50; attempt++) {
		if (madvise(p, COMPAT_PAGES * PG, MADV_PAGEOUT)) {
			fail("compat %s: MADV_PAGEOUT: %s", label, strerror(errno));
			return false;
		}
		tokens = 0;
		for (i = 0; i < COMPAT_PAGES; i++) {
			entries[i] = pagemap_entry(pm, p + i * PG);
			tokens += compat_token(entries[i]);
		}
		if (tokens == COMPAT_PAGES)
			break;
		msleep(20);
	}
	say("compat %s: %zu/%lu actual memcomp token PTEs", label,
	    tokens, COMPAT_PAGES);
	if (tokens == COMPAT_PAGES)
		return true;
	for (i = 0; i < COMPAT_PAGES; i++)
		if (!compat_token(entries[i]))
			fail("compat %s: missing token page=%zu pagemap=0x%016" PRIx64,
			     label, i, entries[i]);
	fail("compat %s: incomplete memcomp token coverage", label);
	return false;
}

static bool compat_mincore(int pm, char *p)
{
	uint64_t before[COMPAT_PAGES];
	unsigned char vec[COMPAT_PAGES];
	unsigned int round;
	size_t i;
	bool ok = true;

	/* No content reads, new pageout or concurrent workers in this window. */
	for (i = 0; i < COMPAT_PAGES; i++) {
		before[i] = pagemap_entry(pm, p + i * PG);
		if (!compat_token(before[i])) {
			fail("compat mincore: before page=%zu vec=n/a pagemap=0x%016"
			     PRIx64 " is not a memcomp token", i, before[i]);
			ok = false;
		}
	}
	if (!ok)
		return false;

	for (round = 1; round <= COMPAT_MINCORE_ROUNDS; round++) {
		int ret;

		/* Do not let a previous query's resident bits mask missing output. */
		memset(vec, 0, sizeof(vec));
		ret = mincore(p, COMPAT_PAGES * PG, vec);
		if (ret) {
			fail("compat mincore: round=%u syscall: %s", round,
			     strerror(errno));
			ok = false;
		}
		for (i = 0; i < COMPAT_PAGES; i++) {
			uint64_t after = pagemap_entry(pm, p + i * PG);

			/* Exported pagemap equality includes swap type/offset identity. */
			if (ret || !(vec[i] & 1) || !compat_token(after) ||
			    after != before[i]) {
				fail("compat mincore: round=%u page=%zu vec=0x%02x"
				     " before=0x%016" PRIx64 " after=0x%016" PRIx64,
				     round, i, (unsigned int)vec[i], before[i], after);
				ok = false;
			}
		}
		if (!ok)
			return false;
	}
	say("compat mincore: %u queries x %lu pages: vec bit0=1,"
	    " unchanged exported pagemap/token identities",
	    COMPAT_MINCORE_ROUNDS, COMPAT_PAGES);
	say("compat mincore: exported identity samples page0=0x%016" PRIx64
	    " page%lu=0x%016" PRIx64, before[0], COMPAT_PAGES - 1,
	    before[COMPAT_PAGES - 1]);
	return true;
}

static void compat_test(void)
{
	const size_t bytes = COMPAT_PAGES * PG;
	char *p = MAP_FAILED, *dest = MAP_FAILED, *copy = NULL;
	int pm = -1, status;
	pid_t child;
	size_t i, j;
	int64_t owner0 = EV("folio_owner_pages");
	int64_t pending0 = EV("pending_pages");
	struct iovec local, remote;
	ssize_t n;

	header("swap-entry compatibility");
	pm = open("/proc/self/pagemap", O_RDONLY);
	p = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	copy = malloc(bytes);
	if (pm < 0 || p == MAP_FAILED || !copy) {
		fail("compat: setup: %s", strerror(errno));
		goto out;
	}
	madvise(p, bytes, MADV_NOHUGEPAGE);
	for (i = 0; i < COMPAT_PAGES; i++)
		for (j = 0; j < PG; j++)
			p[i * PG + j] = compat_byte(i, j);

	/* clear_refs must alter non-present memcomp PTEs too (CRIU). */
	if (!compat_pageout(pm, p, "soft-dirty"))
		goto out;
	{
		int fd = open("/proc/self/clear_refs", O_WRONLY);

		if (fd < 0 || write(fd, "4", 1) != 1) {
			fail("compat: clear_refs failed");
			if (fd >= 0)
				close(fd);
			goto out;
		}
		close(fd);
	}
	for (i = 0; i < COMPAT_PAGES; i++)
		if (pagemap_entry(pm, p + i * PG) & (1ULL << 55))
			fail("compat: soft-dirty not cleared at page %zu", i);
	if (compat_bad(p))
		fail("compat: read fault corrupted soft-dirty region");
	for (i = 0; i < COMPAT_PAGES; i++) {
		if (pagemap_entry(pm, p + i * PG) & (1ULL << 55))
			fail("compat: read fault dirtied page %zu", i);
		/* A volatile store of the same byte must still fault writable. */
		*(volatile char *)(p + i * PG) = compat_byte(i, 0);
		if (!(pagemap_entry(pm, p + i * PG) & (1ULL << 55)))
			fail("compat: write not soft-dirty at page %zu", i);
	}

	say("compat soft-dirty: clear_refs/read/write checked %lu pages", COMPAT_PAGES);
	if (!compat_pageout(pm, p, "mincore") || !compat_mincore(pm, p))
		goto out;
	if (madvise(p, bytes, MADV_WILLNEED) ||
	    madvise(p, bytes, MADV_COLD))
		fail("compat: WILLNEED/COLD: %s", strerror(errno));
	if (compat_bad(p))
		fail("compat: WILLNEED/COLD changed contents");

	if (!compat_pageout(pm, p, "mprotect"))
		goto out;
	if (mprotect(p, bytes, PROT_READ) || compat_bad(p) ||
	    mprotect(p, bytes, PROT_READ | PROT_WRITE))
		fail("compat: mprotect/read integrity");

	if (!compat_pageout(pm, p, "fork-COW"))
		goto out;
	child = fork();
	if (child == 0) {
		if (compat_bad(p))
			_exit(2);
		for (i = 0; i < COMPAT_PAGES; i++)
			p[i * PG] ^= 0x5a;
		for (i = 0; i < COMPAT_PAGES; i++)
			if ((unsigned char)p[i * PG] != (compat_byte(i, 0) ^ 0x5a))
				_exit(3);
		_exit(0);
	}
	if (child < 0)
		fail("compat: fork: %s", strerror(errno));
	else if (waitpid(child, &status, 0) != child ||
		 !WIFEXITED(status) || WEXITSTATUS(status))
		fail("compat: child token refault/COW failed");
	if (compat_bad(p))
		fail("compat: child COW changed parent contents");

	if (!compat_pageout(pm, p, "process_vm_readv"))
		goto out;
	local = (struct iovec){ .iov_base = copy, .iov_len = bytes };
	remote = (struct iovec){ .iov_base = p, .iov_len = bytes };
	n = process_vm_readv(getpid(), &local, 1, &remote, 1, 0);
	if (n != (ssize_t)bytes || compat_bad(copy))
		fail("compat: process_vm_readv token GUP/integrity: %zd", n);

	if (!compat_pageout(pm, p, "mremap"))
		goto out;
	dest = mmap(NULL, bytes, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (dest == MAP_FAILED) {
		fail("compat: destination mmap failed");
		goto out;
	}
	{
		char *moved = mremap(p, bytes, bytes, MREMAP_MAYMOVE | MREMAP_FIXED, dest);

		if (moved == MAP_FAILED) {
			fail("compat: mremap: %s", strerror(errno));
			goto out;
		}
		p = moved;
		dest = MAP_FAILED;
	}
	if (compat_bad(p))
		fail("compat: mremap token integrity");
	/* FREE permits discarding contents, so only exercise lifetime teardown. */
	if (!compat_pageout(pm, p, "MADV_FREE"))
		goto out;
	if (madvise(p, bytes, MADV_FREE))
		fail("compat: MADV_FREE: %s", strerror(errno));
out:
	if (p != MAP_FAILED)
		munmap(p, bytes);
	if (dest != MAP_FAILED)
		munmap(dest, bytes);
	if (pm >= 0)
		close(pm);
	free(copy);
	msleep(1000);
	if (EV("folio_owner_pages") - owner0 > (int64_t)p_owner_tol ||
	    EV("pending_pages") - pending0 > (int64_t)p_owner_tol)
		fail("compat: owner/pending residue after munmap");
	rstat("after compat");
}
