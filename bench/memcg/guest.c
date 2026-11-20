// SPDX-License-Identifier: GPL-2.0
/* Minimal disposable-VM memcg accounting collector. No trace/new ABI. */
#define _GNU_SOURCE
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
#include <sys/utsname.h>
#include <sys/wait.h>
#include <unistd.h>

#define CG "/sys/fs/cgroup/minimal"
#define MC "/sys/kernel/mm/memcomp"
#define PAGES 16384U
#define BYTES (PAGES * 4096U)
static int authorized;
static pid_t worker = -1;

static void finish(int rc)
{
	if (worker > 0) {
		kill(worker, SIGKILL);
		waitpid(worker, NULL, 0);
	}
	printf("MEMCG-END rc=%d\n", rc);
	fflush(NULL);
	if (authorized)
		reboot(RB_POWER_OFF);
	_exit(rc);
}
static void die(const char *s)
{
	fprintf(stderr, "FAIL %s: %s\n", s, strerror(errno));
	finish(1);
}
static void put(const char *path, const char *s)
{
	int fd = open(path, O_WRONLY | O_CLOEXEC);
	size_t n = strlen(s);
	if (fd < 0 || write(fd, s, n) != (ssize_t)n || close(fd))
		die(path);
}
static size_t get(const char *path, char *s, size_t cap)
{
	int fd = open(path, O_RDONLY | O_CLOEXEC);
	size_t n = 0;
	ssize_t k;
	if (fd < 0)
		die(path);
	while ((k = read(fd, s + n, cap - n - 1)) > 0) {
		n += (size_t)k;
		if (n == cap - 1) { errno = EOVERFLOW; die(path); }
	}
	if (k < 0 || close(fd))
		die(path);
	s[n] = 0;
	return n;
}
static uint64_t number(const char *path)
{
	char b[128], *end;
	uint64_t n;
	get(path, b, sizeof(b));
	errno = 0;
	n = strtoull(b, &end, 10);
	if (errno || end == b || strcmp(end, "\n")) {
		errno = EPROTO; die(path);
	}
	return n;
}
static uint64_t field(const char *s, const char *key)
{
	size_t n = strlen(key);
	while (*s) {
		if (!strncmp(s, key, n) && s[n] == ' ')
			return strtoull(s + n + 1, NULL, 10);
		s = strchr(s, '\n');
		if (!s) break;
		s++;
	}
	errno = EPROTO; die(key);
	return 0;
}
static void snap(const char *phase)
{
	char b[32768], mc[128];
	uint64_t stored, payload, pool;
	get(CG "/memory.stat", b, sizeof(b));
	get(MC "/stat", mc, sizeof(mc));
	if (sscanf(mc, "%" SCNu64 " %" SCNu64 " %" SCNu64,
		   &stored, &payload, &pool) != 3) {
		errno = EPROTO; die("memcomp stat");
	}
	printf("SAMPLE {\"phase\":\"%s\",\"current\":%" PRIu64
	       ",\"anon\":%" PRIu64 ",\"file\":%" PRIu64
	       ",\"kernel\":%" PRIu64 ",\"pagetables\":%" PRIu64
	       ",\"kernel_stack\":%" PRIu64 ",\"slab\":%" PRIu64
	       ",\"percpu\":%" PRIu64 ",\"vmalloc\":%" PRIu64
	       ",\"swap\":%" PRIu64 ",\"stored\":%" PRIu64
	       ",\"payload\":%" PRIu64 ",\"pool\":%" PRIu64 "}\n",
	       phase, number(CG "/memory.current"), field(b, "anon"),
	       field(b, "file"), field(b, "kernel"), field(b, "pagetables"),
	       field(b, "kernel_stack"), field(b, "slab"), field(b, "percpu"),
	       field(b, "vmalloc"), number(CG "/memory.swap.current"),
	       stored, payload, pool);
	printf("RAW phase=%s memory.stat\n%s", phase, b);
	get(CG "/memory.events", b, sizeof(b));
	printf("RAW phase=%s memory.events\n%s", phase, b);
	if (field(b, "oom") || field(b, "oom_kill") || field(b, "max")) {
		errno = EPROTO; die("unexpected memcg event");
	}
	get(CG "/cgroup.procs", b, sizeof(b));
	printf("RAW phase=%s cgroup.procs\n%s", phase, b);
	fflush(NULL);
}
static void byte_write(int fd, char c)
{
	if (write(fd, &c, 1) != 1) die("pipe write");
}
static char byte_read(int fd)
{
	char c;
	if (read(fd, &c, 1) != 1) die("pipe read");
	return c;
}
static void child(int rd, int wr)
{
	unsigned char *map = NULL;
	char b[64], cmd;
	uint32_t rng = 0x91a2b3c4U;
	unsigned int i, j, k;
	if (prctl(PR_SET_PDEATHSIG, SIGKILL) || getppid() != 1)
		_exit(2);
	snprintf(b, sizeof(b), "%ld\n", (long)getpid());
	put(CG "/cgroup.procs", b);
	/* Warm the fixed-size worker stack before baseline. */
	{ volatile char warm[32768];
	  for (i = 0; i < sizeof(warm); i++) warm[i] = (char)i;
	}
	byte_write(wr, 'R');
	while ((cmd = byte_read(rd)) != 'X') {
		if (cmd == 'F') {
			map = mmap(NULL, BYTES, PROT_READ | PROT_WRITE,
				   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
			if (map == MAP_FAILED || madvise(map, BYTES, MADV_NOHUGEPAGE))
				die("map/nohugepage");
			for (i = 0; i < PAGES; i++) {
				for (j = 0; j < 1024; j++) {
					rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5;
					map[i * 4096U + j] = (unsigned char)rng;
				}
				for (k = 1; k < 4; k++)
					memcpy(map + i * 4096U + k * 1024U,
					       map + i * 4096U, 1024U);
			}
		} else if (cmd == 'C') {
			for (k = 0; k < 4; k++) {
				if (madvise(map, BYTES, MADV_PAGEOUT)) die("pageout");
				usleep(250000);
			}
		} else if (cmd == 'U') {
			if (munmap(map, BYTES)) die("munmap");
			map = NULL;
		} else {
			errno = EPROTO; die("worker command");
		}
		byte_write(wr, cmd);
	}
	_exit(0);
}
static void command(int fd, int reply, char c)
{
	byte_write(fd, c);
	if (byte_read(reply) != c) { errno = EPROTO; die("worker reply"); }
}
int main(void)
{
	char b[8192];
	int tx[2], rx[2], status;
	struct utsname u;
	if (getpid() != 1 || geteuid() != 0) {
		fputs("FAIL guest requires root PID 1 in a disposable VM\n", stderr);
		return 77;
	}
	setvbuf(stdout, NULL, _IOLBF, 0);
	if (mount("proc", "/proc", "proc", 0, NULL)) die("mount proc");
	get("/proc/cmdline", b, sizeof(b));
	if (!strstr(b, " memcg_minimal.disposable=1")) die("guest opt-in");
	authorized = 1;
	alarm(120);
	if (mount("sysfs", "/sys", "sysfs", 0, NULL) ||
	    mount("devtmpfs", "/dev", "devtmpfs", 0, NULL)) die("mount");
	if (mkdir("/sys/fs/cgroup", 0755) && errno != EEXIST) die("cgroup dir");
	if (mount("cgroup2", "/sys/fs/cgroup", "cgroup2", 0, NULL)) die("cgroup2");
	get("/proc/swaps", b, sizeof(b));
	if (strchr(b, '\n') && strchr(strchr(b, '\n') + 1, '\n')) {
		errno = EPROTO; die("guest swap must be absent");
	}
	printf("RAW proc.swaps\n%s", b);
	get("/binding.json", b, sizeof(b)); printf("BINDING %s", b);
	if (uname(&u)) die("uname");
	printf("KERNEL release=%s\n", u.release);
	get(MC "/compressor", b, sizeof(b)); printf("COMPRESSOR %s", b);
	if (strcmp(b, "zstd\n")) { errno = EPROTO; die("stock zstd required"); }
	if (sysconf(_SC_PAGESIZE) != 4096 || sysconf(_SC_NPROCESSORS_ONLN) != 1)
		{ errno = EPROTO; die("page size/cpu count"); }
	put("/sys/fs/cgroup/cgroup.subtree_control", "+memory\n");
	if (mkdir(CG, 0755) || pipe(tx) || pipe(rx)) die("setup worker");
	worker = fork();
	if (worker == 0) {
		worker = -1; close(tx[1]); close(rx[0]); child(tx[0], rx[1]);
	}
	if (worker < 0) die("fork");
	printf("WORKER pid=%ld pages=%u\n", (long)worker, PAGES);
	close(tx[0]); close(rx[1]);
	if (byte_read(rx[0]) != 'R') die("ready");
	sleep(3); snap("baseline");
	command(tx[1], rx[0], 'F'); sleep(3); snap("filled");
	command(tx[1], rx[0], 'C'); sleep(3); snap("compressed");
	command(tx[1], rx[0], 'U'); sleep(3); snap("released1");
	sleep(3); snap("released2"); sleep(3); snap("released3");
	byte_write(tx[1], 'X');
	if (waitpid(worker, &status, 0) != worker || !WIFEXITED(status) ||
	    WEXITSTATUS(status)) die("worker status");
	worker = -1;
	if (rmdir(CG)) die("remove empty cgroup");
	puts("MEMCG-COLLECTED");
	finish(0);
	return 1;
}
