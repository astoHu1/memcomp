// SPDX-License-Identifier: GPL-2.0
/* S08: disposable PID1, real online non-root source, root reader. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
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
#include <time.h>
#include <unistd.h>

#define ROOT "/sys/fs/cgroup"
#define CG ROOT "/s08-source"
#define MC "/sys/kernel/mm/memcomp"
#define FILEPATH "/s08/payload"
#define BYTES (64U * 1024U * 1024U)
#define PAGE 4096U
#define BAND (80LL * PAGE)
static pid_t worker = -1;
static int authorized, made_cg, mounted_tmp, made_file, child_process;
static unsigned char *reader;
static volatile sig_atomic_t expired;
static long long baseline_data;
struct sample { long long shmem, stored, payload, pool, data; };

static long long now_ms(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return (long long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
static _Noreturn void finish(int rc)
{
	int status = 0, reaped = worker < 0, e = 0;
	long long until = now_ms() + 3000;
	if (reader && munmap(reader, BYTES)) e = errno;
	reader = NULL;
	if (made_file && unlink(FILEPATH) && errno != ENOENT) e = errno;
	if (worker > 0) {
		if (kill(worker, SIGKILL) && errno != ESRCH) e = errno;
		do {
			pid_t p = waitpid(worker, &status, WNOHANG);
			if (p == worker) {
				reaped = 1;
				if (!WIFSIGNALED(status) || WTERMSIG(status) != SIGKILL) e = ECHILD;
				break;
			}
			if (p < 0) { e = errno; break; }
			usleep(20000);
		} while (now_ms() < until);
	}
	if (!reaped) e = ETIMEDOUT;
	int removed = !made_cg || (reaped && !rmdir(CG));
	if (!removed && !e) e = errno ? errno : EBUSY;
	int unmounted = !mounted_tmp || !umount("/s08");
	if (!unmounted && !e) e = errno;
	if (e) rc = 1;
	printf("S08 {\"event\":\"end\",\"rc\":%d,\"worker_reaped\":%d,"
	       "\"worker_status\":%d,\"file_absent\":%d,\"cgroup_removed\":%d,"
	       "\"tmpfs_unmounted\":%d,\"cleanup_errno\":%d}\n",
	       rc, reaped, status, access(FILEPATH, F_OK) && errno == ENOENT,
	       removed, unmounted, e);
	fflush(NULL);
	if (authorized) reboot(RB_POWER_OFF);
	_exit(rc);
}
static _Noreturn void die(const char *s)
{
	fprintf(stderr, "S08-FAIL %s errno=%d (%s)\n", s, errno, strerror(errno));
	fflush(NULL);
	if (child_process) _exit(2);
	finish(1);
}
static void check(int good, const char *s)
{
	if (!good) { if (!errno) errno = EPROTO; die(s); }
}
static void alarmed(int sig) { (void)sig; expired = 1; }
static int get(const char *path, char *b, size_t cap, int optional)
{
	int fd = open(path, O_RDONLY | O_CLOEXEC), saved;
	ssize_t k; size_t n = 0;
	if (fd < 0) {
		saved = errno;
		if (!optional) die(path);
		b[0] = 0; return saved;
	}
	while ((k = read(fd, b + n, cap - n - 1)) > 0) {
		n += (size_t)k;
		check(n < cap - 1, "read overflow");
	}
	check(k == 0 && !close(fd), path);
	b[n] = 0; return 0;
}
static void put(const char *path, const char *s)
{
	int fd = open(path, O_WRONLY | O_CLOEXEC);
	check(fd >= 0, path);
	check(write(fd, s, strlen(s)) == (ssize_t)strlen(s) && !close(fd), path);
}
static long long field(const char *s, const char *key)
{
	size_t n = strlen(key); char *end;
	while (*s) {
		if (!strncmp(s, key, n) && s[n] == ' ') {
			errno = 0; long long v = strtoll(s + n + 1, &end, 10);
			check(!errno && v >= 0 && *end == '\n', key); return v;
		}
		s = strchr(s, '\n'); if (!s) break; s++;
	}
	errno = EPROTO; die(key); return -1;
}
static void quoted(const char *s)
{
	putchar('"');
	for (; *s; s++) {
		unsigned char c = (unsigned char)*s;
		if (c == '"' || c == '\\') printf("\\%c", c);
		else if (c < 32) printf("\\u%04x", c);
		else putchar(c);
	}
	putchar('"');
}
static void raw(const char *key, const char *path, int optional)
{
	char b[16384]; int e = get(path, b, sizeof(b), optional);
	printf(",\"%s\":", key);
	if (e) printf("null"); else quoted(b);
	printf(",\"%s_errno\":%d", key, e);
}
static long long current(const char *path, int optional, int *error)
{
	char b[128], *end;
	*error = get(path, b, sizeof(b), optional);
	if (*error) return -1;
	errno = 0; long long n = strtoll(b, &end, 10);
	check(!errno && n >= 0 && end != b && !strcmp(end, "\n"), path);
	return n;
}
static struct sample snap(const char *phase, int index)
{
	char stat[16384], mc[128], path[128]; int e;
	struct sample s;
	get(CG "/memory.stat", stat, sizeof(stat), 0);
	get(MC "/stat", mc, sizeof(mc), 0);
	check(sscanf(mc, "%lld %lld %lld", &s.stored, &s.payload, &s.pool) == 3,
	      "memcomp stat");
	long long c = current(CG "/memory.current", 0, &e);
	s.shmem = field(stat, "shmem");
	s.data = c - field(stat, "anon") - (field(stat, "file") - s.shmem)
		 - field(stat, "kernel");
	printf("S08 {\"event\":\"sample\",\"phase\":\"%s\",\"index\":%d,"
	       "\"ms\":%lld,\"worker_pid\":%ld,\"current\":%lld,\"stat\":",
	       phase, index, now_ms(), (long)worker, c);
	quoted(stat);
	printf(",\"stored\":%lld,\"payload\":%lld,\"pool\":%lld",
	       s.stored, s.payload, s.pool);
	raw("a_events", CG "/memory.events", 0);
	raw("a_cgroup_events", CG "/cgroup.events", 0);
	raw("a_procs", CG "/cgroup.procs", 0);
	raw("a_cgroup_stat", CG "/cgroup.stat", 0);
	raw("reader_cgroup", "/proc/self/cgroup", 0);
	snprintf(path, sizeof(path), "/proc/%ld/cgroup", (long)worker);
	raw("source_cgroup", path, 0);
	snprintf(path, sizeof(path), "/proc/%ld/stat", (long)worker);
	raw("source_proc_stat", path, 0); /* Alive, not just an allocated CSS. */
	long long root_c = current(ROOT "/memory.current", 1, &e);
	printf(",\"root_current\":");
	if (e) printf("null"); else printf("%lld", root_c);
	printf(",\"root_current_errno\":%d", e);
	raw("root_stat", ROOT "/memory.stat", 1);
	raw("swap_current", CG "/memory.swap.current", 0);
	puts("}");
	return s;
}
static void stable(const char *phase, long long shmem, long long stored)
{
	int good = 0; long long until = now_ms() + 20000;
	for (int i = 0; now_ms() < until && !expired; i++) {
		struct sample s = snap(phase, i);
		int target = s.shmem == shmem && s.stored == stored &&
			llabs(s.data - baseline_data - shmem) <= BAND &&
			(stored ? s.payload > 0 && s.payload < stored && s.pool > 0 :
			 !s.payload && !s.pool);
		good = target ? good + 1 : 0;
		if (good == 3) {
			printf("S08 {\"event\":\"stable\",\"phase\":\"%s\"}\n", phase);
			return;
		}
		usleep(1000000);
	}
	errno = ETIMEDOUT; die(phase);
}
static char receive(int fd)
{
	struct pollfd p = { .fd = fd, .events = POLLIN }; char c;
	int n = poll(&p, 1, child_process ? 150000 : 10000);
	if (n <= 0 || expired || read(fd, &c, 1) != 1)
		die("pipe timeout/EOF");
	return c;
}
static void sendbyte(int fd, char c) { check(write(fd, &c, 1) == 1, "pipe write"); }
static void command(int tx, int rx, char c)
{
	sendbyte(tx, c); check(receive(rx) == c, "worker reply");
}
static void pattern(unsigned char *b, unsigned int page)
{
	uint32_t r = 0x91a2b3c4U ^ (page + 1);
	for (unsigned int i = 0; i < 1024; i++) {
		r ^= r << 13; r ^= r >> 17; r ^= r << 5; b[i] = (unsigned char)r;
	}
	for (unsigned int i = 1; i < 4; i++) memcpy(b + i * 1024, b, 1024);
}
static void child(int rd, int wr)
{
	char b[64]; unsigned char *map = NULL; int fd = -1;
	child_process = 1;
	check(!prctl(PR_SET_PDEATHSIG, SIGKILL) && getppid() == 1, "parent");
	snprintf(b, sizeof(b), "%ld\n", (long)getpid()); put(CG "/cgroup.procs", b);
	volatile char warm[32768];
	for (unsigned int i = 0; i < sizeof(warm); i++) warm[i] = (char)i;
	sendbyte(wr, 'R');
	for (;;) {
		char cmd = receive(rd);
		if (cmd == 'F') {
			fd = open(FILEPATH, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
			check(fd >= 0 && !ftruncate(fd, BYTES), "create tmpfs");
			map = mmap(NULL, BYTES, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
			check(map != MAP_FAILED && !madvise(map, BYTES, MADV_NOHUGEPAGE),
			      "source mmap/nohugepage");
			for (unsigned int i = 0; i < BYTES / PAGE; i++) pattern(map + i * PAGE, i);
		} else if (cmd == 'C') {
			for (int i = 0; i < 4; i++) {
				check(!madvise(map, BYTES, MADV_PAGEOUT), "source pageout");
				usleep(250000);
			}
		} else if (cmd == 'U') {
			check(!munmap(map, BYTES) && !close(fd), "source unmap/close");
			map = NULL; fd = -1;
		} else { errno = EPROTO; die("worker command"); }
		sendbyte(wr, cmd);
	}
}
/* mincore describes backing availability; pagemap describes reader PTEs. */
static void reader_fault_ready(unsigned char *map, size_t bytes)
{
	unsigned char vec[BYTES / PAGE];
	uint64_t entries[256];
	unsigned int pages = bytes / PAGE, incore = 0, present = 0, swapped = 0;
	check(bytes && bytes <= BYTES && !(bytes % PAGE), "reader range");
	/* Shared file data stays intact; only this reader's mappings are zapped. */
	check(!madvise(map, bytes, MADV_DONTNEED), "reader dontneed");
	check(!mincore(map, bytes, vec), "reader mincore");
	int fd = open("/proc/self/pagemap", O_RDONLY | O_CLOEXEC);
	check(fd >= 0, "reader pagemap open");
	for (unsigned int i = 0; i < pages;) {
		unsigned int n = pages - i < 256 ? pages - i : 256;
		off_t offset = ((uintptr_t)map / PAGE + i) * sizeof(uint64_t);
		check(pread(fd, entries, n * sizeof(uint64_t), offset) ==
		      (ssize_t)(n * sizeof(uint64_t)), "reader pagemap read");
		for (unsigned int j = 0; j < n; j++) {
			incore += !!(vec[i + j] & 1);
			present += !!(entries[j] & (1ULL << 63));
			swapped += !!(entries[j] & (1ULL << 62));
		}
		i += n;
	}
	check(!close(fd), "reader pagemap close");
	printf("S08 {\"event\":\"reader_fault_ready\",\"pages\":%u,\"dontneed\":1,"
	       "\"mincore_incore\":%u,\"pte_present\":%u,\"pte_swap\":%u}\n",
	       pages, incore, present, swapped);
	check(incore == pages && !present && !swapped, "reader fault readiness");
}
#include "s08_major.h"

int main(void)
{
	char b[16384]; int tx[2], rx[2]; struct utsname u; struct stat inode;
	if (getpid() != 1 || geteuid() != 0) {
		fputs("S08-REFUSED requires disposable guest PID1/root\n", stderr); return 77;
	}
	setvbuf(stdout, NULL, _IOLBF, 0);
	check(!mount("proc", "/proc", "proc", 0, NULL), "proc");
	get("/proc/cmdline", b, sizeof(b), 0);
	char *save, *token; int optin = 0;
	for (token = strtok_r(b, " \n", &save); token; token = strtok_r(NULL, " \n", &save))
		if (!strcmp(token, "memcg_s08.disposable=1")) optin = 1;
	check(optin, "opt-in");
	authorized = 1;
	struct sigaction sa = { .sa_handler = alarmed };
	sigemptyset(&sa.sa_mask); check(!sigaction(SIGALRM, &sa, NULL), "alarm handler");
	signal(SIGPIPE, SIG_IGN); alarm(150);
	check(!mount("sysfs", "/sys", "sysfs", 0, NULL) &&
	      !mount("devtmpfs", "/dev", "devtmpfs", 0, NULL), "mounts");
	check(!mkdir(ROOT, 0755) || errno == EEXIST, "cgroup directory");
	check(!mount("cgroup2", ROOT, "cgroup2", 0, NULL), "cgroup2");
	get("/proc/swaps", b, sizeof(b), 0);
	char *line = strchr(b, '\n'); check(line && !line[1], "no guest swap");
	printf("S08 {\"event\":\"environment\",\"swaps\":"); quoted(b);
	check(!uname(&u), "uname");
	printf(",\"release\":"); quoted(u.release);
	get("/binding.json", b, sizeof(b), 0);
	printf(",\"binding\":"); quoted(b);
	get(MC "/compressor", b, sizeof(b), 0);
	check(!strcmp(b, "zstd\n") && sysconf(_SC_PAGESIZE) == PAGE &&
	      sysconf(_SC_NPROCESSORS_ONLN) == 1, "zstd/4K/single CPU");
	printf(",\"compressor\":"); quoted(b);
	printf(",\"page_size\":%ld,\"cpus\":%ld,\"reader_pid\":%ld,\"reader_euid\":%ld}\n",
	       sysconf(_SC_PAGESIZE), sysconf(_SC_NPROCESSORS_ONLN),
	       (long)getpid(), (long)geteuid());
	put(ROOT "/cgroup.subtree_control", "+memory\n");
	check(!mkdir(CG, 0755), "new source cgroup"); made_cg = 1;
	check(!mkdir("/s08", 0700), "tmpfs directory");
	check(!mount("tmpfs", "/s08", "tmpfs", 0, "size=96m,huge=never"), "tmpfs");
	mounted_tmp = 1;
	check(!pipe(tx) && !pipe(rx), "pipes");
	worker = fork(); check(worker >= 0, "fork");
	if (!worker) { close(tx[1]); close(rx[0]); child(tx[0], rx[1]); _exit(2); }
	close(tx[0]); close(rx[1]); check(receive(rx[0]) == 'R', "ready");
	sleep(3);
	struct sample base = snap("baseline", -1);
	check(!base.shmem && !base.stored && !base.payload && !base.pool, "empty baseline");
	baseline_data = base.data; stable("baseline", 0, 0);
	made_file = 1; command(tx[1], rx[0], 'F');
	check(!stat(FILEPATH, &inode) && inode.st_size == BYTES, "payload inode");
	printf("S08 {\"event\":\"file\",\"dev\":%ju,\"ino\":%ju,\"bytes\":%u}\n",
	       (uintmax_t)inode.st_dev, (uintmax_t)inode.st_ino, BYTES);
	stable("filled", BYTES, 0);
	command(tx[1], rx[0], 'C'); command(tx[1], rx[0], 'U');
	stable("compressed", 0, BYTES);
	int fd = open(FILEPATH, O_RDONLY | O_CLOEXEC);
	check(fd >= 0, "root reader open");
	struct stat read_inode;
	check(!fstat(fd, &read_inode) && read_inode.st_dev == inode.st_dev &&
	      read_inode.st_ino == inode.st_ino && read_inode.st_size == BYTES,
	      "same payload inode");
	reader = mmap(NULL, BYTES, PROT_READ, MAP_SHARED, fd, 0);
	if (reader == MAP_FAILED) { reader = NULL; die("root reader mmap"); } check(!close(fd), "reader close");
	unsigned char expected[PAGE];
	major_prewarm(expected);
	reader_fault_ready(reader, BYTES);
	major_collect("before", major_capture());
	for (unsigned int i = 0; i < BYTES / PAGE; i++) {
		pattern(expected, i);
		if (memcmp(reader + i * PAGE, expected, PAGE)) {
			for (unsigned int j = 0; j < PAGE; j++) {
				if (reader[i * PAGE + j] == expected[j]) continue;
				fprintf(stderr, "S08-FAIL content page=%u offset=%u expected=%u actual=%u\n",
					i, j, expected[j], reader[i * PAGE + j]); break;
			}
			errno = EPROTO; die("content");
		}
	}
	struct major_sample immediate = major_capture();
	puts("S08 {\"event\":\"content\",\"bytes\":67108864}");
	major_collect("after", immediate);
	stable("readback", BYTES, 0);
	check(!munmap(reader, BYTES), "root unmap"); reader = NULL;
	check(!unlink(FILEPATH), "unlink"); made_file = 0;
	stable("released", 0, 0);
	finish(0); return 1;
}
