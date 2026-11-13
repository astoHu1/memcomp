# Stage 4 statistics and compact raw telemetry (v4.8 contract)

## Current method: v4.8 documentation contract

The v4.8 release-plan revision supersedes the current W1 denominator, W4
stability and primary memory-accounting rules below. Historical evidence
sections retain their original methods and decisions; they are not v4.8
validation. The serialized top-level `method=v4.6` alone does not establish
v4.8 compatibility: freeze the updated W1/W4 protocols and source hashes.
This documentation update does not claim implementation, smoke, ACK selftest
or formal-matrix completion. See matrix.md for the prerequisites to freezing
new inputs and starting the 565 assigned slots.


`summarize.py` reads workload CSV and joins the phase-source telemetry;
`telemetry.py` reads only delimited raw snapshots. Both require Python 3.10+
and the standard library. Neither starts guests, builds code, rewrites raw
records or changes the producing matrix's machine decisions.

## Input, identity and safety gates

One invocation takes one finished matrix root (or one smoke destination).
Choose a **new output directory outside the input root**. Never merge boots
from separate runs. Examples below are commands, not executed verification:

```sh
python3 bench/compare/summarize.py \
  --output <work>/v46-workload-summary-NEW MATRIX_ROOT
python3 bench/compare/telemetry.py \
  --output <work>/v46-telemetry-summary-NEW MATRIX_ROOT
```

Only `method=v4.6`, `kind=interleaved-comparison` participates in formal boot
statistics. Other methods, missing/unknown methods and the named v43, v43-r2,
v44 and v45 result roots are blocked **before either compressed payload is
opened**. Old v4.5 data remains quarantined even if its files are complete;
these readers neither relabel it nor recalculate its stored decisions.
v4.6 smoke is descriptive only, never a formal sample.

Identity is `workload_id`, `group_id`, `host_boot_round`, plus source directory,
method and kind. A `round` metadata alias must not conflict. Formal W1/W2
expect host rounds 1..10; W3/W4 expect 1..5. Every destination needs exactly
one repeat=1 status with valid guest result, CSV integrity, lifecycle safety
and comparison eligibility. Original `status.json` is retained verbatim as
JSON in the `run.status` row. Effective source/freeze metadata is recorded once
in `run.provenance`; missing boot identity may come only from the matrix job,
not a fabricated successful metadata record. Failed, absent and duplicate boots do not vanish:
manifest destinations retain their assigned identity even without metadata,
status or raw files; missing/duplicate positions stay in the denominators.
Conflicting matrix/metadata identities invalidate that boot.

The runner/matrix's source, helper, binary, kernel/config, CPU-affinity,
protocol and before/after freeze checks remain authoritative. Postprocessing
is not a replacement for them. Do not edit measurement inputs after formal
start. The explicit matrix remains 565 slots (400 W1, 100 W2, 50 W3, 15 W4);
rewrite runs inside W1, not in extra matrix slots.

Formal raw records come from the verified `MEMCOMP_DATA_V1` non-swap `/dev/vdb`
result disk. The supervisor/runner must establish guest/process-group death,
export, exact-inode cleanup/ACK, bytes/SHA256/zero-tail validation and UART
agreement before gzip. The independent result-disk IO is not backend vda IO.
No missing data is reconstructed from serial child-stderr replay. Kernel/data
failure markers remain fatal to the boot. `cache=unsafe` means vda reads may
hit host page cache; guest sectors are not durable host-storage IO.

## Compact outputs and streaming bound

Both commands write only:

- `per-round.csv`: derived workload/phase metrics, with warmup flags retained;
- `per-guest.csv`: one measured-round median per guest/metric plus status rows;
- `summary.csv`: formal boot distributions and expected/valid/invalid/missing
  counts, plus W4 `n_latency_excluded`;
- `pairs.csv`: every assigned paired position, including unavailable ones;
- `paired-summary.csv`: paired ratios, noise/consistency decisions and sample
  context. W3 pairs carry both sides' nonpresent counts/fractions.

