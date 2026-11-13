// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <linux/kernel-page-flags.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#ifndef MADV_PAGEOUT
#error "MADV_PAGEOUT is required"
#endif
#ifndef MADV_MERGEABLE
#error "MADV_MERGEABLE is required"
#endif
#ifndef MADV_UNMERGEABLE
#error "MADV_UNMERGEABLE is required"
#endif

#define TEST_PAGES 32U
#define DEFAULT_MEMCOMP_SWAP_TYPE 30U
#define KSM_WAIT_MS 15000U
#define MEMCOMP_WAIT_MS 3000U
#define KSFT_SKIP 4

struct test_env {
	long page_size;
	unsigned int memcomp_swap_type;
	int ksm_run_fd;
	int ksm_scans_fd;
	int ksm_merging_fd;
	int pagemap_fd;
	int kpageflags_fd;
	char old_ksm_run;
	unsigned long base_merging;
};

static uint64_t now_ms(void)
{
	struct timespec ts;

	if (clock_gettime(CLOCK_MONOTONIC, &ts) < 0)
		return 0;
	return (uint64_t)ts.tv_sec * 1000U + ts.tv_nsec / 1000000U;
}

static void sleep_ms(unsigned int ms)
{
	struct timespec ts = {
		.tv_sec = ms / 1000U,
		.tv_nsec = (long)(ms % 1000U) * 1000000L,
	};

	while (nanosleep(&ts, &ts) < 0 && errno == EINTR)
		;
}

static int read_u64_fd(int fd, unsigned long *value)
{
	char buf[64];
	char *end;
	ssize_t n;

	do {
		n = pread(fd, buf, sizeof(buf) - 1, 0);
	} while (n < 0 && errno == EINTR);
	if (n <= 0)
		return -1;
	buf[n] = '\0';
	errno = 0;
	*value = strtoul(buf, &end, 10);
	if (errno || end == buf)
		return -1;
	return 0;
}

static int read_ksm_run(int fd, char *value)
{
	char buf[8];
	ssize_t n;

	do {
		n = pread(fd, buf, sizeof(buf) - 1, 0);
	} while (n < 0 && errno == EINTR);
	if (n <= 0)
		return -1;
	*value = buf[0];
	return 0;
}

static int write_ksm_run(int fd, char value)
{
	ssize_t n;

	if (lseek(fd, 0, SEEK_SET) < 0)
		return -1;
	do {
		n = write(fd, &value, 1);
	} while (n < 0 && errno == EINTR);
	return n == 1 ? 0 : -1;
}

static int wait_for_scan_delta(struct test_env *env, unsigned long start,
				       unsigned long delta)
{
	uint64_t deadline = now_ms() + KSM_WAIT_MS;
	unsigned long current;

	for (;;) {
		if (read_u64_fd(env->ksm_scans_fd, &current) < 0)
			return -1;
		if (current >= start + delta)
			return 0;
		if (now_ms() >= deadline)
			return 1;
		sleep_ms(20);
	}
}

static int wait_for_merging_at_most(struct test_env *env,
					unsigned long maximum)
{
	uint64_t deadline = now_ms() + KSM_WAIT_MS;
	unsigned long current;

	for (;;) {
		if (read_u64_fd(env->ksm_merging_fd, &current) < 0)
			return -1;
		if (current <= maximum)
			return 0;
		if (now_ms() >= deadline)
			return 1;
		sleep_ms(20);
	}
}

static int advise_nohuge(void *addr, size_t length)
{
	if (madvise(addr, length, MADV_NOHUGEPAGE) < 0 && errno != EINVAL)
		return -1;
	return 0;
}

static void fill_pages(unsigned char *addr, size_t pages, size_t page_size,
			       unsigned int seed)
{
	size_t i;

	for (i = 0; i < pages; i++) {
		unsigned char value = (unsigned char)(seed + i * 37U);

		memset(addr + i * page_size, value, page_size);
	}
}

static int read_pagemap_entry(struct test_env *env, const void *addr,
				      uint64_t *entry)
{
	off_t offset = (off_t)((uintptr_t)addr / (uintptr_t)env->page_size *
				       sizeof(*entry));
	ssize_t n;

	do {
		n = pread(env->pagemap_fd, entry, sizeof(*entry), offset);
	} while (n < 0 && errno == EINTR);
	return n == (ssize_t)sizeof(*entry) ? 0 : -1;
}

