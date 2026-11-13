# Private corpus transport v1 (bounded v4.10)

This is a transport/integrity module, not a workload generator or benchmark.
The captured real corpus must remain private. Importing `pack.py` never opens
that corpus. No capture, packaging of real data, VM/device test, kernel build,
full matrix, or performance test is part of this module's offline validation.

## Manifest adaptation

The packager accepts the existing `memcomp-private-corpus-v1` JSON manifest:

- Root keys: `format`, integer `page_bytes: 4096`, `complete: true`, and ordered
  `applications` containing between 1 and 16 entries.
- Per application: `app` (label), positive integer `pages`, `bytes == pages*4096`,
  `complete: true`, `file` (relative `.pages` path), `sha256` (whole data SHA256),
  `per_page_hash_file` (relative binary `.sha256-pages` path), and
  `per_page_hash_sha256` (whole sidecar SHA256). Digests are 64 hexadecimal
  characters. Each sidecar is exactly `pages*32` raw bytes, not hex lines.
- Optional `requested_sources` must match the `applications` labels in exactly
  their listed order. Optional integer `total_pages` and `total_bytes` must match.
  Duplicate JSON keys, paths, labels and source-file inode aliases are rejected.
- Other capture metadata is ignored, never copied into the image or inspector
  result. No serial, PID, address, package identity, timestamp, or source path
  is exported. Only the caller-approved safe source labels and digests remain.

Labels contain 1..31 ASCII characters `[A-Za-z0-9_.-]`. `combined` is reserved.
There is no sorting, deduplication, content generation, sampling, repetition,
compression, or removal of zero/same-filled pages. Repeated *captured content*
is retained; repeated input file aliases are not accepted as extra sources.

## Exact image layout

All integers are unsigned little-endian. All byte ranges are half-open.
No C structure is written directly to disk.

| Offset | Bytes | Meaning |
| --- | ---: | --- |
| 0 | 16 | `MEMCOMP-CORPUS1` followed by one NUL |
| 16 | 4 | version = 1 |
| 20 | 4 | page_size = 4096 |
| 24 | 4 | source_count = 1..16 |
| 28 | 4 | header_bytes = 4096 |
| 32 | 8 | total_pages > 0 |
| 40 | 8 | hashes_offset = 4096 |
| 48 | 8 | data_offset = align_up(4096 + total_pages*32, 4096) |
| 56 | 8 | image_bytes = data_offset + total_pages*4096 |
| 64 | count*128 | source entries in manifest order |
| end of entries | through 4064 | zero reserved bytes |
| 4064 | 32 | raw SHA256 of header bytes [0, 4064) |
| 4096 | total_pages*32 | unchanged concatenated binary per-page sidecars |
| end of hashes | through data_offset | zero alignment padding |
| data_offset | total_pages*4096 | unchanged concatenated source pages |

Each 128-byte entry has:

| Relative offset | Bytes | Meaning |
| --- | ---: | --- |
| 0 | 32 | ASCII label, NUL terminated and zero padded |
| 32 | 8 | start_page |
| 40 | 8 | pages > 0 |
| 48 | 32 | raw whole-source data SHA256 |
| 80 | 32 | raw whole-source sidecar SHA256 |
| 112 | 16 | zero reserved bytes |

The first start_page is zero; each subsequent start_page is the exact previous
end. The entries cover total_pages exactly. Overflow, unsupported fields,
overlap, gaps, duplicate labels and nonzero reserved bytes are errors. Offset
arithmetic is limited to signed 64-bit file offsets. The capacity must equal
image_bytes exactly: no truncated image or trailing bytes, including on a block
device. This implies an exactly sized read-only device, not a larger partition
containing an image prefix.

## Packaging and privacy

CLI requires both `--root PRIVATE` and `--output PRIVATE`. Both must be explicit
absolute paths without parent traversal. The root and final output directory
must be owned by the caller and mode 0700; inputs must be owned regular files
mode 0600. A missing output leaf directory is created mode 0700. The output must
be outside the input root and outside Git worktrees/results/public directories.
Directory-FD walks plus O_NOFOLLOW reject symlink components and source escapes.
No raw source or sidecar is copied to another location except the requested
private image. No separate exported manifest/metadata file is generated.

