/* SPDX-License-Identifier: GPL-2.0 */
#ifndef MEMCOMP_HMM_MODULE_H
#define MEMCOMP_HMM_MODULE_H

/* The static guest init needs no modprobe or new kernel test ABI. */
static bool memcomp_hmm_load_module(const char *path)
{
	int fd, ret, error;

	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0) {
		fail("HMM: module open: %s", strerror(errno));
		return false;
	}
	ret = syscall(SYS_finit_module, fd, "", 0);
	error = errno;
	close(fd);
	if (ret) {
		fail("HMM: finit_module: %s", strerror(error));
		return false;
	}
	say("HMM: loaded test_hmm module");
	return true;
}

#endif /* MEMCOMP_HMM_MODULE_H */