static int check_ksm_pages(struct test_env *env, const void *addr, size_t pages)
{
	size_t i;

	for (i = 0; i < pages; i++) {
		uint64_t entry, pfn, flags;
		ssize_t n;

		if (read_pagemap_entry(env, (const unsigned char *)addr +
				       i * env->page_size, &entry) < 0) {
			fprintf(stderr, "FAIL: single-mapped KSM pageout precondition: cannot read pagemap for B page %zu\n", i);
			return -1;
		}
		pfn = entry & ((UINT64_C(1) << 55) - 1);
		if (!(entry & (UINT64_C(1) << 63)) ||
		    (entry & (UINT64_C(1) << 62)) || !pfn) {
			fprintf(stderr, "FAIL: single-mapped KSM pageout precondition: B page %zu is not present or its PFN is unavailable\n", i);
			return -1;
		}
		do {
			n = pread(env->kpageflags_fd, &flags, sizeof(flags),
				  (off_t)(pfn * sizeof(flags)));
		} while (n < 0 && errno == EINTR);
		if (n != (ssize_t)sizeof(flags)) {
			fprintf(stderr, "FAIL: single-mapped KSM pageout precondition: cannot read kpageflags for B page %zu\n", i);
			return -1;
		}
		if (!(flags & (UINT64_C(1) << KPF_KSM))) {
			fprintf(stderr, "FAIL: single-mapped KSM pageout precondition: B page %zu is not KSM\n", i);
			return -1;
		}
	}
	return 0;
}

static int count_memcomp_tokens(struct test_env *env, const void *addr,
				size_t pages, unsigned int *count)
{
	size_t i;
	unsigned int found = 0;

	for (i = 0; i < pages; i++) {
		uint64_t entry;

		if (read_pagemap_entry(env, (const unsigned char *)addr +
				       i * env->page_size, &entry) < 0)
			return -1;
		if ((entry & (UINT64_C(1) << 62)) &&
			    (entry & UINT64_C(0x1f)) == env->memcomp_swap_type)
			found++;
	}
	*count = found;
	return 0;
}

static int wait_for_memcomp_tokens(struct test_env *env, const void *addr,
					   size_t pages)
{
	uint64_t deadline = now_ms() + MEMCOMP_WAIT_MS;
	unsigned int count;

	for (;;) {
		if (count_memcomp_tokens(env, addr, pages, &count) < 0)
			return -1;
		if (count == pages)
			return 0;
		if (now_ms() >= deadline)
			return 1;
		sleep_ms(10);
	}
}

static int check_mapping_equal(const unsigned char *actual,
				       const unsigned char *expected, size_t length)
{
	return memcmp(actual, expected, length) == 0 ? 0 : -1;
}

static void usage(const char *program)
{
	printf("Usage: %s [--memcomp-swap-type N]\n", program);
}

