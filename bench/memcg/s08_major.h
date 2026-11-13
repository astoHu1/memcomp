/* SPDX-License-Identifier: GPL-2.0 */
/* S08-only bounded observation, no cgroup/source changes or verdict bypass. */
#ifndef S08_MAJOR_H
#define S08_MAJOR_H
#include <sys/resource.h>

#define MAJOR_SAMPLES 8
#define MAJOR_DEADLINE_MS 9000
struct major_sample {
	long long ms, task, global, root, a;
};
static struct major_sample major_capture(void)
{
	struct major_sample s;
	struct rusage usage;
	char b[16384];

	s.ms = now_ms();
	check(!getrusage(RUSAGE_SELF, &usage), "major getrusage");
	s.task = usage.ru_majflt;
	get("/proc/vmstat", b, sizeof(b), 0);
	s.global = field(b, "pgmajfault");
	get(ROOT "/memory.stat", b, sizeof(b), 0);
	s.root = field(b, "pgmajfault");
	get(CG "/memory.stat", b, sizeof(b), 0);
	s.a = field(b, "pgmajfault");
	check(now_ms() - s.ms < 1000, "major capture deadline");
	return s;
}
static void major_emit(const char *phase, int index, struct major_sample s)
{
	printf("S08 {\"event\":\"major_sample\",\"phase\":\"%s\",\"index\":%d,"
	       "\"ms\":%lld,\"task\":%lld,\"global\":%lld,\"root\":%lld,\"a\":%lld}\n",
	       phase, index, s.ms, s.task, s.global, s.root, s.a);
}
static void major_collect(const char *phase, struct major_sample first)
{
	long long start = first.ms, previous = first.ms;
	struct major_sample s = first;

	for (int i = 0; i < MAJOR_SAMPLES; i++) {
		if (i) {
			long long wait = previous + 1000 - now_ms();
			if (wait > 0) usleep((useconds_t)wait * 1000);
			check(!expired, "major interrupted");
			s = major_capture();
		}
		check(s.ms - start < MAJOR_DEADLINE_MS &&
		      now_ms() - start < MAJOR_DEADLINE_MS, "major collection deadline");
		major_emit(phase, i, s);
		previous = s.ms;
	}
	/* No count assertion here: preserve main readback and cleanup evidence. */
}
static void major_prewarm(unsigned char *expected)
{
	unsigned char copy[PAGE];
	int (*volatile compare)(const void *, const void *, size_t) = memcmp;

	pattern(expected, 0);
	memcpy(copy, expected, PAGE);
	check(!compare(copy, expected, PAGE), "major compare prewarm");
	major_emit("warmup", 0, major_capture());
}
#endif /* S08_MAJOR_H */
