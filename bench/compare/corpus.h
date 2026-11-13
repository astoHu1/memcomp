/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef MEMCOMP_COMPARE_CORPUS_H
#define MEMCOMP_COMPARE_CORPUS_H

/* Include before system headers, or compile with -D_GNU_SOURCE. */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#ifndef _FILE_OFFSET_BITS
#define _FILE_OFFSET_BITS 64
#endif

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <inttypes.h>
#include <stdio.h>
#include <linux/fs.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define BENCH_CORPUS_PAGE_SIZE 4096U
#define BENCH_CORPUS_MAX_SOURCES 16U
#define BENCH_CORPUS_SAMPLING_V411 "uniform-v411"

/* Explicit executable opt-in, never inferred from an image or its contents. */
static int bench_corpus_sampling_v411;

/* Strip only this versioned option before the unchanged historical parser. */
static inline int bench_corpus_sampling_args(int *argc, char **argv)
{
	int i, out = 1, seen = 0, real = 0, synthetic = 0;

	for (i = 1; i < *argc; i++) {
		if (!strcmp(argv[i], "--corpus-sampling")) {
			if (seen++ || i + 1 == *argc ||
			    strcmp(argv[++i], BENCH_CORPUS_SAMPLING_V411))
				return -1;
			continue;
		}
		real |= !strcmp(argv[i], "--corpus");
		synthetic |= !strcmp(argv[i], "--functional-synthetic");
		argv[out++] = argv[i];
	}
	if (seen && (!real || synthetic))
		return -1;
	argv[out] = NULL;
	*argc = out;
	bench_corpus_sampling_v411 = seen;
	return 0;
}

struct bench_corpus_source {
	char label[32];
	uint64_t start_page;
	uint64_t pages;
};

struct bench_corpus {
	int fd;
	uint32_t page_size;
	uint64_t total_pages;
	uint32_t source_count;
	struct bench_corpus_source sources[BENCH_CORPUS_MAX_SOURCES];
	uint64_t hashes_offset;
	uint64_t data_offset;
	/* Private I/O state: one aligned page, no data/hash cache. Not thread safe. */
	unsigned char *io_buffer;
	int direct;
	int uniform_v411;
	/* Virtual slots: target first, then the ordered complement. Index 0 is
	 * combined; index i+1 is source i. No page or hash arrays are retained.
	 */
	uint64_t sampling_targets[BENCH_CORPUS_MAX_SOURCES + 1];
};

struct bench_corpus_sha256 {
	uint32_t h[8];
	uint64_t bytes;
	unsigned char block[64];
	size_t used;
};

static inline uint32_t bench_corpus_rotr(uint32_t x, unsigned int n)
{
	return (x >> n) | (x << (32 - n));
}

static inline void bench_corpus_sha256_block(struct bench_corpus_sha256 *s,
					   const unsigned char *p)
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
		0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
	};
	uint32_t w[64], a, b, c, d, e, f, g, h;
	unsigned int i;

	for (i = 0; i < 16; i++)
		w[i] = (uint32_t)p[i * 4] << 24 |
		       (uint32_t)p[i * 4 + 1] << 16 |
		       (uint32_t)p[i * 4 + 2] << 8 | p[i * 4 + 3];
	for (; i < 64; i++) {
		uint32_t x = w[i - 15], y = w[i - 2];
		uint32_t s0 = bench_corpus_rotr(x, 7) ^
			bench_corpus_rotr(x, 18) ^ (x >> 3);
		uint32_t s1 = bench_corpus_rotr(y, 17) ^
			bench_corpus_rotr(y, 19) ^ (y >> 10);

		w[i] = w[i - 16] + s0 + w[i - 7] + s1;
	}
	a = s->h[0]; b = s->h[1]; c = s->h[2]; d = s->h[3];
	e = s->h[4]; f = s->h[5]; g = s->h[6]; h = s->h[7];
	for (i = 0; i < 64; i++) {
		uint32_t s1 = bench_corpus_rotr(e, 6) ^
			bench_corpus_rotr(e, 11) ^ bench_corpus_rotr(e, 25);
		uint32_t s0 = bench_corpus_rotr(a, 2) ^
			bench_corpus_rotr(a, 13) ^ bench_corpus_rotr(a, 22);
		uint32_t t1 = h + s1 + ((e & f) ^ (~e & g)) + k[i] + w[i];
		uint32_t t2 = s0 + ((a & b) ^ (a & c) ^ (b & c));

		h = g; g = f; f = e; e = d + t1;
		d = c; c = b; b = a; a = t1 + t2;
	}
	s->h[0] += a; s->h[1] += b; s->h[2] += c; s->h[3] += d;
	s->h[4] += e; s->h[5] += f; s->h[6] += g; s->h[7] += h;
}

