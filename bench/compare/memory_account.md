# Strict offline smoke memory accounting

`memory_account.py` is a small diagnostic for a **real smoke** `run1.log.gz`.
It does not run VMs, consume R3, print page contents, rewrite raw/status files,
rank latency or decide whether to change the kernel entry layout.

```sh
python3 -B bench/compare/memory_account.py \
  /absolute/real-smoke/run1.log.gz \
  --source-root /actual/smoke/kernel/source \
  --config /absolute/real-smoke/.config \
  --guest-source /actual/captured/guest.c \
  --output <work>/v410-offline-20261018/memory-report.json
```

API: `report, log = analyze(path, source_root=None, config=None, headers=None,
 guest_source=None)`. Full meminfo/slabinfo/zoneinfo/vmstat/swaps/pool blocks,
sysfs slab aliases and numeric swap-layout records survive the
`telemetry.parse_log` callback. No reducing `retain_snapshot` is applied to raw.

The exact variant `zswap-crosscheck` uses the same accounting pool, source rules
and strict named-cache requirements as `zswap`. This is only an accounting alias:
`backend` and the parsed raw request retain `zswap-crosscheck`; the report exposes
`accounting_backend=zswap` and `accounting_family=zswap` separately. Its independent
kernel artifact/source/config must still be supplied and verified, not borrowed
from an ordinary zswap build. Source/config hashes remain in `evidence.files`.
Unknown or prefix-lookalike variants are not accepted.

## Windows and signs

- Setup: `before_backend_setup -> workload_start`.
- W1: each `rN_seq_fill_begin -> rN_seq_read_begin`, including any warmup.
- W1 total: **setup + that round's payload**. These are two separate windows;
  `workload_start -> fill_begin` is outside the sum. No median is produced.
- W2: `workload_start -> workload_end`; W3/W4: `prepared -> read_begin`.
  These are whole-workload/pressure observations, **not pure backend costs**.

Observed loss is start-minus-end physical free:
`MemFree * 1024 + PCP_count * guest_page_bytes`. PCP coverage must match;
`high`/`batch` are never added. Component growth is end-minus-start and
releases stay negative. Sources are sequential/non-atomic, not exclusive costs.

## Mutually exclusive physical components

The additive terms are:

1. Physical backend pool byte delta.
2. **Global Slab** delta, including large kmalloc allocations accounted by the
   inspected `mm/slub.c` in NR_SLAB_UNRECLAIMABLE_B. No separate compression
   workspace/large-kmalloc addend is fabricated.
3. Five LRU physical partitions: `Active(anon)`, `Inactive(anon)`,
   `Active(file)`, `Inactive(file)`, `Unevictable`; plus captured vmstat
   `nr_isolated_anon` and `nr_isolated_file` physical-page gauges. The source
   classifies a folio into one LRU list and removes isolated folios from LRU
   sizes. Missing/malformed isolation fields are **undefined**, not zero.
4. PageTables, Percpu, VmallocUsed deltas.
5. Independently proven direct swap_cgroup data-page growth.
6. KernelStack **only with explicit CONFIG_VMAP_STACK=n evidence**.
   With VMAP_STACK=y its pages are already inside physical VmallocUsed.

AnonPages/Cached/Buffers/SwapCached are retained as diagnostics **only**. Their
underlying pages are already covered by LRU/isolation; mapped swapcache may
also overlap AnonPages. They are never added to that partition or to each other.
A nonzero SwapCached therefore does not itself invalidate the LRU partition.

SReclaimable/SUnreclaim and named entry/radix/zs_handle/zspage caches are
subdivisions **inside Slab**, never extra addends. Family/per-cache and
cross-family alias views may overlap. No sum of those views is a backend cost.
The alias parser uses slabinfo first; only a missing family may fall back to
captured sysfs inventory, readlink/actualtarget, device/inode and attributes.
A shared physical target counts once per family, never once per alias.
`object_size_bytes` remains allocator-slot size (`slab_size` in fallback);
`sysfs_object_size_bytes` separately preserves `object_size`, which is a
shared-cache size, **not proof of the original requesting struct size**.
Sysfs pages are `slabs * 2**order`; active_slabs is unavailable, not invented.
No missing/merged/uncreated cache is silently zero or N/A without evidence.

## Source/config evidence

