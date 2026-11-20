// SPDX-License-Identifier: GPL-2.0
/*
 * Static arm64 guest for the memcomp stage-2 MTE gate.
 *
 * A tag fault in the main test is a hard failure. The deliberate bad-tag
 * probe runs in a child so the parent can prove that synchronous MTE checking
 * is active without turning the expected probe into a guest failure.
 */
#define _GNU_SOURCE
#include <arm_acle.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/prctl.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/swap.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifndef PROT_MTE
#define PROT_MTE 0x20
#endif
#ifndef MADV_PAGEOUT
#define MADV_PAGEOUT 21
#endif
#ifndef PR_SET_TAGGED_ADDR_CTRL
#define PR_SET_TAGGED_ADDR_CTRL 55
#endif
#ifndef PR_TAGGED_ADDR_ENABLE
#define PR_TAGGED_ADDR_ENABLE (1UL << 0)
#endif
#ifndef PR_MTE_TCF_SHIFT
#define PR_MTE_TCF_SHIFT 1
#endif
#ifndef PR_MTE_TCF_SYNC
#define PR_MTE_TCF_SYNC (1UL << PR_MTE_TCF_SHIFT)
#endif
#ifndef PR_MTE_TAG_MASK
#define PR_MTE_TAG_MASK (0xffffUL << 3)
#endif

#define PAGE_SIZE 4096UL
#define REGION_PAGES 64UL
#define REGION_SIZE (REGION_PAGES * PAGE_SIZE)
#define TAG_GRANULE 16UL
#define BAD_TAG 0UL
#define PATTERN 0x5aU
#define MCDIR "/sys/kernel/mm/memcomp/"

static void tag_fault_handler(int sig, siginfo_t *si, void *ctx)
{
	(void)ctx;
	/* An ordinary protection or mapping fault is not an MTE success. */
	_exit(sig == SIGSEGV && si->si_code == SEGV_MTESERR ? 128 + sig : 4);
}

static void die(const char *what)
{
	fprintf(stderr, "MTE_FAIL: %s: %s\n", what, strerror(errno));
	exit(2);
}

static void mount_pseudo_filesystems(void)
{
	(void)mkdir("/proc", 0755);
	(void)mkdir("/sys", 0755);
	(void)mkdir("/dev", 0755);
	if (mount("proc", "/proc", "proc", 0, NULL) < 0 && errno != EBUSY)
		die("mount proc");
	if (mount("sysfs", "/sys", "sysfs", 0, NULL) < 0 && errno != EBUSY)
		die("mount sysfs");
	if (mount("devtmpfs", "/dev", "devtmpfs", 0, NULL) < 0 && errno != EBUSY)
		die("mount devtmpfs");
}

static int command_has(const char *needle)
{
	char buf[4096];
	FILE *fp = fopen("/proc/cmdline", "r");
	int found = 0;

	if (!fp)
		die("open /proc/cmdline");
	if (fgets(buf, sizeof(buf), fp) && strstr(buf, needle))
		found = 1;
	fclose(fp);
	return found;
}