There is **no `telemetry.csv`**, no expanded snapshot inventory and no
intermediate uncompressed raw table. `gzip.open(..., "rt")` is iterated line
by line. At each snapshot boundary, only the documented C.5/C.6 readings, three optional memory diagnostics and
compact PID/starttime CPU samples are retained; raw block text is released
before the next snapshot. Unknown ABI fields and unsupported-backend trees
are not expanded into thousands of unavailable rows. Each guest's compact
round rows are written immediately; run errors/status/freeze metadata are written
once per guest instead of repeated in every metric row; cross-boot aggregation retains guest
medians, not every guest's raw snapshots or inner-round rows.

The number of telemetry report rows depends on configured rounds and defined
phase windows, not raw lines, capture inventory or per-page touch count.
With one warmup plus five measurements, the current largest W1 telemetry
selection is at most 768 phase-metric rows per guest (128 per inner round, including
12 optional memory rows). W3/W4 add at most six optional endpoint rows and
have fewer than 70 phase-metric rows per guest; W2 uses its whole-workload
source-counter window. These are **source-derived row bounds, not measured
output sizes or an executed exit-0 claim**. Workload CSV statistics are
additional compact rows; repeated raw ABI inventories are never written.
No resource limit is raised. A full new-matrix extraction and actual output
byte-size/exit-status check remain required before acceptance.

Metric `source_path` and JSON `provenance` retain phase endpoints, monotonic
snapshot clocks, each endpoint's actual source-read begin/end, semantics,
quality and accounting basis. A guest median refers back to its per-round
metric, not an invented single atomic sample. Snapshots are non-atomic.
Missing values are empty with a reason, never synthetic zero. Counter reset,
malformed/truncated framing, missing endpoints or decompression failure cannot
be clamped/interpolated into valid metrics. Mandatory C.5/C.6 failures are
run errors, not merely unavailable Cells: `run.raw_complete=0` and
`extraction_failed=true`. Both telemetry and the joined workload-summary CLI
exit 1 after writing the invalid identity. Parser exceptions also exit 1;
telemetry records raw-complete=0 on that error path. Failed/missing identities
remain in the reports; valid complete inputs must finish normally, without a
giant output table.

### Mandatory sources before raw release

Validation is workload/backend/phase-specific and happens while that
snapshot's raw blocks are still available, before clearing them:

| Phase | Mandatory C.5/C.6 inputs |
| --- | --- |
| W1 each measured seq/rand/single/rewrite read_begin/read_end; W2 workload_start/workload_end; W3/W4 read_begin/read_end | vmstat pswpin/pswpout. Zswap also requires zswpin/zswpout/zswpwb; memcomp requires events swapped_read_io/error/cached; zram-writeback requires bd_reads/bd_writes. Disk-backed swap/zswap/memcomp-swap/zram-writeback also require vda read/write counters. |
| W1 seq/rewrite fill_begin and read_begin | MemFree/MemAvailable; zoneinfo PCP counts and guest page size for the corrected main memory metric (N/A with reason if unavailable). At read_begin also require that backend's raw pool gauges. Primer is not a measured read window. |
| W2 workload_start/workload_end; W3/W4 prepared/read_begin | MemFree/MemAvailable and that backend's raw pool gauges. |
| W4 pressure_begin/read_begin/read_end | vda sectors_written for pressure-to-read and readback write deltas; the pressure-to-read delta must be positive. |
| W4 pressure_begin, terminal settled-or-unstable, read_begin | Native writeback counter, with a positive pressure_begin-to-read_begin delta; gate state is descriptive only, never a required exclusion input. |
| W4 every WRITEBACK_POLL | Elapsed time, gate or N/A, native writeback, physical pool bytes, pgscan_kswapd, pgscan_direct, vda sectors_written; three complete unchanged writeback-and-pgscan intervals define stability. |

Raw pool requirements are memcomp L/C/P; zram mm_stat logical/payload/pool;
zswap stored_pages/pool_total_size. A whole missing/NA required block, missing
field, duplicate key or malformed numeric field is fatal. Required sample
begin/end must be ordered and no earlier than its snapshot; consecutive
required samples cannot overlap/go backwards. Required cumulative counters
cannot decrease/reset, and their definitions must match across samples.
Gauges may legitimately shrink after release/writeback: only their sample
clocks, not their values, must be monotonic.

Do not require ratios to be populated: zero pool/payload denominators are
legitimate N/A. PSI, thread CPU, other backends' trees, and plain-zram bd_stat
when writeback is not active remain optional for this C.5/C.6 gate. Their
semantic unavailability does not invalidate a guest; no absent value is
changed to zero. Existing raw framing/transport errors remain errors even
when the malformed frame belongs to an optional node.