Supply sources/config of the actual smoke build, not a mutable unrelated build.
The report records hashes and recognized-rule line locations. This is an
inspected 6.12 x86_64 / 4096-byte-page implementation recognizer, not a general
proof engine. Missing configuration, unrecognized implementation or unresolved
overlap makes the result undefined. Caller must bind kernel/source/config to
the smoke's provenance; source hashes alone do not identify a kernel image.

| Term | Inspected source basis |
|---|---|
| LRU and isolation | fs/proc/meminfo.c; include/linux/mmzone.h and mm_inline.h; mm/vmscan.c; mm/vmstat.c |
| Slab, including large kmalloc | fs/proc/meminfo.c; mm/slub.c |
| Pool | mm/memcomp.c, drivers/block/zram/zram_drv.c or mm/zswap.c; zsmalloc page allocation in mm/zsmalloc.c |
| PageTables | fs/proc/meminfo.c NR_PAGETABLE |
| VmallocUsed | fs/proc/meminfo.c vmalloc_nr_pages(); mm/vmalloc.c physical-page counter |
| Percpu | fs/proc/meminfo.c pcpu_nr_pages(); mm/percpu-vm.c maps independently, without that vmalloc counter |
| KernelStack | kernel/fork.c VMAP_STACK vs alloc_pages path and matching .config |
| Direct swapcg data | mm/swap_cgroup.c; mm/swapfile.c; include/linux/swap.h |

## Swap layout without retaining disks

The guest formatter emits a collector-owned virtual raw source
`/compare/swap_layout/dev/vda` or `/compare/swap_layout/dev/zram0` **after**
header pwrite/fsync, swapon and actual swap inventory checks succeed. It holds
only device/page_bytes/version/last_page/nr_badpages, never header bytes or UUID.
This is not a new kernel ABI. Exactly one record per device is allowed.
A zram setup policy RAW phase can split the layout into a later **implicit**
record named before_backend_setup. A record with only layout sources is
supplemental evidence, not another measurement snapshot. The analyzer keeps
and validates it, but windows require one actual explicit COMPARE_SNAPSHOT
per endpoint. Real duplicate explicit snapshots, missing explicit baselines,
or same-named implicit records mixed with any other source or capture-limit
marker remain undefined.
The pre-reduction source set is checked so an unknown path cannot be hidden
by raw-path filtering. No missing field is filled, and raw logs are untouched.

The analyzer verifies the exact `--guest-source` hash against the adjacent
`metadata.json` `workload_source_hashes["guest.c"]` and recognizes the reviewed
producer. Default source is sibling guest.c; if that file has changed, pass the
exact archived producer. Missing/mismatched hash or missing/duplicate layout is
undefined when needed. Every captured layout must match actually active
`/proc/swaps` usable-page counts. No requested-capacity guess is accepted.

For version 1, no bad pages and verified header exclusion:

```
max_pages = last_page + 1       # /proc/swaps usable pages == last_page
SC_PER_PAGE = 4096 / 2 = 2048
swapcg_direct_data_bytes = ceil(max_pages / 2048) * 4096
```

This uses the formatter's **actual written geometry**, not retained swapdisks.
Existing real header captures may optionally be supplied as
`--swap-header GUEST_DEVICE=HOST_IMAGE`; the tool never manufactures one,
retains a disk, or substitutes a header for invalid captured layout evidence.
Old logs without either proof stay undefined for changed swap inventories.

The vcalloc swapcg pointer array is already in physical VmallocUsed; zram's
vmalloc table, swap_map and cluster_info are not added again. Unchanged swap
inventories have no direct data-page delta. CONFIG_MEMCG=n or a captured
cgroup_disable=memory/all is explicit nonapplicability, not missing data filled
with zero; enabled runtime state requires captured /proc/cmdline.

## Gates and residual

`residual = observed_loss - sum(nonoverlapping_components)`.
Only integer **abs(residual) <= 500000 bytes** passes, decimal 0.5 MB, not 0.5 MiB.
There is no `other` bucket or residual filler. Setup/payload failures cannot
cancel into a PASS total. `residual_gate` reports only this arithmetic result.
`named_cache_required_gate` follows the existing telemetry required cache fields;
missing raw evidence does not relax it. Overall `gate` is undefined if required
named-cache evidence is incomplete, even if a numeric residual is small.
None of these fields relabel the original boot's valid/status result.
Exit codes: 0 PASS, 1 defined residual FAIL, 2 undefined.
A PASS is global arithmetic closure, not backend-exclusive attribution or proof
that there are no leaks. SUnreclaim leakage is not investigated.

