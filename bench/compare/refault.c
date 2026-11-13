// SPDX-License-Identifier: GPL-2.0-only
/* Anonymous pageout/first-touch workload; timing/order based on refault.c. */
#include "common.h"

/* Identity-aware writer; the historical common.h protocol stays unchanged. */
static inline int corpus_csv(const char *workload, const struct bench_options *o,
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
		bench_corpus_identity_csv(o->corpus, o->corpus_first + sample->index);
		putchar('\n');
	}
	return fflush(stdout) || ferror(stdout) ? -1 : 0;
}


/* W4 uses the same guest-provided backend identity as PID 1. No probing
 * by "which sysfs files exist": a compiled-in but inactive backend is not
 * evidence that it performed any writeback.
 */
static int w4_counter(struct bench_writeback *wb, uint64_t *value)
{
	if (!strcmp(wb->source, "memcomp_writeback_payloads"))
		return bench_counter("/sys/kernel/mm/memcomp/packed_swap_events",
				     "writeback_payloads", value);
	if (!strcmp(wb->source, "zswpwb"))
		return bench_counter("/proc/vmstat", "zswpwb", value);
	if (!strcmp(wb->source, "zram_bd_writes")) {
		FILE *file = fopen("/sys/block/zram0/bd_stat", "re");
		unsigned long long count, reads, writes;
		int got, bad;

		if (!file)
			return -1;
		got = fscanf(file, "%llu %llu %llu", &count, &reads, &writes);
		bad = ferror(file);
		if (fclose(file) || bad || got != 3) {
			errno = EIO;
			return -1;
		}
		*value = (uint64_t)writes;
		return 0;
	}
	errno = EINVAL;
	return -1;
}

static int w4_setup(const struct bench_options *o, struct bench_writeback *wb)
{
	FILE *file = fopen("/backend", "re");
	char backend[64], extra;
	unsigned long long memory;
	int got, bad;

	if (!file)
		return -1;
	got = fscanf(file, "%63s %c", backend, &extra);
	bad = ferror(file);
	if (fclose(file) || bad || got != 1) {
		errno = EINVAL;
		return -1;
	}
	if (!strcmp(backend, "memcomp-swap"))
		wb->source = "memcomp_writeback_payloads";
	else if (!strcmp(backend, "zswap"))
		wb->source = "zswpwb";
	else if (!strcmp(backend, "zram-writeback")) {
		wb->source = "zram_bd_writes";
		wb->explicit_idle = 1;
	} else {
		errno = EINVAL;
		return -1;
	}
	file = fopen("/guest-memory-bytes", "re");
	if (!file)
		return -1;
	got = fscanf(file, "%llu %c", &memory, &extra);
	bad = ferror(file);
	if (fclose(file) || bad || got != 1 || memory < (UINT64_C(256) << 20) ||
	    memory > (UINT64_C(4096) << 20) || memory > SIZE_MAX ||
	    memory % (UINT64_C(1) << 20) || memory % o->page_size) {
		errno = EINVAL;
		return -1;
	}
	/* Use configured guest RAM, not backend-dependent reserved pages in
	 * MemTotal: equal-size guests must hold exactly the same pressure.
	 */
	wb->pressure_bytes = ((size_t)memory / o->page_size / 8 * 7) * o->page_size;
	if (!wb->pressure_bytes) {
		errno = EINVAL;
		return -1;
	}
	return 0;
}

static int w4_pressure_alive(struct bench_pressure_child *pressure)
{
	int status;
	pid_t got;

	if (pressure->pid <= 0) {
		errno = ECHILD;
		return -1;
	}
	do {
		got = waitpid(pressure->pid, &status, WNOHANG);
	} while (got < 0 && errno == EINTR && !bench_interrupted);
	if (!got)
		return 0;
	if (got == pressure->pid || (got < 0 && errno == ECHILD))
		pressure->pid = -1;
	if (got > 0)
		errno = ECHILD;
	return -1;
}