## W1: fresh normal rounds and isolated rewrite

The producer retains its existing CSV fields, including v4.7 rewrite-prime
observations, and adds the three v4.8 first-pass/drain-delta batch fields.
Freeze that schema and protocol/source hashes; an old 46-column stream or
`method=v4.6` alone does not satisfy this contract. Each inner round emits modes
`seq`, `rand`, `single`, then `rewrite`; seq/rand/rewrite have touch records
followed by one batch record, single has touches only. `--rewrite` is required.
Rows must cover every assigned round/mode/page in order, with no duplicates,
missing touches or inconsistent warmup flags. Formal W1 is one warmup and
five measured rounds. Full-page verification and bounded-drain checks remain.

Every ordinary inner round starts with a fresh anonymous mapping, so prior
swap slots/backend copies are released before the first fill. Only **seq**
`memfree_before/after/delta_kib` and `memavailable_*` are reported as the fresh
fill-to-drained memory baseline. Rand/rewrite batch `before=-1` is not turned
into a fresh memory result. Telemetry separately records seq fresh baseline,
drained memory and backend pool endpoints. Plain-swap and same-filled-page
near-zero memory deltas are required experimental sanity checks, not a
hardcoded tolerance or an automatically asserted PASS.

Immediately after each **initial timed batch PAGEOUT** returns, stop the
clock and read **only pagemap**, without touching target pages, before any
drain, retry or remap. Freeze `first_pass_nonpresent_pages` from that sample:

```text
pageout_ns = pageout_total_ns / first_pass_nonpresent_pages
first_pass_nonpresent_fraction = first_pass_nonpresent_pages / requested_pages
```

Zero first-pass pages means N/A for the normalized latency, never a fabricated
zero. Show `first_pass_nonpresent_fraction` **alongside `pageout_ns`** in every
result table/figure that reports that batch metric. Keep the later post-drain
`nonpresent_pages` and frozen readback classification separate. Report
`drain_additional_nonpresent_pages = nonpresent_pages - first_pass_nonpresent_pages`
and `purge_ns` descriptively: retries can increase final nonpresent coverage,
but their time and extra pages must not make the initial PAGEOUT look faster.
This applies to seq, rand and measured rewrite batches; the primer and
single-page measurements keep their separate roles.

For cross-backend memory comparisons, the main metric is the signed
**MemFree + PCP free-page delta** over the fresh seq fill-before to post-drain
window. Convert summed zoneinfo pageset `count` pages to KiB with the recorded
guest page size before adding them to MemFree. Preserve the **raw MemFree
delta in a separate column**, plus MemAvailable and native pool gauges.
Use end minus start consistently. If PCP is unavailable, report the corrected
main metric as N/A with its reason, not as zero or a silent raw-MemFree fallback.
Sequential proc reads are not atomic and this is whole-system free memory,
not backend-exclusive object accounting. Do not infer cost from SUnreclaim.

Normal seqrf/randrf uses the frozen pre-traversal eligible population and
per-touch counted predicates. Single SPO and SRF retain independent PTE/cache
identity rules; plain-swap single SRF remains policy-excluded. Zero counted
populations remain N/A, not zero latency. Per-page classes and memory tokens
are legitimate memory observations, not prediction instrumentation.

Rewrite has its own fresh mapping. A random-content primer is PAGEOUTed,
drained, read back and verified under `rN_rewrite_prime_*`, then overwritten
with the slot's target content and measured under `rN_rewrite_*`. Primer
snapshots are not normal measurements. `rewriterf_ns`, `rewrite.*` and its
phase/pool metrics stay distinct from seq/rand/single and fresh-memory
statistics. The rewrite figure is separate from normal W1 figures.

## W2 and W3

W2 preserves verified successful-switch conditional p50/p99, availability,
worker survival and exit/switch completeness checks. Whole-workload PSI,
allocation stalls, disk IO and matched kswapd CPU come from raw snapshots.
CPU is descriptive only; failed switches are not pooled into latency samples.

W3 is tmpfs: backing may be in its xarray rather than a swap PTE. Therefore
**every group and every pair**, including memcomp-swap and zswap-crosscheck,
uses touch-time `present_before == 0`:

- `nonpresent_first_touch_count`;
- `nonpresent_first_touch_p50_ns` and `_p99_ns`;
- `nonpresent_first_touch_fraction = nonpresent / all verified first touches`.

All-first-touch metrics are descriptive only, not an alternative paired
latency population. `eligible_refault_*` is never emitted for W3. Pair rows
and paired summaries show candidate/reference population counts and fractions
beside latency, since sample sizes can differ. Pagemap/mincore classification
and frozen-drain diagnostics are still checked for internal consistency, but
do not define W3's latency selection. Plain swap's shmem swapcache hits cannot
be separated here; counts do not claim otherwise.

W3/W4 also retain compact prepared/read-begin MemFree/MemAvailable and native
pool gauges; W2 retains its whole-workload endpoint gauges. These are
descriptive states, not fresh W1 fill-to-drained memory savings.

W3 read latency must be interpreted with the same read-window source counters
below, not described as pure decompression.

## W4: stable whole-segment readback and common volume

Keep all three groups and their five assigned host rounds. Positive observed
native writeback evidence and pressure-ready evidence remain required.
The producer holds equal configured pressure through readback/verification.
Zram explicit-idle writeback remains distinguished from automatic reclaim.

Readiness retains its 30-second bound. From `pressure_ready`, all three
backends use the same **300-second** stability budget: require three
**complete unchanged one-second polling intervals** for the native writeback
counter **and both** `/proc/vmstat` counters `pgscan_kswapd` and
`pgscan_direct`. The initial sample is a baseline, not a completed interval;
any change restarts the unchanged-interval count. Memcomp uses
`packed_swap_events:writeback_payloads`, zswap uses `zswpwb`, and zram uses
the third `bd_stat` item (`bd_writes`). Stable guests drain and recheck within
the same budget, then emit exactly one `writeback_settled` snapshot;
deadline guests emit `writeback_unstable`.

Keep the existing **passive pressure**: after `pressure_ready`, the pressure
child only holds its mapping until drain, readback and full verification
finish. Do not add a read sweep, mlock/physical hold or another pressure mode.
The memcomp `writeback_gate_open` state is recorded **only descriptively**.
It is admission permission, not an in-progress flag: gate=1 at a poll,
terminal snapshot or `read_begin` is not a wait condition and never excludes
a smoke, telemetry sample or readback latency. Gate availability/state must
not become a gate-specific exclusion; unavailable observations remain N/A.

Emit one small `WRITEBACK_POLL` record per poll with elapsed time, gate state
(or N/A), native writeback counter, physical pool bytes, `pgscan_kswapd`,
`pgscan_direct` and swap-device `sectors_written`. Retain the records to
distinguish completed finite quota from scheduling or I/O stalls.

A W4 smoke requires stability within 300 seconds **and positive deltas in
both the native writeback counter and swap-device diskstats sectors over
`pressure_begin -> read_begin`**. An open gate does not fail it. Record each
backend's `pressure_ready`-to-stability interval descriptively, not as a rank.
At the cap, `writeback_wait_timed_out=1` retains otherwise valid write volume
and the complete verified raw readback distribution, but excludes readback
latency from comparison. Zero/reset/missing required writeback or diskstats
counters, early pressure-child death/OOM, failed verification, transport,
MGLRU or lifecycle failures remain invalid; gate state is not one of them.

`writeback_stability_wait_ns` (pressure-ready to the stable/timeout
endpoint) and CSV `writeback_wait_ns` are descriptive timing diagnostics,
not backend rankings. Only the stability timeout excludes otherwise valid
W4 latency; neither terminal nor `read_begin` gate=1 does so. Keep existing
structural/kernel/data/lifecycle failures separate. Runner
`writeback_latency_excluded`, `eligible_for_readback_comparison` and independent
telemetry decisions must implement the same gate-independent rule. Preserve
the expected five rounds, unavailable paired positions, `n_latency_excluded`,
`readback_valid_pairs` and `readback_invalid_pairs`; do not shrink the sample
or discard valid volume because readback timed out.

The latency metrics are `post_writeback_readback_count` and
`post_writeback_readback_p50_ns` / `_p99_ns`: **every verified touch of the
whole segment after stable writeback**, not just eligible/countable PTEs and
not an attributed written-back target subset. There are no strict-target
N/A rows and no counted-refault aliases. Per-page written-back membership is
not known; this limitation is stated, not disguised as zero samples.

