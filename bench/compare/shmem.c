// SPDX-License-Identifier: GPL-2.0-only
/* Explicit anonymous pressure workload; legacy owned tmpfs is functional-only. */
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


#include <linux/magic.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/vfs.h>
#include <sys/wait.h>

/* The supplied directory must not permit another UID to replace our entry. */
static int unlink_owned(int directory, const char *name, int file)
{
	struct stat opened, named;

	if (fstat(file, &opened))
		return -1;
	if (fstatat(directory, name, &named, AT_SYMLINK_NOFOLLOW))
		return errno == ENOENT ? 0 : -1;
	if (opened.st_dev != named.st_dev || opened.st_ino != named.st_ino) {
		errno = ESTALE;
		return -1;
	}
	return unlinkat(directory, name, 0);
}

static int open_tmpfs_directory(const char *path)
{
	struct stat st;
	struct statfs fs;
	int saved, fd = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);

	if (fd < 0)
		return -1;
	if (fstat(fd, &st) || fstatfs(fd, &fs))
		goto error;
	if (st.st_uid != geteuid() || (st.st_mode & (S_IWGRP | S_IWOTH))) {
		errno = EACCES;
		goto error;
	}
	if ((unsigned long)fs.f_type != (unsigned long)TMPFS_MAGIC) {
		errno = ENODEV;
		goto error;
	}
	return fd;
error:
	saved = errno;
	close(fd);
	errno = saved;
	return -1;
}