/* One compact record per poll, without taking a full PID-1 snapshot. */
static int w4_poll_counters(const struct bench_writeback *wb, uint64_t *scan,
                            uint64_t *direct, uint64_t *pool, uint64_t *sectors)
{
	FILE *file;
	char line[512], name[32];
	unsigned long long a, b, c;
	bool found = false;
	int got, bad;

	if (bench_counter("/proc/vmstat", "pgscan_kswapd", scan) ||
	    bench_counter("/proc/vmstat", "pgscan_direct", direct))
		return -1;
	if (!strcmp(wb->source, "zswpwb")) {
		file = fopen("/sys/kernel/debug/zswap/pool_total_size", "re");
		if (!file)
			return -1;
		got = fscanf(file, "%llu", &c);
	} else {
		file = fopen(!strcmp(wb->source, "memcomp_writeback_payloads") ?
			     "/sys/kernel/mm/memcomp/stat" : "/sys/block/zram0/mm_stat", "re");
		if (!file)
			return -1;
		got = fscanf(file, "%llu %llu %llu", &a, &b, &c);
		got = got == 3 ? 1 : 0;
	}
	bad = ferror(file);
	if (fclose(file) || bad || got != 1) {
		errno = EIO;
		return -1;
	}
	*pool = (uint64_t)c;
	file = fopen("/proc/diskstats", "re");
	if (!file)
		return -1;
	while (fgets(line, sizeof(line), file)) {
		got = sscanf(line, "%*s %*s %31s %*s %*s %*s %*s %*s %*s %llu", name, &a);
		if (got == 2 && !strcmp(name, "vda")) {
			if (found) {
				fclose(file);
				errno = EPROTO;
				return -1;
			}
			*sectors = (uint64_t)a;
			found = true;
		}
	}
	bad = ferror(file);
	if (fclose(file) || bad || !found) {
		errno = EIO;
		return -1;
	}
	return 0;
}

static int w4_wait_writeback(struct bench_writeback *wb, struct bench_pressure_child *pressure)
{
	uint64_t now, previous = 0, previous_scan = 0, previous_direct = 0;
	bool first = true;
	bool memcomp = !strcmp(wb->source, "memcomp_writeback_payloads");

	if (bench_console_drain())
		return -1;
	wb->stable_polls = 0;
	for (;;) {
		uint64_t scan, direct, pool, sectors;

		if (!first) {
			uint64_t remaining = UINT64_C(300000000000) - wb->wait_ns;
			uint64_t sleep_us = (remaining + 999) / 1000;

			if (bench_sleep_us(sleep_us < 1000000 ? sleep_us : 1000000))
				return -1;
		}
		if (w4_pressure_alive(pressure) || w4_counter(wb, &wb->after) ||
		    w4_poll_counters(wb, &scan, &direct, &pool, &sectors))
			return -1;
		if (wb->after < wb->before || (!first &&
		    (wb->after < previous || scan < previous_scan || direct < previous_direct))) {
			errno = EPROTO;
			return -1;
		}
		wb->proven = wb->after > wb->before;
		if (!first)
			wb->stable_polls = wb->proven && wb->after == previous &&
				scan == previous_scan && direct == previous_direct ?
				wb->stable_polls + 1 : 0;
		if (memcomp) {
			uint64_t gate;

			wb->gate_open = !bench_counter("/sys/kernel/mm/memcomp/packed_swap_events",
						     "writeback_gate_open", &gate) && gate <= 1 ? (int)gate : -1;
		}
		if (bench_now(&now))
			return -1;
		wb->wait_ns = now - wb->pressure_ready_ns;
		fprintf(stderr, "WRITEBACK_POLL source=%s elapsed_ns=%" PRIu64
			" gate_open=%d pool_physical_bytes=%" PRIu64 " writeback=%" PRIu64
			" pgscan_kswapd=%" PRIu64 " pgscan_direct=%" PRIu64
			" swap_write_sectors=%" PRIu64 " stable_polls=%u\n",
			wb->source, wb->wait_ns, wb->gate_open, pool, wb->after,
			scan, direct, sectors, wb->stable_polls);
		if (wb->wait_ns >= UINT64_C(300000000000)) {
			wb->timed_out = 1;
			break;
		}
		if (wb->proven && wb->stable_polls >= 3)
			break;
		/* The first observation is a baseline, not a complete interval. */
		previous = wb->after;
		previous_scan = scan;
		previous_direct = direct;
		first = false;
	}
	fprintf(stderr, "WRITEBACK source=%s before=%" PRIu64 " after=%" PRIu64
		" pressure_bytes=%zu explicit_idle=%d proven=%d wait_ns=%" PRIu64
		" timed_out=%d stable_polls=%u condition=counter_pgscan_stable_3"
		" gate_open=%d latency_excluded=%d\n",
		wb->source, wb->before, wb->after, wb->pressure_bytes, wb->explicit_idle,
		wb->proven, wb->wait_ns, wb->timed_out, wb->stable_polls,
		wb->gate_open, wb->timed_out);
	if (!wb->proven) {
		errno = ENODATA;
		return -1;
	}
	return 0;
}