/* Require a whole-token request and observe sysfs, never infer nodes from argv. */
static void verify_numa_topology(void)
{
	char cmd[4096], online[32], *token, *save;
	FILE *fp;
	char requested = 0;
	unsigned int requests = 0;
	int extra;
	size_t len;

	fp = fopen("/proc/cmdline", "r");
	if (!fp)
		die("open NUMA command line");
	if (!fgets(cmd, sizeof(cmd), fp)) {
		errno = EIO;
		die("read NUMA command line");
	}
	extra = fgetc(fp);
	if (ferror(fp) || extra != EOF) {
		errno = EOVERFLOW;
		die("truncated NUMA command line");
	}
	if (fclose(fp))
		die("close NUMA command line");
	for (token = strtok_r(cmd, " \t\n", &save); token;
	     token = strtok_r(NULL, " \t\n", &save)) {
		if (strncmp(token, "mte_numa=", 9))
			continue;
		requests++;
		if ((token[9] != 'n' && token[9] != 'y') || token[10]) {
			errno = EINVAL;
			die("invalid NUMA request");
		}
		requested = token[9];
	}
	if (requests != 1) {
		errno = EINVAL;
		die("missing or duplicate NUMA request");
	}
	fp = fopen("/sys/devices/system/node/online", "r");
	if (!fp) {
		if (errno != ENOENT || requested != 'n')
			die("open NUMA online nodes");
		printf("MTE_NUMA requested=n online=absent\n");
		return;
	}
	if (!fgets(online, sizeof(online), fp)) {
		errno = EIO;
		die("read NUMA online nodes");
	}
	extra = fgetc(fp);
	if (ferror(fp) || extra != EOF) {
		errno = EOVERFLOW;
		die("truncated NUMA online nodes");
	}
	if (fclose(fp))
		die("close NUMA online nodes");
	len = strlen(online);
	if (len && online[len - 1] == '\n')
		online[--len] = 0;
	if (strcmp(online, requested == 'y' ? "0-1" : "0")) {
		errno = EINVAL;
		die("observed NUMA online nodes do not match request");
	}
	printf("MTE_NUMA requested=%c online=%s\n", requested, online);
}

static int active_swap(void)
{
	char line[256];
	FILE *fp = fopen("/proc/swaps", "r");
	int count = 0;

	if (!fp)
		die("open /proc/swaps");
	while (fgets(line, sizeof(line), fp))
		if (strncmp(line, "Filename", 8))
			count++;
	fclose(fp);
	return count != 0;
}

static void enable_swap_if_requested(void)
{
	int requested = command_has("mte_swap=on");
	int enabled = active_swap();

	if (requested && !enabled) {
		if (swapon("/dev/vda", 0) < 0)
			die("swapon /dev/vda");
		enabled = active_swap();
	}
	if (requested != enabled) {
		errno = EINVAL;
		die("swap state does not match case");
	}
	printf("MTE_SWAP requested=%s active=%s\n",
		requested ? "on" : "off", enabled ? "on" : "off");
}

static void set_mte_mode(void)
{
	unsigned long ctrl = PR_TAGGED_ADDR_ENABLE | PR_MTE_TCF_SYNC |
		(PR_MTE_TAG_MASK);

	if (prctl(PR_SET_TAGGED_ADDR_CTRL, ctrl, 0, 0, 0) < 0)
		die("PR_SET_TAGGED_ADDR_CTRL");
}

static void *tagged_ptr(void *base, unsigned long tag)
{
	uintptr_t raw = (uintptr_t)base;

	return (void *)((raw & ~(0xFUL << 56)) | (tag << 56));
}

static unsigned long allocation_tag(size_t offset)
{
	/* Nonzero tags vary between granules and between neighboring pages. */
	return 1 + (offset / TAG_GRANULE + offset / PAGE_SIZE) % 14;
}

static void set_allocation_tags(void *base)
{
	unsigned long offset;

	for (offset = 0; offset < REGION_SIZE; offset += TAG_GRANULE)
		(void)__arm_mte_set_tag(tagged_ptr((char *)base + offset, allocation_tag(offset)));
}

static unsigned char expected_byte(size_t offset)
{
	return (unsigned char)(PATTERN ^ (offset * 17U) ^ (offset / PAGE_SIZE * 29U));
}

static void fill_and_check(void *base)
{
	size_t offset, i;

	for (offset = 0; offset < REGION_SIZE; offset += TAG_GRANULE) {
		unsigned char *ptr = tagged_ptr((char *)base + offset,
					       allocation_tag(offset));

		for (i = 0; i < TAG_GRANULE; i++)
			ptr[i] = expected_byte(offset + i);
		for (i = 0; i < TAG_GRANULE; i++)
			if (ptr[i] != expected_byte(offset + i))
				die("initial data check");
	}
}

struct snap {
	uint64_t stored;
	uint64_t pageout;
	uint64_t writeback;
	uint64_t read_io;
	uint64_t swapped;
};

