// SPDX-License-Identifier: GPL-2.0-only
/* Disposable comparison-VM PID 1; see guest.md. No project headers. */
#define _GNU_SOURCE
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <linux/fs.h>
#include <linux/magic.h>
#include <poll.h>
#include <stddef.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/swap.h>
#include <sys/sysinfo.h>
#include <sys/types.h>
#include <sys/utsname.h>
#include <termios.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define MAX_ARGS 64
#define ARG_CHARS 4096
#define NS_SECOND UINT64_C(1000000000)
#define SNAP_LIMIT 262144U
#define MEMORY_PROBE_MAX_RETRIES 16U
#define W4_PAGEOUT_PHASE "pageout_returned"
#define W4_CGROUP "/sys/fs/cgroup/compare-w4"

static char backend[64], algorithm[32], label[16];
static char *command[MAX_ARGS + 1], *command_data;
static uint64_t timeout_seconds = 300, run_deadline;
static size_t page_bytes;
static bool telemetry_error, control_error, writeback_done, clock_error;
static bool snapshot_vmstat_refresh;
static bool snapshot_memory_consistent;
static volatile sig_atomic_t stop_signal;
static pid_t workload = -1;
static bool workload_reaped, w4_cgroup_created;
static int workload_status;
static FILE *raw_stream;
static int data_fd = -1;
static uint64_t data_bytes_written, data_capacity;
static bool data_failed;
#define DATA_HEADER_BYTES 512U
#define DATA_MAX_BYTES (256U * 1024U * 1024U)

/* v2 memory snapshots never allocate or flush the data disk in a bracket. */
#define MEMORY_CAPTURE_BYTES (2U * 1024U * 1024U)
#define DATA_DIRECT_BYTES 65536U
static unsigned char memory_capture_buffer[MEMORY_CAPTURE_BYTES] __attribute__((aligned(4096)));
static char memory_capture_read_buffer[SNAP_LIMIT + 1] __attribute__((aligned(4096)));
static char raw_output_buffer[65536] __attribute__((aligned(4096)));
static unsigned char data_direct_buffer[DATA_DIRECT_BYTES] __attribute__((aligned(4096)));
static unsigned char data_header[DATA_HEADER_BYTES] __attribute__((aligned(4096)));
static bool memory_buffer_ready, memory_capture_active, memory_capture_overflow, data_direct;
static size_t memory_capture_used, data_direct_used, data_direct_alignment = DATA_HEADER_BYTES;
static uint64_t data_direct_offset = DATA_HEADER_BYTES;
struct snapshot_dir {
	DIR *legacy;
	int fd;
	size_t used, at;
	char buffer[8192];
	struct dirent entry;
};
static struct snapshot_dir snapshot_directory;

static void __attribute__((noinline)) memory_stack_prefault(void)
{
	volatile unsigned char stack[128U * 1024U];

	for (size_t i = 0; i < sizeof(stack); i += 4096)
		stack[i] = 0;
	stack[sizeof(stack) - 1] = 0;
}

static int memory_buffer_prepare(void)
{
	if (memory_buffer_ready)
		return 0;
	memory_stack_prefault();
	memset(memory_capture_buffer, 0, sizeof(memory_capture_buffer));
	memset(memory_capture_read_buffer, 0, sizeof(memory_capture_read_buffer));
	memset(raw_output_buffer, 0, sizeof(raw_output_buffer));
	memset(data_direct_buffer, 0, sizeof(data_direct_buffer));
	memset(data_header, 0, sizeof(data_header));
	memset(&snapshot_directory, 0, sizeof(snapshot_directory));
	if (mlock(memory_capture_buffer, sizeof(memory_capture_buffer)) ||
	    mlock(memory_capture_read_buffer, sizeof(memory_capture_read_buffer)) ||
	    mlock(raw_output_buffer, sizeof(raw_output_buffer)) ||
	    mlock(data_direct_buffer, sizeof(data_direct_buffer)) ||
	    mlock(data_header, sizeof(data_header)) ||
	    mlock(&snapshot_directory, sizeof(snapshot_directory)))
		return -1;
	memory_buffer_ready = true;
	return 0;
}

/* SHA-256 hashes the bytes accepted by the raw-data writer, not a receipt. */
struct sha256 {
	uint32_t h[8];
	uint64_t bytes;
	unsigned char block[64];
	size_t used;
};
static struct sha256 data_hash = { .h = {
	0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
	0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
} };
static uint32_t ror32(uint32_t x, unsigned int n)
{
	return (x >> n) | (x << (32 - n));
}
static void sha256_block(struct sha256 *s, const unsigned char *p)
{
	static const uint32_t k[64] = {
		0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
		0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
		0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
		0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
		0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
		0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
		0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
		0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
		0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
		0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
		0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
		0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
		0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
		0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
		0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
		0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
	};
	uint32_t w[64], a = s->h[0], b = s->h[1], c = s->h[2], d = s->h[3];
	uint32_t e = s->h[4], f = s->h[5], g = s->h[6], h = s->h[7];

	for (unsigned int i = 0; i < 16; i++)
		w[i] = (uint32_t)p[i * 4] << 24 | (uint32_t)p[i * 4 + 1] << 16 |
		       (uint32_t)p[i * 4 + 2] << 8 | p[i * 4 + 3];
	for (unsigned int i = 16; i < 64; i++) {
		uint32_t x = w[i - 15], y = w[i - 2];

		w[i] = w[i - 16] + (ror32(x, 7) ^ ror32(x, 18) ^ (x >> 3)) +
		       w[i - 7] + (ror32(y, 17) ^ ror32(y, 19) ^ (y >> 10));
	}
	for (unsigned int i = 0; i < 64; i++) {
		uint32_t t1 = h + (ror32(e, 6) ^ ror32(e, 11) ^ ror32(e, 25)) +
			     ((e & f) ^ (~e & g)) + k[i] + w[i];
		uint32_t t2 = (ror32(a, 2) ^ ror32(a, 13) ^ ror32(a, 22)) +
			     ((a & b) ^ (a & c) ^ (b & c));

		h = g; g = f; f = e; e = d + t1;
		d = c; c = b; b = a; a = t1 + t2;
	}
	s->h[0] += a; s->h[1] += b; s->h[2] += c; s->h[3] += d;
	s->h[4] += e; s->h[5] += f; s->h[6] += g; s->h[7] += h;
}
static void sha256_update(struct sha256 *s, const void *input, size_t length)
{
	const unsigned char *p = input;

	s->bytes += length;
	while (length) {
		size_t n = sizeof(s->block) - s->used;

		if (n > length)
			n = length;
		memcpy(s->block + s->used, p, n);
		s->used += n; p += n; length -= n;
		if (s->used == sizeof(s->block)) {
			sha256_block(s, s->block);
			s->used = 0;
		}
	}
}
static void sha256_final(struct sha256 *s, char hex[65])
{
	uint64_t bits = s->bytes * 8;
	unsigned char pad[64] = { 0x80 }, length[8];
	size_t n = s->used < 56 ? 56 - s->used : 120 - s->used;

	for (unsigned int i = 0; i < 8; i++)
		length[7 - i] = (unsigned char)(bits >> (8 * i));
	sha256_update(s, pad, n);
	sha256_update(s, length, sizeof(length));
	for (unsigned int i = 0; i < 8; i++)
		snprintf(hex + i * 8, 9, "%08" PRIx32, s->h[i]);
}
/* Direct chunks and their offsets are logical-sector aligned; never fall back. */
static int data_direct_flush(bool final)
{
	size_t bytes = data_direct_used, done = 0;

	if (data_failed) {
		errno = EIO;
		return -1;
	}
	if (!data_direct_alignment || data_direct_alignment > DATA_HEADER_BYTES ||
	    bytes > sizeof(data_direct_buffer)) {
		errno = EINVAL;
		data_failed = true;
		return -1;
	}
	if (!bytes)
		return 0;
	if (final) {
		bytes += (data_direct_alignment - bytes % data_direct_alignment) % data_direct_alignment;
		memset(data_direct_buffer + data_direct_used, 0, bytes - data_direct_used);
	}
	if (memory_capture_active || !data_direct_alignment || bytes % data_direct_alignment ||
	    data_direct_offset % data_direct_alignment ||
	    (uintptr_t)data_direct_buffer % data_direct_alignment ||
	    data_direct_offset > data_capacity || bytes > data_capacity - data_direct_offset) {
		errno = EINVAL;
		data_failed = true;
		return -1;
	}
	while (done < bytes) {
		ssize_t n = pwrite(data_fd, data_direct_buffer + done, bytes - done,
				   (off_t)(data_direct_offset + done));

		if (n < 0 && errno == EINTR)
			continue;
		if (n > 0 && (size_t)n <= bytes - done) {
			size_t logical = done < data_direct_used ? data_direct_used - done : 0;

			if (logical > (size_t)n)
				logical = (size_t)n;
			sha256_update(&data_hash, data_direct_buffer + done, logical);
			data_bytes_written += logical;
		}
		if (n <= 0 || (size_t)n > bytes - done || (size_t)n % data_direct_alignment) {
			errno = n < 0 ? errno : EIO;
			data_failed = true;
			return -1;
		}
		done += (size_t)n;
	}
	data_direct_offset += bytes;
	data_direct_used = 0;
	return 0;
}

/* Buffered stdio writes and CSV share one ordered, bounded block stream. */
static ssize_t data_write(void *cookie, const char *buf, size_t length)
{
	size_t done = 0;
	uint64_t accepted = data_bytes_written;

	(void)cookie;
	if (memory_capture_active) {
		if (memory_capture_overflow || length > sizeof(memory_capture_buffer) - memory_capture_used) {
			errno = EOVERFLOW;
			memory_capture_overflow = true;
			return 0;
		}
		memcpy(memory_capture_buffer + memory_capture_used, buf, length);
		memory_capture_used += length;
		return (ssize_t)length;
	}
	if (data_direct && accepted <= UINT64_MAX - data_direct_used)
		accepted += data_direct_used;
	else if (data_direct)
		data_failed = true;
	if (data_failed || data_capacity < DATA_HEADER_BYTES ||
	    accepted > data_capacity - DATA_HEADER_BYTES ||
	    length > data_capacity - DATA_HEADER_BYTES - accepted) {
		errno = ENOSPC;
		data_failed = true;
		return 0;
	}
	if (data_direct) {
		while (done < length) {
			size_t n = sizeof(data_direct_buffer) - data_direct_used;

			if (n > length - done)
				n = length - done;
			memcpy(data_direct_buffer + data_direct_used, buf + done, n);
			data_direct_used += n;
			done += n;
			if (data_direct_used == sizeof(data_direct_buffer) && data_direct_flush(false))
				return (ssize_t)(done - n);
		}
		return (ssize_t)done;
	}
	while (done < length) {
		ssize_t n = write(data_fd, buf + done, length - done);

		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0) {
			data_failed = true;
			return (ssize_t)done;
		}
		sha256_update(&data_hash, buf + done, (size_t)n);
		data_bytes_written += (size_t)n;
		done += (size_t)n;
	}
	return (ssize_t)done;
}

static int memory_buffer_flush(void)
{
	int error = fflush(raw_stream);
	size_t used = memory_capture_used;

	memory_capture_active = false;
	memory_capture_used = 0;
	if (error || memory_capture_overflow) {
		clearerr(raw_stream);
		errno = memory_capture_overflow ? EOVERFLOW : EIO;
		return -1; /* Discard the whole failed attempt, never publish a truncation. */
	}
	return data_write(NULL, (const char *)memory_capture_buffer, used) == (ssize_t)used ? 0 : -1;
}

/* getdents uses resident fixed storage, unlike opendir's malloc-backed DIR. */
static struct snapshot_dir *snapshot_opendir(const char *path)
{
	struct snapshot_dir *dir = &snapshot_directory;

	dir->at = dir->used = 0;
	dir->legacy = NULL;
	dir->fd = -1;
	if (!memory_capture_active) {
		dir->legacy = opendir(path);
		return dir->legacy ? dir : NULL;
	}
	dir->fd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
	return dir->fd >= 0 ? dir : NULL;
}
static struct dirent *snapshot_readdir(struct snapshot_dir *dir)
{
	struct kernel_dirent64 {
		uint64_t ino;
		int64_t off;
		unsigned short reclen;
		unsigned char type;
		char name[];
	};
	struct kernel_dirent64 *entry;
	size_t prefix = offsetof(struct kernel_dirent64, name), bytes;

