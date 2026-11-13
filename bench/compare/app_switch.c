// SPDX-License-Identifier: GPL-2.0-only
/* W2: backend-neutral multi-process app switching. See app_switch.md. */
#define _GNU_SOURCE
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <limits.h>
#include <poll.h>
#include <sched.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/types.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <time.h>
#include <termios.h>
#include <unistd.h>

#include "corpus.h"

#define MAX_WORKERS 64U
#define MAX_SWITCHES 1000000ULL
#define MAX_TIMEOUT_MS 3600000ULL
#define CGROUP2_MAGIC 0x63677270
#define MSG_MAGIC 0x57325357U
#define NS_PER_MS 1000000ULL
#define NS_PER_SEC 1000000000ULL

/* Native layout, only between forked copies of this same executable. */
enum operation { OP_INIT = 1, OP_TOUCH, OP_QUIT, OP_JOIN, OP_VERIFY };
enum answer { ANSWER_OK, ANSWER_RESOURCE, ANSWER_CORRUPT, ANSWER_ERROR };
enum transfer { IO_OK, IO_EOF, IO_PIPE, IO_TIMEOUT, IO_ERROR, IO_CANCEL };
struct command {
	uint64_t sequence;
	uint32_t magic;
	uint32_t operation;
};
struct reply {
	uint64_t sequence;
	uint64_t wall_ns;
	uint64_t cpu_ns;
	uint64_t user_us;
	uint64_t system_us;
	uint64_t minflt;
	uint64_t majflt;
	uint64_t nvcsw;
	uint64_t nivcsw;
	uint64_t maxrss_kib;
	uint64_t checked_words;
	uint64_t checksum;
	uint64_t mismatch_word;
	uint64_t expected;
	uint64_t observed;
	uint32_t magic;
	uint32_t operation;
	uint32_t worker;
	uint32_t answer;
	int32_t error;
};
struct options {
	const char *parent;
	unsigned int workers;
	size_t bytes;
	size_t page_size;
	uint64_t memory_max;
	uint64_t swap_max;
	uint64_t switches;
	uint64_t seed;
	uint64_t timeout_ms;
	int cpu;
	bool swap_unlimited;
	bool allow_pressure;
	bool functional_synthetic;
	const char *corpus_path;
};
struct worker {
	pid_t pid;
	int command[2];
	int reply[2];
	bool active;
	bool initialized;
	bool reaped;
	bool wait_valid;
	bool survived;
	int wait_status;
	struct rusage final_usage;
	const char *loss;
	int loss_errno;
	const char *controller_kill;
	uint64_t attempts;
	uint64_t ok;
	uint64_t unavailable;
};
static struct options opt = {
	.seed = 1, .timeout_ms = 10000, .cpu = -1, .swap_unlimited = true,
};
static struct bench_corpus corpus = { .fd = -1 };
static uint64_t corpus_first[MAX_WORKERS];
static struct worker workers[MAX_WORKERS];
static volatile sig_atomic_t interrupted;
static int parent_fd = -1, group_fd = -1;
static char group_name[96];
static struct stat group_identity;
static bool group_identified;
static bool own_group, infrastructure_error, content_error, output_error;
static uint64_t emitted_switches, successful_switches, unavailable_switches;

static void on_signal(int signo)
{
	interrupted = signo;
}
static void close_fd(int *fd)
{
	if (*fd >= 0) {
		/* Linux releases the fd even on EINTR: do not retry close(). */
		close(*fd);
		*fd = -1;
	}
}
/* stdout belongs to the CSV pipe; drain the serial console explicitly. */
static int console_drain(void)
{
	static int console = -1;
	int rc;

	if (interrupted) {
		errno = EINTR;
		return -1;
	}
	if (console < 0) {
		console = open("/dev/console", O_WRONLY | O_NOCTTY | O_CLOEXEC);
		if (console < 0)
			return -1;
	}
	do {
		rc = tcdrain(console);
	} while (rc && errno == EINTR && !interrupted);
	return rc;
}