/* Fixed storage belongs to the original working set pinned by MCL_CURRENT. */
static char read_buffer[16384];

static const char *read_text(const char *path)
{
	size_t used = 0;
	ssize_t n;
	int fd = open(path, O_RDONLY);

	if (fd < 0)
		die(path);
	while (used < sizeof(read_buffer) - 1) {
		n = read(fd, read_buffer + used, sizeof(read_buffer) - 1 - used);
		if (n < 0)
			die(path);
		if (!n)
			break;
		used += n;
	}
	close(fd);
	if (used == sizeof(read_buffer) - 1) {
		errno = EOVERFLOW;
		die(path);
	}
	read_buffer[used] = 0;
	return read_buffer;
}

static uint64_t read_counter(const char *path, const char *wanted)
{
	char key[128];
	uint64_t value;
	const char *line = read_text(path);

	while (line && *line) {
		if (sscanf(line, "%127s %" SCNu64, key, &value) == 2 &&
		    !strcmp(key, wanted))
			return value;
		line = strchr(line, '\n');
		if (line)
			line++;
	}
	errno = ENOENT;
	die(wanted);
	return 0;
}

static void take_snap(struct snap *s)
{
	uint64_t compressed, pool;

	if (sscanf(read_text(MCDIR "stat"), "%" SCNu64 " %" SCNu64 " %" SCNu64,
		   &s->stored, &compressed, &pool) != 3) {
		errno = EINVAL;
		die("parse memcomp stat");
	}
	s->pageout = read_counter(MCDIR "events", "pageout_store_ok");
	s->read_io = read_counter(MCDIR "events", "swapped_read_io");
	s->swapped = read_counter(MCDIR "events", "swapped_entries");
	s->writeback = read_counter(MCDIR "packed_swap_events",
				    "writeback_payloads");
}

static unsigned long swapped_pages(void *base)
{
	uint64_t entries[REGION_PAGES];
	off_t offset = (uintptr_t)base / PAGE_SIZE * sizeof(entries[0]);
	unsigned long i, count = 0;
	int fd = open("/proc/self/pagemap", O_RDONLY);

	if (fd < 0)
		die("open pagemap");
	if (pread(fd, entries, sizeof(entries), offset) != sizeof(entries))
		die("read pagemap");
	close(fd);
	for (i = 0; i < REGION_PAGES; i++)
		if ((entries[i] & (1ULL << 62)) && !(entries[i] & (1ULL << 63)))
			count++;
	return count;
}

static void check_data(void *base, const char *label)
{
	size_t offset, i;

	for (offset = 0; offset < REGION_SIZE; offset += TAG_GRANULE) {
		unsigned char *ptr = tagged_ptr((char *)base + offset,
					       allocation_tag(offset));

		for (i = 0; i < TAG_GRANULE; i++)
			if (ptr[i] != expected_byte(offset + i)) {
				errno = EILSEQ;
				die(label);
			}
	}
}

static void compress_target(void *base, int report)
{
	struct snap before, after;
	unsigned long swapped = 0;
	unsigned int retry;

	printf("MTE_TRACE compression_snapshot_before\n");
	take_snap(&before);
	printf("MTE_TRACE compression_pageout_begin\n");
	if (madvise(base, REGION_SIZE, MADV_PAGEOUT) < 0)
		die("MADV_PAGEOUT");
	printf("MTE_TRACE compression_pageout_returned\n");
	for (retry = 0; retry < 100; retry++) {
		swapped = swapped_pages(base);
		if (swapped == REGION_PAGES)
			break;
		usleep(10000);
	}
	take_snap(&after);
	if (swapped != REGION_PAGES || after.stored < before.stored + REGION_SIZE ||
	    after.pageout <= before.pageout) {
		fprintf(stderr, "MTE_FAIL: compression not observed stored=%" PRIu64
			"->%" PRIu64 " pageout=%" PRIu64 "->%" PRIu64
			" swapped=%lu\n", before.stored, after.stored,
			before.pageout, after.pageout, swapped);
		exit(2);
	}
	if (report)
		printf("MTE_COMPRESS stored_bytes_delta=%" PRIu64
		       " pageout_delta=%" PRIu64 " swapped_pages=%lu\n",
		       after.stored - before.stored, after.pageout - before.pageout,
		       swapped);
}

