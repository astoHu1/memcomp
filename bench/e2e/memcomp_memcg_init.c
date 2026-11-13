// SPDX-License-Identifier: GPL-2.0
/* PID 1 for a diskless disposable memcg guest. */
#define _GNU_SOURCE
#include <ctype.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <sched.h>
#include <linux/capability.h>
#include <sys/syscall.h>
#ifdef __x86_64__
#include <cpuid.h>
#endif
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

/* Reject physical machines and non-QEMU/KVM hypervisors before mounting.
 * Boot opt-in then runs in a private mount namespace; no root delegation or
 * sysfs/tracefs operation is permitted until every early gate succeeds.
 */
static bool allowed_hypervisor(const char vendor[12])
{
	return !memcmp(vendor, "KVMKVMKVM\0\0\0", 12) ||
	       !memcmp(vendor, "TCGTCGTCGTCG", 12);
}

static bool boot_opt_in(const char *text)
{
	const char *opt = "memcomp_memcg.disposable_guest=1";
	size_t len = strlen(opt);

	while (*text) {
		const char *start;

		while (isspace((unsigned char)*text)) text++;
		start = text;
		while (*text && !isspace((unsigned char)*text)) text++;
		if ((size_t)(text - start) == len && !memcmp(start, opt, len))
			return true;
	}
	return false;
}

static bool no_swap_entries(const char *text)
{
	const char *end = strchr(text, '\n');

	if (!end || strncmp(text, "Filename", 8)) return false;
	for (end++; *end; end++)
		if (!isspace((unsigned char)*end)) return false;
	return true;
}

static int early_read(const char *path, char *text, size_t cap)
{
	int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	size_t used = 0;

	if (fd < 0) return -1;
	for (;;) {
		ssize_t n = read(fd, text + used, cap - 1 - used);

		if (n < 0 && errno == EINTR) continue;
		if (n < 0 || (n && used + (size_t)n >= cap - 1)) {
			close(fd); return -1;
		}
		if (!n) break;
		used += (size_t)n;
	}
	text[used] = 0;
	return close(fd);
}

static int authorize_guest(void)
{
	struct __user_cap_header_struct header = {
		.version = _LINUX_CAPABILITY_VERSION_3,
	};
	struct __user_cap_data_struct caps[2] = {};
	char text[65536];
#ifdef __x86_64__
	unsigned int a, b, c, d;
	char vendor[12];

	if (!__get_cpuid(1, &a, &b, &c, &d) || !(c & (1U << 31)))
		return -1;
	__cpuid(0x40000000, a, b, c, d);
	memcpy(vendor, &b, 4); memcpy(vendor + 4, &c, 4);
	memcpy(vendor + 8, &d, 4);
	if (!allowed_hypervisor(vendor)) return -1;
#else
	return -1; /* This PID1 runner is currently x86_64-only. */
#endif
	if (getuid() || geteuid() || syscall(SYS_capget, &header, caps) ||
	    !(caps[CAP_SYS_ADMIN / 32].effective & (1U << (CAP_SYS_ADMIN % 32))))
		return -1;
	/* A PID namespace alone is not permission to alter shared host mounts. */
	if (unshare(CLONE_NEWNS) || mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL) ||
	    mount("proc", "/proc", "proc", MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL) ||
	    early_read("/proc/cmdline", text, sizeof(text)) || !boot_opt_in(text) ||
	    early_read("/proc/swaps", text, sizeof(text)) || !no_swap_entries(text))
		return -1;
	return 0;
}

static _Noreturn void finish(int code)
{
	printf("MEMCG-GUEST: END exit=%d\n", code);
	fflush(NULL);
	sync();
	reboot(RB_POWER_OFF);
	_exit(code);
}

int main(void)
{
	pid_t child;
	int status, fd;

	if (getpid() != 1)
		return 4;
	setvbuf(stdout, NULL, _IOLBF, 0);
	if (authorize_guest()) {
		fputs("MEMCG-GUEST: refused before sysfs/delegation/tracefs/reboot\n", stderr);
		return 4;
	}
	puts("MEMCG-GUEST: authorized disposable VM private_mount_namespace=1");
	if (mount("sysfs", "/sys", "sysfs", 0, NULL) ||
	    mount("devtmpfs", "/dev", "devtmpfs", 0, NULL)) {
		perror("mount guest filesystems");
		finish(1);
	}
	if (mkdir("/sys/fs/cgroup", 0755) && errno != EEXIST) {
		perror("mkdir cgroup mount");
		finish(1);
	}
	if (mount("cgroup2", "/sys/fs/cgroup", "cgroup2", 0, NULL)) {
		perror("mount guest cgroup2");
		finish(1);
	}
	fd = open("/sys/fs/cgroup/cgroup.subtree_control", O_WRONLY | O_CLOEXEC);
	if (fd < 0 || write(fd, "+memory\n", 8) != 8 || close(fd)) {
		perror("delegate guest memory controller");
		finish(1);
	}
	if ((mkdir("/sys/kernel/tracing", 0755) && errno != EEXIST) ||
	    mount("tracefs", "/sys/kernel/tracing", "tracefs", 0, NULL)) {
		perror("mount guest tracefs");
		finish(1);
	}
	child = fork();
	if (!child) {
		/* Preserve the full workload and raw trace under KASAN + serial I/O. */
		execl("/memcomp-memcg", "memcomp-memcg", "--disposable-guest",
		      "--timeout", "120", (char *)NULL);
		perror("exec memcg verifier");
		_exit(127);
	}
	if (child < 0 || waitpid(child, &status, 0) != child ||
	    !WIFEXITED(status)) {
		perror("wait memcg verifier");
		finish(1);
	}
	if (!WEXITSTATUS(status))
		puts("RESULT: PASS scope=memcg-source-attribution");
	finish(WEXITSTATUS(status));
	return 1;
}