Common write volume is `write_volume_bytes`:

```
(vda sectors_written at read_begin - at pressure_begin) * 512
```

Only swap-device `/proc/diskstats` sectors define this comparison; native
backend counters are never a substitute. Runner validation of vda swap/backing
identity and recorded sector/byte status is retained, and the raw-derived
bytes must agree with `swap_write_bytes`. `writeback_native_delta` separately
reports memcomp payload conversions, zswap zswpwb page events or zram
bd_writes in fixed 4096-byte units. Their different accounting points/units
are descriptive, not cross-backend write-volume ratios. No strict target-only
latency is synthesized from these global counters.

Also report the **readback write-sector delta for every W4 backend**:

```text
vda sectors_written at read_end - at read_begin
```

Retain its sector unit and optional bytes (= sectors * 512), separately from
`write_volume_bytes`. Readback can trigger new reclaim/writeback, so keep the
full verified first-touch latency distribution and read-source deltas beside
this diagnostic; do not drop slow touches or relabel it pure decompression.
A stability timeout still retains those raw samples but not a comparative
readback latency. An open gate imposes no such exclusion.

## Phase sources and accounting

Required W1 windows include fresh-mapping -> seq fill-before and separate
rewrite-primer mapping/fill/read-end -> measured rewrite-fill gates, plus each mode's read begin/end; seq/rand/rewrite
prepared-to-read; seq fill-before-to-drained; separate rewrite-fill-to-drained.
W3 with pressure requires prepared -> pressure_begin -> pressure_ready -> read.
W4 requires prepared -> pressure_begin -> pageout_returned -> pressure_ready
-> settled-or-unstable -> read_begin -> read_end. The common write-volume
window is pressure_begin -> read_begin, not prepared -> settled. Native
positive-writeback evidence uses prepared -> read_begin and includes the
zram explicit hook. Required missing/duplicate/out-of-order phases invalidate
rather than disappear. W1 warmup stays in per-round output but never in its
per-guest measured median.

| Source | Meaning and limits |
| --- | --- |
| vmstat | pswpin/out and zswpin/out/wb read-window deltas are page events, not unique pages or physical IO; allocstall_sum sums zones without adding the aggregate twice. |
| diskstats vda | Read/write bytes = sectors * 512; never use zram logical diskstats or vdb result-disk traffic. Includes guest vda activity, not exclusive durable host IO. |
| PSI | memory/cpu/io some/full cumulative total-us deltas only, not avg10/60/300. |
| meminfo | Original MemFree/MemAvailable in KiB; signed net changes, with the W1 fresh baseline above. Raw MemFree includes PCP migration effects, not backend-exclusive cost. No SUnreclaim attribution. |
| zoneinfo | Sum pagesets CPU count as PCP pages; convert with recorded guest page_bytes. MemFree+PCP delta is the main cross-backend memory metric; retain raw MemFree separately. Missing PCP makes the corrected metric N/A, never a raw-MemFree substitution. |
| memcomp | L/C/P gauges and L/C, L/P ratios; P is zsmalloc pool, not total backend metadata. swapped_read_io/error/cached are ABI load/decode/cache events; swapped_read_io may hit swapcache. Do not invent an unexposed resident-load counter. |
| zram | mm_stat logical/payload/pool bytes separately; bd_reads/writes are cumulative fixed-4-KiB units, not requests or guest-sized pages. |
| zswap | stored_pages/pool_total_size gauges; logical/pool includes allocator overhead, not payload compression ratio. |
| CPU | Same PID+starttime kswapd utime+stime matches only, converted using guest COMPARE_CLOCK_TICKS. Churn/missing census stays partial/unavailable; shared kworker writeback CPU is not attributable. CPU is never ranked. |

A zero ratio denominator is N/A. Gauge net changes are not traffic;
representation-specific pool sizes/ratios are descriptive. Missing unsupported
backend sources are not manufactured as zeros. Read-source counters are
shown beside workload latency but must not be summed as disjoint populations.

## Main physical-free metric and separate raw MemFree (v4.8)