static int refault_once(int argc, char **argv)
{
	struct bench_options o;
	struct bench_observer observer = { .pagemap = -1 };
	struct bench_sample *samples = NULL;
	unsigned char *map = MAP_FAILED, *expected = NULL;
	uint64_t start, end, wall_ns = 0;
	struct bench_drain drain = { 0 };
	struct bench_writeback wb = { .gate_open = -1 };
	struct bench_pressure_child pressure = { .pid = -1, .stop_fd = -1 };
	size_t i, done = 0, mismatches = 0;
	const char *operation = "setup";
	int parsed, rc = 1;

	parsed = bench_parse(argc, argv, false, &o);
	if (parsed)
		return parsed > 0 ? 0 : 2;
	if (o.rewrite) {
		fputs("ERROR --rewrite is W1-only\n", stderr);
		return 2;
	}
	if (o.corpus && o.corpus->uniform_v411 && strcmp(o.corpus_source, "combined")) {
		fputs("ERROR uniform-v411 W4 requires combined source\n", stderr);
		return 2;
	}
	bench_config("w4", &o);
	if (w4_setup(&o, &wb))
		goto error;
	if (bench_signals() || bench_pin_cpu(&o))
		goto error;
	operation = "allocate workload";
	samples = calloc(o.pages, sizeof(*samples));
	expected = malloc(o.page_size);
	map = mmap(NULL, o.bytes, PROT_READ | PROT_WRITE,
		   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (!samples || !expected || map == MAP_FAILED)
		goto error;
	operation = "MADV_NOHUGEPAGE";
	if (madvise(map, o.bytes, MADV_NOHUGEPAGE))
		goto error;
	bench_order(&o, samples);
	operation = "fill";
	if (bench_phase("fill_begin"))
		goto error;
	for (i = 0; i < o.pages; i++) {
		if (bench_interrupted) {
			errno = EINTR;
			goto error;
		}
		if (bench_fill_page(map + i * o.page_size, &o, i))
				goto error;
	}
	/* Fault the one-page verification scratch before the measured phases. */
	memset(expected, 0, o.page_size);
	bench_observer_open(&observer);
	if (observer.pagemap < 0 || observer.errors || bench_phase("prepared"))
		goto error;
	operation = "MADV_PAGEOUT";
	if (w4_counter(&wb, &wb.before) || bench_console_drain() || bench_now(&start))
		goto error;
	if (madvise(map, o.bytes, MADV_PAGEOUT))
		goto error;
	if (bench_now(&end))
		goto error;
	fprintf(stderr, "PAGEOUT bytes=%zu call_ns=%" PRIu64 "\n", o.bytes, end - start);
	operation = "settle/snapshot";
	/* Start the common write window after target PAGEOUT, but before PID 1
	 * performs zram's explicit idle writeback at pageout_returned. The hook
	 * still precedes pressure allocation and is NOT automatic reclaim.
	 */
	if (bench_phase("pressure_begin") || bench_phase("pageout_returned") ||
	    bench_start_pressure(&o, wb.pressure_bytes, &pressure, map, -1, -1) ||
	    bench_phase("pressure_ready"))
		goto error;
	wb.pressure_ready = 1;
	if (bench_now(&wb.pressure_ready_ns))
		goto error;
	operation = "W4 real-writeback gate under held anonymous pressure";
	if (w4_wait_writeback(&wb, &pressure))
		goto error;
	/* A cap-expired guest still contributes its pressure-to-read disk bytes.
	 * Do not turn an ongoing global writeback wait into a drain failure.
	 * Its full readback is verified/retained but excluded from comparisons.
	 */
	if (!wb.timed_out &&
	    (bench_sleep_us((uint64_t)o.settle_ms * 1000) ||
	     bench_drain_pageout(map, &o, &observer, &drain, false) ||
	     w4_wait_writeback(&wb, &pressure)))
		goto error;
	if (bench_phase(wb.timed_out ? "writeback_unstable" : "writeback_settled") ||
	    bench_snapshot(map, &o, &observer, samples) || bench_phase("read_begin") ||
	    w4_pressure_alive(&pressure))
		goto error;
	operation = "first-touch/verification";
	if (bench_measure(map, &o, &observer, samples, expected, &done, &wall_ns, &mismatches))
		goto error;
	if (w4_pressure_alive(&pressure) || bench_phase("read_end"))
		goto error;
	rc = mismatches || observer.errors ? 1 : 0;
	goto out;
error:
	fprintf(stderr, "ERROR operation=%s errno=%d message=%s\n", operation, errno, strerror(errno));
out:
	if (bench_finish_pressure(&pressure, rc != 0 || bench_interrupted))
		rc = 1;
	if (map != MAP_FAILED && munmap(map, o.bytes)) {
		perror("munmap");
		rc = 1;
	}
	if (observer.pagemap >= 0)
		close(observer.pagemap);
	if (bench_interrupted)
		rc = 128 + bench_interrupted;
	if (corpus_csv("w4", &o, samples, done, &drain, &wb)) {
		fputs("ERROR CSV output failed\n", stderr);
		rc = 1;
	}
	if (bench_summary("w4", &o, samples, done, mismatches, wall_ns, observer.errors, rc)) {
		fputs("ERROR summary output failed\n", stderr);
		rc = 1;
	}
	free(expected);
	free(samples);
	return rc;
}


/* Saved before prime readback; rewrite reuses and overwrites its samples. */
struct w1_prime {
	size_t requested, observed, real_swap, memcomp, present, other;
	int swap_type, memcomp_type;
	struct bench_drain drain;
};

static void w1_prime_csv(const struct w1_prime *prime)
{
	if (!prime || !prime->requested) {
		fputs(",,,,,,,,,,,,,", stdout);
		return;
	}
	printf(",pagemap_after_prime_drain_before_readback,%d,%d,%zu,%zu,%zu,%zu,%zu,%zu,%u,%zu,%zu,%d",
	       prime->swap_type, prime->memcomp_type, prime->requested, prime->observed,
	       prime->real_swap, prime->memcomp, prime->present, prime->other,
	       prime->drain.rounds, prime->drain.remaining, prime->drain.remapped,
	       prime->drain.timed_out);
}

struct w1_batch {
	struct bench_sample *samples;
	size_t done, nonpresent, first_pass_nonpresent;
	uint64_t pageout_ns, wall_ns;
	struct bench_drain drain;
	long free_before, available_before, free_after, available_after;
	struct w1_prime prime;
};

static int w1_phase(unsigned int round, const char *mode, const char *name)
{
	char phase[64];
	int n = snprintf(phase, sizeof(phase), "r%u_%s_%s", round, mode, name);

	if (n < 0 || (size_t)n >= sizeof(phase)) {
		errno = EOVERFLOW;
		return -1;
	}
	return bench_phase(phase);
}

static int w1_single(unsigned char *map, const struct bench_options *o,
		     struct bench_observer *observer, struct w1_batch *batch,
		     unsigned char *expected, size_t count, size_t *mismatches)
{
	for (size_t i = 0; i < count; i++) {
		struct bench_sample *sample = &batch->samples[i];
		unsigned char *page = map + sample->index * o->page_size;
		uint64_t a, b;

		if (bench_sleep_us(o->gap_us) || bench_console_drain() || bench_now(&a) ||
		    madvise(page, o->page_size, MADV_PAGEOUT) || bench_now(&b))
			return -1;
		/* Keep pageout and first-byte time as separate per-sample values. */
		if (bench_console_drain())
			return -1;
		sample->snapshot = (struct bench_observation){ .present = -1 };
		bench_observe(observer, page, o->page_size, &sample->before);
		sample->snapshot = sample->before;
		sample->cpu = sched_getcpu();
		uint64_t pageout = b - a;
		if (bench_now(&a))
			return -1;
		(void)*(volatile unsigned char *)page;
		if (bench_now(&b))
			return -1;
		sample->ns = b - a;
		sample->pageout_ns = pageout;
		int verification = bench_verify_page(page, o, sample->index, expected);

		if (verification < 0)
			return -1;
		sample->verified = verification == 0;
		*mismatches += !sample->verified;
		batch->nonpresent += sample->before.present == 0;
		batch->done++;
	}
	return 0;
}

/* Pagemap only: no mincore, target touch or drain/retry in this sample. */
static int w1_first_pass(unsigned char *map, const struct bench_options *o,
                         const struct bench_observer *observer, size_t *nonpresent)
{
	uint64_t entries[256];
	off_t start = (off_t)((uintptr_t)map / o->page_size * sizeof(entries[0]));

	*nonpresent = 0;
	for (size_t i = 0; i < o->pages;) {
		size_t count = o->pages - i;
		ssize_t got;

		if (count > sizeof(entries) / sizeof(entries[0]))
			count = sizeof(entries) / sizeof(entries[0]);
		do {
			got = pread(observer->pagemap, entries, count * sizeof(entries[0]),
				    start + (off_t)(i * sizeof(entries[0])));
		} while (got < 0 && errno == EINTR && !bench_interrupted);
		if (got != (ssize_t)(count * sizeof(entries[0]))) {
			errno = EIO;
			return -1;
		}
		for (size_t j = 0; j < count; j++)
			*nonpresent += !(entries[j] & (UINT64_C(1) << 63));
		i += count;
	}
	return 0;
}

static int w1_batch_run(unsigned char *map, struct bench_options *o,
			struct bench_observer *observer, struct w1_batch *batch,
			unsigned char *expected, unsigned int round, const char *mode,
			size_t *mismatches)
{
	uint64_t a, b;
	bool priming = !strcmp(mode, "rewrite_prime");

	if (w1_phase(round, mode, "prepared") || bench_console_drain() || bench_now(&a) ||
	    madvise(map, o->bytes, MADV_PAGEOUT) || bench_now(&b))
		return -1;
	batch->pageout_ns = b - a;
	if (w1_first_pass(map, o, observer, &batch->first_pass_nonpresent) ||
	    bench_drain_pageout(map, o, observer, &batch->drain, true) ||
	    bench_snapshot(map, o, observer, batch->samples))
		return -1;
	if (priming) {
		batch->prime = (struct w1_prime) {
			.requested = o->pages, .swap_type = observer->swap_type,
			.memcomp_type = observer->memcomp_type, .drain = batch->drain
		};
	}
	for (size_t i = 0; i < o->pages; i++) {
		const struct bench_observation *snapshot = &batch->samples[i].snapshot;
		struct w1_prime *prime = &batch->prime;

		batch->nonpresent += snapshot->present == 0;
		if (!priming)
			continue;
		if (snapshot->present < 0 || snapshot->swapped < 0) {
			errno = EIO;
			return -1;
		}
		prime->observed++;
		if (snapshot->present == 1)
			prime->present++;
		else if (snapshot->real_swap == 1)
			prime->real_swap++;
		else if (snapshot->memcomp == 1)
			prime->memcomp++;
		else
			prime->other++;
	}
	if (bench_meminfo(&batch->free_after, &batch->available_after) ||
	    w1_phase(round, mode, "read_begin") ||
	    bench_measure(map, o, observer, batch->samples, expected,
			  &batch->done, &batch->wall_ns, mismatches) ||
	    w1_phase(round, mode, "read_end"))
		return -1;
	return 0;
}

static int w1_csv(const struct bench_options *o, const struct w1_batch *batches,
		  unsigned int total)
{
	static const char *const modes[] = { "seq", "rand", "single", "rewrite" };
	static const char *const kinds[] = { "zero", "same", "text", "record", "alpha", "random", "real-unclassified" };

	fputs("workload,content,seed,page_size,order,sequence,page_index,page_kind,"
	     "snapshot_present,snapshot_swapped,snapshot_resident,present_before,"
	     "swapped_before,resident_before,classification,touch_ns,verify_ok,cpu_before,"
	     "swap_type_before,method,record,round,warmup,mode,pageout_ns,pageout_total_ns,"
	     "purge_ns,nonpresent_pages,requested_pages,drain_timed_out,memfree_before_kib,"
	     "memfree_after_kib,memavailable_before_kib,memavailable_after_kib,"
	     "first_pass_nonpresent_pages,first_pass_nonpresent_fraction,drain_additional_nonpresent_pages", stdout);
	bench_sample_csv_header();
	bench_drain_csv_header();
	fputs(",prime_source,prime_swap_type,prime_memcomp_type,prime_requested_pages,"
	      "prime_observed_pages,prime_real_swap_pages,prime_memcomp_pages,prime_present_pages,"
	      "prime_other_pages,prime_drain_rounds,prime_drain_remaining_swapcache_pages,"
	      "prime_drain_remapped_pages,prime_drain_timed_out", stdout);
	if (o->corpus)
		fputs(",corpus_source,corpus_page_index", stdout);
	putchar('\n');
	for (unsigned int r = 0; r < total; r++) {
		struct bench_options rotated = *o;

		rotated.round_shift = ((size_t)r * 7919) % o->pages;
		for (unsigned int mode = 0; mode < 4; mode++) {
			const struct w1_batch *batch = &batches[r * 4 + mode];

			for (size_t i = 0; i < batch->done; i++) {
				const struct bench_sample *sample = &batch->samples[i];

				printf("w1,%s,%" PRIu64 ",%zu,%s,%zu,%zu,%s,%d,%d,%d,%d,%d,%d,%s,%"
				       PRIu64 ",%d,%d,%d,v4.6,touch,%u,%d,%s,",
				       bench_content_name(o->content), o->seed, o->page_size,
				       mode ? "rand" : "seq", i, sample->index,
				       kinds[bench_page_kind(&rotated, sample->index)],
				       sample->snapshot.present, sample->snapshot.swapped,
				       sample->snapshot.resident, sample->before.present,
				       sample->before.swapped, sample->before.resident,
				       bench_class_name(bench_classify(&sample->before)), sample->ns,
				       sample->verified, sample->cpu, sample->before.swap_type,
				       r, r < o->warmup, modes[mode]);
				if (mode == 2)
					printf("%" PRIu64, sample->pageout_ns);
				fputs(",,,,,,,,,,,,", stdout);
				bench_sample_csv(sample);
				/* Batch diagnostics are empty on per-touch records. */
				fputs(",,,,,,,", stdout);
				w1_prime_csv(NULL);
				bench_corpus_identity_csv(o->corpus, o->corpus_first + sample->index);
				putchar('\n');
			}
			if (mode == 2)
				continue;
			printf("w1,%s,%" PRIu64 ",%zu,%s,,,,,,,,,,,,,,,v4.6,batch,%u,%d,%s,,%"
			       PRIu64 ",%" PRIu64 ",%zu,%zu,%d,%ld,%ld,%ld,%ld",
			       bench_content_name(o->content), o->seed, o->page_size, mode ? "rand" : "seq",
			       r, r < o->warmup, modes[mode], batch->pageout_ns, batch->drain.purge_ns,
			       batch->nonpresent, o->pages, batch->drain.timed_out,
			       batch->free_before, batch->free_after,
			       batch->available_before, batch->available_after);
			printf(",%zu,%.12f,%zd", batch->first_pass_nonpresent,
			       (double)batch->first_pass_nonpresent / o->pages,
			       (ssize_t)batch->nonpresent - (ssize_t)batch->first_pass_nonpresent);
			struct bench_population population = bench_population(batch->samples, o->pages, batch->done);

			fputs(",,,,,", stdout);
			bench_drain_csv(&batch->drain, &population);
			w1_prime_csv(mode == 3 ? &batch->prime : NULL);
			if (o->corpus)
				fputs(",,", stdout);
			putchar('\n');
		}
	}
	return fflush(stdout) || ferror(stdout) ? -1 : 0;
}

int main(int argc, char **argv)
{
	const char *label = getenv("COMPARE_WORKLOAD_LABEL");
	struct bench_options o;
	struct bench_observer observer = { .pagemap = -1 };
	struct w1_batch *batches = NULL;
	struct bench_sample *samples = NULL, *order = NULL;
	unsigned char *map = MAP_FAILED, *expected = NULL;
	size_t single, count, mismatches = 0, offset = 0;
	unsigned int total = 0;
	const char *operation = "parse";
	int parsed, rc = 1;

	if (bench_corpus_sampling_args(&argc, argv)) {
		fputs("ERROR --corpus-sampling requires uniform-v411 and real corpus\n", stderr);
		return 2;
	}
	if (label && !strcmp(label, "w4"))
		return refault_once(argc, argv);
	parsed = bench_parse(argc, argv, false, &o);
	if (parsed)
		return parsed > 0 ? 0 : 2;
	if (!o.rewrite) {
		fputs("ERROR v4.6 W1 requires --rewrite\n", stderr);
		return 2;
	}
	if (o.corpus) {
		uint64_t prime_first;

		if (bench_input_extra(&o, o.pages, &prime_first)) {
			fputs("ERROR corpus lacks disjoint rewrite-prime pages\n", stderr);
			return 2;
		}
	}
	bench_config("w1-v4.6", &o);
	total = o.rounds + o.warmup;
	single = o.single < o.pages ? o.single : o.pages;
	count = total * (3 * o.pages + single);
	if (count > (128U * BENCH_MIB) / sizeof(*samples)) {
		fputs("ERROR W1 sample storage exceeds 128 MiB safety bound\n", stderr);
		return 2;
	}
	operation = "allocate/prefault measurement storage";
	batches = calloc(total * 4, sizeof(*batches));
	samples = calloc(count, sizeof(*samples));
	order = calloc(o.pages, sizeof(*order));
	expected = malloc(o.page_size);
	if (!batches || !samples || !order || !expected ||
	    bench_signals() || bench_pin_cpu(&o))
		goto out;
	/* Exclude result-buffer first faults from the memory deltas and workload. */
	for (size_t i = 0; i < count; i++)
		samples[i].verified = -1;
	memset(expected, 0, o.page_size);
	bench_order(&o, order);
	for (unsigned int i = 0; i < total * 4; i++)
		batches[i].free_before = -1;
	bench_observer_open(&observer);
	if (observer.pagemap < 0 || observer.errors)
		goto out;
	for (unsigned int r = 0; r < total; r++) {
		long free_before, available_before;
		o.round_shift = ((size_t)r * 7919) % o.pages;
		operation = "fresh round mapping";
		if (map != MAP_FAILED) {
			if (munmap(map, o.bytes))
				goto out;
			map = MAP_FAILED;
		}
		map = mmap(NULL, o.bytes, PROT_READ | PROT_WRITE,
			   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if (map == MAP_FAILED || madvise(map, o.bytes, MADV_NOHUGEPAGE))
			goto out;
		operation = "round fill";
		if (w1_phase(r, "seq", "fresh_mapping") ||
		    w1_phase(r, "seq", "fill_begin") ||
		    bench_meminfo(&free_before, &available_before))
			goto out;
		for (size_t i = 0; i < o.pages; i++) {
			if (bench_interrupted) {
				errno = EINTR;
				goto out;
			}
			if (bench_fill_page(map + i * o.page_size, &o, i))
				goto out;
		}
		for (unsigned int mode = 0; mode < 4; mode++) {
			struct w1_batch *batch = &batches[r * 4 + mode];
			struct bench_options random = o;

			batch->samples = samples + offset;
			offset += mode == 2 ? single : o.pages;
			/* Only seq follows the fresh fill. Later phases retain backing
			 * copies deliberately; they cannot reuse the fresh baseline.
			 */
			batch->free_before = mode ? -1 : free_before;
			batch->available_before = mode ? -1 : available_before;
			random.random_order = mode != 0;
			random.seed += r;
			bench_order(&random, order);
			for (size_t i = 0; i < (mode == 2 ? single : o.pages); i++)
				batch->samples[i].index = order[i].index;
			if (mode == 3) {
				struct bench_options prime_options = o;
				struct w1_batch prime = { .samples = batch->samples };

				/* Isolate rewrite from the seq/rand/single measurements.
				 * Prime with a disjoint real slice, read/verify, then
				 * dirty every page with this slot's requested content.
				 */
				operation = "rewrite fresh mapping";
				if (munmap(map, o.bytes))
					goto out;
				map = MAP_FAILED;
				map = mmap(NULL, o.bytes, PROT_READ | PROT_WRITE,
					   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
				if (map == MAP_FAILED || madvise(map, o.bytes, MADV_NOHUGEPAGE))
					goto out;
				if (o.corpus)
					prime_options.corpus_first += o.pages;
				else
					prime_options.content = CONTENT_RANDOM;
				prime_options.seed ^= UINT64_C(0x7265777269746501);
				if (w1_phase(r, "rewrite_prime", "fresh_mapping") ||
				    w1_phase(r, "rewrite_prime", "fill_begin"))
					goto out;
				for (size_t i = 0; i < o.pages; i++) {
					if (bench_interrupted) {
						errno = EINTR;
						goto out;
					}
					if (bench_fill_page(map + i * o.page_size, &prime_options, i))
						goto out;
				}
				operation = "rewrite prime pageout/readback";
				if (w1_batch_run(map, &prime_options, &observer, &prime, expected,
						 r, "rewrite_prime", &mismatches) ||
				    w1_phase(r, "rewrite", "fill_begin"))
					goto out;
				batch->prime = prime.prime;
				for (size_t i = 0; i < o.pages; i++) {
					if (bench_interrupted) {
						errno = EINTR;
						goto out;
					}
					if (bench_fill_page(map + i * o.page_size, &o, i))
				goto out;
				}
			}
			operation = mode == 2 ? "single-page measurements" : "batch measurements";
			if (mode == 2) {
				if (w1_phase(r, "single", "read_begin") ||
				    w1_single(map, &o, &observer, batch, expected, single, &mismatches) ||
				    w1_phase(r, "single", "read_end"))
					goto out;
			} else if (w1_batch_run(map, &o, &observer, batch, expected, r,
						mode == 3 ? "rewrite" : mode ? "rand" : "seq", &mismatches)) {
				goto out;
			}
		}
	}
	rc = mismatches || observer.errors ? 1 : 0;
out:
	if (rc)
		fprintf(stderr, "ERROR W1 operation=%s errno=%d mismatches=%zu observation_errors=%u\n",
			operation, errno, mismatches, observer.errors);
	/* No output/export during the measured rounds. Release the tested mapping first. */
	if (map != MAP_FAILED && munmap(map, o.bytes))
		rc = 1;
	if (observer.pagemap >= 0)
		close(observer.pagemap);
	if (batches && samples && w1_csv(&o, batches, total))
		rc = 1;
	if (bench_interrupted)
		rc = 128 + bench_interrupted;
	fprintf(stderr, "W1_END method=v4.6 status=%d rounds=%u warmup=%u samples=%zu mismatches=%zu\n",
		rc, o.rounds, o.warmup, count, mismatches);
	free(expected);
	free(order);
	free(samples);
	free(batches);
	return rc;
}