The separate provisional illustration is 16 B zram table + 1 B swap_map +
2 B swap_cgroup per slot versus 80 B memcomp entry + approximately 9 B xarray
per stored page: **19/89 = 21.35% approximately**. Actual build/pahole layout
proof is pending. These are not measured addends and do not include every
allocator header/rounding, cluster_info, cache pool or other detail.

## Host tests

Use the approved scratch as TMPDIR, set PYTHONDONTWRITEBYTECODE=1, then run:

```sh
python3 -B -m unittest discover \
  -s bench/compare/tests \
  -p test_v410_memory_account.py -v
```

Tests create boundary text/header fixtures only, not performance corpora or
synthetic smoke results. No production raw logs or status labels are modified.


## Historical v4.11 v1 W1 memory consistency (replay only)

This is a userspace collector protocol, **not** a kernel ABI or instrumentation.
It is sequential and non-atomic; acceptance is not proof of a root cause.
Do not relabel any historical result or replace the legacy free-memory basis.

Front-end integration contract:

- Flag: `--snapshot-memory-consistent`, W1 only. Package the regular file
  `/snapshot-memory-consistent` with `1\n`. Missing or `0\n` disables it.
  It is mutually exclusive with an enabled `/snapshot-vmstat-refresh`.
- Metadata: `snapshot_memory_consistent=true` and
  `snapshot_counter_protocol="v4.11-w1-memory-consistent-v1"`.
- The versioned RAW marker identity (`telemetry.MEMORY_PROTOCOL`) is
  `v4.11-w1-memory-consistent-v1`, exactly matching the metadata value above.
- Keep the original `COMPARE_SNAPSHOT`. Each one is followed by
  `COMPARE_MEMORY_SNAPSHOT_BEGIN`, 1..10 framed
  `COMPARE_MEMORY_ATTEMPT_BEGIN` / `COMPARE_MEMORY_ATTEMPT_END` pairs, and
  `COMPARE_MEMORY_SNAPSHOT_END`. All have `monotonic_ns` and `phase`.
- Snapshot BEGIN includes `version`, `page_bytes`, `tolerance_pages=16`,
  `max_attempts=10`, `memfree_check=max_abs_before_after`, `free_sample=after`.
  Snapshot END includes `version`, `status=CONSISTENT|INVALID|ERROR`, `attempts`.
- Every attempt starts from one successful `COMPARE_VMSTAT_REFRESH`, reads
  buddyinfo then zoneinfo, captures meminfo/slabinfo and other classifications,
  then reads buddyinfo then zoneinfo again. The four free-memory RAW aliases
  are `/compare/memory/v411/{before,after}/{buddyinfo,zoneinfo}`; the real input
  files are `/proc/buddyinfo` and `/proc/zoneinfo`. Classifications retain their
  original source paths. Attempt frames disambiguate repeated paths.
- Attempt END includes `attempt`, `status=CONSISTENT|INCONSISTENT|ERROR`,
  `errno`, `buddy_before_pages`, `pcp_before_pages`, `buddy_after_pages`,
  `pcp_after_pages`, `memfree_pages`, `free_drift_pages`,
  `memfree_buddy_max_pages`. The analyzer recomputes these from the RAW blocks.

With `B0/B1` buddy totals, `P0/P1` PCP totals, and `M` MemFree in base pages,
**both** inclusive conditions must hold:

```
free_drift_pages = abs((B1 + P1) - (B0 + P0)) <= 16
memfree_buddy_max_pages = max(abs(M - B0), abs(M - B1)) <= 16
```

The maximum of **both** MemFree/buddy distances is deliberate. No averaging,
preferred endpoint, signed cancellation or MemFree+PCP substitution is used.
An accepted snapshot's physical-free value is `(B1 + P1) * page_bytes`.
At 4096-byte pages the tolerance is 65536 bytes; the gate is in pages.