The v4.7 PCP diagnostics now supply the **main cross-backend memory delta**
under v4.8. Preserve the underlying MemFree values/signs and retain raw
MemFree deltas in a separate column; native backend gauges remain separate.
This promotion changes reporting, not the supported parser or the rule that
missing/invalid PCP yields N/A rather than a fabricated correction:


- `zoneinfo.PCP_pages`: sum each captured node/zone/online-CPU pageset's
  `count` exactly once. `high` and `batch` are validated, not summed.
- `zoneinfo.PCP_kib`: `PCP_pages * guest page_bytes / 1024`.
- `memory.physical_free_MemFree_plus_PCP_kib`: explicitly **physical-free =
  MemFree+PCP**, in KiB. It uses the original `meminfo.MemFree_kib` plus the
  preceding PCP KiB value. Neither buddyinfo nor zone `pages free` is added.

The existing W1 `seq_fill_begin -> seq_read_begin` (`fill_to_drained_delta`)
window automatically emits all three signed deltas, plus `fresh_baseline`
and `drained_memory` endpoints. The separate `rewrite_delta` window retains
its own deltas. All signs remain **end minus start**, not start minus end or
an absolute cost. W1 warmup rows stay visible but are excluded from the
per-guest measured-round median, as for the old memory readings. W2/W3/W4
also retain the optional readings at their existing memory-state endpoints.

The supported producer format is Linux 6.12 `zoneinfo_show_print()` in
`mm/vmstat.c`: a unique node/zone header, nonnegative `pages free`, `present`
and `managed`, then `pagesets` with unique CPUs and one nonnegative integer
each for `count`, `high`, `batch`. Populated zones must have CPUs and their
`node_unreclaimable`/`start_pfn` tail. Unpopulated (`present=0`) zones
legitimately have no pagesets; their free/managed fields must also be zero.
The parser ignores unrelated zone/vmstat fields. Duplicate headers, CPUs or
consumed fields, negative/malformed consumed values, missing fields/tails,
and inconsistent CPU sets across populated zones yield **NA with an
explicit `zoneinfo PCP:` reason**, never a plausible partial sum. This is
validation of the captured, supported format, not independent proof of all
system topology: a consistently omitted whole zone/CPU cannot be detected
from zoneinfo alone. Global malformed/truncated RAW framing remains an
existing extraction error, independent of optional metric handling.

Missing historical zoneinfo, optional `COMPARE_RAW_NA`, or source-read error
makes PCP/physical-free unavailable with its source reason, **never zero**.
A complete explicit zero count is a valid zero. Missing/invalid guest
`page_bytes` leaves known PCP pages available but makes PCP KiB and
physical-free NA. Optional content errors do not invalidate otherwise valid
required MemFree/backend inputs; their reasons are retained in compact
per-round/per-guest rows. Missing one measured round prevents a complete
corrected guest median rather than silently selecting other rounds.
Do not fall back to raw MemFree for the main comparison when this occurs;
retain other valid workload metrics and the reason the main memory metric is N/A.

Only scalar Cells and a SHA256 digest of sorted `(node, zone, CPU IDs)`
coverage survive each streamed snapshot; raw zoneinfo text and temporary
inventories are discarded. Optional deltas require identical coverage
(and page size for KiB) at both endpoints. Hotplug/coverage differences or
out-of-order source clocks give NA rather than an inferred delta.

Meminfo and zoneinfo are read sequentially, **not atomically**. The combined
Cell's sample interval is the envelope of the two reads; its source names
both files. Separate raw-MemFree and PCP rows retain their respective
sample intervals in provenance. PCP migration between global free lists
and per-CPU caches can dominate raw MemFree changes. Physical-free helps
expose that confounder but remains a whole-system descriptive approximation,
not backend-exclusive allocation cost. No allocator draining, sysctl change,
SUnreclaim attribution or performance PASS threshold is introduced here.
The sole primary-metric change is the v4.8 MemFree+PCP delta specified above.

Host-only tests (no guests, build, or file fixtures):

```sh
python3 -B bench/compare/tests/test_v47_memory.py -v
```

## Pairing and noise decisions

Each per-guest metric requires **all configured measured inner rounds**;
missing one makes that median unavailable, not survivor-only. Cross-boot
quantiles use linear interpolation at `(n-1)*p`, with Decimal arithmetic.
Summary gives median, Q1/Q3, IQR and spread `(max-min)/median`; nonpositive
median makes spread N/A. Partial distributions are descriptive only.