Input descriptors are pinned (at most 32); metadata is snapshotted. The packager
streams one page and its 32-byte digest at a time, checking each page digest and
both whole-source digests, exact lengths, and source metadata stability. The
manifest is bounded to 1 MiB. All padding starts as zero. Bytes are reproducible:
there is no random ID, timestamp, source path or host metadata in the image.

The output inode is created as `OUTPUT.partial` with O_CREAT|O_EXCL and mode 0600.
Only after complete verification, header write and fsync is it linked atomically
to OUTPUT. linkat publication is exclusive (EEXIST, never overwrite), unlike
rename. The partial link is removed and the directory fsynced. Caught failures
clean up owned partials and roll back owned publication. Existing final images
and stale partials are never changed. A crash/SIGKILL can leave a private partial;
subsequent attempts fail closed rather than silently reuse it. Filesystem cleanup
or fsync failures are errors, not successful publication claims. Same-UID hostile
writers/administrative device changes are outside this private-input trust model.
Checksums ensure integrity, not authentication or encryption.

## Shared Python inspector API

`pack.read_header(path)` returns this JSON-serializable shape, with lowercase
hexadecimal hashes (no paths or private capture identity fields):

```python
{
    "total_pages": 6,
    "page_size": 4096,
    "image_bytes": 32768,
    "header_sha256": "<64 hex characters>",
    "sources": [
        {"label": "app-a", "start_page": 0, "pages": 6,
         "data_sha256": "<64 hex characters>",
         "hashes_sha256": "<64 hex characters>"}
    ]
}
```

This example is illustrative metadata, not a real-data description. The function
validates every header field, label/index/reserved byte, header digest, overflow
and exact size without scanning the hash section or page data. Regular files use
O_RDONLY. Blocks require BLKROGET==1, supported sector size and O_DIRECT; the sole
header read uses an mmap-aligned 4096-byte buffer. Invalid metadata raises
`CorpusError` (a ValueError); I/O errors raise OSError. CLI failures sanitize
filenames/identities and never print payloads.

Import this function in run.py instead of maintaining another Python parser.
Use source pages/start_page for W1 adjacent double slices, W2 distinct source
entries, W3 pressure, and W4 held+ramp preflight; the transport does not implement
workload glue or infer those policies. A header inspection alone does **not**
verify sidecars/data, and cannot attest readback integrity.

## Header-only C API

Include `bench/compare/corpus.h` before system headers (or build with
`-D_GNU_SOURCE -D_FILE_OFFSET_BITS=64`). It is Linux userspace code and uses only
libc/Linux UAPI, with a self-contained SHA256 implementation, no OpenSSL.

- `int bench_corpus_open(struct bench_corpus *c, const char *path)`:
  initialize a fresh or previously closed object. Checks header/hash, all fields,
  exact size/index, and zero hash-to-data alignment padding. Does not scan data
  or recompute whole-source hashes. On failure, releases all owned resources and
  leaves fd=-1. Close before re-opening an already open object.
- `void bench_corpus_close(struct bench_corpus *c)`: release fd/buffer; repeatable
  on initialized/closed objects. Objects used without open must start fd=-1.
- `int bench_corpus_range(c, source, offset, pages, &start)`:
  source is `combined` or an exact label. Requires a nonempty in-bounds range.
  Never wraps, repeats, pads, or crosses a named-source end. Failure leaves start
  unchanged. Repeated API calls are stateless; disjoint workload slice selection
  is the caller's responsibility. For W1 request the immediately following slice
  from the same source and fail if it does not fit; never synthesize a prime.
- `int bench_corpus_load(c, absolute_page, dst)`: read sidecar digest and page,
  verify the page, then copy to arbitrary (possibly unaligned) dst. On error dst
  is unchanged. This loads only the explicitly selected absolute page.