static uint64_t meminfo_bytes(const char *wanted)
{
	char key[128];
	uint64_t kb;
	const char *line = read_text("/proc/meminfo");

	while (line && *line) {
		if (sscanf(line, "%127s %" SCNu64 " kB", key, &kb) == 2 &&
		    !strcmp(key, wanted))
			return kb * 1024;
		line = strchr(line, '\n');
		if (line)
			line++;
	}
	errno = ENOENT;
	die(wanted);
	return 0;
}

static uint64_t random_word(uint64_t *state)
{
	*state ^= *state << 13;
	*state ^= *state >> 7;
	*state ^= *state << 17;
	return *state;
}

static void *allocate_pressure(size_t bytes, int compressible)
{
	unsigned char *base = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
				 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	uint64_t state = 0x91a739c285ef641dULL;
	size_t offset;

	if (base == MAP_FAILED)
		die("mmap pressure");
	if (madvise(base, bytes, MADV_NOHUGEPAGE) < 0)
		die("MADV_NOHUGEPAGE pressure");
	for (offset = 0; offset < bytes; offset += PAGE_SIZE) {
		uint64_t *words = (uint64_t *)(base + offset);
		size_t limit = PAGE_SIZE / sizeof(*words), i;

		if (compressible)
			limit /= 2;
		for (i = 0; i < limit; i++)
			words[i] = random_word(&state);
		if (compressible) {
			memcpy(base + offset + PAGE_SIZE / 2,
			       base + offset, PAGE_SIZE / 2);
			/* Keep the resident working set bounded while growing the pool. */
			if ((offset + PAGE_SIZE) % (4UL << 20) == 0 &&
			    madvise(base + offset + PAGE_SIZE - (4UL << 20),
				    4UL << 20, MADV_PAGEOUT) < 0)
				die("MADV_PAGEOUT filler");
		}
	}
	if (compressible && madvise(base, bytes, MADV_PAGEOUT) < 0)
		die("MADV_PAGEOUT filler tail");
	return base;
}

/* Refault existing filler in 4 MiB batches, matching its initial pageout. */
static void renew_compressible_filler(void *base, size_t bytes)
{
	size_t offset = 0;

	while (offset < bytes) {
		size_t length = bytes - offset;
		volatile unsigned char *pages = (unsigned char *)base + offset;
		volatile unsigned char touched = 0;
		size_t page;

		if (length > (4UL << 20))
			length = 4UL << 20;
		for (page = 0; page < length; page += PAGE_SIZE)
			touched ^= pages[page];
		(void)touched;
		if (madvise((unsigned char *)base + offset, length, MADV_PAGEOUT) < 0)
			die("MADV_PAGEOUT filler renewal");
		offset += length;
	}
}

/* Read-only diagnostics, kept outside the target refault IO interval. */
static void trace_writeback_meminfo(const char *phase, unsigned int round)
{
	uint64_t swap_total = meminfo_bytes("SwapTotal:");
	uint64_t swap_free = meminfo_bytes("SwapFree:");
	uint64_t available = meminfo_bytes("MemAvailable:");
	uint64_t total = meminfo_bytes("MemTotal:");

	printf("MTE_TRACE writeback_meminfo phase=%s round=%u"
	       " SwapTotal_bytes=%" PRIu64 " SwapFree_bytes=%" PRIu64
	       " MemAvailable_bytes=%" PRIu64 " MemTotal_bytes=%" PRIu64 "\n",
	       phase, round, swap_total, swap_free, available, total);
}