Pair the same workload and host round: memcomp zstd vs zram/zswap zstd,
memcomp lz4 vs zram lz4; W4 memcomp-swap-zstd vs zram-writeback-zstd/zswap-zstd;
zswap-crosscheck vs zswap-zstd for environmental control. All assigned 10/5
pair positions are retained. Ratio requires nonnegative candidate and positive
reference. A directional claim requires complete 10/10 pairs with at least
8/10 consistent directions, or complete direction-consistent 5/5 pairs, and
`abs(median_ratio-1) > reference_spread` from the full reference population.
Missing/invalid pairs never reduce that denominator. There is no invented
fixed 2% significance floor.

All applicable W1 content, W2 and W3 crosschecks must be complete within noise
before **any** matrix directional claim. Drift blocks the matrix; incomplete
controls leave it unknown. W4 has no invented fourth backend/crosscheck.
Within-noise does not establish equivalence. Kernel CPU and stable-wait time
are always descriptive. `plot_compare.py` additionally requires all 565
terminal lifecycle-safe slots, exact matrix/summary status identity and the
same matrix-wide environmental gate before plotting. Figures are descriptive
median/IQR distributions, not standalone speedup evidence.

## Verification boundary

This v4.8 documentation-only edit claims source/document checks only: no
build, test, VM, extraction, runtime compatibility, memory sanity-check or
figure-rendering PASS. The parent's implementation checks must cover the
first-pass-smaller-than-post-drain denominator, fraction display, uniform
writeback-plus-pgscan polling, gate-open non-exclusion, both positive W4
pressure-to-read deltas and all-backend readback write sectors. The three W4
smokes, two W1 smokes and ACK-with-swap 8/0/0 are prerequisites before freezing
source/tool/image hashes and starting a new 565-slot matrix (matrix.md).
Historical decisions and evidence remain unchanged, not promoted to v4.8.

The W4 `pressure_begin` snapshot is taken after the target PAGEOUT syscall
returns but **before** the `pageout_returned` ACK/hook and pressure allocation.
This common window includes zram's explicitly requested idle writeback; its
native/physical write increments remain required, not assumed. The explicit
hook is not autonomous reclaim and is still reported separately.


## v4.11 W1 memory-only collector protocol

See memory_account.md for the complete flag/file/marker/driver contract.
`telemetry.MEMORY_PROTOCOL` is `v4.11-w1-memory-consistent-v3`.
Metadata uses the same full version string:
`snapshot_counter_protocol=v4.11-w1-memory-consistent-v3`. Historical v1/v2
records remain replayable with their original semantics. This opt-in mode is
W1-only, mutually exclusive with the legacy refresh mode, and applies only to
newly collected runs.

The parser isolates every framed attempt, validates refresh ordering, both
adjacent buddy/PCP pairs and the intervening classification clocks, recomputes
the inclusive 16-page double condition, and verifies each terminal marker.
It promotes only accepted classifications, retaining compact evidence for
**every** rejected attempt. Ten legitimate inconsistencies yield INVALID;
I/O, parsing, framing and evidence failures yield ERROR, never INVALID.

The new physical-free scalar is
`memory.physical_free_buddy_plus_PCP_kib`, computed from the accepted after
pair. It does not reuse or overwrite
`memory.physical_free_MemFree_plus_PCP_kib`. Legacy captures and their required
metrics continue using the old protocol. These are sequential non-atomic
observations, not backend-exclusive costs or root-cause evidence.

The Python boot object exposes `memory_snapshot_protocol`,
`memory_snapshot_validity=VALID|INVALID|ERROR`, `memory_snapshot_status`, and
`memory_snapshots` (with all per-phase attempts). The `memory.snapshot_protocol`
metric's JSON provenance preserves the protocol, validity and all point/attempt
evidence in ordinary telemetry exports. A boot with an INVALID point has its
memory/pool/cache aggregate values suppressed, including otherwise accepted
points; timing and non-memory counters remain independently usable. INVALID
alone does not become a raw extraction failure. The cohort controller must
check the expected unique phase inventory (22 for the current one-round W1
cohort) and terminal statuses, not just a residual threshold. No historical
status, RAW file, or result label is rewritten.