- `int bench_corpus_verify(c, absolute_page, page)`: SHA256 readback bytes and
  compare to that page's sidecar digest. Run this **outside timed refault**.

Public fields: fd, page_size, total_pages, source_count, sources[16] with
label[32]/start_page/pages, hashes_offset and data_offset. Other fields are private.
Returns 0/-1 with errno: page mismatch from load/verify is **EBADMSG**; malformed
header is EINVAL; invalid page/range is ERANGE; missing label is ENOENT; actual
read/ioctl failures retain their errno; unexpected EOF/short direct read is EIO.
No function logs content. Readers are not thread-safe; serialize access to a
shared struct or open one per thread. Forked private copies do not share buffers.

Real block devices must report BLKROGET==1 before any read; their fd is switched
to O_DIRECT and checked again. All header/hash/data I/O uses ONE posix_memalign
4096-byte buffer at aligned offsets. There is no fallback to cached reads and no
cache of corpus pages. EINTR is retried; regular-file short reads are completed;
a short direct read fails rather than issuing a misaligned retry. Regular files
are supported for host functional fixtures, not an uncached measurement claim.

## Offline validation

Run only as WSL nakixii uid1000. Every fixture, temporary library and C build is
under `<work>/v410-offline-20261018`, never /tmp. Existing tests
and build scripts are untouched. Commands (no real-data packaging):

```sh
python3 -B bench/corpus/test_pack.py -v
python3 -B bench/compare/tests/test_v410_corpus_reader.py -v
```

The C harness pins compiler TMPDIR/TMP/TEMP inside its scratch build directory,
compiles with -std=gnu11 -O2 -Wall -Wextra -Werror, uses independent
binary/zero/same-page fixtures and SHA NIST known-answer tests, and simulates
block ioctls/direct-I/O alignment and errors without touching a device. Tests
cover byte reproducibility, no aliasing/repetition, all ranges, metadata/hash/
data corruption, truncation/tails, overflow/counts/labels/reserved fields, path
and permission checks, exclusive output races, interrupted/short I/O, cleanup,
and Python-to-C interoperability. Mock coverage is not physical block-device,
VM or workload validation. No throughput/timing claims follow from these tests.


## Private page classification side table v1

`classify.py` is an offline description tool, not a workload generator, kernel
classifier, timing benchmark or refault-origin oracle. It reuses the existing
`verify.Codecs` exactly: independent 4096-byte pages through userspace
`ZSTD_compressCCtx` at level 3 and `LZ4_compress_default`, no dictionary. It
records both actual library versions. Encoded sizes describe those userspace
APIs/formats, not kernel codec output sizes or zsmalloc memory consumption.

The CLI requires explicit absolute `--root PRIVATE_CAPTURE --output PRIVATE`.
It reuses pack's manifest order, per-page SHA256, whole-data and whole-sidecar
SHA256 validation, private owned 0700 directories/0600 files, no-symlink path
walks, length/alias/stability checks and Git/results/public exclusions. The output
must be outside the capture root. It streams one actual source page at a time
and uses bounded codec contexts, a bounded header and at most 32 source FDs.
There is no complete-corpus cache or unbounded histogram. Sources are neither
reordered nor repeated. Only functional test fixtures are generated by tests.
The tool does not run a VM or read PTEs, writeback counters or timestamps.

One binary file contains a 16384-byte header followed by exactly five bytes per
captured page. All integer fields are unsigned little-endian:

| Offset | Bytes | Meaning |
| --- | ---: | --- |
| 0 | 16 | `MEMCOMP-CLASS1` followed by two NULs |
| 16 | 4 | version = 1 |
| 20 | 4 | header_bytes = 16384 |
| 24 | 4 | record_bytes = 5 |
| 28 | 4 | metadata JSON byte length (1..16288) |
| 32 | 8 | total_pages |
| 40 | 8 | file_bytes = 16384 + total_pages*5 (exact, no tail) |
| 48 | 16 | zero reserved bytes |
| 64 | metadata length | compact sorted ASCII JSON metadata |
| end of metadata | through 16352 | zero reserved padding |
| 16352 | 32 | raw SHA256 of header [0, 16352) |
| 16384 | total_pages*5 | records in unchanged manifest/page order |