static void verify_writeback(void *base)
{
	struct snap before, after, read_before, read_after;
	uint64_t target_read_io = 0;
	unsigned long target_swapped = 0;
	uint64_t total = meminfo_bytes("MemTotal:");
	size_t filler_bytes = (total * 3 / 4) & ~(PAGE_SIZE - 1);
	size_t pressure_bytes;
	void *filler, *pressure;
	unsigned int retry;

	trace_writeback_meminfo("start", 0);
	if (!(strtoul(read_text("/sys/kernel/mm/lru_gen/enabled"), NULL, 0) & 1)) {
		errno = ENODEV;
		die("MGLRU must be enabled for memcomp writeback");
	}
	compress_target(base, 0);
	take_snap(&before);
	/* Compress target first; FIFO selection still uses clusters/second chances. */
	filler = allocate_pressure(filler_bytes, 1);
	take_snap(&after);
	printf("MTE_POOL stored_bytes=%" PRIu64 " pageout_delta=%" PRIu64
	       " pool_pages=%" PRIu64 " gate_open=%" PRIu64 "\n",
	       after.stored, after.pageout - before.pageout,
	       read_counter(MCDIR "packed_swap_events", "writeback_pool_pages"),
	       read_counter(MCDIR "packed_swap_events", "writeback_gate_open"));
	pressure_bytes = (meminfo_bytes("MemAvailable:") + total / 8) &
			 ~(PAGE_SIZE - 1);
	if (pressure_bytes > total * 3 / 4)
		pressure_bytes = (total * 3 / 4) & ~(PAGE_SIZE - 1);
	printf("MTE_PRESSURE filler_bytes=%zu pressure_bytes=%zu\n",
	       filler_bytes, pressure_bytes);
	fflush(stdout);
	pressure = allocate_pressure(pressure_bytes, 0);
	for (retry = 0; retry < 16; retry++) {
		volatile unsigned char *bytes = pressure;
		volatile unsigned char touched = 0;
		size_t offset;

		/* Offer the existing filler to reclaim again before the pressure sweep. */
		renew_compressible_filler(filler, filler_bytes);
		printf("MTE_TRACE filler_renewal round=%u bytes=%zu"
		       " pool_pages=%" PRIu64 " gate_open=%" PRIu64 "\n",
		       retry, filler_bytes,
		       read_counter(MCDIR "packed_swap_events", "writeback_pool_pages"),
		       read_counter(MCDIR "packed_swap_events", "writeback_gate_open"));

		/* Sustained refault pressure, not merely one allocation spike. */
		for (offset = 0; offset < pressure_bytes; offset += PAGE_SIZE)
			touched ^= bytes[offset];
		(void)touched;
		if (madvise(pressure, pressure_bytes, MADV_PAGEOUT) < 0)
			die("MADV_PAGEOUT pressure sweep");
		usleep(100000);
		take_snap(&after);
		printf("MTE_PRESSURE_ROUND round=%u stored_bytes=%" PRIu64
		       " payloads=%" PRIu64 " swapped_entries=%" PRIu64
		       " pressure_scans=%" PRIu64 "\n", retry, after.stored,
		       after.writeback, after.swapped,
		       read_counter(MCDIR "packed_swap_events",
				    "writeback_pressure_scans"));
		trace_writeback_meminfo("post-pressure", retry);
		/*
		 * Global writeback can belong entirely to filler.  Probe only
		 * the tagged target while pressure is still held, and require
		 * an actual MC_SWAPPED load during its data/tag verification.
		 */
		target_swapped = swapped_pages(base);
		take_snap(&read_before);
		check_data(base, "post-writeback tagged data check");
		take_snap(&read_after);
		target_read_io = read_after.read_io > read_before.read_io ?
			read_after.read_io - read_before.read_io : 0;
		printf("MTE_TARGET_ROUND round=%u swapped_pages=%lu"
		       " swapped_read_io_delta=%" PRIu64 " data=PASS tag=PASS\n",
		       retry, target_swapped, target_read_io);
		if (after.writeback > before.writeback && target_read_io)
			break;
		/* Refault made it resident: requeue it before the next round. */
		if (retry + 1 < 16)
			compress_target(base, 0);
	}
	if (munmap(pressure, pressure_bytes) < 0 || munmap(filler, filler_bytes) < 0)
		die("munmap pressure");
	/* The acceptance observation was made before dropping the pressure. */
	if (after.writeback <= before.writeback || !target_read_io) {
		fprintf(stderr, "MTE_FAIL: tagged writeback/refault not observed "
			"payloads=%" PRIu64 "->%" PRIu64 " remaining=%" PRIu64
			" read_io=%" PRIu64 "->%" PRIu64 "\n", before.writeback,
			after.writeback, read_before.swapped, read_before.read_io,
			read_after.read_io);
		exit(2);
	}
	printf("MTE_WRITEBACK payloads_delta=%" PRIu64
	       " swapped_read_io_delta=%" PRIu64 " data=PASS tag=PASS\n",
	       after.writeback - before.writeback,
	       target_read_io);
}