static int clock_ns(clockid_t id, uint64_t *value)
{
	struct timespec ts;

	if (clock_gettime(id, &ts))
		return -1;
	*value = (uint64_t)ts.tv_sec * NS_PER_SEC + (uint64_t)ts.tv_nsec;
	return 0;
}
static int deadline_after(uint64_t ms, uint64_t *deadline)
{
	uint64_t now;

	if (clock_ns(CLOCK_MONOTONIC, &now))
		return -1;
	if (now > UINT64_MAX - ms * NS_PER_MS) {
		errno = EOVERFLOW;
		return -1;
	}
	*deadline = now + ms * NS_PER_MS;
	return 0;
}
static enum transfer transfer_bytes(int fd, void *buffer, size_t length,
				   bool writing, uint64_t deadline)
{
	unsigned char *p = buffer;
	size_t done = 0;

	while (done < length) {
		struct pollfd fds = { .fd = fd };
		uint64_t now, left;
		ssize_t n;
		int rc, ms = -1;

		if (interrupted) {
			errno = EINTR;
			return IO_CANCEL;
		}
		if (deadline) {
			if (clock_ns(CLOCK_MONOTONIC, &now))
				return IO_ERROR;
			if (now >= deadline) {
				errno = ETIMEDOUT;
				return IO_TIMEOUT;
			}
			left = deadline - now;
			ms = (int)((left + NS_PER_MS - 1) / NS_PER_MS);
		}
		n = writing ? write(fd, p + done, length - done) :
			      read(fd, p + done, length - done);
		if (n > 0) {
			done += (size_t)n;
			continue;
		}
		if (!n) {
			errno = EPIPE;
			return IO_EOF;
		}
		if (errno == EINTR)
			continue;
		if (errno == EPIPE)
			return IO_PIPE;
		if (errno != EAGAIN && errno != EWOULDBLOCK)
			return IO_ERROR;
		fds.events = writing ? POLLOUT : POLLIN;
		rc = poll(&fds, 1, ms);
		if (rc < 0) {
			if (errno == EINTR)
				continue;
			return IO_ERROR;
		}
		if (!rc) {
			errno = ETIMEDOUT;
			return IO_TIMEOUT;
		}
		if (fds.revents & POLLNVAL) {
			errno = EBADF;
			return IO_ERROR;
		}
		/* Drain on HUP; write on ERR reports real EPIPE. */
	}
	return IO_OK;
}
static const char *io_name(enum transfer result)
{
	switch (result) {
	case IO_OK: return "ok";
	case IO_EOF: return "eof";
	case IO_PIPE: return "epipe";
	case IO_TIMEOUT: return "timeout";
	case IO_CANCEL: return "interrupted";
	default: return "io_error";
	}
}
static int write_control(int dir, const char *name, const char *value)
{
	size_t len = strlen(value);
	int fd, saved;
	ssize_t n;

	fd = openat(dir, name, O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0)
		return -1;
	do {
		n = write(fd, value, len);
	} while (n < 0 && errno == EINTR && !interrupted);
	saved = n == (ssize_t)len ? 0 : (n < 0 ? errno : EIO);
	if (close(fd) && !saved)
		saved = errno;
	if (saved) {
		errno = saved;
		return -1;
	}
	return 0;
}
static int read_control(int dir, const char *name, char *buf, size_t size)
{
	int fd, saved = 0;
	size_t used = 0;
	ssize_t n;

	fd = openat(dir, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0)
		return -1;
	while (used < size - 1) {
		n = read(fd, buf + used, size - 1 - used);
		if (!n)
			break;
		if (n < 0) {
			if (errno == EINTR && !interrupted)
				continue;
			saved = errno;
			break;
		}
		used += (size_t)n;
	}
	if (!saved && used == size - 1) {
		char extra;

		n = read(fd, &extra, 1);
		if (n)
			saved = n < 0 ? errno : EOVERFLOW;
	}
	close(fd);
	buf[used] = '\0';
	if (saved) {
		errno = saved;
		return -1;
	}
	return 0;
}
static bool has_word(const char *text, const char *word)
{
	size_t n = strlen(word);

	while (*text) {
		const char *start;

		while (isspace((unsigned char)*text))
			text++;
		start = text;
		while (*text && !isspace((unsigned char)*text))
			text++;
		if ((size_t)(text - start) == n && !memcmp(start, word, n))
			return true;
	}
	return false;
}
static void usage(FILE *file)
{
	fputs("W2 application-switch workload: disposable Linux VM ONLY.\n"
	      "No pressure without --allow-pressure and explicit sizes.\n\n"
	      "app_switch --allow-pressure --cgroup-parent ABSOLUTE_PATH\n"
	      "  --workers N --worker-bytes BYTES --memory-max BYTES --switches N\n"
	      "  --corpus /dev/vdc [--swap-max max|BYTES] [--seed N] [--cpu N]\n"
	      "  --functional-synthetic is TEST ONLY, not performance evidence.\n"
	      "  Each real worker requires a different ordered corpus source.\n\n"
	      "Parent must already enable memory in cgroup.subtree_control.\n"
	      "Only a unique direct child is created/modified.\n"
	      "workers: 1..64; switches: 1..1000000; seed: unsigned 64-bit.\n"
	      "Sizes: decimal bytes, page multiples, no suffixes; swap-max may be 0.\n"
	      "swap-max defaults to max; seed to 1; timeout-ms to 10000 (1..3600000), independently per ACK/verify.\n"
	      "cpu optionally pins workers to ONE allowed guest logical CPU.\n"
	      "CSV: stdout; configuration, raw snapshots, summary: stderr.\n"
	      "No backend setup, mounts, swap or global tuning is done.\n", file);
}
static int parse_number(const char *s, uint64_t max, uint64_t *value)
{
	const char *p;
	char *end;
	unsigned long long n;

	if (!s || !*s)
		return -1;
	for (p = s; *p; p++)
		if (*p < '0' || *p > '9')
			return -1;
	errno = 0;
	n = strtoull(s, &end, 10);
	if (errno || *end || n > max)
		return -1;
	*value = (uint64_t)n;
	return 0;
}
static int parse_options(int argc, char **argv)
{
	enum {
		OPT_ALLOW = 256, OPT_PARENT, OPT_WORKERS, OPT_BYTES, OPT_MEMORY,
		OPT_SWAP, OPT_SWITCHES, OPT_SEED, OPT_CPU, OPT_TIMEOUT, OPT_CORPUS, OPT_FUNCTIONAL,
	};
	static const struct option args[] = {
		{ "corpus", required_argument, NULL, OPT_CORPUS },
		{ "functional-synthetic", no_argument, NULL, OPT_FUNCTIONAL },
		{ "help", no_argument, NULL, 'h' },
		{ "allow-pressure", no_argument, NULL, OPT_ALLOW },
		{ "cgroup-parent", required_argument, NULL, OPT_PARENT },
		{ "workers", required_argument, NULL, OPT_WORKERS },
		{ "worker-bytes", required_argument, NULL, OPT_BYTES },
		{ "memory-max", required_argument, NULL, OPT_MEMORY },
		{ "swap-max", required_argument, NULL, OPT_SWAP },
		{ "switches", required_argument, NULL, OPT_SWITCHES },
		{ "seed", required_argument, NULL, OPT_SEED },
		{ "cpu", required_argument, NULL, OPT_CPU },
		{ "timeout-ms", required_argument, NULL, OPT_TIMEOUT },
		{ NULL, 0, NULL, 0 },
	};
	uint64_t value;
	long page_size;
	int c;

	if (argc == 1) {
		usage(stdout);
		return 1;
	}
	while ((c = getopt_long(argc, argv, "h", args, NULL)) != -1) {
		if (c == 'h') {
			usage(stdout);
			return 1;
		}
		if (c == OPT_CORPUS) {
			if (opt.corpus_path || optarg[0] != '/')
				return -1;
			opt.corpus_path = optarg;
			continue;
		}
		if (c == OPT_FUNCTIONAL) {
			opt.functional_synthetic = true;
			continue;
		}
		if (c == OPT_ALLOW) {
			opt.allow_pressure = true;
			continue;
		}
		if (c == OPT_PARENT) {
			opt.parent = optarg;
			continue;
		}
		if (c == OPT_SWAP && !strcmp(optarg, "max")) {
			opt.swap_unlimited = true;
			continue;
		}
		if (c < OPT_WORKERS || c > OPT_TIMEOUT ||
		    parse_number(optarg, UINT64_MAX, &value)) {
			fputs("Invalid option or non-decimal/out-of-range number\n", stderr);
			return -1;
		}
		switch (c) {
		case OPT_WORKERS:
			if (!value || value > MAX_WORKERS)
				return -1;
			opt.workers = (unsigned int)value;
			break;
		case OPT_BYTES:
			if (!value || value > (uint64_t)PTRDIFF_MAX || value > SIZE_MAX)
				return -1;
			opt.bytes = (size_t)value;
			break;
		case OPT_MEMORY:
			if (!value || value > INT64_MAX)
				return -1;
			opt.memory_max = value;
			break;
		case OPT_SWAP:
			if (value > INT64_MAX)
				return -1;
			opt.swap_max = value;
			opt.swap_unlimited = false;
			break;
		case OPT_SWITCHES:
			if (!value || value > MAX_SWITCHES)
				return -1;
			opt.switches = value;
			break;
		case OPT_SEED:
			opt.seed = value;
			break;
		case OPT_CPU:
			if (value >= CPU_SETSIZE)
				return -1;
			opt.cpu = (int)value;
			break;
		case OPT_TIMEOUT:
			if (!value || value > MAX_TIMEOUT_MS)
				return -1;
			opt.timeout_ms = value;
			break;
		default:
			return -1;
		}
	}
	if (optind != argc || !opt.allow_pressure || !opt.parent ||
	    opt.parent[0] != '/' || !opt.workers || !opt.bytes ||
	    !opt.memory_max || !opt.switches) {
		fputs("Refusing pressure: explicit opt-in, parent and sizes required\n",
		      stderr);
		return -1;
	}
	if (strlen(opt.parent) >= PATH_MAX)
		return -1;
	for (const char *p = opt.parent; *p; p++)
		if ((unsigned char)*p < 32 || (unsigned char)*p == 127)
			return -1;
	page_size = sysconf(_SC_PAGESIZE);
	if (page_size < 32 || page_size % 32)
		return -1;
	opt.page_size = (size_t)page_size;
	if (opt.bytes % opt.page_size || opt.memory_max % opt.page_size ||
	    (!opt.swap_unlimited && opt.swap_max % opt.page_size)) {
		fputs("All byte sizes must be native-page multiples\n", stderr);
		return -1;
	}
	if (opt.bytes > SIZE_MAX / opt.workers ||
	    (uint64_t)opt.bytes > (uint64_t)INT64_MAX / opt.workers) {
		fputs("Total worker bytes overflow the supported size range\n", stderr);
		return -1;
	}
	if (opt.cpu >= 0) {
		cpu_set_t allowed;

		if (sched_getaffinity(0, sizeof(allowed), &allowed) ||
		    !CPU_ISSET(opt.cpu, &allowed)) {
			fputs("Requested CPU is outside inherited affinity\n", stderr);
			return -1;
		}
	}
	if (opt.functional_synthetic) {
		if (opt.corpus_path)
			return -1;
	} else {
		if (!opt.corpus_path || bench_corpus_open(&corpus, opt.corpus_path) ||
		    corpus.page_size != opt.page_size || opt.workers > corpus.source_count) {
			fputs("Real corpus unavailable or not enough distinct applications\n", stderr);
			return -1;
		}
		for (unsigned int id = 0; id < opt.workers; id++) {
			if (bench_corpus_range(&corpus, corpus.sources[id].label, 0,
					       opt.bytes / opt.page_size, &corpus_first[id])) {
				fputs("Application corpus too small; refusing repetition\n", stderr);
				return -1;
			}
			fprintf(stderr, "CORPUS_WORKER worker=%u source=%s first_page=%" PRIu64
				" pages=%zu\n", id, corpus.sources[id].label,
				corpus_first[id], opt.bytes / opt.page_size);
		}
	}
	fprintf(stderr, "INPUT kind=%s measurement_eligible=%d touch=read_only_first_byte_per_page"
		" verification=sha256_after_timing_ack verify_timeout=independent\n",
		opt.functional_synthetic ? "synthetic-functional-only" : "real-corpus-v1",
		!opt.functional_synthetic);
	return 0;
}