int main(int argc, char **argv)
{
	struct test_env env = {
		.ksm_run_fd = -1,
		.ksm_scans_fd = -1,
		.ksm_merging_fd = -1,
		.pagemap_fd = -1,
		.kpageflags_fd = -1,
		.memcomp_swap_type = DEFAULT_MEMCOMP_SWAP_TYPE,
	};
	unsigned char *control = MAP_FAILED;
	unsigned char *map_a = MAP_FAILED;
	unsigned char *map_b = MAP_FAILED;
	unsigned char *expected_b = NULL;
	unsigned char *expected_control = NULL;
	size_t length;
	unsigned long scans_before;
	unsigned long merging;
	unsigned long merging_after_pageout;
	unsigned int tokens_a, tokens_b, tokens_control;
	int option;
	int result = KSFT_SKIP;
	static const struct option options[] = {
		{ "memcomp-swap-type", required_argument, NULL, 't' },
		{ "help", no_argument, NULL, 'h' },
		{ NULL, 0, NULL, 0 },
	};

	while ((option = getopt_long(argc, argv, "t:h", options, NULL)) != -1) {
		char *end;
		unsigned long value;

		switch (option) {
		case 't':
			errno = 0;
			value = strtoul(optarg, &end, 10);
			if (errno || end == optarg || *end || value > 31) {
				fprintf(stderr, "invalid memcomp swap type: %s\n", optarg);
				return EXIT_FAILURE;
			}
			env.memcomp_swap_type = (unsigned int)value;
			break;
		case 'h':
			usage(argv[0]);
			return EXIT_SUCCESS;
		default:
			usage(argv[0]);
			return EXIT_FAILURE;
		}
	}

	env.page_size = sysconf(_SC_PAGESIZE);
	if (env.page_size <= 0) {
		fprintf(stderr, "SKIP: cannot determine page size\n");
		return KSFT_SKIP;
	}
	length = TEST_PAGES * (size_t)env.page_size;

	env.ksm_run_fd = open("/sys/kernel/mm/ksm/run", O_RDWR | O_CLOEXEC);
	env.ksm_scans_fd = open("/sys/kernel/mm/ksm/full_scans", O_RDONLY | O_CLOEXEC);
	env.ksm_merging_fd = open("/proc/self/ksm_merging_pages", O_RDONLY | O_CLOEXEC);
	env.pagemap_fd = open("/proc/self/pagemap", O_RDONLY | O_CLOEXEC);
	if (env.ksm_run_fd < 0 || env.ksm_scans_fd < 0 ||
	    env.ksm_merging_fd < 0) {
		printf("SKIP: KSM sysfs/proc support is unavailable\n");
		goto out;
	}
	if (access("/sys/kernel/mm/memcomp/stat", R_OK) < 0) {
		printf("SKIP: /sys/kernel/mm/memcomp/stat is unavailable\n");
		goto out;
	}
	if (env.pagemap_fd < 0) {
		printf("SKIP: /proc/self/pagemap is unreadable\n");
		goto out;
	}
	if (read_ksm_run(env.ksm_run_fd, &env.old_ksm_run) < 0 ||
	    (env.old_ksm_run != '0' && env.old_ksm_run != '1' &&
	     env.old_ksm_run != '2') ||
	    read_u64_fd(env.ksm_merging_fd, &env.base_merging) < 0) {
		printf("SKIP: cannot read KSM control/accounting files\n");
		goto out;
	}
	printf("INFO: page_size=%ld pages=%u memcomp_swap_type=%u\n",
	       env.page_size, TEST_PAGES, env.memcomp_swap_type);
	result = EXIT_FAILURE;

	if (write_ksm_run(env.ksm_run_fd, '1') < 0) {
		perror("write /sys/kernel/mm/ksm/run");
		goto out;
	}

	control = mmap(NULL, length, PROT_READ | PROT_WRITE,
			      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (control == MAP_FAILED || advise_nohuge(control, length) < 0) {
		perror("control mapping setup");
		goto out;
	}
	fill_pages(control, TEST_PAGES, (size_t)env.page_size, 0x51);
	expected_control = malloc(length);
	if (!expected_control) {
		perror("malloc expected control mapping");
		goto out;
	}
	memcpy(expected_control, control, length);
	if (madvise(control, length, MADV_PAGEOUT) < 0) {
		perror("MADV_PAGEOUT control mapping");
		goto out;
	}
	if (wait_for_memcomp_tokens(&env, control, TEST_PAGES) != 0 ||
	    count_memcomp_tokens(&env, control, TEST_PAGES, &tokens_control) < 0 ||
	    tokens_control != TEST_PAGES) {
		fprintf(stderr, "FAIL: ordinary anonymous pageout did not expose %u memcomp tokens\n",
			TEST_PAGES);
		goto out;
	}
	printf("PASS: ordinary anonymous MADV_PAGEOUT produced %u memcomp tokens\n",
	       tokens_control);
	if (check_mapping_equal(control, expected_control, length) < 0) {
		fprintf(stderr, "FAIL: memcomp refault changed control mapping data\n");
		goto out;
	}

	map_a = mmap(NULL, length, PROT_READ | PROT_WRITE,
			    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	map_b = mmap(NULL, length, PROT_READ | PROT_WRITE,
			    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (map_a == MAP_FAILED || map_b == MAP_FAILED ||
	    advise_nohuge(map_a, length) < 0 || advise_nohuge(map_b, length) < 0) {
		perror("KSM mapping setup");
		goto out;
	}
	fill_pages(map_a, TEST_PAGES, (size_t)env.page_size, 0x23);
	fill_pages(map_b, TEST_PAGES, (size_t)env.page_size, 0x23);
	expected_b = malloc(length);
	if (!expected_b) {
		perror("malloc expected mapping");
		goto out;
	}
	memcpy(expected_b, map_b, length);
	if (madvise(map_a, length, MADV_MERGEABLE) < 0 ||
	    madvise(map_b, length, MADV_MERGEABLE) < 0) {
		perror("MADV_MERGEABLE");
		goto out;
	}
	if (read_u64_fd(env.ksm_scans_fd, &scans_before) < 0 ||
	    wait_for_scan_delta(&env, scans_before, 2) != 0 ||
	    read_u64_fd(env.ksm_merging_fd, &merging) < 0) {
		fprintf(stderr, "FAIL: KSM did not complete two full scans\n");
		goto out;
	}
	if (merging < env.base_merging + 2U * TEST_PAGES) {
		fprintf(stderr, "FAIL: KSM merge accounting is %lu, expected at least %lu\n",
			merging, env.base_merging + 2U * TEST_PAGES);
		goto out;
	}
	printf("PASS: real KSM merge accounted for %u pages in each mapping\n",
	       TEST_PAGES);

	if (madvise(map_a, length, MADV_PAGEOUT) < 0 ||
	    madvise(map_b, length, MADV_PAGEOUT) < 0) {
		perror("MADV_PAGEOUT KSM mappings");
		goto out;
	}
	if (count_memcomp_tokens(&env, map_a, TEST_PAGES, &tokens_a) < 0 ||
	    count_memcomp_tokens(&env, map_b, TEST_PAGES, &tokens_b) < 0 ||
	    read_u64_fd(env.ksm_merging_fd, &merging_after_pageout) < 0) {
		perror("read KSM pagemap");
		goto out;
	}
	if (tokens_a || tokens_b ||
	    merging_after_pageout < env.base_merging + 2U * TEST_PAGES ||
	    check_mapping_equal(map_b, expected_b, length) < 0) {
		fprintf(stderr, "FAIL: merged KSM mappings produced memcomp tokens (%u + %u), lost KSM merge, or changed data\n",
			tokens_a, tokens_b);
		goto out;
	}
	printf("PASS: merged KSM MADV_PAGEOUT produced no memcomp token\n");

	map_a[0] ^= 0x5a;
	if (map_a[0] == expected_b[0] ||
	    check_mapping_equal(map_b, expected_b, length) < 0) {
		fprintf(stderr, "FAIL: single-sided KSM write did not preserve the other mapping\n");
		goto out;
	}
	printf("PASS: single-sided modification left the other mapping unchanged\n");

	env.kpageflags_fd = open("/proc/kpageflags", O_RDONLY | O_CLOEXEC);
	if (env.kpageflags_fd < 0) {
		perror("FAIL: single-mapped KSM pageout: open /proc/kpageflags");
		goto out;
	}
	/* Keep the stable KSM pages while dropping their other mapping. */
	if (write_ksm_run(env.ksm_run_fd, '0') < 0) {
		perror("FAIL: single-mapped KSM pageout: pause ksmd");
		goto out;
	}
	if (madvise(map_a, length, MADV_DONTNEED) < 0) {
		perror("FAIL: single-mapped KSM pageout: discard A aliases");
		goto out;
	}
	if (check_ksm_pages(&env, map_b, TEST_PAGES) < 0)
		goto out;
	if (madvise(map_b, length, MADV_PAGEOUT) < 0) {
		perror("FAIL: single-mapped KSM pageout: MADV_PAGEOUT B");
		goto out;
	}
	/* Inspect tokens before reading B can refault and hide them. */
	if (count_memcomp_tokens(&env, map_b, TEST_PAGES, &tokens_b) < 0) {
		perror("FAIL: single-mapped KSM pageout token check: read pagemap");
		goto out;
	}
	if (tokens_b) {
		fprintf(stderr, "FAIL: single-mapped KSM pageout token check: B has %u memcomp tokens\n", tokens_b);
		goto out;
	}
	if (check_mapping_equal(map_b, expected_b, length) < 0) {
		fprintf(stderr, "FAIL: single-mapped KSM pageout changed B data\n");
		goto out;
	}
	printf("INFO: single-mapped KSM MADV_PAGEOUT produced no memcomp token and preserved data\n");
	if (write_ksm_run(env.ksm_run_fd, '1') < 0) {
		perror("FAIL: single-mapped KSM pageout: resume ksmd");
		goto out;
	}

	if (madvise(map_a, length, MADV_UNMERGEABLE) < 0 ||
	    madvise(map_b, length, MADV_UNMERGEABLE) < 0 ||
	    wait_for_merging_at_most(&env, env.base_merging) != 0 ||
	    check_mapping_equal(map_b, expected_b, length) < 0) {
		fprintf(stderr, "FAIL: explicit MADV_UNMERGEABLE did not remove KSM mappings\n");
		goto out;
	}
	printf("PASS: explicit MADV_UNMERGEABLE removed the KSM merge\n");
	result = EXIT_SUCCESS;

out:
	if (map_a != MAP_FAILED) {
		madvise(map_a, length, MADV_UNMERGEABLE);
		munmap(map_a, length);
	}
	if (map_b != MAP_FAILED) {
		madvise(map_b, length, MADV_UNMERGEABLE);
		munmap(map_b, length);
	}
	if (control != MAP_FAILED)
		munmap(control, length);
	free(expected_b);
	free(expected_control);
	if (env.ksm_run_fd >= 0 && env.old_ksm_run &&
	    write_ksm_run(env.ksm_run_fd, env.old_ksm_run) < 0) {
		perror("FAIL: restore /sys/kernel/mm/ksm/run");
		result = EXIT_FAILURE;
	}
	if (env.kpageflags_fd >= 0)
		close(env.kpageflags_fd);
	if (env.pagemap_fd >= 0)
		close(env.pagemap_fd);
	if (env.ksm_merging_fd >= 0)
		close(env.ksm_merging_fd);
	if (env.ksm_scans_fd >= 0)
		close(env.ksm_scans_fd);
	if (env.ksm_run_fd >= 0)
		close(env.ksm_run_fd);
	return result;
}