	if (dir->legacy)
		return readdir(dir->legacy);
	if (dir->at == dir->used) {
		ssize_t n;

		do {
			n = syscall(SYS_getdents64, dir->fd, dir->buffer, sizeof(dir->buffer));
		} while (n < 0 && errno == EINTR && !stop_signal);
		if (n <= 0 || (size_t)n > sizeof(dir->buffer)) {
			if (!n)
				errno = 0;
			else
				telemetry_error = true;
			return NULL;
		}
		dir->used = (size_t)n;
		dir->at = 0;
	}
	bytes = dir->used - dir->at;
	entry = (struct kernel_dirent64 *)(dir->buffer + dir->at);
	if (bytes <= prefix || entry->reclen <= prefix || entry->reclen > bytes ||
	    entry->reclen % _Alignof(struct kernel_dirent64) ||
	    !memchr(entry->name, 0, entry->reclen - prefix) ||
	    strlen(entry->name) >= sizeof(dir->entry.d_name)) {
		errno = EPROTO;
		telemetry_error = true;
		return NULL;
	}
	memcpy(dir->entry.d_name, entry->name, strlen(entry->name) + 1);
	dir->at += entry->reclen;
	return &dir->entry;
}
static void snapshot_closedir(struct snapshot_dir *dir)
{
	if (dir->legacy)
		closedir(dir->legacy);
	else if (close(dir->fd))
		telemetry_error = true;
}

static uint64_t monotonic_ns(void)
{
	struct timespec ts;

	if (clock_gettime(CLOCK_MONOTONIC, &ts)) {
		clock_error = true;
		control_error = true;
		return 0;
	}
	return (uint64_t)ts.tv_sec * NS_SECOND + (uint64_t)ts.tv_nsec;
}
static void signal_stop(int signo)
{
	stop_signal = signo;
}
static int fail(const char *what)
{
	if (raw_stream != stderr)
		fprintf(stderr, "COMPARE_ERROR operation=%s errno=%d\n", what, errno);
	fprintf(raw_stream, "COMPARE_ERROR monotonic_ns=%" PRIu64
		" operation=%s errno=%d message=%s\n",
		monotonic_ns(), what, errno, strerror(errno));
	return -1;
}
static void close_fd(int *fd)
{
	if (*fd >= 0) {
		close(*fd);
		*fd = -1;
	}
}
static int number(const char *s, uint64_t max, uint64_t *value)
{
	char *end;
	const char *p;
	unsigned long long v;

	if (!s || !*s)
		return -1;
	for (p = s; *p; p++)
		if (*p < '0' || *p > '9')
			return -1;
	errno = 0;
	v = strtoull(s, &end, 10);
	if (errno || *end || v > max)
		return -1;
	*value = (uint64_t)v;
	return 0;
}
/* Bounded reads; initramfs configuration additionally must be regular files. */
static int read_text(const char *path, char *buf, size_t capacity, bool regular)
{
	struct stat st;
	size_t used = 0;
	int fd, saved = 0;

	fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
	if (fd < 0)
		return -1;
	if (fstat(fd, &st) || (regular && !S_ISREG(st.st_mode))) {
		saved = errno ? errno : EINVAL;
		goto out;
	}
	while (used < capacity - 1) {
		ssize_t n = read(fd, buf + used, capacity - 1 - used);

		if (!n)
			break;
		if (n < 0) {
			if (errno == EINTR && !stop_signal)
				continue;
			saved = errno;
			goto out;
		}
		if (memchr(buf + used, '\0', (size_t)n)) {
			saved = EINVAL;
			goto out;
		}
		used += (size_t)n;
	}
	if (used == capacity - 1) {
		char extra;
		ssize_t n = read(fd, &extra, 1);

		if (n) {
			saved = n < 0 ? errno : E2BIG;
			goto out;
		}
	}
	buf[used] = '\0';
	/* Text controls/configuration have at most one trailing newline. */
	if (strcmp(path, "/command") && used && buf[used - 1] == '\n')
		buf[used - 1] = '\0';
out:
	close(fd);
	if (saved) {
		errno = saved;
		return -1;
	}
	return 0;
}
static int write_text(const char *path, const char *value)
{
	int fd, saved;
	size_t length = strlen(value);
	ssize_t n;

	fd = open(path, O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0)
		return -1;
	do {
		n = write(fd, value, length);
	} while (n < 0 && errno == EINTR && !stop_signal);
	saved = n == (ssize_t)length ? 0 : n < 0 ? errno : EIO;
	if (close(fd) && !saved)
		saved = errno;
	if (saved) {
		errno = saved;
		return -1;
	}
	return 0;
}
static bool token(const char *s, const char *wanted)
{
	size_t n = strlen(wanted);

	while (*s) {
		const char *begin;

		while (isspace((unsigned char)*s))
			s++;
		begin = s;
		while (*s && !isspace((unsigned char)*s))
			s++;
		if ((size_t)(s - begin) == n && !memcmp(begin, wanted, n))
			return true;
	}
	return false;
}
static bool selected(const char *actual, const char *wanted)
{
	char bracket[64];

	snprintf(bracket, sizeof(bracket), "[%s]", wanted);
	return !strcmp(actual, wanted) || token(actual, bracket);
}
static int regular_marker(void)
{
	struct stat st;
	int fd;

	if (getpid() != 1) {
		errno = EPERM;
		return -1;
	}
	fd = open("/COMPARE_DISPOSABLE_GUEST", O_PATH | O_NOFOLLOW | O_CLOEXEC);
	if (fd < 0)
		return -1;
	if (fstat(fd, &st) || !S_ISREG(st.st_mode)) {
		close(fd);
		errno = EINVAL;
		return -1;
	}
	close(fd);
	return 0;
}
static int load_configuration(void)
{
	char buf[64];
	bool known;
	long pg;
	size_t length, offset = 0;
	unsigned int argc = 0;

	if (read_text("/backend", backend, sizeof(backend), true) ||
	    read_text("/algorithm", algorithm, sizeof(algorithm), true) ||
	    read_text("/workload-label", label, sizeof(label), true))
		return fail("read configuration");
	known = !strcmp(backend, "memcomp-noswap") || !strcmp(backend, "memcomp-swap") ||
		!strcmp(backend, "zram") || !strcmp(backend, "zram-writeback") ||
		!strcmp(backend, "zswap") || !strcmp(backend, "swap") ||
		!strcmp(backend, "zswap-crosscheck");
	if (!known || (strcmp(algorithm, "zstd") && strcmp(algorithm, "lz4") &&
		       strcmp(algorithm, "none")) ||
	    (!strcmp(backend, "swap") != !strcmp(algorithm, "none"))) {
		errno = EINVAL;
		return fail("backend/algorithm");
	}
	if (read_text("/timeout-seconds", buf, sizeof(buf), true)) {
		if (errno != ENOENT)
			return fail("timeout-seconds");
	} else if (number(buf, 86400, &timeout_seconds) || !timeout_seconds) {
		errno = EINVAL;
		return fail("timeout-seconds range");
	}
	if (read_text("/snapshot-vmstat-refresh", buf, sizeof(buf), true)) {
		if (errno != ENOENT)
			return fail("snapshot-vmstat-refresh");
	} else {
		uint64_t value;

		if (number(buf, 1, &value)) {
			errno = EINVAL;
			return fail("snapshot-vmstat-refresh range");
		}
		if (value && strcmp(label, "w1")) {
			errno = EINVAL;
			return fail("snapshot vmstat refresh requires W1");
		}
		snapshot_vmstat_refresh = value;
	}
	pg = sysconf(_SC_PAGESIZE);
	if (pg < 2048 || pg > 1048576) {
		errno = EINVAL;
		return fail("native page size");
	}
	page_bytes = (size_t)pg;
	command_data = malloc(MAX_ARGS * (ARG_CHARS + 1U) + 1U);
	if (!command_data)
		return fail("command allocation");
	if (read_text("/command", command_data,
		      MAX_ARGS * (ARG_CHARS + 1U) + 1U, true))
		return fail("command input");
	length = strlen(command_data);
	while (offset < length) {
		char *arg = command_data + offset;
		char *end = strchr(arg, '\n');
		size_t count = end ? (size_t)(end - arg) : length - offset;

		if (argc == MAX_ARGS || count > ARG_CHARS || memchr(arg, '\r', count)) {
			errno = E2BIG;
			return fail("command argument bounds");
		}
		command[argc++] = arg;
		if (end)
			*end = '\0';
		offset += count + (end ? 1U : 0U);
	}
	command[argc] = NULL;
	if (!argc || (strcmp(command[0], "/refault") &&
		      strcmp(command[0], "/shmem") && strcmp(command[0], "/app_switch"))) {
		errno = EINVAL;
		return fail("command executable allowlist");
	}
	if ((!strcmp(command[0], "/refault") && strcmp(label, "w1") && strcmp(label, "w4")) ||
	    (!strcmp(command[0], "/shmem") && strcmp(label, "w3")) ||
	    (!strcmp(command[0], "/app_switch") && strcmp(label, "w2"))) {
		errno = EINVAL;
		return fail("workload-label/command mismatch");
	}
	unsigned int corpus_args = 0;

	for (unsigned int i = 1; i < argc; i++) {
		if (!strcmp(command[i], "--functional-synthetic")) {
			errno = EINVAL;
			return fail("synthetic input is not a comparison workload");
		}
		if (!strcmp(command[i], "--corpus")) {
			if (i + 1 >= argc || strcmp(command[++i], "/dev/vdc")) {
				errno = EINVAL;
				return fail("corpus must be the dedicated read-only vdc");
			}
			corpus_args++;
		}
	}
	if (corpus_args != 1) {
		errno = EINVAL;
		return fail("exactly one real corpus input is required");
	}
	return 0;
}
static void log_configuration(void)
{
	unsigned int argc = 0;

	while (command[argc])
		argc++;
	fprintf(raw_stream, "COMPARE_REQUEST backend=%s algorithm=%s workload=%s"
		" argc=%u timeout_seconds=%" PRIu64 " page_bytes=%zu\n",
		backend, algorithm, label, argc, timeout_seconds, page_bytes);
	for (unsigned int i = 0; command[i]; i++)
		fprintf(raw_stream, "COMPARE_ARG index=%u value=%s\n", i, command[i]);
}
static int directory(const char *path)
{
	struct stat st;

	if (!mkdir(path, 0755))
		return 0;
	if (errno != EEXIST || lstat(path, &st) || !S_ISDIR(st.st_mode))
		return -1;
	return 0;
}
static int mount_system(const char *source, const char *path, const char *type,
			unsigned long magic)
{
	struct statfs fs;

	if (directory(path))
		return fail(path);
	if (!mount(source, path, type, MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL))
		return 0;
	if (errno == EBUSY && !statfs(path, &fs) &&
	    (unsigned long)fs.f_type == magic)
		return 0;
	return fail(path);
}
static int mount_filesystems(void)
{
	struct sysinfo info;
	char options[96], enabled[4096];
	uint64_t memory, limit;
	int console;

	if (mount_system("proc", "/proc", "proc", PROC_SUPER_MAGIC) ||
	    mount_system("sysfs", "/sys", "sysfs", SYSFS_MAGIC))
		return -1;
	if (directory("/dev"))
		return fail("/dev");
	if (mount("devtmpfs", "/dev", "devtmpfs", MS_NOSUID, "mode=0755")) {
		struct statfs fs;

		if (errno != EBUSY || statfs("/dev", &fs) ||
		    (unsigned long)fs.f_type != TMPFS_MAGIC)
			return fail("devtmpfs");
	}
	console = open("/dev/console", O_RDWR | O_CLOEXEC);
	if (console < 0)
		return fail("console");
	for (int i = 0; i < 3; i++)
		if (dup2(console, i) < 0) {
			close(console);
			return fail("console dup2");
		}
	if (console > 2)
		close(console);
	if (mount_system("cgroup2", "/sys/fs/cgroup", "cgroup2", CGROUP2_SUPER_MAGIC) ||
	    mount_system("debugfs", "/sys/kernel/debug", "debugfs", DEBUGFS_MAGIC))
		return -1;
	if (write_text("/sys/fs/cgroup/cgroup.subtree_control", "+memory\n") ||
	    read_text("/sys/fs/cgroup/cgroup.subtree_control", enabled, sizeof(enabled), false) ||
	    !token(enabled, "memory")) {
		errno = errno ? errno : ENODEV;
		return fail("enable cgroup memory controller");
	}
	if (sysinfo(&info) || !info.mem_unit ||
	    (uint64_t)info.totalram > UINT64_MAX / info.mem_unit)
		return fail("guest total RAM");
	memory = (uint64_t)info.totalram * info.mem_unit;
	limit = memory / 10 * 9 + memory % 10 * 9 / 10;
	limit -= limit % page_bytes;
	if (!limit || directory("/mnt") || directory("/mnt/test")) {
		errno = EINVAL;
		return fail("tmpfs directory/size");
	}
	snprintf(options, sizeof(options), "size=%" PRIu64 ",mode=0700", limit);
	/* Never reuse/remount an existing test filesystem. */
	if (mount("compare-test", "/mnt/test", "tmpfs", MS_NOSUID | MS_NODEV, options))
		return fail("owned test tmpfs");
	fprintf(raw_stream, "COMPARE_TMPFS path=/mnt/test guest_total_bytes=%" PRIu64
		" size_limit_bytes=%" PRIu64 " policy=90_percent_guest_ram\n", memory, limit);
	limit = memory / 100;
	limit -= limit % page_bytes;
	if (limit < page_bytes)
		limit = page_bytes;
	snprintf(options, sizeof(options), "size=%" PRIu64 ",mode=0755", limit);
	if (directory("/run") ||
	    mount("compare-run", "/run", "tmpfs", MS_NOSUID | MS_NODEV, options))
		return fail("owned /run tmpfs");
	return 0;
}