static int create_owned(int directory, uint64_t seed, char *name, size_t capacity)
{
	unsigned int attempt;
	int fd;

	for (attempt = 0; attempt < 128; attempt++) {
		int n = snprintf(name, capacity, "compare-shmem-%ld-%016" PRIx64 "-%u",
				 (long)getpid(), seed, attempt);

		if (n < 0 || (size_t)n >= capacity) {
			errno = ENAMETOOLONG;
			return -1;
		}
		fd = openat(directory, name, O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
		if (fd >= 0 || errno != EEXIST)
			return fd;
	}
	errno = EEXIST;
	return -1;
}

int main(int argc, char **argv)
{
	struct bench_options o;
	struct bench_observer observer = { .pagemap = -1 };
	struct bench_pressure_child pressure = { .pid = -1, .stop_fd = -1 };
	struct bench_sample *samples = NULL;
	unsigned char *map = MAP_FAILED, *expected = NULL;
	uint64_t start, end, wall_ns = 0;
	struct bench_drain drain = { 0 };
	size_t i, done = 0, mismatches = 0;
	const char *operation = "setup";
	char name[128] = "";
	bool named = false;
	int directory = -1, file = -1, parsed, rc = 1;

	if (bench_corpus_sampling_args(&argc, argv)) {
		fputs("ERROR --corpus-sampling requires uniform-v411 and real corpus\n", stderr);
		return 2;
	}
	parsed = bench_parse(argc, argv, true, &o);
	if (parsed)
		return parsed > 0 ? 0 : 2;
	if (o.corpus && o.corpus->uniform_v411 && strcmp(o.corpus_source, "combined")) {
		fputs("ERROR uniform-v411 W3 requires combined source\n", stderr);
		return 2;
	}
	if (o.corpus && o.corpus->uniform_v411 && o.pressure_mib) {
		uint64_t pressure_first;

		if (bench_input_extra(&o, (size_t)o.pressure_mib * BENCH_MIB / o.page_size,
				      &pressure_first)) {
			fputs("ERROR corpus lacks disjoint pressure pages\n", stderr);
			return 2;
		}
	}
	bench_config("w3", &o);
	fputs("REFAULT_SCOPE workload=w3 definition=pre_touch_pagemap_present_0 "
	      "plain_swap_cache_hits=indistinguishable\n", stderr);
	if (bench_signals() || bench_pin_cpu(&o))
		goto error;
	if (!o.anonymous) {
		operation = "open owned non-writable-by-others tmpfs directory";
		directory = open_tmpfs_directory(o.directory);
		if (directory < 0)
			goto error;
		fprintf(stderr, "TMPFS directory=%s bytes=%zu\n", o.directory, o.bytes);
		operation = "create O_EXCL tmpfs file";
		file = create_owned(directory, o.seed, name, sizeof(name));
		if (file < 0)
			goto error;
		named = true;
		/* Keep only the descriptor/mapping: later signals cannot leave file data. */
		operation = "unlink own new inode";
		if (unlink_owned(directory, name, file))
			goto error;
		named = false;
		operation = "size tmpfs file";
		if (ftruncate(file, (off_t)o.bytes))
			goto error;
	}
	fprintf(stderr, "MAPPING workload=w3 kind=%s\n",
		o.anonymous ? "private-anonymous-v410" : "legacy-shared-tmpfs");
	operation = "allocate/map workload";
	samples = calloc(o.pages, sizeof(*samples));
	expected = malloc(o.page_size);
	map = mmap(NULL, o.bytes, PROT_READ | PROT_WRITE,
		   o.anonymous ? MAP_PRIVATE | MAP_ANONYMOUS : MAP_SHARED,
		   o.anonymous ? -1 : file, 0);
	if (!samples || !expected || map == MAP_FAILED)
		goto error;
	operation = "MADV_NOHUGEPAGE";
	if (madvise(map, o.bytes, MADV_NOHUGEPAGE))
		goto error;
	bench_order(&o, samples);
	operation = "fill real-page mapping";
	if (bench_phase("fill_begin") || bench_console_drain() || bench_now(&start))
		goto error;
	for (i = 0; i < o.pages; i++) {
		if (bench_interrupted) {
			errno = EINTR;
			goto error;
		}
		if (bench_fill_page(map + i * o.page_size, &o, i))
			goto error;
	}
	if (bench_now(&end))
		goto error;
	fprintf(stderr, "FILL bytes=%zu load_and_write_ns=%" PRIu64 "\n", o.bytes, end - start);
	memset(expected, 0, o.page_size);
	bench_observer_open(&observer);
	if (observer.pagemap < 0 || observer.errors || bench_phase("prepared"))
		goto error;
	operation = "bounded pressure";
	if (o.pressure_mib) {
		if (bench_phase("pressure_begin") || bench_start_pressure(&o, (size_t)o.pressure_mib * BENCH_MIB,
				     &pressure, map, file, directory) ||
		    bench_phase("pressure_ready"))
			goto error;
	} else if (bench_phase("pressure_disabled")) {
		goto error;
	}
	operation = "settle/snapshot";
	if (bench_sleep_us((uint64_t)o.settle_ms * 1000) ||
	    madvise(map, o.bytes, MADV_PAGEOUT) ||
	    bench_drain_pageout(map, &o, &observer, &drain, false) ||
	    bench_snapshot(map, &o, &observer, samples) || bench_phase("read_begin"))
		goto error;
	operation = "readback/verification";
	if (bench_measure(map, &o, &observer, samples, expected, &done, &wall_ns, &mismatches) ||
	    bench_phase("read_end"))
		goto error;
	rc = mismatches || observer.errors ? 1 : 0;
	goto out;
error:
	fprintf(stderr, "ERROR operation=%s errno=%d message=%s\n", operation, errno, strerror(errno));
out:
	if (bench_finish_pressure(&pressure, rc != 0 || bench_interrupted)) {
		perror("pressure child did not complete normally");
		rc = 1;
	}
	if (map != MAP_FAILED && munmap(map, o.bytes)) {
		perror("munmap");
		rc = 1;
	}
	if (named && unlink_owned(directory, name, file)) {
		perror("cleanup own tmpfs inode");
		rc = 1;
	}
	if (file >= 0 && close(file)) {
		perror("close tmpfs file");
		rc = 1;
	}
	if (directory >= 0)
		close(directory);
	if (observer.pagemap >= 0)
		close(observer.pagemap);
	if (bench_interrupted)
		rc = 128 + bench_interrupted;
	if (corpus_csv("w3", &o, samples, done, &drain, NULL)) {
		fputs("ERROR CSV output failed\n", stderr);
		rc = 1;
	}
	if (bench_summary("w3", &o, samples, done, mismatches, wall_ns, observer.errors, rc)) {
		fputs("ERROR summary output failed\n", stderr);
		rc = 1;
	}
	free(expected);
	free(samples);
	return rc;
}