Ten INCONSISTENT attempts produce **INVALID**, not residual FAIL or guest
execution failure. All attempts remain in RAW and in the compact report.
I/O errors, malformed/missing evidence, unsupported versions, changed topology,
missing terminal events, or marker/RAW disagreement are errors, never INVALID.
A consistent window still FAILs for `abs(residual_bytes) > 500000`, unchanged
in either sign. An independently defined FAIL is not hidden by an INVALID
window; otherwise any INVALID point invalidates the boot's memory result,
including points outside the setup/payload residual windows. Timing is not
invalidated merely by bounded consistency exhaustion.

New JSON fields are `memory_snapshot_protocol`, `memory_snapshot_validity`
(`VALID|INVALID|ERROR`), `memory_snapshot_status`
(`CONSISTENT|INVALID|ERROR`), `memory_snapshot_count`, and `memory_snapshots`.
Each point contains phase, terminal status, selected attempt (null if INVALID),
and an `attempts` list with recomputed counts, refresh and source-clock evidence.
The driver must compare **the exact expected phase set and unique count**, not
only residuals; the present one-round cohort expects 22 points. The generic
analyzer does not hard-code 22 because rounds/warmup determine that inventory.

CLI exits: PASS=0, FAIL=1, undefined/error=2, INVALID=3. Only INVALID permits the
plan's one additional boot, retaining both attempts; a second INVALID stops.
ERROR does not take that retry branch. The analyzer neither launches that retry
nor alters any original run status. Old logs keep their original schema and
MemFree+PCP formula.

Offline tests (no VM, device, kernel build, or real stat_refresh):

```sh
TMPDIR=<work>/v411-memory-snapshot \
MEMCOMP_TEST_SCRATCH=<work>/v411-memory-snapshot \
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover \
  -s bench/compare/tests \
  -p test_v411_memory_snapshot.py -v
```


## v4.12 W1 memory consistency v2 design (current protocol v3; new cohort-v3 only)

The same opt-in flag now produces `v4.11-w1-memory-consistent-v3`.
Metadata, every marker, the actual guest and all host consumers must agree.
Old v1 records retain their original condition and labels during replay;
never merge their accepted jobs into the new 34-job cohort.

The collector pre-touches and mlocks a fixed 2MiB output staging buffer,
plus its input/stdio/direct-I/O/directory scratch, before the first baseline.
Between the before/after buddy+PCP reads it only appends to this resident
buffer. The entire attempt is released to the writer after the second
pair; overflow discards the attempt and reports ERROR, not truncated data
or INVALID. Fixed getdents storage and bounded sysfs-name resolution avoid
malloc-backed directory streams/realpath scratch in that bracket.

Only this memory path opens the owned data disk O_DIRECT. Resident aligned
chunks use the validated logical-sector size; the last chunk is zero-padded.
The unchanged 512-byte header and transport SHA256 describe real logical
bytes only, excluding padding. There is no buffered-I/O fallback. The normal
formal-matrix path keeps its buffered writer and default snapshot protocol.

The BEGIN memfree_check value is `between_buddy_with_tolerance`. END still
retains memfree_buddy_max_pages as a diagnostic; it is NOT the v2 gate.
From RAW in base pages, accept iff both inclusive conditions hold:

```
abs((B1 + P1) - (B0 + P0)) <= 16
min(B0, B1) - 16 <= M <= max(B0, B1) + 16
```

The integer implementation uses distance to the interval, avoiding unsigned
underflow/overflow. MemFree is still not added to physical-free accounting;
use after buddy+PCP. Between unsuccessful attempts, wait 50ms. Ten attempts,
one additional boot per INVALID job, second INVALID stop, and signed
absolute residual limit500000B are unchanged. All22 phases are required.

This protocol was chosen AFTER observing cohort-v2 INVALIDs, not before.
Those boots had not reached any memcomp backend. Their RAW records show
64KiB Buffers increments and normal PCP transfer effects; no result labels
are replaced and the numerical residual threshold has not changed.

Arithmetic caveat: the plan's illustrative 238335/1047 ->238272/1093 pair
has total-free drift17. It passes only the NEW MemFree interval test, not
the complete unchanged drift<=16 gate. Tests retain these exact numbers;
a separate real QQ pair240371/3336 ->240308/3385, M240320 has drift14 and
passes the complete v2 test while failing v1's max-distance condition.
No missing pre-buddy observation is invented for the older QQ failure.