static void raw_file(const char *phase, const char *path, bool required)
{
	char buf[2048];
	uint64_t timestamp = monotonic_ns();
	size_t total = 0;
	bool probe_retry = required && snapshot_memory_consistent && memory_capture_active &&
		(!strcmp(path, "/sys/kernel/debug/memcomp/memory_accounting") ||
		 !strcmp(path, "/sys/kernel/debug/memcomp/memory_occupancy"));
	unsigned int retries = 0;
	bool started = false;
	int fd;

retry_open:
	fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0) {
		fprintf(raw_stream, "COMPARE_RAW_NA monotonic_ns=%" PRIu64
			" phase=%s path=%s errno=%d\n", timestamp, phase, path, errno);
		if (required)
			telemetry_error = true;
		return;
	}
	if (!probe_retry) {
		fprintf(raw_stream, "COMPARE_RAW_BEGIN monotonic_ns=%" PRIu64
			" phase=%s path=%s\n", timestamp, phase, path);
		started = true;
	}
	while (total < SNAP_LIMIT) {
		ssize_t n = read(fd, buf, sizeof(buf));
		int error = n < 0 ? errno : 0;

		if (n < 0 && error == EINTR && !stop_signal)
			continue;
		if (!started) {
			/* Observer capture can race before emitting any report bytes.
			 * Never splice partial captures or consume another whole
			 * memory-consistency attempt for this error.
			 */
			if (probe_retry && n < 0 && error == EAGAIN &&
			    retries < MEMORY_PROBE_MAX_RETRIES && !stop_signal) {
				int close_error = close(fd) ? (errno ? errno : EIO) : 0;

				/* close() must not be retried, even when interrupted. */
				fd = -1;
				if (!close_error && !stop_signal) {
					struct timespec delay = { .tv_sec = 0, .tv_nsec = 1000000 };
					int sleep_error;

					retries++;
					fprintf(raw_stream, "COMPARE_PROBE_READ_RETRY monotonic_ns=%" PRIu64
						" phase=%s path=%s errno=%d retry=%u max_retries=%u\n",
						monotonic_ns(), phase, path, error, retries,
						MEMORY_PROBE_MAX_RETRIES);
					do {
						sleep_error = nanosleep(&delay, &delay);
					} while (sleep_error && errno == EINTR && !stop_signal);
					if (stop_signal) {
						error = EINTR;
					} else if (sleep_error) {
						error = errno ? errno : EIO;
					} else {
						goto retry_open;
					}
				} else {
					error = close_error ? close_error : EINTR;
				}
			}
			fprintf(raw_stream, "COMPARE_RAW_BEGIN monotonic_ns=%" PRIu64
				" phase=%s path=%s\n", timestamp, phase, path);
			started = true;
		}
		if (!n)
			break;
		if (n < 0) {
			fprintf(raw_stream, "\nCOMPARE_RAW_READ_ERROR errno=%d\n", error);
			if (required)
				telemetry_error = true;
			break;
		}
		if (fwrite(buf, 1, (size_t)n, raw_stream) != (size_t)n)
			telemetry_error = true;
		total += (size_t)n;
	}
	if (total == SNAP_LIMIT) {
		fprintf(raw_stream, "\nCOMPARE_RAW_TRUNCATED limit=%u\n", SNAP_LIMIT);
		if (required)
			telemetry_error = true;
	}
	fprintf(raw_stream, "\nCOMPARE_RAW_END monotonic_ns=%" PRIu64
		" phase=%s path=%s\n", monotonic_ns(), phase, path);
	if (fd >= 0)
		close(fd);
}
static void raw_directory(const char *phase, const char *path)
{
	struct snapshot_dir *dir = snapshot_opendir(path);
	struct dirent *entry;
	unsigned int count = 0;
	char name[PATH_MAX];

	if (!dir) {
		fprintf(raw_stream, "COMPARE_RAW_NA monotonic_ns=%" PRIu64
			" phase=%s path=%s errno=%d\n", monotonic_ns(), phase, path, errno);
		return;
	}
	while ((entry = snapshot_readdir(dir)) != NULL) {
		struct stat st;
		int n;

		if (entry->d_name[0] == '.')
			continue;
		n = snprintf(name, sizeof(name), "%s/%s", path, entry->d_name);
		if (n < 0 || (size_t)n >= sizeof(name) || lstat(name, &st) || !S_ISREG(st.st_mode))
			continue;
		if (count++ == 64) {
			fprintf(raw_stream, "COMPARE_RAW_LIMIT monotonic_ns=%" PRIu64
				" phase=%s path=%s files=64\n", monotonic_ns(), phase, path);
			break;
		}
		raw_file(phase, name, false);
	}
	snapshot_closedir(dir);
}
static void kernel_threads(const char *phase)
{
	struct snapshot_dir *dir = snapshot_opendir("/proc");
	struct dirent *entry;
	char comm[128], path[128];
	unsigned int seen = 0, matched = 0;

	if (!dir) {
		fprintf(raw_stream, "COMPARE_RAW_NA monotonic_ns=%" PRIu64
			" phase=%s path=kernel_threads errno=%d\n", monotonic_ns(), phase, errno);
		return;
	}
	while ((entry = snapshot_readdir(dir)) != NULL && seen < 65536) {
		const char *p = entry->d_name;

		if (!*p)
			continue;
		while (*p >= '0' && *p <= '9')
			p++;
		if (*p || strlen(entry->d_name) > 20)
			continue;
		seen++;
		snprintf(path, sizeof(path), "/proc/%s/comm", entry->d_name);
		if (read_text(path, comm, sizeof(comm), false) ||
		    (strncmp(comm, "kswapd", 6) && strncmp(comm, "kworker", 7)))
			continue;
		if (matched++ == 64) {
			fprintf(raw_stream, "COMPARE_RAW_LIMIT monotonic_ns=%" PRIu64
				" phase=%s path=kernel_threads matches=64\n", monotonic_ns(), phase);
			break;
		}
		raw_file(phase, path, false);
		snprintf(path, sizeof(path), "/proc/%s/stat", entry->d_name);
		raw_file(phase, path, false);
	}
	snapshot_closedir(dir);
}
/* Keep merged-cache identity and geometry without changing normal slab merging. */
static bool snapshot_slab_name(const char *name)
{
	return !strcmp(name, "memcomp_entry") || !strcmp(name, "memcomp_entry_cache") ||
		!strcmp(name, "radix_tree_node") || !strcmp(name, "zs_handle") ||
		!strncmp(name, "zs_handle-", 10) || !strcmp(name, "zspage") ||
		!strncmp(name, "zspage-", 7);
}

static void snapshot_slab_aliases(const char *phase)
{
	static const char root[] = "/sys/kernel/slab";
	static const char inventory[] = "/sys/kernel/slab/compare_aliases";
	static const char *const attrs[] = {
		"objects", "total_objects", "object_size", "slab_size",
		"objs_per_slab", "order", "slabs", "aliases",
	};
	char names[64][NAME_MAX + 1], path[PATH_MAX], identity[PATH_MAX];
	struct dirent *entry;
	unsigned int count = 0;
	bool capped = false;
	struct snapshot_dir *dir = snapshot_opendir(root);

	if (!dir) {
		fprintf(raw_stream, "COMPARE_RAW_NA monotonic_ns=%" PRIu64
			" phase=%s path=%s errno=%d\n", monotonic_ns(), phase, inventory, errno);
		return;
	}
	fprintf(raw_stream, "COMPARE_RAW_BEGIN monotonic_ns=%" PRIu64
		" phase=%s path=%s\n", monotonic_ns(), phase, inventory);
	errno = 0;
	while ((entry = snapshot_readdir(dir)) != NULL) {
		if (!snapshot_slab_name(entry->d_name))
			continue;
		if (count == 64) {
			capped = true;
			break;
		}
		snprintf(names[count], sizeof(names[count]), "%s", entry->d_name);
		fprintf(raw_stream, "%s\n", names[count++]);
	}
	if (errno)
		fprintf(raw_stream, "COMPARE_RAW_READ_ERROR errno=%d\n", errno);
	fprintf(raw_stream, "COMPARE_RAW_END monotonic_ns=%" PRIu64
		" phase=%s path=%s\n", monotonic_ns(), phase, inventory);
	snapshot_closedir(dir);
	if (capped)
		fprintf(raw_stream, "COMPARE_RAW_LIMIT monotonic_ns=%" PRIu64
			" phase=%s path=%s files=64\n", monotonic_ns(), phase, root);
	for (unsigned int i = 0; i < count; i++) {
		char target[PATH_MAX], link[PATH_MAX];
		struct stat st;
		ssize_t n;
		int error = 0;

		snprintf(path, sizeof(path), "%s/%.*s", root, NAME_MAX, names[i]);
		snprintf(identity, sizeof(identity), "%s/%.*s/compare_identity", root, NAME_MAX, names[i]);
		n = readlink(path, link, sizeof(link) - 1);
		if (n < 0) {
			if (errno == EINVAL)
				strcpy(link, "."); /* Real cache directory, not a symlink. */
			else
				error = errno;
		} else if ((size_t)n == sizeof(link) - 1) {
			error = ENAMETOOLONG;
		} else {
			link[n] = '\0';
		}
		if (!error && memory_capture_active) {
			if (strchr(link, '/') || !strcmp(link, ".."))
				error = EPROTO;
			else if (snprintf(target, sizeof(target), "%s/%s", root,
					 !strcmp(link, ".") ? names[i] : link) >= (int)sizeof(target))
				error = ENAMETOOLONG;
			else if (stat(path, &st))
				error = errno;
		} else if (!error && (!realpath(path, target) || stat(path, &st))) {
			error = errno;
		}
		if (error) {
			fprintf(raw_stream, "COMPARE_RAW_NA monotonic_ns=%" PRIu64
				" phase=%s path=%s errno=%d\n", monotonic_ns(), phase, identity, error);
		} else {
			fprintf(raw_stream, "COMPARE_RAW_BEGIN monotonic_ns=%" PRIu64
				" phase=%s path=%s\n", monotonic_ns(), phase, identity);
			fprintf(raw_stream, "readlink %s\ntarget %s\ndevice %" PRIuMAX
				"\ninode %" PRIuMAX "\n", link, target,
				(uintmax_t)st.st_dev, (uintmax_t)st.st_ino);
			fprintf(raw_stream, "COMPARE_RAW_END monotonic_ns=%" PRIu64
				" phase=%s path=%s\n", monotonic_ns(), phase, identity);
		}
		for (size_t j = 0; j < sizeof(attrs) / sizeof(attrs[0]); j++) {
			snprintf(path, sizeof(path), "%s/%.*s/%s", root, NAME_MAX, names[i], attrs[j]);
			raw_file(phase, path, false);
		}
	}
}

/*
 * Opt-in, once per snapshot, never a retry-until-balanced operation.
 * A positive-length read returns EOF after stock folds zone/node statistics.
 * It may also decay/drain PCP and schedules work on every online CPU. It is
 * neither atomic nor passive, and must remain outside workload touch timers.
 */
static int refresh_snapshot_vmstat(const char *phase)
{
	uint64_t begin = monotonic_ns(), end;
	unsigned int attempts = 0;
	char unused;
	ssize_t count = -1;
	int error = 0, fd;

	fd = open("/proc/sys/vm/stat_refresh", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0) {
		error = errno;
	} else {
		do {
			attempts++;
			count = read(fd, &unused, 1);
		} while (count < 0 && errno == EINTR && !stop_signal);
		if (count < 0)
			error = errno;
		else if (count)
			error = EPROTO;
		if (close(fd) && !error)
			error = errno;
	}
	end = monotonic_ns();
	if (clock_error || end < begin)
		error = error ? error : EIO;
	fprintf(raw_stream, "COMPARE_VMSTAT_REFRESH begin_ns=%" PRIu64
		" end_ns=%" PRIu64 " phase=%s status=%s errno=%d attempts=%u\n",
		begin, end, phase, error ? "error" : "ok", error, attempts);
	if (error) {
		telemetry_error = true;
		errno = error;
		return fail("snapshot vmstat refresh");
	}
	return 0;
}