static void bad_tag_probe(void *base)
{
	volatile unsigned char value;
	unsigned char *bad = tagged_ptr(base, BAD_TAG);

	value = bad[0];
	(void)value;
	_exit(3);
}

static void verify_bad_tag_fault(void *base)
{
	struct sigaction sa = {
		.sa_sigaction = tag_fault_handler,
		.sa_flags = SA_SIGINFO | SA_RESETHAND,
	};
	pid_t pid;
	int status;

	sigemptyset(&sa.sa_mask);
	pid = fork();
	if (pid < 0)
		die("fork bad-tag probe");
	if (pid == 0) {
		if (sigaction(SIGSEGV, &sa, NULL) < 0)
			die("sigaction SIGSEGV in probe child");
		bad_tag_probe(base);
	}
	if (waitpid(pid, &status, 0) < 0)
		die("waitpid bad-tag probe");
	if (WIFEXITED(status) && WEXITSTATUS(status) == 128 + SIGSEGV) {
		printf("MTE_BAD_TAG probe=PASS signal=SIGSEGV si_code=SEGV_MTESERR\n");
		return;
	}
	fprintf(stderr, "MTE_FAIL: bad-tag probe status=0x%x\n", status);
	exit(3);
}

int main(void)
{
	void *base;
	struct snap warm;

	setvbuf(stdout, NULL, _IONBF, 0);
	mount_pseudo_filesystems();
	verify_numa_topology();
	enable_swap_if_requested();
	set_mte_mode();
	if (!read_counter(MCDIR "events", "enabled") ||
	    !read_counter(MCDIR "events", "comp_ctx_count")) {
		errno = ENODEV;
		die("memcomp unavailable: require a built-in or loaded compressor");
	}
	/* Warm parsing paths before pinning; later samples use fixed storage. */
	take_snap(&warm);
	/* Pin the original working set, but not the subsequent tagged mapping. */
	if (mlockall(MCL_CURRENT) < 0)
		die("mlockall original working set");
	base = mmap(NULL, REGION_SIZE, PROT_READ | PROT_WRITE | PROT_MTE,
			MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (base == MAP_FAILED)
		die("mmap PROT_MTE");
	if (((uintptr_t)base & (PAGE_SIZE - 1)) != 0) {
		errno = EINVAL;
		die("mapping alignment");
	}
	if (madvise(base, REGION_SIZE, MADV_NOHUGEPAGE) < 0)
		die("MADV_NOHUGEPAGE tagged mapping");
	set_allocation_tags(base);
	fill_and_check(base);
	printf("MTE_CASE prot_mte=PASS tagged_write=PASS\n");
	compress_target(base, 1);
	check_data(base, "post-pageout data check");
	printf("MTE_PAGEOUT data=PASS tag=PASS pages=%lu\n", REGION_PAGES);
	if (command_has("mte_swap=on"))
		verify_writeback(base);
	verify_bad_tag_fault(base);
	if (munmap(base, REGION_SIZE) < 0)
		die("munmap");
	printf("MTE_RESULT PASS\n");
	fflush(stdout);
	sync();
	if (reboot(RB_POWER_OFF) < 0)
		die("reboot poweroff");
	return 0;
}