static int install_signals(void)
{
	struct sigaction sa = { .sa_handler = on_signal };
	struct sigaction ignore = { .sa_handler = SIG_IGN };
	struct sigaction normal = { .sa_handler = SIG_DFL };

	sigemptyset(&sa.sa_mask);
	sigemptyset(&ignore.sa_mask);
	sigemptyset(&normal.sa_mask);
	/* No SA_RESTART: blocking operations must notice cancellation. */
	if (sigaction(SIGINT, &sa, NULL) || sigaction(SIGTERM, &sa, NULL) ||
	    sigaction(SIGHUP, &sa, NULL) || sigaction(SIGPIPE, &ignore, NULL) ||
	    sigaction(SIGCHLD, &normal, NULL))
		return -1;
	return 0;
}
static int create_group(void)
{
	struct statfs fs;
	char buf[4096], number[64];
	uint64_t now;
	unsigned int attempt;

	parent_fd = open(opt.parent, O_RDONLY | O_DIRECTORY | O_CLOEXEC |
			 O_NOFOLLOW);
	if (parent_fd < 0 || fstatfs(parent_fd, &fs))
		return -1;
	if ((unsigned long)fs.f_type != CGROUP2_MAGIC) {
		errno = ENODEV;
		return -1;
	}
	if (read_control(parent_fd, "cgroup.subtree_control", buf, sizeof(buf)))
		return -1;
	if (!has_word(buf, "memory")) {
		errno = ENODEV;
		return -1;
	}
	if (clock_ns(CLOCK_MONOTONIC, &now))
		return -1;
	for (attempt = 0; attempt < 128; attempt++) {
		snprintf(group_name, sizeof(group_name), "app-switch.%ld.%" PRIx64
			 ".%u", (long)getpid(), now, attempt);
		if (!mkdirat(parent_fd, group_name, 0700)) {
			own_group = true;
			if (fstatat(parent_fd, group_name, &group_identity,
				    AT_SYMLINK_NOFOLLOW))
				return -1;
			group_identified = true;
			break;
		}
		if (errno != EEXIST)
			return -1;
	}
	if (!own_group) {
		errno = EEXIST;
		return -1;
	}
	group_fd = openat(parent_fd, group_name, O_RDONLY | O_DIRECTORY |
			  O_CLOEXEC | O_NOFOLLOW);
	if (group_fd < 0)
		return -1;
	snprintf(number, sizeof(number), "%" PRIu64 "\n", opt.memory_max);
	if (write_control(group_fd, "memory.max", number) ||
	    read_control(group_fd, "memory.max", buf, sizeof(buf)))
		return -1;
	if (strcmp(buf, number)) {
		errno = EPROTO;
		return -1;
	}
	if (opt.swap_unlimited)
		snprintf(number, sizeof(number), "max\n");
	else
		snprintf(number, sizeof(number), "%" PRIu64 "\n", opt.swap_max);
	if (write_control(group_fd, "memory.swap.max", number) ||
	    read_control(group_fd, "memory.swap.max", buf, sizeof(buf)))
		return -1;
	if (strcmp(buf, number)) {
		errno = EPROTO;
		return -1;
	}
	fprintf(stderr, "OWNED_CGROUP parent=%s child=%s memory.max=%" PRIu64
		" memory.swap.max=%s", opt.parent, group_name, opt.memory_max, buf);
	return 0;
}

