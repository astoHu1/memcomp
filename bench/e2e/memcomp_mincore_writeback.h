/* SPDX-License-Identifier: GPL-2.0 */
/* Local E2E check, not a measurement workload. Included after compat helpers. */
/* Sequential counters witness the later read, not cache state atomically at query. */
static bool mcwb_vma_window(void *page, bool isolated)
{
	unsigned long addr = (unsigned long)page, lo, hi;
	char line[4096], perms[5];
	FILE *maps = fopen("/proc/self/maps", "r");
	bool ok = false;

	if (!maps)
		return false;
	while (fgets(line, sizeof(line), maps)) {
		if (sscanf(line, "%lx-%lx %4s", &lo, &hi, perms) != 3 ||
		    addr < lo || addr >= hi)
			continue;
		if (isolated)
			ok = lo == addr && hi == addr + PG && !strcmp(perms, "r--p");
		else
			ok = addr >= PG && lo <= addr - PG && hi >= addr + 2 * PG &&
			     !strcmp(perms, "rw-p");
		break;
	}
	fclose(maps);
	return ok;
}

static bool mcwb_present_control(int pm, void *page)
{
	unsigned int attempt;

	for (attempt = 0; attempt < 3; attempt++) {
		uint64_t before, after;
		unsigned char vec = 0;

		*(volatile uint8_t *)page = 0;
		before = pagemap_entry(pm, page);
		if (mincore(page, PG, &vec))
			break;
		after = pagemap_entry(pm, page);
		if (!(before & (1ULL << 63)) || before != after)
			continue;
		if (vec & 1)
			return true;
		break;
	}
	fail("mincore writeback: present positive control failed");
	return false;
}

struct mcwb_sample {
	int64_t io, cached;
	uint64_t pswpin;
};

static void mcwb_sample(struct mcwb_sample *s)
{
	struct snap snap;

	s->io = EV("swapped_read_io");
	s->cached = EV("swapped_read_cached");
	take_snap(&snap);
	s->pswpin = snap.pswpin;
}

static void compat_mincore_writeback(struct region *a, struct region *b)
{
	struct region *regions[] = { a, b };
	uint8_t *tmp = NULL;
	void *restore = NULL;
	unsigned int n;
	int pm = -1;
	bool found = false;

	if (p_no_swap) {
		coverage_expected_skip("mincore writeback", "NO_SWAP=1; pool check retained");
		return;
	}
	header("mincore: single-page VMA uncached transport witness");
	pm = open("/proc/self/pagemap", O_RDONLY);
	tmp = aligned_alloc(PG, PG);
	if (pm < 0 || !tmp) {
		fail("mincore writeback: pagemap or scratch allocation failed");
		goto out;
	}
	if (!mcwb_present_control(pm, tmp))
		goto out;
	for (n = 0; n < sizeof(regions) / sizeof(regions[0]) && !found; n++) {
		struct region *r = regions[n];
		size_t i;

		for (i = 1; i + 1 < r->npages && !found; i++) {
			void *page = r->base + i * PG;
			uint64_t before, after;
			unsigned char vec = 0xff;
			struct mcwb_sample q0, q1, s0, s1;

			/* Select tokens, not a result supplied by the queried function. */
			if (!compat_token(pagemap_entry(pm, page)))
				continue;
			if (!mcwb_vma_window(page, false) ||
			    mprotect(page, PG, PROT_READ)) {
				fail("mincore writeback: cannot isolate an interior RW page");
				goto out;
			}
			restore = page;
			if (!mcwb_vma_window(page, true)) {
				fail("mincore writeback: maps did not confirm single-page R VMA");
				goto out;
			}
			/* mprotect precedes the identity snapshot, never the converse. */
			before = pagemap_entry(pm, page);
			if (!compat_token(before))
				goto restore_page;
			mcwb_sample(&q0);
			if (mincore(page, PG, &vec)) {
				fail("mincore writeback: query syscall failed");
				goto out;
			}
			after = pagemap_entry(pm, page);
			mcwb_sample(&q1);
			if (after != before || q1.io != q0.io ||
			    q1.cached != q0.cached || q1.pswpin != q0.pswpin) {
				fail("mincore writeback: query changed identity or read counters");
				goto out;
			}
			/* Read even for vec1: evidence, not vec, qualifies this sample. */
			mcwb_sample(&s0);
			if (check_page(r, i, tmp, "mincore-writeback")) {
				fail("mincore writeback: content mismatch");
				goto out;
			}
			mcwb_sample(&s1);
			if (s1.io - s0.io == 1 && s1.cached == s0.cached &&
			    s1.pswpin > s0.pswpin) {
				if (vec & 1) {
					fail("mincore writeback: independent uncached witness had vec1");
					goto out;
				}
				say("mincore writeback: %s[%zu] vec0 token=0x%016" PRIx64
				    " unchanged; subsequent single-page read: content OK,"
				    " io=1 cache=0 pswpin=%" PRIu64,
				    r->name, i, before, s1.pswpin - s0.pswpin);
				found = true;
			}
restore_page:
			if (mprotect(restore, PG, PROT_READ | PROT_WRITE)) {
				fail("mincore writeback: RW permission restore failed");
				goto out;
			}
			restore = NULL;
		}
	}
	if (!found)
		fail("mincore writeback: no independent single-page uncached witness");
out:
	if (restore && mprotect(restore, PG, PROT_READ | PROT_WRITE))
		fail("mincore writeback: cleanup RW permission restore failed");
	free(tmp);
	if (pm >= 0)
		close(pm);
}