static inline void bench_corpus_sha256_init(struct bench_corpus_sha256 *s)
{
	static const uint32_t initial[8] = {
		0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
		0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
	};

	memcpy(s->h, initial, sizeof(initial));
	s->bytes = 0;
	s->used = 0;
}

static inline void bench_corpus_sha256_update(struct bench_corpus_sha256 *s,
					    const void *data, size_t bytes)
{
	const unsigned char *p = data;

	s->bytes += bytes;
	while (bytes) {
		size_t n = sizeof(s->block) - s->used;

		if (n > bytes)
			n = bytes;
		memcpy(s->block + s->used, p, n);
		s->used += n;
		p += n;
		bytes -= n;
		if (s->used == sizeof(s->block)) {
			bench_corpus_sha256_block(s, s->block);
			s->used = 0;
		}
	}
}

static inline void bench_corpus_sha256_final(struct bench_corpus_sha256 *s,
					   unsigned char out[32])
{
	uint64_t bits = s->bytes * 8;
	unsigned int i;

	s->block[s->used++] = 0x80;
	if (s->used > 56) {
		memset(s->block + s->used, 0, 64 - s->used);
		bench_corpus_sha256_block(s, s->block);
		s->used = 0;
	}
	memset(s->block + s->used, 0, 56 - s->used);
	for (i = 0; i < 8; i++)
		s->block[63 - i] = (unsigned char)(bits >> (i * 8));
	bench_corpus_sha256_block(s, s->block);
	for (i = 0; i < 32; i++)
		out[i] = (unsigned char)(s->h[i / 4] >> (24 - (i % 4) * 8));
	memset(s, 0, sizeof(*s));
}

static inline void bench_corpus_digest(const void *data, size_t bytes,
				       unsigned char out[32])
{
	struct bench_corpus_sha256 s;

	bench_corpus_sha256_init(&s);
	bench_corpus_sha256_update(&s, data, bytes);
	bench_corpus_sha256_final(&s, out);
}