/* Raw bounded snapshots; never assert thresholds on cgroup event values. */
static void snapshot_file(const char *phase, int dir, const char *name,
			  bool required)
{
	char buf[1024];
	size_t total = 0;
	int fd;
	ssize_t n;

	fd = openat(dir, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0) {
		fprintf(stderr, "RAW_NA phase=%s file=%s errno=%d\n", phase, name, errno);
		if (required)
			infrastructure_error = true;
		return;
	}
	fprintf(stderr, "RAW_BEGIN phase=%s file=%s\n", phase, name);
	while (total < 262144U) {
		n = read(fd, buf, sizeof(buf));
		if (!n)
			break;
		if (n < 0) {
			if (errno == EINTR && !interrupted)
				continue;
			fprintf(stderr, "\nRAW_READ_ERROR errno=%d\n", errno);
			if (required)
				infrastructure_error = true;
			break;
		}
		if (fwrite(buf, 1, (size_t)n, stderr) != (size_t)n)
			output_error = true;
		total += (size_t)n;
	}
	if (total == 262144U)
		fputs("\nRAW_TRUNCATED limit=262144\n", stderr);
	fprintf(stderr, "\nRAW_END phase=%s file=%s\n", phase, name);
	close(fd);
}
static void snapshot_kswapd(const char *phase)
{
	DIR *proc = opendir("/proc");
	struct dirent *entry;
	char path[96], name[128];
	unsigned int count = 0;

	if (!proc) {
		fprintf(stderr, "RAW_NA phase=%s file=kswapd_stats errno=%d\n",
			phase, errno);
		return;
	}
	while ((entry = readdir(proc)) != NULL && count < 65536U) {
		const char *p = entry->d_name;

		if (!*p)
			continue;
		while (*p >= '0' && *p <= '9')
			p++;
		if (*p)
			continue;
		count++;
		if (strlen(entry->d_name) > 20)
			continue;
		snprintf(path, sizeof(path), "%s/comm", entry->d_name);
		if (read_control(dirfd(proc), path, name, sizeof(name)))
			continue;
		if (strncmp(name, "kswapd", 6))
			continue;
		snprintf(path, sizeof(path), "%s/stat", entry->d_name);
		snapshot_file(phase, dirfd(proc), path, false);
	}
	closedir(proc);
}
static void snapshot(const char *phase)
{
	static const char *const cg_files[] = {
		"memory.max", "memory.swap.max", "memory.current", "memory.peak",
		"memory.swap.current", "memory.events", "memory.events.local",
		"memory.swap.events", "memory.pressure", "memory.stat",
		"cpu.stat", "cpu.pressure", "io.stat", "io.pressure",
		"cgroup.events", "cgroup.procs",
	};
	static const char *const system_files[] = {
		"/proc/uptime", "/proc/swaps", "/proc/meminfo", "/proc/vmstat",
		"/proc/stat", "/proc/diskstats", "/proc/pressure/memory",
		"/proc/pressure/cpu", "/proc/pressure/io", "/proc/self/cgroup",
		"/sys/kernel/mm/transparent_hugepage/enabled",
	};
	uint64_t now = 0;
	size_t i;

	if (clock_ns(CLOCK_MONOTONIC, &now))
		infrastructure_error = true;
	fprintf(stderr, "SNAPSHOT phase=%s monotonic_ns=%" PRIu64 "\n", phase, now);
	for (i = 0; i < sizeof(cg_files) / sizeof(cg_files[0]); i++)
		snapshot_file(phase, group_fd, cg_files[i],
			      !strcmp(cg_files[i], "memory.events"));
	for (i = 0; i < sizeof(system_files) / sizeof(system_files[0]); i++)
		snapshot_file(phase, AT_FDCWD, system_files[i], false);
	snapshot_kswapd(phase);
}
static uint64_t mix64(uint64_t x)
{
	x = (x ^ (x >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
	x = (x ^ (x >> 27)) * UINT64_C(0x94d049bb133111eb);
	return x ^ (x >> 31);
}
/* No second mapping, page-sized replica, or resident expected-data array. */
static uint64_t expected_word(unsigned int id, size_t word)
{
	uint64_t words_per_page = opt.page_size / sizeof(uint64_t);
	uint64_t page = word / words_per_page;
	uint64_t offset = word % words_per_page;
	uint64_t key;

	/* 7/8 pages repeat a seeded quarter-page; 1/8 is full-width random. */
	if ((page + id) % 8 != 7)
		offset %= words_per_page / 4;
	key = mix64(opt.seed ^ (UINT64_C(0x9e3779b97f4a7c15) * (id + 1)));
	key ^= mix64(page + UINT64_C(0xd1b54a32d192ed03));
	return mix64(key + offset * UINT64_C(0x9e3779b97f4a7c15));
}
static uint64_t timeval_us(struct timeval t)
{
	return (uint64_t)t.tv_sec * 1000000ULL + (uint64_t)t.tv_usec;
}
static int collect_usage(struct reply *r, const struct rusage *before,
			 const struct rusage *after)
{
	uint64_t u0 = timeval_us(before->ru_utime);
	uint64_t s0 = timeval_us(before->ru_stime);
	uint64_t u1 = timeval_us(after->ru_utime);
	uint64_t s1 = timeval_us(after->ru_stime);

	if (u1 < u0 || s1 < s0 || after->ru_minflt < before->ru_minflt ||
	    after->ru_majflt < before->ru_majflt ||
	    after->ru_nvcsw < before->ru_nvcsw ||
	    after->ru_nivcsw < before->ru_nivcsw || after->ru_maxrss < 0) {
		errno = EPROTO;
		return -1;
	}
	r->user_us = u1 - u0;
	r->system_us = s1 - s0;
	r->minflt = (uint64_t)(after->ru_minflt - before->ru_minflt);
	r->majflt = (uint64_t)(after->ru_majflt - before->ru_majflt);
	r->nvcsw = (uint64_t)(after->ru_nvcsw - before->ru_nvcsw);
	r->nivcsw = (uint64_t)(after->ru_nivcsw - before->ru_nivcsw);
	r->maxrss_kib = (uint64_t)after->ru_maxrss;
	return 0;
}
static void worker_work(unsigned int id, unsigned int operation,
			volatile uint64_t **mapping, struct reply *r)
{
	struct rusage before, after;
	uint64_t wall_start, wall_end, cpu_start, cpu_end;
	size_t i, words = opt.bytes / sizeof(uint64_t);

	if (console_drain() || getrusage(RUSAGE_SELF, &before) ||
	    clock_ns(CLOCK_MONOTONIC, &wall_start) ||
	    clock_ns(CLOCK_PROCESS_CPUTIME_ID, &cpu_start))
		goto error;
	if (operation == OP_INIT) {
		void *map;

		if (*mapping) {
			errno = EPROTO;
			goto error;
		}
		map = mmap(NULL, opt.bytes, PROT_READ | PROT_WRITE,
			   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if (map == MAP_FAILED) {
			r->error = errno;
			r->answer = errno == ENOMEM ? ANSWER_RESOURCE : ANSWER_ERROR;
			goto metrics;
		}
		*mapping = map;
		if (madvise(map, opt.bytes, MADV_NOHUGEPAGE))
			goto error;
		if (!opt.functional_synthetic) {
			for (i = 0; i < opt.bytes / opt.page_size; i++)
				if (bench_corpus_load(&corpus, corpus_first[id] + i,
						      (unsigned char *)map + i * opt.page_size))
					goto error;
		} else {
			for (i = 0; i < words; i++)
				(*mapping)[i] = expected_word(id, i);
		}
	} else if (operation == OP_TOUCH && *mapping && !opt.functional_synthetic) {
		/* First-byte reads only; no target writes or hash I/O in this timer. */
		for (i = 0; i < opt.bytes / opt.page_size; i++)
			(void)*((volatile unsigned char *)*mapping + i * opt.page_size);
	} else if (operation == OP_TOUCH && *mapping) {
		for (i = 0; i < words; i++) {
			uint64_t expected = expected_word(id, i);
			uint64_t observed = (*mapping)[i];

			r->checked_words++;
			r->checksum ^= mix64(observed ^ (uint64_t)i);
			if (observed != expected) {
				r->answer = ANSWER_CORRUPT;
				r->mismatch_word = (uint64_t)i;
				r->expected = expected;
				r->observed = observed;
				break;
			}
			/* Volatile read/write of the SAME full set every switch. */
			(*mapping)[i] = observed;
		}
	} else {
		errno = EPROTO;
		goto error;
	}
metrics:
	if (clock_ns(CLOCK_PROCESS_CPUTIME_ID, &cpu_end) ||
	    clock_ns(CLOCK_MONOTONIC, &wall_end) ||
	    getrusage(RUSAGE_SELF, &after))
		goto error;
	if (wall_end < wall_start || cpu_end < cpu_start) {
		errno = EPROTO;
		goto error;
	}
	r->wall_ns = wall_end - wall_start;
	r->cpu_ns = cpu_end - cpu_start;
	if (collect_usage(r, &before, &after))
		goto error;
	return;
error:
	r->error = errno;
	/* Never erase observed corruption if a later statistics call fails. */
	if (r->answer != ANSWER_CORRUPT)
		r->answer = ANSWER_ERROR;
}
/* Called only after the controller has stopped its ACK clock and granted
 * OP_VERIFY. No target writes or raw-word replies, even on corruption.
 * A separate per-verification deadline also bounds multi-page verification;
 * the controller independently bounds the entire verify request/reply.
 */
static void worker_verify(unsigned int id, const volatile uint64_t *mapping,
			  struct reply *r)
{
	uint64_t deadline, now;

	r->checked_words = 0;
	r->checksum = r->expected = r->observed = 0;
	r->mismatch_word = UINT64_MAX;
	if (opt.functional_synthetic || !mapping) {
		errno = EPROTO;
		goto error;
	}
	if (deadline_after(opt.timeout_ms, &deadline))
		goto error;
	for (size_t i = 0; i < opt.bytes / opt.page_size; i++) {
		const unsigned char *page = (const unsigned char *)mapping + i * opt.page_size;

		if (interrupted) {
			errno = EINTR;
			goto error;
		}
		if (clock_ns(CLOCK_MONOTONIC, &now))
			goto error;
		if (now >= deadline) {
			errno = ETIMEDOUT;
			goto error;
		}
		if (bench_corpus_verify(&corpus, corpus_first[id] + i, page)) {
			if (errno != EBADMSG)
				goto error;
			r->answer = ANSWER_CORRUPT;
			r->mismatch_word = i * opt.page_size / sizeof(uint64_t);
			return;
		}
		r->checked_words += opt.page_size / sizeof(uint64_t);
	}
	if (clock_ns(CLOCK_MONOTONIC, &now))
		goto error;
	if (now >= deadline) {
		errno = ETIMEDOUT;
		goto error;
	}
	return;
error:
	r->error = errno;
	r->answer = ANSWER_ERROR;
}
static void make_reply(struct reply *r, unsigned int id,
		       unsigned int operation, uint64_t sequence)
{
	memset(r, 0, sizeof(*r));
	r->magic = MSG_MAGIC;
	r->worker = id;
	r->operation = operation;
	r->sequence = sequence;
	r->mismatch_word = UINT64_MAX;
}
static _Noreturn void send_reply_and_exit(int fd, struct reply *r, int exit_code)
{
	uint64_t deadline;

	if (!deadline_after(opt.timeout_ms, &deadline))
		(void)transfer_bytes(fd, r, sizeof(*r), true, deadline);
	_exit(exit_code);
}
/* No cgroup setup here: the joined worker already owns these pipe ends.
 * The second command is a barrier: hash I/O cannot start before the parent
 * receives the first reply and records elapsed_ns. A pending verification
 * must be completed before another switch (or graceful quit) is accepted.
 */
static _Noreturn void worker_commands(unsigned int id, int rd, int wr)
{
	volatile uint64_t *mapping = NULL;
	bool pending = false;
	uint64_t sequence = 0, deadline = 0;
	struct reply r;

	for (;;) {
		struct command command;

		/* Ordinary idle time may span other workers; a verify grant may not. */
		if (transfer_bytes(rd, &command, sizeof(command), false,
				   pending ? deadline : 0) != IO_OK)
			_exit(11);
		make_reply(&r, id, command.operation, command.sequence);
		if (command.magic != MSG_MAGIC ||
		    (pending ? command.operation != OP_VERIFY || command.sequence != sequence :
		     command.operation != OP_INIT && command.operation != OP_TOUCH &&
		     command.operation != OP_QUIT)) {
			r.answer = ANSWER_ERROR;
			r.error = EPROTO;
			send_reply_and_exit(wr, &r, 11);
		}
		if (command.operation == OP_QUIT) {
			if (mapping && munmap((void *)mapping, opt.bytes)) {
				r.answer = ANSWER_ERROR;
				r.error = errno;
			}
			send_reply_and_exit(wr, &r, r.answer ? 11 : 0);
		}
		if (command.operation == OP_VERIFY) {
			worker_verify(id, mapping, &r);
			pending = false;
		} else {
			worker_work(id, command.operation, &mapping, &r);
			pending = command.operation == OP_TOUCH && !opt.functional_synthetic;
			sequence = command.sequence;
		}
		if (r.answer != ANSWER_OK)
			send_reply_and_exit(wr, &r,
					r.answer == ANSWER_RESOURCE ? 10 :
					r.answer == ANSWER_CORRUPT ? 12 : 11);
		if (deadline_after(opt.timeout_ms, &deadline) ||
		    transfer_bytes(wr, &r, sizeof(r), true, deadline) != IO_OK)
			_exit(11);
	}
}
static _Noreturn void child_main(unsigned int id, pid_t controller)
{
	struct sigaction normal = { .sa_handler = SIG_DFL };
	struct worker *w = &workers[id];
	struct reply r;
	char pid_text[32];
	uint64_t deadline;
	unsigned int j;
	int rd = w->command[0], wr = w->reply[1];

	/* No peer writers (false EOF) or peer readers (false EPIPE). */
	for (j = 0; j < opt.workers; j++) {
		int *ends[] = {
			&workers[j].command[0], &workers[j].command[1],
			&workers[j].reply[0], &workers[j].reply[1],
		};
		size_t k;

		for (k = 0; k < sizeof(ends) / sizeof(ends[0]); k++)
			if (*ends[k] != rd && *ends[k] != wr)
				close_fd(ends[k]);
	}
	close_fd(&parent_fd);
	interrupted = 0;
	sigemptyset(&normal.sa_mask);
	make_reply(&r, id, OP_JOIN, 0);
	if (sigaction(SIGINT, &normal, NULL) ||
	    sigaction(SIGTERM, &normal, NULL) ||
	    sigaction(SIGHUP, &normal, NULL) ||
	    prctl(PR_SET_PDEATHSIG, SIGKILL) || getppid() != controller) {
		r.answer = ANSWER_ERROR;
		r.error = errno ? errno : ESRCH;
		send_reply_and_exit(wr, &r, 11);
	}
	if (opt.cpu >= 0) {
		cpu_set_t set;

		CPU_ZERO(&set);
		CPU_SET(opt.cpu, &set);
		if (sched_setaffinity(0, sizeof(set), &set)) {
			r.answer = ANSWER_ERROR;
			r.error = errno;
			send_reply_and_exit(wr, &r, 11);
		}
	}
	snprintf(pid_text, sizeof(pid_text), "%ld\n", (long)getpid());
	if (write_control(group_fd, "cgroup.procs", pid_text)) {
		r.answer = ANSWER_ERROR;
		r.error = errno;
		send_reply_and_exit(wr, &r, 11);
	}
	close_fd(&group_fd);
	if (deadline_after(opt.timeout_ms, &deadline) ||
	    transfer_bytes(wr, &r, sizeof(r), true, deadline) != IO_OK)
		_exit(11);
	worker_commands(id, rd, wr);
}

static void reap_one(struct worker *w)
{
	pid_t got;

	if (w->pid <= 0 || w->reaped)
		return;
	do {
		/* wait4 is waitpid with per-child final cumulative rusage. */
		got = wait4(w->pid, &w->wait_status, WNOHANG, &w->final_usage);
	} while (got < 0 && errno == EINTR);
	if (got == w->pid) {
		w->reaped = true;
		w->wait_valid = true;
		if (WIFEXITED(w->wait_status)) {
			int code = WEXITSTATUS(w->wait_status);

			if (code == 12)
				content_error = true;
			else if (code && code != 10)
				infrastructure_error = true;
		} else if (WIFSIGNALED(w->wait_status) &&
			   WTERMSIG(w->wait_status) != SIGKILL) {
			/* OOM uses SIGKILL; a crash is not a valid resource sample. */
			infrastructure_error = true;
		}
	} else if (got < 0) {
		fprintf(stderr, "WAIT_ERROR pid=%ld errno=%d\n", (long)w->pid, errno);
		infrastructure_error = true;
		/* ECHILD: no longer own this PID, NEVER kill it. */
		w->reaped = true;
	}
}
static void reap_all(void)
{
	unsigned int i;

	for (i = 0; i < opt.workers; i++)
		reap_one(&workers[i]);
}
static void kill_owned(struct worker *w, const char *reason)
{
	reap_one(w);
	if (w->pid <= 0 || w->reaped || w->controller_kill)
		return;
	if (!kill(w->pid, SIGKILL)) {
		w->controller_kill = reason;
	} else if (errno != ESRCH) {
		fprintf(stderr, "KILL_ERROR pid=%ld errno=%d\n", (long)w->pid, errno);
		infrastructure_error = true;
	}
}
static void lose_worker(struct worker *w, const char *reason, int error)
{
	w->active = false;
	if (!w->loss) {
		w->loss = reason;
		w->loss_errno = error;
	}
	kill_owned(w, reason);
	close_fd(&w->command[1]);
	close_fd(&w->reply[0]);
}
static bool valid_reply(const struct reply *r, unsigned int id,
			unsigned int operation, uint64_t sequence)
{
	return r->magic == MSG_MAGIC && r->worker == id &&
	       r->operation == operation && r->sequence == sequence &&
	       r->answer <= ANSWER_ERROR && r->error >= 0;
}
static enum transfer exchange(struct worker *w, unsigned int id,
			      unsigned int operation,
			      uint64_t sequence, struct reply *r,
			      uint64_t *elapsed)
{
	struct command command = {
		.sequence = sequence, .magic = MSG_MAGIC, .operation = operation,
	};
	uint64_t start, end, deadline;
	enum transfer result;
	int saved;

	*elapsed = 0;
	if (console_drain() || clock_ns(CLOCK_MONOTONIC, &start) ||
	    deadline_after(opt.timeout_ms, &deadline))
		return IO_ERROR;
	result = transfer_bytes(w->command[1], &command, sizeof(command),
				true, deadline);
	if (result == IO_OK)
		result = transfer_bytes(w->reply[0], r, sizeof(*r), false, deadline);
	saved = errno;
	if (clock_ns(CLOCK_MONOTONIC, &end))
		return IO_ERROR;
	if (end < start) {
		errno = EPROTO;
		return IO_ERROR;
	}
	*elapsed = end - start;
	errno = saved;
	if (result != IO_OK || operation != OP_TOUCH || opt.functional_synthetic)
		return result;
	if (!valid_reply(r, id, operation, sequence))
		goto protocol_error;
	if (r->answer != ANSWER_OK)
		return IO_OK;
	if (r->error || r->checked_words || r->checksum || r->expected || r->observed ||
	    r->mismatch_word != UINT64_MAX)
		goto protocol_error;

	/* elapsed_ns is final before this authorization. Never wait for hash I/O
	 * on the ACK deadline or fold it into either controller/worker timing.
	 */
	struct reply verification;

	command.operation = OP_VERIFY;
	if (deadline_after(opt.timeout_ms, &deadline))
		return IO_ERROR;
	result = transfer_bytes(w->command[1], &command, sizeof(command), true, deadline);
	if (result == IO_OK)
		result = transfer_bytes(w->reply[0], &verification, sizeof(verification),
					false, deadline);
	if (result != IO_OK)
		return result;
	if (!valid_reply(&verification, id, OP_VERIFY, sequence) ||
	    verification.answer == ANSWER_RESOURCE || verification.checksum ||
	    verification.expected || verification.observed ||
	    verification.checked_words > opt.bytes / sizeof(uint64_t) ||
	    (verification.answer == ANSWER_OK &&
	     (verification.error || verification.mismatch_word != UINT64_MAX ||
	      verification.checked_words != opt.bytes / sizeof(uint64_t))) ||
	    (verification.answer == ANSWER_CORRUPT &&
	     (verification.error || verification.mismatch_word >= opt.bytes / sizeof(uint64_t))) ||
	    (verification.answer == ANSWER_ERROR && !verification.error))
		goto protocol_error;
	/* Keep only first-ACK timing/usage. Promote success solely after the
	 * matching verifier completes; no second public CSV row/schema needed.
	 */
	r->answer = verification.answer;
	r->error = verification.error;
	r->checked_words = verification.checked_words;
	r->mismatch_word = verification.mismatch_word;
	return IO_OK;
protocol_error:
	errno = EPROTO;
	return IO_ERROR;
}
static void csv_number(bool present, uint64_t value)
{
	if (present)
		fprintf(stdout, ",%" PRIu64, value);
	else
		fputc(',', stdout);
}
static void csv_row(const char *record, uint64_t sequence, unsigned int id,
		    const char *status, const char *reason, int error,
		    bool timed, uint64_t elapsed, const struct reply *r,
		    bool final)
{
	struct worker *w = &workers[id];
	bool waited = final && w->wait_valid;
	bool exited = waited && WIFEXITED(w->wait_status);
	bool signaled = waited && WIFSIGNALED(w->wait_status);

	fprintf(stdout, "%s,%" PRIu64 ",%u,%ld,%s,%s,%d", record, sequence,
		id, (long)w->pid, status, reason ? reason : "none", error);
	csv_number(timed, elapsed);
	csv_number(r != NULL, r ? r->wall_ns : 0);
	csv_number(r != NULL, r ? r->cpu_ns : 0);
	csv_number(r != NULL || waited, r ? r->user_us :
		   timeval_us(w->final_usage.ru_utime));
	csv_number(r != NULL || waited, r ? r->system_us :
		   timeval_us(w->final_usage.ru_stime));
	csv_number(r != NULL || waited, r ? r->minflt :
		   (uint64_t)w->final_usage.ru_minflt);
	csv_number(r != NULL || waited, r ? r->majflt :
		   (uint64_t)w->final_usage.ru_majflt);
	csv_number(r != NULL || waited, r ? r->nvcsw :
		   (uint64_t)w->final_usage.ru_nvcsw);
	csv_number(r != NULL || waited, r ? r->nivcsw :
		   (uint64_t)w->final_usage.ru_nivcsw);
	csv_number(r != NULL || waited, r ? r->maxrss_kib :
		   (uint64_t)w->final_usage.ru_maxrss);
	csv_number(r != NULL, r ? r->checked_words : 0);
	csv_number(opt.functional_synthetic && r != NULL, r ? r->checksum : 0);
	csv_number(r && r->answer == ANSWER_CORRUPT, r ? r->mismatch_word : 0);
	csv_number(opt.functional_synthetic && r && r->answer == ANSWER_CORRUPT,
		   r ? r->expected : 0);
	csv_number(opt.functional_synthetic && r && r->answer == ANSWER_CORRUPT,
		   r ? r->observed : 0);
	fprintf(stdout, ",%s", exited ? "exit" : signaled ? "signal" :
		final ? "unreaped_or_unknown" : "");
	csv_number(exited, exited ? (uint64_t)WEXITSTATUS(w->wait_status) : 0);
	csv_number(signaled, signaled ? (uint64_t)WTERMSIG(w->wait_status) : 0);
	csv_number(signaled, signaled ? (uint64_t)!!WCOREDUMP(w->wait_status) : 0);
	fprintf(stdout, ",%s", w->controller_kill ? w->controller_kill : "none");
	csv_number(true, w->initialized);
	csv_number(final, w->survived);
	csv_number(true, w->attempts);
	csv_number(true, w->ok);
	csv_number(true, w->unavailable);
	fputc('\n', stdout);
	if (ferror(stdout))
		output_error = true;
}
static void handle_answer(unsigned int id, const char *record,
			  unsigned int operation, uint64_t sequence)
{
	struct worker *w = &workers[id];
	struct reply r;
	uint64_t elapsed;
	enum transfer result;
	const char *status, *reason;
	int error = 0;
	bool valid = false;

	memset(&r, 0, sizeof(r));
	result = exchange(w, id, operation, sequence, &r, &elapsed);
	if (result != IO_OK) {
		error = errno;
		reason = io_name(result);
		status = "unavailable";
		if (result == IO_ERROR)
			infrastructure_error = true;
		lose_worker(w, reason, error);
	} else if (!valid_reply(&r, id, operation, sequence) ||
		   (operation == OP_TOUCH && r.answer == ANSWER_OK &&
		    r.checked_words != opt.bytes / sizeof(uint64_t))) {
		error = EPROTO;
		reason = "protocol_error";
		status = "error";
		infrastructure_error = true;
		lose_worker(w, reason, error);
	} else {
		valid = true;
		error = r.error;
		switch (r.answer) {
		case ANSWER_OK:
			reason = "none";
			status = operation == OP_INIT ? "ready" : "ok";
			if (operation == OP_INIT)
				w->initialized = true;
			break;
		case ANSWER_RESOURCE:
			reason = "allocation_failed";
			status = "unavailable";
			lose_worker(w, reason, error);
			break;
		case ANSWER_CORRUPT:
			reason = "content_mismatch";
			status = "corrupt";
			content_error = true;
			lose_worker(w, reason, error);
			break;
		default:
			reason = "worker_error";
			status = "error";
			infrastructure_error = true;
			lose_worker(w, reason, error);
			break;
		}
	}
	if (operation == OP_TOUCH) {
		if (valid && r.answer == ANSWER_OK) {
			w->ok++;
			successful_switches++;
		} else {
			w->unavailable++;
			unavailable_switches++;
		}
		emitted_switches++;
	}
	csv_row(record, sequence, id, status, reason, error, true, elapsed,
		valid ? &r : NULL, false);
}
static int spawn_workers(void)
{
	pid_t controller = getpid();
	unsigned int i;

	/* Allocate all endpoints first; each fork explicitly closes all peers. */
	for (i = 0; i < opt.workers; i++)
		if (pipe2(workers[i].command, O_CLOEXEC | O_NONBLOCK) ||
		    pipe2(workers[i].reply, O_CLOEXEC | O_NONBLOCK))
			return -1;
	for (i = 0; i < opt.workers; i++) {
		if (interrupted) {
			errno = EINTR;
			return -1;
		}
		workers[i].pid = fork();
		if (workers[i].pid < 0)
			return -1;
		if (!workers[i].pid)
			child_main(i, controller);
		workers[i].active = true;
		/* Never pass these child endpoints to later forks. */
		close_fd(&workers[i].command[0]);
		close_fd(&workers[i].reply[1]);
	}
	for (i = 0; i < opt.workers; i++) {
		struct reply r;
		uint64_t deadline;
		enum transfer result;

		if (deadline_after(opt.timeout_ms, &deadline))
			return -1;
		result = transfer_bytes(workers[i].reply[0], &r, sizeof(r),
					false, deadline);
		if (result != IO_OK) {
			int error = errno;

			/* Even a pre-INIT OOM/timeout is an availability sample. */
			fprintf(stderr, "JOIN_UNAVAILABLE worker=%u pid=%ld reason=%s"
				" errno=%d\n", i, (long)workers[i].pid,
				io_name(result), error);
			lose_worker(&workers[i], io_name(result), error);
			if (result == IO_ERROR || result == IO_CANCEL) {
				errno = error;
				return -1;
			}
			continue;
		}
		if (!valid_reply(&r, i, OP_JOIN, 0) || r.answer != ANSWER_OK) {
			int error = r.error > 0 ? r.error : EPROTO;

			fprintf(stderr, "JOIN_ERROR worker=%u pid=%ld errno=%d\n",
				i, (long)workers[i].pid, error);
			errno = error;
			return -1;
		}
	}
	return 0;
}
static void initialize_workers(void)
{
	unsigned int i;

	for (i = 0; i < opt.workers && !interrupted && !output_error; i++) {
		struct worker *w = &workers[i];

		reap_all();
		if (!w->active || w->reaped) {
			if (w->active)
				lose_worker(w, "exited_before_init", 0);
			csv_row("init", 0, i, "unavailable", w->loss, w->loss_errno,
				false, 0, NULL, false);
			continue;
		}
		handle_answer(i, "init", OP_INIT, 0);
	}
}
static void run_switches(void)
{
	uint64_t sequence, epoch = UINT64_MAX;
	unsigned int order[MAX_WORKERS], i, previous = UINT_MAX;

	for (sequence = 0; sequence < opt.switches; sequence++) {
		uint64_t next_epoch = sequence / opt.workers;
		struct worker *w;
		unsigned int id;

		if (interrupted || output_error)
			break;
		if (next_epoch != epoch) {
			/* Every epoch visits ALL original IDs, including dead workers. */
			epoch = next_epoch;
			for (i = 0; i < opt.workers; i++)
				order[i] = i;
			for (i = opt.workers; i > 1; i--) {
				unsigned int j = (unsigned int)(mix64(opt.seed ^
					mix64(epoch) ^ (uint64_t)i) % i);
				unsigned int tmp = order[i - 1];

				order[i - 1] = order[j];
				order[j] = tmp;
			}
			/* A new epoch must not repeat its predecessor foreground. */
			if (opt.workers > 1 && order[0] == previous) {
				unsigned int tmp = order[0];

				order[0] = order[1];
				order[1] = tmp;
			}
		}
		id = order[sequence % opt.workers];
		previous = id;
		w = &workers[id];
		w->attempts++;
		reap_all();
		if (!w->active || !w->initialized || w->reaped) {
			if (w->active)
				lose_worker(w, "exited", 0);
			w->unavailable++;
			unavailable_switches++;
			emitted_switches++;
			csv_row("switch", sequence + 1, id, "unavailable",
				w->loss ? w->loss : "not_initialized", w->loss_errno,
				false, 0, NULL, false);
			continue;
		}
		handle_answer(id, "switch", OP_TOUCH, sequence + 1);
	}
}

static void stop_and_reap(void)
{
	uint64_t deadline, now;
	uint64_t grace = opt.timeout_ms < 1000 ? 1000 :
			 opt.timeout_ms > 5000 ? 5000 : opt.timeout_ms;
	unsigned int i;

	for (i = 0; i < opt.workers; i++) {
		struct worker *w = &workers[i];

		reap_one(w);
		if (w->pid <= 0 || w->reaped)
			continue;
		if (w->active && !interrupted && !output_error) {
			struct reply r;
			uint64_t elapsed;
			enum transfer result;

			result = exchange(w, i, OP_QUIT, 0, &r, &elapsed);
			if (result != IO_OK || !valid_reply(&r, i, OP_QUIT, 0) ||
			    r.answer != ANSWER_OK) {
				fprintf(stderr, "QUIT_UNAVAILABLE worker=%u pid=%ld reason=%s\n",
					i, (long)w->pid, result != IO_OK ?
					io_name(result) : "worker_error");
				if (result == IO_ERROR || result == IO_OK)
					infrastructure_error = true;
				kill_owned(w, "cleanup_quit_failed");
			}
		} else {
			kill_owned(w, "cleanup");
		}
		close_fd(&w->command[1]);
		close_fd(&w->reply[0]);
	}
	if (deadline_after(grace, &deadline)) {
		infrastructure_error = true;
		deadline = 0;
	}
	for (;;) {
		bool pending = false;
		struct timespec sleep_time = { .tv_nsec = 10000000 };

		reap_all();
		for (i = 0; i < opt.workers; i++)
			if (workers[i].pid > 0 && !workers[i].reaped)
				pending = true;
		if (!pending)
			break;
		if (!deadline || clock_ns(CLOCK_MONOTONIC, &now) || now >= deadline)
			break;
		(void)nanosleep(&sleep_time, NULL);
	}
	/* Never block forever on a child stuck exiting, even after SIGKILL. */
	for (i = 0; i < opt.workers; i++)
		kill_owned(&workers[i], "cleanup_deadline");
	if (deadline_after(grace, &deadline))
		deadline = 0;
	for (;;) {
		bool pending = false;
		struct timespec sleep_time = { .tv_nsec = 10000000 };

		reap_all();
		for (i = 0; i < opt.workers; i++)
			if (workers[i].pid > 0 && !workers[i].reaped)
				pending = true;
		if (!pending)
			break;
		if (!deadline || clock_ns(CLOCK_MONOTONIC, &now) || now >= deadline) {
			infrastructure_error = true;
			break;
		}
		(void)nanosleep(&sleep_time, NULL);
	}
	for (i = 0; i < opt.workers; i++) {
		struct worker *w = &workers[i];

		close_fd(&w->command[0]);
		close_fd(&w->command[1]);
		close_fd(&w->reply[0]);
		close_fd(&w->reply[1]);
		if (w->pid > 0)
			csv_row("pid_exit", 0, i, w->wait_valid ? "reaped" : "unknown",
				w->loss ? w->loss : "none", w->loss_errno,
				false, 0, NULL, true);
	}
}
static void remove_group(void)
{
	struct stat named;

	if (!own_group)
		goto close;
	/* Never remove a replacement or pre-existing directory. */
	if (!group_identified ||
	    fstatat(parent_fd, group_name, &named, AT_SYMLINK_NOFOLLOW) ||
	    group_identity.st_dev != named.st_dev ||
	    group_identity.st_ino != named.st_ino ||
	    !S_ISDIR(named.st_mode)) {
		fputs("CLEANUP_ERROR: owned cgroup identity unavailable/changed; left intact\n",
		      stderr);
		infrastructure_error = true;
		goto close;
	}
	if (unlinkat(parent_fd, group_name, AT_REMOVEDIR)) {
		fprintf(stderr, "CLEANUP_ERROR child=%s errno=%d; no recursive removal\n",
			group_name, errno);
		infrastructure_error = true;
	} else {
		fprintf(stderr, "CLEANUP removed_child=%s\n", group_name);
	}
close:
	close_fd(&group_fd);
	close_fd(&parent_fd);
}
int main(int argc, char **argv)
{
	static char stdout_buffer[4096];
	struct utsname kernel;
	unsigned int i, alive = 0;
	int rc;
	bool started = false;

	if (bench_corpus_sampling_args(&argc, argv)) {
		fputs("ERROR --corpus-sampling requires uniform-v411 and real corpus\n", stderr);
		return 2;
	}
	rc = parse_options(argc, argv);
	if (rc > 0)
		return 0;
	if (rc < 0) {
		usage(stderr);
		return 2;
	}
	for (i = 0; i < MAX_WORKERS; i++) {
		workers[i].command[0] = workers[i].command[1] = -1;
		workers[i].reply[0] = workers[i].reply[1] = -1;
	}
	if (setvbuf(stdout, stdout_buffer, _IOLBF, sizeof(stdout_buffer)) ||
	    install_signals()) {
		perror("stdio/signals");
		return 2;
	}
	fprintf(stderr, "CONFIG workers=%u worker_bytes=%zu total_bytes=%" PRIu64
		" memory_max=%" PRIu64 " switches=%" PRIu64 " seed=%" PRIu64
		" page_size=%zu worker_cpu=%d timeout_ms=%" PRIu64 "\n",
		opt.workers, opt.bytes, (uint64_t)opt.bytes * opt.workers,
		opt.memory_max, opt.switches, opt.seed, opt.page_size,
		opt.cpu, opt.timeout_ms);
	if (!uname(&kernel))
		fprintf(stderr, "KERNEL release=%s machine=%s\n", kernel.release,
			kernel.machine);
	fputs("N/A dedicated_backend_writeback_cpu: not separable here\n"
	      "N/A exact_backend_metadata_and_ratio: external backend collector required\n",
	      stderr);
	if (create_group()) {
		perror("cgroup setup (parent must enable memory and expose swap.max)");
		infrastructure_error = true;
		goto cleanup;
	}
	snapshot_file("parent_constraints", parent_fd, "memory.max", false);
	snapshot_file("parent_constraints", parent_fd, "memory.high", false);
	snapshot_file("parent_constraints", parent_fd, "memory.swap.max", false);
	snapshot_file("parent_constraints", parent_fd, "memory.oom.group", false);
	fputs("record,sequence,worker,pid,status,reason,errno,elapsed_ns,"
	      "worker_wall_ns,worker_cpu_ns,user_us,system_us,minflt,majflt,nvcsw,"
	      "nivcsw,maxrss_kib,checked_words,checksum,mismatch_word,expected,observed,"
	      "wait_kind,exit_code,term_signal,core_dumped,controller_kill,init_ok,"
	      "alive_before_cleanup,attempts,ok_switches,unavailable_switches\n", stdout);
	started = true;
	if (spawn_workers()) {
		perror("worker fork/join");
		infrastructure_error = true;
		goto cleanup;
	}
	snapshot("joined_before_allocation");
	initialize_workers();
	snapshot("after_initialization");
	run_switches();
	reap_all();
	for (i = 0; i < opt.workers; i++) {
		workers[i].survived = workers[i].active && workers[i].initialized &&
				      !workers[i].reaped;
		alive += workers[i].survived;
	}
	snapshot("after_switches_before_cleanup");
cleanup:
	stop_and_reap();
	if (group_fd >= 0 && started)
		snapshot("after_reap");
	remove_group();
	fprintf(stderr, "SUMMARY planned=%" PRIu64 " emitted=%" PRIu64
		" ack_ok=%" PRIu64 " unavailable=%" PRIu64
		" alive_before_cleanup=%u/%u content_error=%d infrastructure_error=%d"
		" interrupted=%d\n", opt.switches, emitted_switches,
		successful_switches, unavailable_switches, alive, opt.workers,
		content_error, infrastructure_error, (int)interrupted);
	if (fflush(stdout) || ferror(stdout) || fflush(stderr) || ferror(stderr))
		output_error = true;
	if (content_error)
		return 1;
	if (interrupted)
		return 128 + interrupted;
	if (infrastructure_error || output_error || emitted_switches != opt.switches)
		return 2;
	/* Zero is a complete measurement, NOT a claim of worker survival. */
	return 0;
}