static void snapshot(const char *phase)
{
	static const char *const core[] = {
		"/proc/vmstat", "/proc/meminfo", "/proc/stat", "/proc/diskstats", "/proc/swaps",
	};
	static const char *const optional[] = {
		"/proc/pressure/memory", "/proc/pressure/cpu", "/proc/pressure/io",
		"/proc/zoneinfo", "/proc/buddyinfo", "/proc/slabinfo",
		"/sys/block/zram0/mm_stat", "/sys/block/zram0/bd_stat", "/sys/block/zram0/io_stat",
		"/sys/block/zram0/backing_dev",
		"/sys/kernel/mm/memcomp/stat", "/sys/kernel/mm/memcomp/compressor",
		"/sys/kernel/mm/memcomp/events", "/sys/kernel/mm/memcomp/packed_swap_events",
		"/sys/kernel/mm/memcomp/execution_events",
		"/sys/module/zswap/parameters/enabled", "/sys/module/zswap/parameters/compressor",
		"/sys/module/zswap/parameters/zpool", "/sys/module/zswap/parameters/shrinker_enabled",
	};
	if (snapshot_vmstat_refresh && refresh_snapshot_vmstat(phase))
		return;
	fprintf(raw_stream, "COMPARE_SNAPSHOT monotonic_ns=%" PRIu64 " phase=%s\n", monotonic_ns(), phase);
	for (size_t i = 0; i < sizeof(core) / sizeof(core[0]); i++)
		raw_file(phase, core[i], true);
	for (size_t i = 0; i < sizeof(optional) / sizeof(optional[0]); i++)
		raw_file(phase, optional[i], false);
	if (w4_cgroup_created) {
		static const char *const files[] = {
			W4_CGROUP "/memory.current", W4_CGROUP "/memory.max",
			W4_CGROUP "/memory.swap.current", W4_CGROUP "/memory.swap.max",
			W4_CGROUP "/memory.zswap.current", W4_CGROUP "/memory.zswap.max",
			W4_CGROUP "/memory.zswap.writeback", W4_CGROUP "/memory.events",
			W4_CGROUP "/memory.stat", W4_CGROUP "/cgroup.events",
		};

		for (size_t i = 0; i < sizeof(files) / sizeof(files[0]); i++)
			raw_file(phase, files[i], false);
	}
	snapshot_slab_aliases(phase);
	raw_directory(phase, "/sys/kernel/debug/zswap");
	kernel_threads(phase);
	if (ferror(raw_stream))
		telemetry_error = true;
}
/* v4.11 W1-only collector protocol, not a kernel ABI or atomic snapshot. */
#define MEMORY_PROTOCOL "v4.11-w1-memory-consistent-v3"
#define MEMORY_TOLERANCE 16U
#define MEMORY_ATTEMPTS 10U
#define MEMORY_RAW "/compare/memory/v411/"

static int load_memory_configuration(void)
{
	char buf[64];
	uint64_t value;

	if (read_text("/snapshot-memory-consistent", buf, sizeof(buf), true)) {
		if (errno == ENOENT)
			return 0;
		return fail("snapshot-memory-consistent");
	}
	if (number(buf, 1, &value) ||
	    (value && (strcmp(label, "w1") || snapshot_vmstat_refresh))) {
		errno = EINVAL;
		return fail("memory snapshot requires W1 and excludes legacy refresh");
	}
	snapshot_memory_consistent = value;
	return 0;
}

struct memory_counts {
	uint64_t buddy, pcp, zones, all_zones, cpus;
};

static uint64_t memory_distance(uint64_t a, uint64_t b)
{
	return a > b ? a - b : b - a;
}

static int memory_uint(char **text, uint64_t *value)
{
	char *p = *text, *end;
	unsigned long long n;

	while (isspace((unsigned char)*p))
		p++;
	if (!isdigit((unsigned char)*p))
		return -1;
	errno = 0;
	n = strtoull(p, &end, 10);
	if (errno || (*end && !isspace((unsigned char)*end)))
		return -1;
	*value = n;
	*text = end;
	return 0;
}

/* Coverage identity: fail on a topology/order change rather than retry it. */
static uint64_t memory_identity(uint64_t hash, const char *text)
{
	for (; *text; text++)
		hash = (hash ^ (unsigned char)*text) * UINT64_C(1099511628211);
	return (hash ^ 0xff) * UINT64_C(1099511628211);
}