static inline uint32_t bench_corpus_le32(const unsigned char *p)
{
	return (uint32_t)p[0] | (uint32_t)p[1] << 8 |
	       (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static inline uint64_t bench_corpus_le64(const unsigned char *p)
{
	return (uint64_t)bench_corpus_le32(p) |
	       (uint64_t)bench_corpus_le32(p + 4) << 32;
}

static inline int bench_corpus_zero(const unsigned char *p, size_t bytes)
{
	while (bytes--)
		if (*p++)
			return 0;
	return 1;
}

static inline int bench_corpus_error(int error)
{
	errno = error;
	return -1;
}

static inline void bench_corpus_close(struct bench_corpus *c)
{
	int saved = errno;

	if (!c)
		return;
	if (c->fd >= 0)
		close(c->fd);
	if (c->io_buffer) {
		memset(c->io_buffer, 0, BENCH_CORPUS_PAGE_SIZE);
		free(c->io_buffer);
	}
	memset(c, 0, sizeof(*c));
	c->fd = -1;
	errno = saved;
}

/* All actual I/O is page-aligned, including header and single-hash reads. */
static inline int bench_corpus_read_block(struct bench_corpus *c, uint64_t off)
{
	size_t done = 0;

	if (off % BENCH_CORPUS_PAGE_SIZE ||
	    off > (uint64_t)INT64_MAX - BENCH_CORPUS_PAGE_SIZE ||
	    sizeof(off_t) < 8)
		return bench_corpus_error(EOVERFLOW);
	while (done < BENCH_CORPUS_PAGE_SIZE) {
		ssize_t n = pread(c->fd, c->io_buffer + done,
				  BENCH_CORPUS_PAGE_SIZE - done, (off_t)(off + done));

		if (n < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		if (!n)
			return bench_corpus_error(EIO);
		done += (size_t)n;
		/* Never issue a misaligned retry after a short direct read. */
		if (c->direct && done != BENCH_CORPUS_PAGE_SIZE)
			return bench_corpus_error(EIO);
	}
	return 0;
}

static inline int bench_corpus_label(const unsigned char *p)
{
	size_t i;

	for (i = 0; i < 32 && p[i]; i++)
		if (!((p[i] >= 'A' && p[i] <= 'Z') ||
		      (p[i] >= 'a' && p[i] <= 'z') ||
		      (p[i] >= '0' && p[i] <= '9') ||
		      p[i] == '_' || p[i] == '.' || p[i] == '-'))
			return 0;
	return i && i < 32 && bench_corpus_zero(p + i, 32 - i) &&
	       strcmp((const char *)p, "combined");
}

static inline int bench_corpus_open(struct bench_corpus *c, const char *path)
{
	static const unsigned char magic[16] = "MEMCOMP-CORPUS1";
	unsigned char digest[32], *h;
	uint64_t image_size, hashes_end, expected_data, expected_size, next = 0;
	struct stat st;
	uint32_t i, j;
	int rc, flags, readonly, sector_size;

	if (!c)
		return bench_corpus_error(EINVAL);
	memset(c, 0, sizeof(*c));
	c->fd = -1;
	c->uniform_v411 = bench_corpus_sampling_v411;
	if (!path || !*path)
		return bench_corpus_error(EINVAL);
	c->fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
	if (c->fd < 0)
		return -1;
	if (fstat(c->fd, &st))
		goto fail;
	if (S_ISBLK(st.st_mode)) {
		if (ioctl(c->fd, BLKROGET, &readonly))
			goto fail;
		if (readonly != 1) {
			errno = EACCES;
			goto fail;
		}
		if (ioctl(c->fd, BLKGETSIZE64, &image_size) ||
		    ioctl(c->fd, BLKSSZGET, &sector_size))
			goto fail;
		if (sector_size <= 0 || sector_size > 4096 ||
		    4096 % sector_size) {
			errno = EINVAL;
			goto fail;
		}
#ifdef O_DIRECT
		flags = fcntl(c->fd, F_GETFL);
		if (flags < 0 || fcntl(c->fd, F_SETFL, flags | O_DIRECT) < 0)
			goto fail;
		flags = fcntl(c->fd, F_GETFL);
		if (flags < 0)
			goto fail;
		if (!(flags & O_DIRECT)) {
			errno = EOPNOTSUPP;
			goto fail;
		}
		c->direct = 1;
#else
		(void)flags;
		errno = EOPNOTSUPP;
		goto fail;
#endif
	} else if (S_ISREG(st.st_mode)) {
		if (st.st_size < 0) {
			errno = EINVAL;
			goto fail;
		}
		image_size = (uint64_t)st.st_size;
	} else {
		errno = EINVAL;
		goto fail;
	}
	if (image_size < 8192 || image_size > (uint64_t)INT64_MAX) {
		errno = EINVAL;
		goto fail;
	}
	rc = posix_memalign((void **)&c->io_buffer, 4096, 4096);
	if (rc) {
		errno = rc;
		goto fail;
	}
	if (bench_corpus_read_block(c, 0))
		goto fail;
	h = c->io_buffer;
	bench_corpus_digest(h, 4064, digest);
	if (memcmp(h, magic, 16) || memcmp(digest, h + 4064, 32) ||
	    bench_corpus_le32(h + 16) != 1 ||
	    bench_corpus_le32(h + 20) != 4096 ||
	    bench_corpus_le32(h + 28) != 4096)
		goto invalid;
	c->page_size = 4096;
	c->source_count = bench_corpus_le32(h + 24);
	c->total_pages = bench_corpus_le64(h + 32);
	c->hashes_offset = bench_corpus_le64(h + 40);
	c->data_offset = bench_corpus_le64(h + 48);
	if (!c->source_count || c->source_count > BENCH_CORPUS_MAX_SOURCES ||
	    !c->total_pages || c->hashes_offset != 4096 ||
	    c->total_pages > ((uint64_t)INT64_MAX - 8191) / 32)
		goto invalid;
	hashes_end = 4096 + c->total_pages * 32;
	expected_data = (hashes_end + 4095) & ~UINT64_C(4095);
	if (c->data_offset != expected_data ||
	    c->total_pages > ((uint64_t)INT64_MAX - expected_data) / 4096)
		goto invalid;
	expected_size = expected_data + c->total_pages * 4096;
	if (bench_corpus_le64(h + 56) != expected_size ||
	    image_size != expected_size)
		goto invalid;
	for (i = 0; i < c->source_count; i++) {
		const unsigned char *entry = h + 64 + i * 128;
		struct bench_corpus_source *source = &c->sources[i];

		if (!bench_corpus_label(entry) ||
		    !bench_corpus_zero(entry + 112, 16))
			goto invalid;
		memcpy(source->label, entry, 32);
		for (j = 0; j < i; j++)
			if (!strcmp(source->label, c->sources[j].label))
				goto invalid;
		source->start_page = bench_corpus_le64(entry + 32);
		source->pages = bench_corpus_le64(entry + 40);
		if (source->start_page != next || !source->pages ||
		    source->pages > c->total_pages - next)
			goto invalid;
		next += source->pages;
	}
	if (next != c->total_pages ||
	    !bench_corpus_zero(h + 64 + c->source_count * 128,
			       4064 - 64 - c->source_count * 128))
		goto invalid;
	if (hashes_end != expected_data) {
		if (bench_corpus_read_block(c, hashes_end & ~UINT64_C(4095)))
			goto fail;
		if (!bench_corpus_zero(c->io_buffer + hashes_end % 4096,
				       (size_t)(expected_data - hashes_end)))
			goto invalid;
	}
	/* Retain neither the header nor a page of hash-section contents. */
	memset(c->io_buffer, 0, 4096);
	return 0;
invalid:
	errno = EINVAL;
fail:
	bench_corpus_close(c);
	return -1;
}

static inline int bench_corpus_ready(const struct bench_corpus *c)
{
	return c && c->fd >= 0 && c->io_buffer && c->page_size == 4096;
}

static inline int bench_corpus_range(struct bench_corpus *c, const char *source,
				     uint64_t offset, uint64_t pages,
				     uint64_t *start)
{
	uint64_t base = 0, available;
	uint32_t i, selection = 0;

	if (!bench_corpus_ready(c) || !source || !start)
		return bench_corpus_error(EINVAL);
	available = c->total_pages;
	if (strcmp(source, "combined")) {
		for (i = 0; i < c->source_count; i++)
			if (!strcmp(source, c->sources[i].label))
				break;
		if (i == c->source_count)
			return bench_corpus_error(ENOENT);
		selection = i + 1;
		base = c->sources[i].start_page;
		available = c->sources[i].pages;
	}
	if (!pages || offset > available || pages > available - offset)
		return bench_corpus_error(ERANGE);
	if (c->uniform_v411) {
		/* A combined selection and per-app selections cannot coexist. */
		for (i = 0; i <= c->source_count; i++)
			if (c->sampling_targets[i] && i != selection &&
			    (!i || !selection))
				return bench_corpus_error(EINVAL);
		if (!c->sampling_targets[selection]) {
			if (offset)
				return bench_corpus_error(EINVAL);
			c->sampling_targets[selection] = pages;
			fprintf(stderr, "CORPUS_SAMPLING protocol=corpus-page-uniform-disjoint-v411 "
				"source=%s source_pages=%" PRIu64 " target_pages=%" PRIu64
				" auxiliary=ordered_complement\n", source, available, pages);
		} else if ((!offset && pages != c->sampling_targets[selection]) ||
			   (offset && offset < c->sampling_targets[selection])) {
			return bench_corpus_error(EINVAL);
		}
	}
	*start = base + offset;
	return 0;
}

/* Pure mapping, including the full uint64_t domain. Wide products cannot
 * overflow: both factors are uint64_t, and no floating-point is involved.
 * Complement rank j is ceil((j+1)*N/(N-T))-1, the jth non-target page.
 */
static inline int bench_corpus_sample_index(uint64_t n, uint64_t target,
					    uint64_t slot, uint64_t *page)
{
	if (!page || !target || target > n || slot >= n)
		return bench_corpus_error(ERANGE);
	if (slot < target)
		*page = (uint64_t)((__uint128_t)slot * n / target);
	else
		*page = (uint64_t)(((__uint128_t)(slot - target + 1) * n - 1) /
				   (n - target));
	return 0;
}

static inline int bench_corpus_page_index(const struct bench_corpus *c,
					  uint64_t slot, uint64_t *page)
{
	uint64_t base = 0, n, target, local;
	uint32_t i;

	if (!c || !page || slot >= c->total_pages)
		return bench_corpus_error(ERANGE);
	if (!c->uniform_v411) {
		*page = slot;
		return 0;
	}
	n = c->total_pages;
	target = c->sampling_targets[0];
	if (!target) {
		for (i = 0; i < c->source_count; i++)
			if (slot >= c->sources[i].start_page &&
			    slot - c->sources[i].start_page < c->sources[i].pages)
				break;
		if (i == c->source_count)
			return bench_corpus_error(ERANGE);
		base = c->sources[i].start_page;
		n = c->sources[i].pages;
		target = c->sampling_targets[i + 1];
	}
	if (bench_corpus_sample_index(n, target, slot - base, &local))
		return -1;
	*page = base + local;
	return 0;
}

/* Same physical page identity as load/verify, not the virtual slot number. */
static inline void bench_corpus_identity_csv(const struct bench_corpus *c,
					    uint64_t slot)
{
	uint64_t page;
	uint32_t i;

	if (!c)
		return;
	if (!bench_corpus_page_index(c, slot, &page))
		for (i = 0; i < c->source_count; i++)
			if (page >= c->sources[i].start_page &&
			    page - c->sources[i].start_page < c->sources[i].pages) {
				printf(",%s,%" PRIu64, c->sources[i].label, page);
				return;
			}
	fputs(",unavailable,", stdout);
}

static inline int bench_corpus_expected(struct bench_corpus *c, uint64_t index,
					unsigned char expected[32])
{
	uint64_t off;

	if (!bench_corpus_ready(c))
		return bench_corpus_error(EINVAL);
	if (index >= c->total_pages)
		return bench_corpus_error(ERANGE);
	off = c->hashes_offset + index * 32;
	if (bench_corpus_read_block(c, off & ~UINT64_C(4095)))
		return -1;
	memcpy(expected, c->io_buffer + off % 4096, 32);
	return 0;
}

/* Run verification outside the timed refault section; never prints bytes. */
static inline int bench_corpus_verify(struct bench_corpus *c, uint64_t index,
				      const void *page)
{
	unsigned char digest[32], expected[32];

	if (!page || !bench_corpus_ready(c))
		return bench_corpus_error(EINVAL);
	if (index >= c->total_pages)
		return bench_corpus_error(ERANGE);
	if (bench_corpus_page_index(c, index, &index))
		return -1;
	bench_corpus_digest(page, 4096, digest);
	if (bench_corpus_expected(c, index, expected))
		return -1;
	if (memcmp(digest, expected, 32))
		return bench_corpus_error(EBADMSG);
	return 0;
}

static inline int bench_corpus_load(struct bench_corpus *c, uint64_t absolute_page,
				    void *page)
{
	unsigned char digest[32], expected[32];

	if (!page)
		return bench_corpus_error(EINVAL);
	if (bench_corpus_page_index(c, absolute_page, &absolute_page) ||
	    bench_corpus_expected(c, absolute_page, expected))
		return -1;
	if (bench_corpus_read_block(c, c->data_offset + absolute_page * 4096))
		return -1;
	bench_corpus_digest(c->io_buffer, 4096, digest);
	if (memcmp(digest, expected, 32))
		return bench_corpus_error(EBADMSG);
	/* Do not expose a corrupt page to the caller, even on the error path. */
	memcpy(page, c->io_buffer, 4096);
	memset(c->io_buffer, 0, 4096);
	return 0;
}

#endif /* MEMCOMP_COMPARE_CORPUS_H */