Record `i` is at `16384 + i*5` and has Python struct layout `<BHH`:

1. uint8 `same_word8_flag` = 1 iff the entire page repeats its first **8-byte
   word**, including all-zero pages; otherwise 0. The word itself is never saved
   or printed. This models the 64-bit unsigned-long same-fill scan in zram and
   memcomp, independent of the host's byte order. It is **not** capture's former
   repeated-single-byte statistic. Both counts, plus zero counts, are separately
   identified in metadata. A repeated non-byte-uniform word is same-word8 only.
2. uint16 `zstd_size` = actual userspace encoded size, including same-word pages.
3. uint16 `lz4_size` = actual userspace encoded size, including same-word pages.

For each codec independently, `classify.page_type(flag, size)` derives:

- 0: zero or same-word8 (flag takes priority over encoded size).
- 1: non-same page with userspace encoded size **strictly less than 3625**.
- 2: otherwise, including encoded size **equal to 3625** or larger.

The equality boundary was checked in the source-bound
`memcomp_payload_raw_reason()` (`mm/memcomp.c:4096-4105`): raw if compressed_len
is at least src_len or at least a nonzero huge_class_size. Zram likewise uses
`comp_len >= huge_class_size`. Metadata pins the semantic-reference revision
and memcomp file SHA256, and explicitly labels 3625 as the operator-reported
**prior actual stock x86_64/arm64 value, not a fresh probe by this tool**. It is
not a universal allocator threshold or a claim about a different build/config.

Metadata contains ordered source label/start_page/pages and whole data/sidecar
hashes matching the transport image, codec versions/configuration, threshold
provenance and strict comparison, per-source/combined counts, limitations, and
SHA256 over the complete record section. It contains no source paths, serials,
PIDs, raw words or page payloads. Standard output reports only completion,
page/source counts and the record-section SHA256; failures are sanitized.

The private output inode is created with O_EXCL at `OUTPUT.partial`, mode 0600;
only verified content and a finalized/fsynced header are atomically linked to
OUTPUT without overwrite. Caught failures clean up owned partial/publication;
existing outputs and stale partials remain untouched. Crash/SIGKILL can leave a
private partial and requires explicit handling, as with the image packager.
For identical inputs and codec versions, the classification bytes are reproducible.

`classify.read_header(path)` validates the owned private header/hash, source
index and exact file size and returns JSON-safe metadata plus header_bytes,
record_bytes, file_bytes and header_sha256. It reads no records or capture pages;
consumers must also verify `records_sha256` over the records before evidence use.
Match all source ranges and data/sidecar hashes to the corpus image before
joining absolute `corpus_page_index` values. Never relabel this userspace content
bucket as kernel storage/refault origin: timing attribution requires a later
join with actual PTE observations and writeback-counter evidence. No generated
kind or synthetic timing is a substitute.

Functional fixtures only (WSL nakixii uid1000, all scratch beneath the existing
private v410-offline directory):

```sh
python3 -B bench/corpus/test_classify.py -v
```

Actual classification is deliberately left for the operator to invoke once;
choose a new private output path, never Git/results/public. No actual capture
pages are read by implementation/testing of this tool.

## v4.11 explicit versioned manifest selection

pack.py and classify.py accept --manifest LOCAL_NAME.json (default remains
manifest.json). The selected file must remain inside the same private root
and pass the unchanged no-symlink/private-owner/schema checks. Selection
never falls back to another manifest on an error. Supplemental capture and
eight-source aggregation use separate names; the original manifest and
source files are not modified. Image/classification layouts are unchanged.

Uniform targets and ascending disjoint auxiliary sets are a workload-layer
selection, not a rewrite or resampling of transport bytes. sample_stats.py
reports aggregate selected-set vs full-source content descriptions from
private image/classification bindings; no page payload, word or individual
content hash is exported.
