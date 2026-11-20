// SPDX-License-Identifier: GPL-2.0
/* Standalone PID 1 for the two real-swap KUnit suites. */
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/swap.h>
#include <unistd.h>

#define SWAPDEV "/dev/vda"
#define SCRATCH_MARKER "memcomp-kunit-swap-scratch-v1\n"
#define DISK_BYTES (UINT64_C(512) << 20)
#define KUNIT_DIR "/sys/kernel/debug/kunit/"

static const char *const suites[] = {
	"swap-memcomp-budget",
	"swap-memcomp-packed-budget",
};

static _Noreturn void poweroff(void)
{
	if (reboot(RB_POWER_OFF))
		perror("poweroff");
	for (;;)
		pause();
}

static _Noreturn void fail(const char *why)
{
	printf("KUNIT_SWAP FAIL %s\n", why);
	poweroff();
}

static _Noreturn void sysfail(const char *op)
{
	perror(op);
	fail(op);
}

static void check_cmdline(void)
{
	FILE *f = fopen("/proc/cmdline", "r");
	char *line = NULL, *word, *save;
	size_t cap = 0;
	bool disposable = false, enabled = false;

	if (!f || getline(&line, &cap, f) < 0)
		sysfail("read /proc/cmdline");
	fclose(f);
	if (strchr(line, '"'))
		fail("quoted boot arguments are not supported by this init");
	for (word = strtok_r(line, " \t\n", &save); word;
	     word = strtok_r(NULL, " \t\n", &save)) {
		if (!strcmp(word, "kunit_swap_disposable=1"))
			disposable = true;
		if (!strcmp(word, "kunit.enable=1"))
			enabled = true;
		else if (!strncmp(word, "kunit.", 6) ||
			 !strncmp(word, "kunit_shutdown", 14) ||
			 !strncmp(word, "kunit-shutdown", 14))
			fail("requires an unfiltered kunit.enable=1 boot");
	}
	free(line);
	if (!disposable || !enabled)
		fail("requires standalone disposable guest opt-in and kunit.enable=1");
}

static void check_swaps(bool active)
{
	FILE *f = fopen("/proc/swaps", "r");
	char line[256], path[128], type[32];

	if (!f)
		sysfail("open /proc/swaps");
	if (!fgets(line, sizeof(line), f) || !strchr(line, '\n') ||
	    sscanf(line, "%127s %31s", path, type) != 2 ||
	    strcmp(path, "Filename") || strcmp(type, "Type"))
		fail("invalid /proc/swaps header");
	if (active &&
	    (!fgets(line, sizeof(line), f) || !strchr(line, '\n') ||
	     sscanf(line, "%127s %31s", path, type) != 2 ||
	     strcmp(path, SWAPDEV) || strcmp(type, "partition")))
		fail("scratch disk is not active swap");
	if (fgetc(f) != EOF || ferror(f))
		fail("unexpected active swap or unreadable /proc/swaps");
	fclose(f);
}

static void setup_swap(void)
{
	long page_size = sysconf(_SC_PAGESIZE);
	uint32_t info[3] = { 1, 0, 0 };
	unsigned char *header;
	struct stat st;
	uint64_t bytes;
	int fd;

	if (page_size < 4096 || DISK_BYTES % page_size)
		fail("unexpected base page size");
	info[1] = DISK_BYTES / page_size - 1;
	header = malloc(page_size);
	if (!header)
		sysfail("allocate swap header");
	for (int i = 0; i < 50 && access(SWAPDEV, F_OK); i++)
		usleep(100000);
	fd = open(SWAPDEV, O_RDWR | O_CLOEXEC | O_NOFOLLOW | O_EXCL);
	if (fd < 0)
		sysfail("open scratch /dev/vda");
	if (fstat(fd, &st) || !S_ISBLK(st.st_mode) ||
	    ioctl(fd, BLKGETSIZE64, &bytes) || bytes != DISK_BYTES)
		fail("/dev/vda is not the expected 512 MiB scratch block device");
	if (pread(fd, header, page_size, 0) != page_size ||
	    memcmp(header, SCRATCH_MARKER, sizeof(SCRATCH_MARKER) - 1))
		fail("scratch disk marker not found; refusing to format");
	for (long i = sizeof(SCRATCH_MARKER) - 1; i < page_size; i++)
		if (header[i])
			fail("scratch disk header is not blank after marker");
	memset(header, 0, page_size);
	memcpy(header + 1024, info, sizeof(info));
	memcpy(header + page_size - 10, "SWAPSPACE2", 10);
	if (pwrite(fd, header, page_size, 0) != page_size || fsync(fd) || close(fd))
		sysfail("write scratch swap header");
	free(header);
	if (swapon(SWAPDEV, 0))
		sysfail("swapon " SWAPDEV);
	check_swaps(true);
	puts("KUNIT_SWAP SWAPON " SWAPDEV);
}

static void emit_results(const char *phase, const char *suite)
{
	char path[128], *line = NULL;
	size_t cap = 0;
	ssize_t len;
	FILE *f;

	snprintf(path, sizeof(path), KUNIT_DIR "%s/results", suite);
	f = fopen(path, "r");
	if (!f)
		sysfail(path);
	printf("KUNIT_SWAP BEGIN %s %s\n", phase, suite);
	while ((len = getline(&line, &cap, f)) >= 0) {
		if (!len || line[len - 1] != '\n')
			fail("incomplete debugfs result line");
		printf("KUNIT_SWAP KTAP %s", line);
	}
	if (!feof(f) || ferror(f))
		sysfail(path);
	fclose(f);
	free(line);
	printf("KUNIT_SWAP END %s %s\n", phase, suite);
}

static void run_suite(const char *suite)
{
	char path[128];
	int fd;

	snprintf(path, sizeof(path), KUNIT_DIR "%s/run", suite);
	fd = open(path, O_WRONLY | O_CLOEXEC);
	if (fd < 0)
		sysfail(path);
	printf("KUNIT_SWAP RUN %s\n", suite);
	/* Synchronous in this kernel. A successful write is not a verdict. */
	if (write(fd, "1", 1) != 1 || close(fd))
		sysfail(path);
	emit_results("live", suite);
}

int main(void)
{
	if (getpid() != 1) {
		fputs("kunit-swap init must run as disposable guest PID 1\n", stderr);
		return 1;
	}
	setvbuf(stdout, NULL, _IOLBF, 0);
	if (mount("proc", "/proc", "proc", 0, NULL))
		sysfail("mount proc");
	check_cmdline();
	if (mount("sysfs", "/sys", "sysfs", 0, NULL) ||
	    mount("devtmpfs", "/dev", "devtmpfs", 0, NULL) ||
	    mount("debugfs", "/sys/kernel/debug", "debugfs", 0, NULL))
		sysfail("mount sysfs/devtmpfs/debugfs");
	check_swaps(false);
	for (size_t i = 0; i < sizeof(suites) / sizeof(suites[0]); i++)
		emit_results("boot", suites[i]);
	setup_swap();
	for (size_t i = 0; i < sizeof(suites) / sizeof(suites[0]); i++)
		run_suite(suites[i]);
	if (swapoff(SWAPDEV))
		sysfail("swapoff " SWAPDEV);
	check_swaps(false);
	puts("KUNIT_SWAP SWAPOFF " SWAPDEV);
	puts("KUNIT_SWAP DONE");
	poweroff();
}