static int memory_buddy(char *text, struct memory_counts *out)
{
	char *line, *save;
	unsigned int rows = 0, orders = 0;

	for (line = strtok_r(text, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
		char zone[64], node[32], *p;
		uint64_t value, total = 0;
		int used = 0;
		unsigned int order = 0;

		if (sscanf(line, "Node %31[0-9], zone %63s %n", node, zone, &used) != 2 || !used)
			return -1;
		out->zones = memory_identity(memory_identity(out->zones, node), zone);
		p = line + used;
		while (*p) {
			while (isspace((unsigned char)*p))
				p++;
			if (!*p)
				break;
			if (order >= 64 || memory_uint(&p, &value) || value > (UINT64_MAX >> order) ||
			    UINT64_MAX - total < (value << order))
				return -1;
			total += value << order++;
		}
		if (!order || (orders && orders != order) || UINT64_MAX - out->buddy < total)
			return -1;
		out->buddy += total;
		orders = order;
		rows++;
	}
	return rows ? 0 : -1;
}

static bool memory_zone_complete(uint64_t present, uint64_t managed, uint64_t free,
				 unsigned int zone_fields, unsigned int cpu_fields,
				 unsigned int cpus, bool pagesets)
{
	if ((zone_fields & 7) != 7 || (cpu_fields && cpu_fields != 7))
		return false;
	if (!present)
		return !managed && !free && !pagesets && !cpus;
	return pagesets && cpus && zone_fields == 31;
}

static int memory_pcp(char *text, struct memory_counts *out)
{
	char *line, *save;
	uint64_t zones = 0, cpu_hash = 0, present = 0, managed = 0, free = 0, last_cpu = 0;
	unsigned int rows = 0, fields = 0, zone_fields = 0, cpus = 0;
	bool pagesets = false, tail = false, in_cpu = false;
	char zone[64], node[32];

	for (line = strtok_r(text, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
		static const char *const labels[] = {
			"pages free", "present", "managed", "node_unreclaimable:", "start_pfn:",
		};
		char *p = line;
		uint64_t value;
		int used = 0;
		bool matched = false;

		while (isspace((unsigned char)*p))
			p++;
		if (!strncmp(p, "Node", 4)) {
			if (rows && !memory_zone_complete(present, managed, free, zone_fields, fields, cpus, pagesets))
				return -1;
			if (sscanf(p, "Node %31[0-9], zone %63s %n", node, zone, &used) != 2 || !used || p[used])
				return -1;
			/* Rescan split headers; SNAP_LIMIT bounds work without a zone-count cap. */
			for (char *prior = text; prior < line; prior += strlen(prior) + 1) {
				char seen_node[32], seen_zone[64], *q = prior;

				while (isspace((unsigned char)*q))
					q++;
				if (q >= line || strncmp(q, "Node", 4))
					continue;
				if (sscanf(q, "Node %31[0-9], zone %63s", seen_node, seen_zone) == 2 &&
				    !strcmp(node + strspn(node, "0"), seen_node + strspn(seen_node, "0")) &&
				    !strcmp(zone, seen_zone))
					return -1;
			}
			out->all_zones = memory_identity(memory_identity(out->all_zones, node), zone);
			rows++;
			fields = zone_fields = cpus = 0;
			present = managed = free = last_cpu = 0;
			pagesets = tail = in_cpu = false;
			continue;
		}
		if (!rows)
			return -1;
		for (size_t i = 0; i < sizeof(labels) / sizeof(labels[0]); i++) {
			size_t n = strlen(labels[i]);
			char *q;

			if (strncmp(p, labels[i], n) || (p[n] && !isspace((unsigned char)p[n])))
				continue;
			q = p + n;
			if ((zone_fields & (1U << i)) || memory_uint(&q, &value) || *q)
				return -1;
			zone_fields |= 1U << i;
			if (i == 0)
				free = value;
			else if (i == 1) {
				present = value;
				/* buddyinfo omits unpopulated zones; zoneinfo lists them. */
				if (present)
					zones = memory_identity(memory_identity(zones, node), zone);
			} else if (i == 2)
				managed = value;
			else {
				if (fields != 7)
					return -1;
				tail = true;
			}
			matched = true;
			break;
		}
		if (matched)
			continue;
		if (!strncmp(p, "pagesets", 8)) {
			if (strcmp(p, "pagesets") || pagesets || tail)
				return -1;
			pagesets = true;
		} else if (!strncmp(p, "cpu:", 4)) {
			char *q = p + 4;

			if (!pagesets || tail || (in_cpu && fields != 7) ||
			    memory_uint(&q, &value) || *q || (cpus && value <= last_cpu))
				return -1;
			cpu_hash = memory_identity(cpu_hash, p);
			last_cpu = value;
			cpus++;
			fields = 0;
			in_cpu = true;
		} else if (!strncmp(p, "count:", 6) || !strncmp(p, "high:", 5) || !strncmp(p, "batch:", 6)) {
			unsigned int bit = *p == 'c' ? 1 : *p == 'h' ? 2 : 4;

			if (!pagesets || !in_cpu || tail || (fields & bit))
				return -1;
			p = strchr(p, ':') + 1;
			if (memory_uint(&p, &value) || *p || (bit == 1 && UINT64_MAX - out->pcp < value))
				return -1;
			fields |= bit;
			if (bit == 1)
				out->pcp += value;
		} else if (!strncmp(p, "count", 5) || !strncmp(p, "batch", 5) || !strncmp(p, "cpu", 3)) {
			return -1;
		}
	}
	if (!rows || !memory_zone_complete(present, managed, free, zone_fields, fields, cpus, pagesets) || zones != out->zones)
		return -1;
	out->cpus = cpu_hash;
	return 0;
}

static int memory_memfree(char *text, uint64_t *pages)
{
	static const char *const names[] = {
		"MemFree", "MemAvailable", "Slab", "SReclaimable", "SUnreclaim",
		"PageTables", "Percpu", "VmallocUsed", "AnonPages", "Cached",
		"Buffers", "SwapCached", "KernelStack", "Active(anon)", "Inactive(anon)",
		"Active(file)", "Inactive(file)", "Unevictable",
	};
	char *line, *save;
	uint64_t seen = 0, kib = 0, slab = 0, reclaim = 0, unreclaim = 0;

	for (line = strtok_r(text, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
		for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
			size_t n = strlen(names[i]);
			char *p;
			uint64_t value;

			if (strncmp(line, names[i], n) || line[n] != ':')
				continue;
			p = line + n + 1;
			if ((seen & (UINT64_C(1) << i)) || memory_uint(&p, &value) || value > UINT64_MAX / 1024)
				return -1;
			seen |= UINT64_C(1) << i;
			while (isspace((unsigned char)*p))
				p++;
			if (strcmp(p, "kB"))
				return -1;
			if (!i)
				kib = value;
			else if (i == 2)
				slab = value;
			else if (i == 3)
				reclaim = value;
			else if (i == 4)
				unreclaim = value;
		}
	}
	if (seen != (UINT64_C(1) << (sizeof(names) / sizeof(names[0]))) - 1 ||
	    !page_bytes || kib * 1024 % page_bytes || slab != reclaim + unreclaim)
		return -1;
	*pages = kib * 1024 / page_bytes;
	return 0;
}

static int memory_slab(char *text)
{
	char *line, *save;

	line = strtok_r(text, "\n", &save);
	if (!line || strcmp(line, "slabinfo - version: 2.1"))
		return -1;
	while ((line = strtok_r(NULL, "\n", &save)) != NULL) {
		char *p = line;
		uint64_t values[11];

		while (isspace((unsigned char)*p))
			p++;
		if (!*p || *p == '#')
			continue;
		while (*p && !isspace((unsigned char)*p))
			p++;
		for (size_t i = 0; i < 11; i++) {
			if (i == 5 || i == 8) {
				const char *section_label = i == 5 ? ": tunables" : ": slabdata";

				while (isspace((unsigned char)*p))
					p++;
				if (strncmp(p, section_label, strlen(section_label)))
					return -1;
				p += strlen(section_label);
			}
			if (memory_uint(&p, &values[i]))
				return -1;
		}
		while (isspace((unsigned char)*p))
			p++;
		if (*p || !values[2] || !values[3] || !values[4] || values[0] > values[1] ||
		    values[8] > values[9] || values[9] > UINT64_MAX / values[3] ||
		    values[1] > values[9] * values[3] || !!values[1] != !!values[9])
			return -1;
	}
	return 0;
}

static int memory_vmstat(char *text)
{
	char *line, *save;
	unsigned int seen = 0;

	for (line = strtok_r(text, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
		char *p = line;
		uint64_t value;
		unsigned int bit = 0;

		while (*p && !isspace((unsigned char)*p))
			p++;
		if (!strncmp(line, "nr_isolated_anon ", 17))
			bit = 1;
		else if (!strncmp(line, "nr_isolated_file ", 17))
			bit = 2;
		if ((seen & bit) || memory_uint(&p, &value))
			return -1;
		while (isspace((unsigned char)*p))
			p++;
		if (*p)
			return -1;
		seen |= bit;
	}
	return seen == 3 ? 0 : -1;
}

static int memory_swaps(char *text)
{
	static const char *const header[] = { "Filename", "Type", "Size", "Used", "Priority" };
	char *line, *save;
	bool first = true;

	for (line = strtok_r(text, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
		char *words[5], *part, *word_save, *p;
		unsigned int n = 0;
		uint64_t size, used, priority;

		for (part = strtok_r(line, " \t", &word_save); part; part = strtok_r(NULL, " \t", &word_save)) {
			if (n == 5)
				return -1;
			words[n++] = part;
		}
		if (!n)
			continue;
		if (n != 5)
			return -1;
		if (first) {
			for (n = 0; n < 5; n++)
				if (strcmp(words[n], header[n]))
					return -1;
			first = false;
			continue;
		}
		p = words[2];
		if (memory_uint(&p, &size) || *p || !size || size > UINT64_MAX / 1024)
			return -1;
		p = words[3];
		if (memory_uint(&p, &used) || *p || used > size)
			return -1;
		p = words[4];
		if (*p == '-')
			p++;
		if (memory_uint(&p, &priority) || *p ||
		    (strcmp(words[1], "file") && strcmp(words[1], "partition")) ||
		    size * 1024 % page_bytes || used * 1024 % page_bytes)
			return -1;
	}
	return first ? -1 : 0;
}

/* Raw bytes and the live retry decision come from exactly the same read. */
static int memory_capture(const char *phase, const char *path, const char *source,
			  int kind, struct memory_counts *counts, uint64_t *memfree)
{
	char *buf = memory_capture_read_buffer;
	uint64_t begin = monotonic_ns(), end;
	size_t used = 0;
	int fd, error = 0, rc = -1;

	fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0) {
		error = errno;
	} else {
		while (used < sizeof(memory_capture_read_buffer)) {
			ssize_t n = read(fd, buf + used, sizeof(memory_capture_read_buffer) - used);

			if (n < 0 && errno == EINTR && !stop_signal)
				continue;
			if (n < 0) {
				error = errno;
				break;
			}
			if (!n)
				break;
			if (memchr(buf + used, 0, (size_t)n))
				error = EPROTO;
			used += (size_t)n;
		}
		if (close(fd) && !error)
			error = errno;
		if (used == sizeof(memory_capture_read_buffer))
			error = E2BIG;
	}
	end = monotonic_ns();
	if (clock_error || end < begin)
		error = error ? error : EIO;
	fprintf(raw_stream, "COMPARE_RAW_BEGIN monotonic_ns=%" PRIu64 " phase=%s path=%s\n", begin, phase, source);
	if (used)
		fwrite(buf, 1, used, raw_stream);
	if (error)
		fprintf(raw_stream, "\nCOMPARE_RAW_READ_ERROR errno=%d\n", error);
	fprintf(raw_stream, "\nCOMPARE_RAW_END monotonic_ns=%" PRIu64 " phase=%s path=%s\n", end, phase, source);
	if (!error) {
		buf[used] = 0;
		rc = kind == 0 ? memory_buddy(buf, counts) : kind == 1 ? memory_pcp(buf, counts) :
		     kind == 2 ? memory_memfree(buf, memfree) : kind == 3 ? memory_slab(buf) :
		     kind == 4 ? memory_vmstat(buf) : memory_swaps(buf);
		if (rc)
			error = EPROTO;
	}
	if (error || ferror(raw_stream)) {
		telemetry_error = true;
		errno = error ? error : EIO;
		return -1;
	}
	return 0;
}

static int memory_pair(const char *phase, bool after, struct memory_counts *counts)
{
	return memory_capture(phase, "/proc/buddyinfo", after ? MEMORY_RAW "after/buddyinfo" : MEMORY_RAW "before/buddyinfo", 0, counts, NULL) ||
		memory_capture(phase, "/proc/zoneinfo", after ? MEMORY_RAW "after/zoneinfo" : MEMORY_RAW "before/zoneinfo", 1, counts, NULL);
}

static void snapshot_memory(const char *phase)
{
	static const char *const required[] = {
		"/proc/stat", "/proc/diskstats",
	};
	static const char *const optional[] = {
		"/proc/pressure/memory", "/proc/pressure/cpu", "/proc/pressure/io",
		"/sys/block/zram0/mm_stat", "/sys/block/zram0/bd_stat", "/sys/block/zram0/io_stat",
		"/sys/kernel/mm/memcomp/stat", "/sys/kernel/mm/memcomp/events",
		"/sys/kernel/mm/memcomp/packed_swap_events", "/sys/kernel/mm/memcomp/execution_events",
	};
	const char *status = "INVALID";
	unsigned int attempt;

	if (memory_buffer_prepare()) {
		telemetry_error = true;
		fail("memory snapshot locked storage");
		return;
	}
	fprintf(raw_stream, "COMPARE_SNAPSHOT monotonic_ns=%" PRIu64 " phase=%s\n", monotonic_ns(), phase);
	fprintf(raw_stream, "COMPARE_MEMORY_SNAPSHOT_BEGIN monotonic_ns=%" PRIu64
		" phase=%s version=%s page_bytes=%zu tolerance_pages=%u max_attempts=%u"
		" memfree_check=between_buddy_with_tolerance free_sample=after\n",
		monotonic_ns(), phase, MEMORY_PROTOCOL, page_bytes, MEMORY_TOLERANCE, MEMORY_ATTEMPTS);
	for (attempt = 1; attempt <= MEMORY_ATTEMPTS; attempt++) {
		struct memory_counts before = { 0 }, after = { 0 };
		uint64_t memfree = 0, drift = 0, mismatch = 0, outside = 0;
		uint64_t attempt_begin;
		int error = 0;
		bool consistent = false;

		if (fflush(raw_stream)) {
			telemetry_error = true;
			status = "ERROR";
			break;
		}
		memory_capture_used = 0;
		memory_capture_overflow = false;
		memory_capture_active = true;
		attempt_begin = monotonic_ns();
		fprintf(raw_stream, "COMPARE_MEMORY_ATTEMPT_BEGIN monotonic_ns=%" PRIu64
			" phase=%s attempt=%u\n", attempt_begin, phase, attempt);
		if (refresh_snapshot_vmstat(phase) || memory_pair(phase, false, &before) ||
		    memory_capture(phase, "/proc/meminfo", "/proc/meminfo", 2, NULL, &memfree) ||
		    memory_capture(phase, "/proc/slabinfo", "/proc/slabinfo", 3, NULL, NULL) ||
		    memory_capture(phase, "/proc/vmstat", "/proc/vmstat", 4, NULL, NULL) ||
		    memory_capture(phase, "/proc/swaps", "/proc/swaps", 5, NULL, NULL)) {
			error = errno ? errno : EIO;
		} else {
			for (size_t i = 0; i < sizeof(required) / sizeof(required[0]); i++)
				raw_file(phase, required[i], true);
			for (size_t i = 0; i < sizeof(optional) / sizeof(optional[0]); i++)
				raw_file(phase, optional[i], false);
			if (!strncmp(backend, "memcomp-", 8)) {
				raw_file(phase, "/sys/kernel/debug/memcomp/memory_accounting", true);
				raw_file(phase, "/sys/kernel/debug/memcomp/memory_occupancy", true);
			}
			snapshot_slab_aliases(phase);
			raw_directory(phase, "/sys/kernel/debug/zswap");
			kernel_threads(phase);
			if (telemetry_error || memory_pair(phase, true, &after) ||
			    before.zones != after.zones || before.all_zones != after.all_zones ||
			    before.cpus != after.cpus ||
			    UINT64_MAX - before.buddy < before.pcp || UINT64_MAX - after.buddy < after.pcp)
				error = EPROTO;
		}
		if (!error) {
			drift = memory_distance(before.buddy + before.pcp, after.buddy + after.pcp);
			mismatch = memory_distance(memfree, before.buddy);
			if (memory_distance(memfree, after.buddy) > mismatch)
				mismatch = memory_distance(memfree, after.buddy);
			uint64_t low = before.buddy < after.buddy ? before.buddy : after.buddy;
			uint64_t high = before.buddy > after.buddy ? before.buddy : after.buddy;

			outside = memfree < low ? low - memfree : memfree > high ? memfree - high : 0;
			consistent = drift <= MEMORY_TOLERANCE && outside <= MEMORY_TOLERANCE;
		}
		fprintf(raw_stream, "COMPARE_MEMORY_ATTEMPT_END monotonic_ns=%" PRIu64
			" phase=%s attempt=%u status=%s errno=%d buddy_before_pages=%" PRIu64
			" pcp_before_pages=%" PRIu64 " buddy_after_pages=%" PRIu64
			" pcp_after_pages=%" PRIu64 " memfree_pages=%" PRIu64
			" free_drift_pages=%" PRIu64 " memfree_buddy_max_pages=%" PRIu64 "\n",
			monotonic_ns(), phase, attempt, error ? "ERROR" : consistent ? "CONSISTENT" : "INCONSISTENT",
			error, before.buddy, before.pcp, after.buddy, after.pcp, memfree, drift, mismatch);
		if (memory_buffer_flush()) {
			error = errno ? errno : EIO;
			consistent = false;
			if (memory_capture_overflow) {
				fprintf(raw_stream, "COMPARE_MEMORY_ATTEMPT_BEGIN monotonic_ns=%" PRIu64
					" phase=%s attempt=%u\n", attempt_begin, phase, attempt);
				fprintf(raw_stream, "COMPARE_MEMORY_ATTEMPT_END monotonic_ns=%" PRIu64
					" phase=%s attempt=%u status=ERROR errno=%d buddy_before_pages=0"
					" pcp_before_pages=0 buddy_after_pages=0 pcp_after_pages=0"
					" memfree_pages=0 free_drift_pages=0 memfree_buddy_max_pages=0\n",
					monotonic_ns(), phase, attempt, error);
			}
		}
		if (error || consistent) {
			status = error ? "ERROR" : "CONSISTENT";
			if (error)
				telemetry_error = true;
			break;
		}
		if (attempt < MEMORY_ATTEMPTS) {
			struct timespec delay = { .tv_sec = 0, .tv_nsec = 50000000 };
			int rc;

			do {
				rc = nanosleep(&delay, &delay);
			} while (rc && errno == EINTR && !stop_signal);
			if (rc) {
				telemetry_error = true;
				status = "ERROR";
				break;
			}
		}
	}
	fprintf(raw_stream, "COMPARE_MEMORY_SNAPSHOT_END monotonic_ns=%" PRIu64
		" phase=%s version=%s status=%s attempts=%u\n", monotonic_ns(), phase,
		MEMORY_PROTOCOL, status, attempt > MEMORY_ATTEMPTS ? MEMORY_ATTEMPTS : attempt);
	if (ferror(raw_stream))
		telemetry_error = true;
}

/* Keep the legacy function and protocol intact, including extraction tests. */
static void snapshot_boundary(const char *phase)
{
	if (snapshot_memory_consistent)
		snapshot_memory(phase);
	else
		snapshot(phase);
}

/* Route production boundaries while retaining the legacy collector. */
#define snapshot(phase) snapshot_boundary(phase)

static int set_never(const char *path)
{
	char actual[4096];

	if (read_text(path, actual, sizeof(actual), false)) {
		if (errno != ENOENT)
			return fail(path);
		raw_file("thp_policy_absent", path, false);
		return 0;
	}
	if (write_text(path, "never\n") || read_text(path, actual, sizeof(actual), false) ||
	    !selected(actual, "never")) {
		errno = errno ? errno : EPROTO;
		return fail(path);
	}
	raw_file("thp_policy_never", path, true);
	return 0;
}
static int disable_thp(void)
{
	const char *root = "/sys/kernel/mm/transparent_hugepage";
	DIR *dir;
	struct dirent *entry;
	char path[PATH_MAX];

	fprintf(raw_stream, "COMPARE_THP requested_policy=never anonymous_and_shmem\n");
	if (set_never("/sys/kernel/mm/transparent_hugepage/enabled") ||
	    set_never("/sys/kernel/mm/transparent_hugepage/shmem_enabled"))
		return -1;
	dir = opendir(root);
	if (!dir)
		return errno == ENOENT ? 0 : fail(root);
	while ((entry = readdir(dir)) != NULL) {
		if (strncmp(entry->d_name, "hugepages-", 10))
			continue;
		if (strlen(entry->d_name) > 64) {
			closedir(dir);
			errno = E2BIG;
			return fail("THP policy path");
		}
		snprintf(path, sizeof(path), "%s/%s/enabled", root, entry->d_name);
		if (set_never(path)) {
			closedir(dir);
			return -1;
		}
		snprintf(path, sizeof(path), "%s/%s/shmem_enabled", root, entry->d_name);
		if (set_never(path)) {
			closedir(dir);
			return -1;
		}
	}
	closedir(dir);
	return 0;
}
static int wait_path(const char *path)
{
	uint64_t deadline = monotonic_ns() + 5 * NS_SECOND;

	while (access(path, F_OK)) {
		struct timespec delay = { .tv_nsec = 50000000 };

		if (errno != ENOENT || stop_signal || clock_error || monotonic_ns() >= deadline) {
			errno = stop_signal ? EINTR : ENODEV;
			return fail(path);
		}
		nanosleep(&delay, NULL);
	}
	return 0;
}
static int swap_entries(const char *expected)
{
	char buf[16384], path[PATH_MAX];
	char *line;
	unsigned int count = 0;

	if (read_text("/proc/swaps", buf, sizeof(buf), false))
		return fail("/proc/swaps");
	line = strchr(buf, '\n');
	while (line && *++line) {
		if (sscanf(line, "%4095s", path) != 1 || !expected || strcmp(path, expected)) {
			errno = EBUSY;
			return fail("unexpected active swap");
		}
		count++;
		line = strchr(line, '\n');
	}
	if (count != (unsigned int)(expected != NULL)) {
		errno = EPROTO;
		return fail("active swap count");
	}
	return 0;
}
static int format_and_swapon(const char *path)
{
	struct stat st;
	uint64_t bytes = 0, pages;
	unsigned char *header;
	uint32_t value;
	size_t done = 0;
	int fd, saved = 0;

	/* This is the complete formatting allowlist. NEVER format WB backing. */
	if ((strcmp(path, "/dev/vda") && strcmp(path, "/dev/zram0")) ||
	    (!strcmp(backend, "zram-writeback") && strcmp(path, "/dev/zram0"))) {
		errno = EPERM;
		return fail("swap target allowlist");
	}
	if (wait_path(path))
		return -1;
	fd = open(path, O_RDWR | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0)
		return fail(path);
	if (fstat(fd, &st) || !S_ISBLK(st.st_mode) || ioctl(fd, BLKGETSIZE64, &bytes) ||
	    bytes < 16 * (uint64_t)page_bytes || bytes % page_bytes) {
		saved = errno ? errno : EINVAL;
		goto close;
	}
	pages = bytes / page_bytes;
	if (pages - 1 > UINT32_MAX) {
		saved = EOVERFLOW;
		goto close;
	}
	header = calloc(1, page_bytes);
	if (!header) {
		saved = errno;
		goto close;
	}
	value = 1;
	memcpy(header + 1024, &value, sizeof(value));
	value = (uint32_t)(pages - 1);
	memcpy(header + 1028, &value, sizeof(value));
	/* nr_badpages, UUID and label stay zero, just as an empty swap area. */
	memcpy(header + page_bytes - 10, "SWAPSPACE2", 10);
	while (done < page_bytes) {
		ssize_t n = pwrite(fd, header + done, page_bytes - done, (off_t)done);

		if (n < 0 && errno == EINTR && !stop_signal)
			continue;
		if (n <= 0) {
			saved = n < 0 ? errno : EIO;
			break;
		}
		done += (size_t)n;
	}
	free(header);
	if (!saved && fsync(fd))
		saved = errno;
close:
	if (close(fd) && !saved)
		saved = errno;
	if (saved) {
		errno = saved;
		return fail("format swap header");
	}
	if (swapon(path, 0))
		return fail("swapon");
	fprintf(raw_stream, "COMPARE_SWAP path=%s block_bytes=%" PRIu64
		" native_page_bytes=%zu flags=0\n", path, bytes, page_bytes);
	if (swap_entries(path))
		return -1;
	/* Collector-owned numeric evidence, not raw header bytes or a kernel ABI. */
	fprintf(raw_stream, "COMPARE_RAW_BEGIN monotonic_ns=%" PRIu64
		" phase=before_backend_setup path=/compare/swap_layout%s\n", monotonic_ns(), path);
	fprintf(raw_stream, "device %s\npage_bytes %zu\nversion 1\nlast_page %" PRIu64
		"\nnr_badpages 0\n", path, page_bytes, pages - 1);
	fprintf(raw_stream, "COMPARE_RAW_END monotonic_ns=%" PRIu64
		" phase=before_backend_setup path=/compare/swap_layout%s\n", monotonic_ns(), path);
	return 0;
}
static int verify_zswap(void)
{
	char actual[4096];
	bool requested = !strcmp(backend, "zswap") || !strcmp(backend, "zswap-crosscheck");
	const char *root = "/sys/module/zswap/parameters/";
	const char *names[] = { "compressor", "zpool", "shrinker_enabled" };
	const char *wanted[] = { algorithm, "zsmalloc", "Y" };
	char path[128];

	if (read_text("/sys/module/zswap/parameters/enabled", actual, sizeof(actual), false)) {
		if (!requested && errno == ENOENT)
			return 0;
		return fail("read zswap enabled");
	}
	if ((!strcmp(actual, "Y") || !strcmp(actual, "1")) != requested) {
		errno = EPROTO;
		return fail("zswap actual enabled mismatch (set boot parameters)");
	}
	if (!requested)
		return 0;
	for (size_t i = 0; i < 3; i++) {
		snprintf(path, sizeof(path), "%s%s", root, names[i]);
		if (read_text(path, actual, sizeof(actual), false) ||
		    (!selected(actual, wanted[i]) &&
		     !(i == 2 && !strcmp(actual, "1")))) {
			errno = errno ? errno : EPROTO;
			return fail(path);
		}
	}
	return 0;
}
static int setup_backend(void)
{
	char actual[4096], value[64];
	uint64_t bytes, current;
	bool zram = !strcmp(backend, "zram") || !strcmp(backend, "zram-writeback");

	if (swap_entries(NULL) || verify_zswap())
		return -1;
	/* Record observed parameters, including on setup failure, without writing. */
	if (!strncmp(backend, "memcomp-", 8)) {
		if (read_text("/sys/kernel/mm/memcomp/compressor", actual, sizeof(actual), false) ||
		    !selected(actual, algorithm)) {
			errno = errno ? errno : EPROTO;
			return fail("actual memcomp compressor mismatch (set boot parameters)");
		}
	}
	if (!strcmp(backend, "memcomp-noswap"))
		return swap_entries(NULL);
	if (!zram)
		return format_and_swapon("/dev/vda");
	if (read_text("/zram-bytes", value, sizeof(value), true) ||
	    number(value, UINT64_MAX, &bytes) || bytes < 16 * (uint64_t)page_bytes ||
	    bytes % page_bytes || bytes / page_bytes - 1 > UINT32_MAX) {
		errno = EINVAL;
		return fail("zram-bytes");
	}
	if (wait_path("/sys/block/zram0/disksize") || wait_path("/dev/zram0") ||
	    read_text("/sys/block/zram0/disksize", actual, sizeof(actual), false) ||
	    number(actual, UINT64_MAX, &current) || current) {
		errno = EBUSY;
		return fail("zram must be uninitialized");
	}
	snprintf(value, sizeof(value), "%s\n", algorithm);
	if (write_text("/sys/block/zram0/comp_algorithm", value) ||
	    read_text("/sys/block/zram0/comp_algorithm", actual, sizeof(actual), false) ||
	    !selected(actual, algorithm)) {
		errno = errno ? errno : EPROTO;
		return fail("zram comp_algorithm before disksize");
	}
	raw_file("zram_algorithm_before_disksize", "/sys/block/zram0/comp_algorithm", true);
	if (!strcmp(backend, "zram-writeback")) {
		if (wait_path("/dev/vda") || write_text("/sys/block/zram0/backing_dev", "/dev/vda\n"))
			return fail("zram backing_dev before disksize");
		raw_file("zram_backing_before_disksize", "/sys/block/zram0/backing_dev", true);
	}
	snprintf(value, sizeof(value), "%" PRIu64 "\n", bytes);
	if (write_text("/sys/block/zram0/disksize", value) ||
	    read_text("/sys/block/zram0/disksize", actual, sizeof(actual), false) ||
	    number(actual, UINT64_MAX, &current) || current != bytes) {
		errno = errno ? errno : EPROTO;
		return fail("zram disksize readback");
	}
	return format_and_swapon("/dev/zram0");
}

#define CSV_LINE_BYTES 16384U
static char csv_line[CSV_LINE_BYTES], csv_frame[CSV_LINE_BYTES + 32];
static size_t csv_used;
static bool csv_rejected;
static char phase_line[64];
static size_t phase_used;

/* /dev/vda remains the existing backing allowlist; never mount/format vdb. */
static int open_data_disk(void)
{
	cookie_io_functions_t io = { .write = data_write };
	char serial[64], config[64];
	unsigned char zero[DATA_HEADER_BYTES] = { 0 };
	unsigned char *header = data_header;
	int sector;
	struct stat st;
	uint64_t expected;

	if (read_text("/data-bytes", config, sizeof(config), true) ||
	    number(config, DATA_MAX_BYTES, &expected) || expected < (16U << 20) ||
	    wait_path("/dev/vdb") ||
	    read_text("/sys/block/vdb/serial", serial, sizeof(serial), false) ||
	    strcmp(serial, "memcomp-data")) {
		errno = errno ? errno : EINVAL;
		return fail("data disk configuration/serial allowlist");
	}
	data_direct = snapshot_memory_consistent;
	if (data_direct && memory_buffer_prepare())
		return fail("locked memory output storage");
	data_fd = open("/dev/vdb", O_RDWR | O_CLOEXEC | O_NOFOLLOW | (data_direct ? O_DIRECT : 0));
	if (data_fd < 0)
		return fail("data disk open");
	if (data_direct && (ioctl(data_fd, BLKSSZGET, &sector) || sector <= 0 ||
			    DATA_HEADER_BYTES % (unsigned int)sector ||
			    sizeof(data_direct_buffer) % (unsigned int)sector))
		return fail("data disk direct alignment");
	if (data_direct)
		data_direct_alignment = (size_t)sector;
	if (fstat(data_fd, &st) || !S_ISBLK(st.st_mode) ||
	    ioctl(data_fd, BLKGETSIZE64, &data_capacity) || data_capacity != expected ||
	    pread(data_fd, header, DATA_HEADER_BYTES, 0) != (ssize_t)DATA_HEADER_BYTES ||
	    memcmp(header, zero, DATA_HEADER_BYTES) ||
	    lseek(data_fd, DATA_HEADER_BYTES, SEEK_SET) != (off_t)DATA_HEADER_BYTES) {
		errno = errno ? errno : EINVAL;
		return fail("data disk type/size/fresh header");
	}
	raw_stream = fopencookie(NULL, "w", io);
	if (!raw_stream) {
		raw_stream = stderr;
		return fail("data stream");
	}
	if (setvbuf(raw_stream, raw_output_buffer, _IOFBF, sizeof(raw_output_buffer)))
		return fail("data stream buffer");
	if (data_direct)
		fprintf(raw_stream, "COMPARE_DATA_IO direct=1 alignment=%zu capture_bytes=%u locked=1 prefault=1\n",
			data_direct_alignment, MEMORY_CAPTURE_BYTES);
	return 0;
}
static int finish_data_disk(void)
{
	unsigned char *header = data_header;
	char hex[65];
	size_t done = 0;
	int status;

	if (data_fd < 0 || raw_stream == stderr)
		return -1;
	if (fflush(raw_stream) || ferror(raw_stream) ||
	    (data_direct && data_direct_flush(true)) || fsync(data_fd))
		data_failed = true;
	status = data_failed ? 1 : 0;
	sha256_final(&data_hash, hex);
	memset(header, 0, DATA_HEADER_BYTES);
	snprintf((char *)header, DATA_HEADER_BYTES, "MEMCOMP_DATA_V1 bytes=%" PRIu64
		 " sha256=%s status=%d\n", data_bytes_written, hex, status);
	while (done < DATA_HEADER_BYTES) {
		ssize_t n = pwrite(data_fd, header + done, DATA_HEADER_BYTES - done, (off_t)done);

		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0 || (size_t)n > DATA_HEADER_BYTES - done ||
		    (data_direct && (size_t)n % data_direct_alignment)) {
			status = 1;
			break;
		}
		done += (size_t)n;
	}
	if (fsync(data_fd))
		status = 1;
	fprintf(stdout, "COMPARE_DATA bytes=%" PRIu64 " sha256=%s status=%d\n",
		data_bytes_written, hex, status);
	fclose(raw_stream);
	raw_stream = stderr;
	close_fd(&data_fd);
	return status ? -1 : 0;
}
/* All raw framing, including rejected bytes and workload stderr, stays off UART. */
static int data_bytes(const char *buf, size_t length)
{
	if (fwrite(buf, 1, length, raw_stream) != length) {
		control_error = true;
		return -1;
	}
	return 0;
}
/* Invalid stdout is NOT silently truncated or published as valid CSV. */
static void rejected_bytes(const char *bytes, size_t length)
{
	static const char hex[] = "0123456789abcdef";
	char out[1080];
	size_t offset = 0;

	while (offset < length) {
		const char prefix[] = "COMPARE_STDOUT_REJECTED_HEX ";
		size_t take = length - offset;
		size_t n = sizeof(prefix) - 1;

		if (take > 512)
			take = 512;
		memcpy(out, prefix, n);
		for (size_t i = 0; i < take; i++) {
			unsigned char c = (unsigned char)bytes[offset + i];

			out[n++] = hex[c >> 4];
			out[n++] = hex[c & 15];
		}
		out[n++] = '\n';
		(void)data_bytes(out, n);
		offset += take;
	}
}
static int drain_csv(int fd, bool *eof)
{
	char input[4096];

	/* Bounded per-poll work, so continuous stdout cannot starve phases. */
	for (unsigned int turn = 0; turn < 16; turn++) {
		ssize_t n = read(fd, input, sizeof(input));

		if (n < 0) {
			if (errno == EINTR)
				continue;
			return errno == EAGAIN || errno == EWOULDBLOCK ? 0 : fail("stdout read");
		}
		if (!n) {
			*eof = true;
			if (csv_used || csv_rejected) {
				fprintf(raw_stream, "COMPARE_STDOUT_ERROR reason=nonterminated_line\n");
				rejected_bytes(csv_line, csv_used);
				csv_used = 0;
				control_error = true;
			}
			return 0;
		}
		for (ssize_t i = 0; i < n; i++) {
			unsigned char c = (unsigned char)input[i];

			if (csv_rejected) {
				csv_line[csv_used++] = (char)c;
				if (c == '\n' || csv_used == CSV_LINE_BYTES) {
					rejected_bytes(csv_line, csv_used);
					csv_used = 0;
				}
				if (c == '\n')
					csv_rejected = false;
				continue;
			}
			if (c == '\n') {
				const char prefix[] = "COMPARE_CSV ";
				size_t prefix_bytes = sizeof(prefix) - 1;

				memcpy(csv_frame, prefix, prefix_bytes);
				memcpy(csv_frame + prefix_bytes, csv_line, csv_used);
				csv_frame[prefix_bytes + csv_used] = '\n';
				if (data_bytes(csv_frame, prefix_bytes + csv_used + 1))
					return -1;
				csv_used = 0;
				continue;
			}
			if (csv_used == CSV_LINE_BYTES || !c) {
				fprintf(raw_stream, "COMPARE_STDOUT_ERROR reason=%s limit=%u\n",
					c ? "overlong_line" : "nul_byte", CSV_LINE_BYTES);
				control_error = true;
				rejected_bytes(csv_line, csv_used);
				csv_used = 0;
				csv_rejected = true;
			}
			csv_line[csv_used++] = (char)c;
		}
	}
	return 0;
}
static int phase_ack(int fd, uint64_t deadline)
{
	for (;;) {
		struct pollfd p = { .fd = fd, .events = POLLOUT };
		uint64_t now = monotonic_ns();
		ssize_t n;
		int ms;

		if (stop_signal || clock_error || now >= deadline) {
			errno = ETIMEDOUT;
			return fail("phase ACK deadline");
		}
		n = write(fd, "1", 1);
		if (n == 1)
			return 0;
		if (n < 0 && errno == EINTR)
			continue;
		if (n >= 0 || (errno != EAGAIN && errno != EWOULDBLOCK))
			return fail("phase ACK write");
		ms = (int)((deadline - now + 999999) / 1000000);
		if (poll(&p, 1, ms) < 0 && errno != EINTR)
			return fail("phase ACK poll");
	}
}
static int handle_phase(const char *phase, int ack_fd)
{
	uint64_t start = monotonic_ns();
	uint64_t deadline = start + 25 * NS_SECOND;

	fprintf(raw_stream, "COMPARE_PHASE monotonic_ns=%" PRIu64 " name=%s\n", start, phase);
	if (!strcmp(label, "w4") && !strcmp(backend, "zram-writeback") &&
	    !strcmp(phase, W4_PAGEOUT_PHASE)) {
		if (writeback_done) {
			errno = EPROTO;
			return fail("duplicate W4 pageout phase");
		}
		/* Match the existing child barrier for synchronous writeback. */
		deadline = start + 30 * NS_SECOND;
		snapshot("w4_before_explicit_writeback");
		if (telemetry_error || stop_signal)
			return -1;
		fprintf(raw_stream, "COMPARE_ZRAM_WB_BEGIN monotonic_ns=%" PRIu64
			" phase=%s idle=all writeback=idle\n", monotonic_ns(), phase);
		if (write_text("/sys/block/zram0/idle", "all\n") ||
		    write_text("/sys/block/zram0/writeback", "idle\n"))
			return fail("W4 synchronous idle writeback");
		writeback_done = true;
		fprintf(raw_stream, "COMPARE_ZRAM_WB_END monotonic_ns=%" PRIu64 "\n", monotonic_ns());
	}
	snapshot(phase);
	if (telemetry_error || control_error || stop_signal)
		return -1;
	if (fflush(raw_stream))
		return fail("phase data flush");
	fprintf(stdout, "COMPARE_PHASE name=%s\n", phase);
	if (tcdrain(STDOUT_FILENO))
		return fail("phase console drain");
	return phase_ack(ack_fd, deadline);
}
static int drain_phases(int fd, int ack_fd, bool *eof)
{
	for (unsigned int turn = 0; turn < 128; turn++) {
		char c;
		ssize_t n = read(fd, &c, 1);

		if (n < 0) {
			if (errno == EINTR)
				continue;
			return errno == EAGAIN || errno == EWOULDBLOCK ? 0 : fail("phase read");
		}
		if (!n) {
			*eof = true;
			if (phase_used) {
				errno = EPROTO;
				return fail("partial phase at EOF");
			}
			return 0;
		}
		if (c == '\n') {
			if (!phase_used) {
				errno = EPROTO;
				return fail("empty phase");
			}
			phase_line[phase_used] = '\0';
			phase_used = 0;
			if (handle_phase(phase_line, ack_fd))
				return -1;
			continue;
		}
		if ((!isalnum((unsigned char)c) && c != '_' && c != '.' && c != '-') ||
		    phase_used == sizeof(phase_line) - 1) {
			errno = EPROTO;
			return fail("phase name bounds/characters");
		}
		phase_line[phase_used++] = c;
	}
	return 0;
}
static void reap_children(void)
{
	for (;;) {
		int status;
		pid_t pid = waitpid(-1, &status, WNOHANG);

		if (pid <= 0) {
			if (pid < 0 && errno != ECHILD && errno != EINTR)
				control_error = true;
			return;
		}
		fprintf(raw_stream, "COMPARE_REAP monotonic_ns=%" PRIu64
			" pid=%ld raw_status=%d exit=%d signal=%d\n", monotonic_ns(),
			(long)pid, status, WIFEXITED(status) ? WEXITSTATUS(status) : -1,
			WIFSIGNALED(status) ? WTERMSIG(status) : 0);
		if (pid == workload) {
			workload_reaped = true;
			workload_status = status;
		} else {
			/* Only workload descendants are created by this fresh PID 1. */
			control_error = true;
		}
	}
}
struct channels {
	int phase[2];
	int ack[2];
	int csv[2];
	int startup[2];
	int diagnostic;
};
static void close_channels(struct channels *c)
{
	close_fd(&c->diagnostic);
	for (unsigned int i = 0; i < 2; i++) {
		close_fd(&c->phase[i]);
		close_fd(&c->ack[i]);
		close_fd(&c->csv[i]);
		close_fd(&c->startup[i]);
	}
}
static int prepare_w4_cgroup(void)
{
	static const char *const limits[] = {
		W4_CGROUP "/memory.max", W4_CGROUP "/memory.high",
		W4_CGROUP "/memory.swap.max", W4_CGROUP "/memory.zswap.max",
	};
	char value[64];

	if (strcmp(label, "w4"))
		return 0;
	/* Own one fresh group for every W4 backend. No limit or reclaim policy
	 * is tuned: the target and its pressure child inherit the same group.
	 */
	if (mkdir(W4_CGROUP, 0700))
		return fail("fresh W4 memory cgroup");
	w4_cgroup_created = true;
	for (size_t i = 0; i < sizeof(limits) / sizeof(limits[0]); i++) {
		if (read_text(limits[i], value, sizeof(value), false)) {
			if (errno == ENOENT && strcmp(backend, "zswap") &&
			    !strcmp(limits[i], W4_CGROUP "/memory.zswap.max"))
				continue;
			return fail("W4 default memory scope read");
		}
		if (strcmp(value, "max")) {
			errno = EPROTO;
			return fail("W4 default unlimited memory scope");
		}
	}
	fprintf(raw_stream, "COMPARE_CGROUP path=/compare-w4 limits=default_unlimited\n");
	return 0;
}
static _Noreturn void exec_child(struct channels *c)
{
	char write_fd[24], read_fd[24];
	struct sigaction normal = { .sa_handler = SIG_DFL };
	int error, flags;

	alarm(0);
	sigemptyset(&normal.sa_mask);
	close_fd(&c->phase[0]);
	close_fd(&c->ack[1]);
	close_fd(&c->csv[0]);
	close_fd(&c->startup[0]);
	if (sigaction(SIGINT, &normal, NULL) || sigaction(SIGTERM, &normal, NULL) ||
	    sigaction(SIGHUP, &normal, NULL) || sigaction(SIGALRM, &normal, NULL) ||
	    setpgid(0, 0) || fcntl(c->phase[1], F_SETFD, 0) ||
	    fcntl(c->ack[0], F_SETFD, 0))
		goto failed;
	flags = fcntl(c->csv[1], F_GETFL);
	if (flags < 0 || fcntl(c->csv[1], F_SETFL, flags & ~O_NONBLOCK) ||
	    dup2(c->csv[1], STDOUT_FILENO) < 0)
		goto failed;
	close_fd(&c->csv[1]);
	if (dup2(c->diagnostic, STDERR_FILENO) < 0)
		goto failed;
	close_fd(&c->diagnostic);
	if (w4_cgroup_created) {
		char actual[256];

		if (write_text(W4_CGROUP "/cgroup.procs", "0\n") ||
		    read_text("/proc/self/cgroup", actual, sizeof(actual), false))
			goto failed;
		if (strcmp(actual, "0::/compare-w4")) {
			errno = EPROTO;
			goto failed;
		}
		fputs("W4_CGROUP path=/compare-w4 limits=default_unlimited\n", stderr);
	}
	snprintf(write_fd, sizeof(write_fd), "%d", c->phase[1]);
	snprintf(read_fd, sizeof(read_fd), "%d", c->ack[0]);
	if (setenv("COMPARE_PHASE_WRITE_FD", write_fd, 1) ||
	    setenv("COMPARE_PHASE_READ_FD", read_fd, 1) ||
	    setenv("COMPARE_WORKLOAD_LABEL", label, 1) ||
	    (strcmp(backend, "memcomp-noswap") &&
	     setenv("COMPARE_SWAP_TYPE", "0", 1)) ||
	    (!strncmp(backend, "memcomp-", 8) &&
	     setenv("COMPARE_MEMCOMP_TYPE", "30", 1)))
		goto failed;
	execv(command[0], command);
failed:
	error = errno;
	while (write(c->startup[1], &error, sizeof(error)) < 0 && errno == EINTR)
		;
	_exit(126);
}
static void signal_workload(int signo)
{
	/* Child creates this group before exec; never signal the PID-1 group. */
	if (workload > 1 && kill(-workload, signo) && errno != ESRCH)
		control_error = true;
}
static int replay_diagnostic(int fd)
{
	struct stat st;
	char buf[4096];
	size_t left;

	if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size < 0 ||
	    (uint64_t)st.st_size > 16U * 1024U * 1024U || lseek(fd, 0, SEEK_SET) < 0)
		return fail("diagnostic spool bounds/rewind");
	left = (size_t)st.st_size;
	fprintf(raw_stream, "COMPARE_STDERR_BEGIN bytes=%zu storage=owned_tmpfs\n", left);
	while (left) {
		ssize_t n = read(fd, buf, left < sizeof(buf) ? left : sizeof(buf));

		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0 || data_bytes(buf, (size_t)n))
			return fail("diagnostic spool replay");
		left -= (size_t)n;
	}
	fputs("\nCOMPARE_STDERR_END\n", raw_stream);
	return ftruncate(fd, 0) ? fail("diagnostic spool release") : 0;
}
static int monitor_workload(void)
{
	struct channels c = {
		.phase = { -1, -1 }, .ack = { -1, -1 }, .csv = { -1, -1 },
		.startup = { -1, -1 }, .diagnostic = -1,
	};
	bool csv_eof = false, phase_eof = false, startup_eof = false;
	bool exec_ok = false, stopping = false, killed = false;
	uint64_t stop_at = 0, drain_deadline = 0;
	uint64_t next_sample = monotonic_ns() + NS_SECOND;
	unsigned char exec_error[sizeof(int)];
	size_t exec_used = 0;
	int result = 2;

	/* Only PID1 writes the data stream; children use the existing spool/pipes. */
	c.diagnostic = open("/run/compare-workload.stderr",
		O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
	if (c.diagnostic < 0 || unlink("/run/compare-workload.stderr")) {
		fail("owned diagnostic spool");
		goto out;
	}
	if (pipe2(c.phase, O_NONBLOCK | O_CLOEXEC) || pipe2(c.ack, O_NONBLOCK | O_CLOEXEC) ||
	    pipe2(c.csv, O_NONBLOCK | O_CLOEXEC) || pipe2(c.startup, O_NONBLOCK | O_CLOEXEC)) {
		fail("workload pipes");
		goto out;
	}
	if (c.phase[1] < 3 || c.phase[1] > 1023 || c.ack[0] < 3 ||
	    c.ack[0] > 1023 || c.phase[1] == c.ack[0]) {
		errno = ERANGE;
		fail("phase FDs must be distinct 3..1023");
		goto out;
	}
	fputs("COMPARE_WORKLOAD_BEGIN\n", raw_stream);
	if (fflush(raw_stream)) {
		fail("workload data flush");
		goto out;
	}
	fputs("COMPARE_WORKLOAD_BEGIN\n", stdout);
	if (tcdrain(STDOUT_FILENO)) {
		fail("workload console drain");
		goto out;
	}
	workload = fork();
	if (workload < 0) {
		fail("workload fork");
		goto out;
	}
	if (!workload)
		exec_child(&c);
	if (setpgid(workload, workload) && errno != EACCES && errno != ESRCH)
		control_error = true;
	close_fd(&c.phase[1]);
	close_fd(&c.ack[0]);
	close_fd(&c.csv[1]);
	close_fd(&c.startup[1]);
	fprintf(raw_stream, "COMPARE_WORKLOAD_PID pid=%ld\n", (long)workload);
	for (;;) {
		struct pollfd pollfds[3];
		uint64_t now = monotonic_ns();
		int rc, wait_ms = 100;

		if (clock_error) {
			signal_workload(SIGKILL);
			break;
		}
		reap_children();
		if (now >= run_deadline && !stop_signal)
			stop_signal = SIGALRM;
		if ((stop_signal || control_error || telemetry_error) && !stopping) {
			stopping = true;
			stop_at = now + NS_SECOND;
			drain_deadline = now + 5 * NS_SECOND;
			signal_workload(SIGTERM);
			close_fd(&c.ack[1]);
			close_fd(&c.phase[0]);
		}
		if (workload_reaped && !drain_deadline)
			drain_deadline = now + 3 * NS_SECOND;
		if (stopping && !killed && now >= stop_at) {
			signal_workload(SIGKILL);
			killed = true;
		}
		if (workload_reaped && csv_eof && startup_eof)
			break;
		if (drain_deadline && now >= drain_deadline) {
			fprintf(raw_stream, "COMPARE_DRAIN_ERROR stdout_eof=%d startup_eof=%d reaped=%d\n",
				csv_eof, startup_eof, workload_reaped);
			control_error = true;
			signal_workload(SIGKILL);
			break;
		}
		if (!stopping && !workload_reaped && !strcmp(label, "w2") && now >= next_sample) {
			snapshot("w2_periodic_1s");
			next_sample = monotonic_ns() + NS_SECOND;
			continue;
		}
		pollfds[0] = (struct pollfd){ .fd = csv_eof ? -1 : c.csv[0], .events = POLLIN };
		pollfds[1] = (struct pollfd){ .fd = phase_eof || stopping ? -1 : c.phase[0], .events = POLLIN };
		pollfds[2] = (struct pollfd){ .fd = startup_eof ? -1 : c.startup[0], .events = POLLIN };
		rc = poll(pollfds, 3, wait_ms);
		if (rc < 0) {
			if (errno != EINTR)
				control_error = true;
			continue;
		}
		if (pollfds[0].revents && drain_csv(c.csv[0], &csv_eof))
			control_error = true;
		if (pollfds[1].revents && drain_phases(c.phase[0], c.ack[1], &phase_eof))
			control_error = true;
		if (pollfds[2].revents) {
			unsigned char buf[sizeof(int)];
			ssize_t n = read(c.startup[0], buf, sizeof(buf));

			if (n > 0) {
				if ((size_t)n > sizeof(exec_error) - exec_used) {
					control_error = true;
				} else {
					memcpy(exec_error + exec_used, buf, (size_t)n);
					exec_used += (size_t)n;
				}
				if (exec_used == sizeof(int)) {
					int error;

					memcpy(&error, exec_error, sizeof(error));
					fprintf(raw_stream, "COMPARE_STARTUP_ERROR errno=%d\n", error);
					control_error = true;
				}
			} else if (!n) {
				startup_eof = true;
				exec_ok = exec_used == 0;
				if (!exec_ok)
					control_error = true;
			} else if (errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK) {
				control_error = true;
			}
		}
	}
	/* A completed child must not leave process descendants holding pipes. */
	signal_workload(SIGKILL);
	{
		uint64_t until = monotonic_ns() + 2 * NS_SECOND;

		for (;;) {
			int status;
			pid_t pid = waitpid(-1, &status, WNOHANG);

			if (pid > 0) {
				fprintf(raw_stream, "COMPARE_LATE_REAP pid=%ld raw_status=%d\n", (long)pid, status);
				if (pid == workload) {
					workload_reaped = true;
					workload_status = status;
				} else {
					control_error = true;
				}
				continue;
			}
			if (pid < 0 && errno == ECHILD)
				break;
			if (clock_error || monotonic_ns() >= until) {
				control_error = true;
				break;
			}
			struct timespec delay = { .tv_nsec = 10000000 };

			nanosleep(&delay, NULL);
		}
	}
	if (!csv_eof && drain_csv(c.csv[0], &csv_eof))
		control_error = true;
	if (!csv_eof || !startup_eof || !exec_ok || !workload_reaped)
		control_error = true;
	if (phase_used) {
		fprintf(raw_stream, "COMPARE_PHASE_ERROR reason=nonterminated_phase\n");
		control_error = true;
	}
	if (!strcmp(label, "w4") && !strcmp(backend, "zram-writeback") && !writeback_done) {
		fprintf(raw_stream, "COMPARE_PHASE_ERROR expected=%s writeback_not_performed=1\n", W4_PAGEOUT_PHASE);
		control_error = true;
	}
	if (stop_signal)
		result = stop_signal == SIGALRM ? 124 : 128 + stop_signal;
	else if (workload_reaped && WIFEXITED(workload_status))
		result = WEXITSTATUS(workload_status);
	else if (workload_reaped && WIFSIGNALED(workload_status))
		result = 128 + WTERMSIG(workload_status);
	if (!result && (control_error || telemetry_error))
		result = 2;
	fprintf(raw_stream, "COMPARE_WORKLOAD_END pid=%ld raw_status=%d status=%d stdout_eof=%d"
		" exec_confirmed=%d control_error=%d telemetry_error=%d\n", (long)workload,
		workload_status, result, csv_eof, exec_ok, control_error, telemetry_error);
out:
	if (c.diagnostic >= 0 && replay_diagnostic(c.diagnostic) && !result)
		result = 2;
	close_channels(&c);
	return result;
}
static int install_signals(void)
{
	struct sigaction action = { .sa_handler = signal_stop };
	struct sigaction ignored = { .sa_handler = SIG_IGN };
	struct sigaction normal = { .sa_handler = SIG_DFL };

	sigemptyset(&action.sa_mask);
	sigemptyset(&ignored.sa_mask);
	sigemptyset(&normal.sa_mask);
	if (sigaction(SIGALRM, &action, NULL) || sigaction(SIGTERM, &action, NULL) ||
	    sigaction(SIGINT, &action, NULL) || sigaction(SIGHUP, &action, NULL) ||
	    sigaction(SIGPIPE, &ignored, NULL) || sigaction(SIGCHLD, &normal, NULL))
		return fail("signal handlers");
	return 0;
}
int main(void)
{
	struct utsname kernel;
	int status = 2;

	raw_stream = stderr;
	setvbuf(stdout, NULL, _IONBF, 0);
	setvbuf(stderr, NULL, _IONBF, 0);
	if (regular_marker()) {
		fail("refuse: PID1 plus regular /COMPARE_DISPOSABLE_GUEST required");
		fputs("COMPARE_DONE status=2\n", stdout);
		return 2; /* Never reboot or change mounts/devices in an unauthorized context. */
	}
	if (install_signals() || (load_configuration() || load_memory_configuration()))
		goto done;
	run_deadline = monotonic_ns() + timeout_seconds * NS_SECOND;
	alarm((unsigned int)timeout_seconds);
	if (mount_filesystems() || open_data_disk())
		goto done;
	log_configuration();
	if (!uname(&kernel))
		fprintf(raw_stream, "COMPARE_KERNEL release=%s machine=%s\n", kernel.release, kernel.machine);
	fprintf(raw_stream, "COMPARE_CLOCK_TICKS value=%ld\n", sysconf(_SC_CLK_TCK));
	raw_file("boot", "/proc/cmdline", true);
	raw_file("boot", "/sys/kernel/mm/lru_gen/enabled", true);
	if (disable_thp())
		goto end_snapshot;
	/* Record actual state even if backend verification/setup subsequently fails. */
	if (wait_path("/dev/vdc"))
		goto end_snapshot;
	snapshot("before_backend_setup");
	if (telemetry_error || setup_backend() || prepare_w4_cgroup())
		goto end_snapshot;
	fputs("COMPARE_ATTRIBUTION dedicated_writeback_cpu=N/A shared_kworkers\n"
	      "COMPARE_ATTRIBUTION exact_backend_metadata=N/A no_object_accounting\n", raw_stream);
	snapshot("workload_start");
	if (telemetry_error || control_error || stop_signal)
		goto end_snapshot;
	status = monitor_workload();
end_snapshot:
	snapshot("workload_end");
	if (!status && (telemetry_error || control_error || stop_signal))
		status = stop_signal == SIGALRM ? 124 : stop_signal ? 128 + stop_signal : 2;
done:
	if (w4_cgroup_created && rmdir(W4_CGROUP)) {
		fail("owned W4 cgroup cleanup");
		status = status ? status : 2;
	}
	free(command_data);
	if (stop_signal)
		status = stop_signal == SIGALRM ? 124 : 128 + stop_signal;
	if (!status && (ferror(stdout) || ferror(raw_stream)))
		status = 2;
	/* Sync before the sentinel; no swapoff, device reset or foreign cgroup cleanup. */
	sync();
	if (stop_signal)
		status = stop_signal == SIGALRM ? 124 : 128 + stop_signal;
	if (!status && (control_error || telemetry_error || ferror(stdout) || ferror(raw_stream)))
		status = 2;
	if (finish_data_disk())
		status = status ? status : 2;
	fprintf(stdout, "COMPARE_DONE status=%d\n", status);
	(void)reboot(RB_POWER_OFF);
	/* Host timeout remains the authority if poweroff or kernel I/O stalls. */
	for (;;)
		pause();
}
