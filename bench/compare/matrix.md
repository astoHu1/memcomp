<!-- F1 v4.98 preparation; kernel selection and execution freeze are separate. -->
## F1 v4.98 real-corpus update

The current plan is v4.98, SHA256
`c35d21e610ecf4cc92a41ce39c32c986645a4bd0de2f560ece37c961600cf354`.
The dated build/prerequisite evidence below remains historical. F3 selects the
current artifact cache before the shared freeze; this preparation starts no job.

Approved source order is system_server, systemui, qq, wechat, chrome,
netease_music, discord, telegram. All commands use `/dev/vdc` and the existing
`uniform-v411` selector. W2 maps workers to these eight sources; W3 and W4 use
combined source order. Ten A.1 groups stay unchanged; entry80 is excluded.

For each W1 timing and host round, compute all eight application candidate /
reference ratios, then their geometric mean. Apply existing E.4 to the ten
round means; retain every per-application result. No partial application set
is used. The reference spread is computed from ten per-round geometric means
of the eight reference timings, using the existing (max-min)/median rule.
Both pageout denominators remain separately reported. A missing/undefined
metric stays unavailable rather than becoming zero. Derived combined rows
add no slots and no E.5 control requirement. E.5 has 84 primary timings
(8*10 + W2*2 + W3*2), counted in `comparisons.json`.

W3 refault_p50_ns/refault_p99_ns use C.3 frozen pagemap/swapcache eligibility,
including eligible read-ahead hits. eligible_count/refault_fraction and the
existing class/readahead counters accompany the values.
nonpresent_first_touch remains descriptive. The earlier tmpfs-specific W3
interpretation below is superseded for w3-anon-512m.

`matrix.json` retains slot pairing. A sibling `comparisons.json` inventories
72 scheduled comparison populations plus 7 derived W1 combined populations.
Summary output writes its metric comparisons to its own comparisons.json.
The final metric count is read from that file, never inferred as 44.

<!-- v4.6 producer/runner update: no builds, tests, VM runs or result processing performed. -->
# Comparison matrix, v4.8 contract (serialized method v4.6)

## Current method: v4.8 documentation contract

The v4.8 release-plan revision supersedes the current W1 denominator, W4
stability and primary memory-accounting rules below. Historical evidence
sections retain their original methods and decisions; they are not v4.8
validation. The serialized top-level `method=v4.6` alone does not establish
v4.8 compatibility: freeze the updated W1/W4 protocols and source hashes.
This documentation update does not claim implementation, smoke, ACK selftest
or formal-matrix completion. See matrix.md for the prerequisites to freezing
new inputs and starting the 965 assigned slots.


This is the v4.6 replacement for the rejected v4.5 population, the
method-invalid v4.4 population and configuration-blocked v4.3/r2 populations.
All pre-v4.6 methods remain excluded;
none may be resumed, relabeled, copied into or substituted for new slots.
It integrates the old matrix's workloads into `matrix.py` and the existing
`run.py` / `mkinitramfs.py` / e2e QEMU helper, not a separate framework.
The authoritative release plan's method A–E takes precedence. Older
`runner.md` examples describing five repeats, CPU 0, or stopping on the first
workload failure describe v4.3 and must not be used for this matrix.

**Do not execute the formal matrix until its owner explicitly approves.**
Planning is the default; only `--execute --allow-pressure` starts guests.
Neither driver builds a kernel or userspace program. Prebuild the guest tools
and `gen_init_cpio` separately: a missing packer is an error, not permission to
invoke the packer's fallback compiler. This implementation has no VM or build
execution implied by its existence or by offline validator tests.

## v4.8 acceptance order and frozen image provenance

The parent must finish the W1/W4 implementation and host checks first; this
documentation-only edit executes none of them. Before a new formal freeze,
run serially, with at most one VM at a time:

1. Three W4 smokes: `memcomp-swap-zstd`, `zswap-zstd`,
   `zram-writeback-zstd`. Each must stabilize within 300 seconds and show
   positive native-writeback **and** diskstats-sector deltas over
   `pressure_begin -> read_begin`; record each stability time. No gate test.
2. Two W1 mixed smokes: `memcomp-swap-zstd` and `zswap-zstd`. Verify the
   initial first-pass denominator before drain/retry (expected about
   13860/16384 versus 16384/16384, not new numeric acceptance cutoffs), and
   show its fraction beside `pageout_ns`.
3. ACK arm64 full selftest with an active swap disk and explicit swapoff
   authorization: **8/0/0** (pass/fail/skip). Image and selftest must come
   from source matching `fcfb980544378`; the earlier no-swap **3/0/5** does
   not satisfy this prerequisite.

Only after all prerequisites pass, freeze inputs and start **all 965 slots
from scratch in a new result directory**. Retain failures without replacement;
do not promote old evidence. The fixed release sources are flat candidate
`672629a97ee38`, equivalent series `b757ab1aa8445`, and ACK `fcfb980544378`.
Memcomp and zswap-crosscheck images must come only from
`v47-20261017/compare-kernels/{memcomp,zswap-crosscheck}` under the work root;
the other backends reuse their directed-A/B images. Check every image/config
SHA256 against its build record and source identity before freezing the exact
hashes. **Never take formal images from mutable `build/e2e-current`**: it holds
the dirty-check mutant, not the release image. Protocol/source/tool/kernel
hashes remain fixed, with existing before/after-slot drift checks. This is a
freeze requirement, not a claim that these checks or smokes have passed.

## Inputs and reused images (A)

Supply explicit CLI paths; the driver does not embed a developer's home,
checkout, artifact cache or result directory. `--kernels` contains exactly the
six required **existing** build.py artifact directories (other unrelated cache
entries are ignored):

| Directory | Runtime groups (`group_id`) |
| --- | --- |
| `swap` | `swap-none` |
| `zram` | `zram-zstd`, `zram-lz4` |
| `zram-writeback` | `zram-writeback-zstd` |
| `zswap` | `zswap-zstd` |
| `memcomp` | `memcomp-noswap-zstd`, `memcomp-noswap-lz4`, `memcomp-swap-zstd`, `memcomp-swap-lz4` |
| `zswap-crosscheck` | `zswap-crosscheck-zstd` |

Each has `bzImage`, `.config`, `source.txt`, and `SHA256SUMS`; both artifact
hashes and the declared backend are checked. The two memcomp compressors use
the same image and different boot command lines. No kernel source checkout,
patch application, rebuild, replacement kernel or TCG fallback is performed.
`--binaries` must contain existing static `guest`, `refault`, `shmem`,
`app_switch`, and `gen_init_cpio`. Their hashes, the driver/source hashes,
helper hash, kernel/config and initramfs hashes are recorded.

### Per-slot frozen-input checks

The starting manifest freezes measurement `.c`/`.h`/`.py` sources, the QEMU
helper, all five static tools and kernel artifacts. Once formal execution
starts, do not edit measurement sources/helper or replace binaries/artifacts.
Before every slot, `matrix.py` rechecks all source/helper hashes, all five
binaries and that slot's kernel/config/source identity against the freeze.
After the slot, live sources/helper, binaries and kernel bytes are rechecked,
and runner metadata is compared with frozen source/helper,
binary and kernel identities plus `reclaim` and applicable `w4_protocol`.
A mismatch persists `valid=false`, `eligible_for_comparison=false` and the
drift reason in that slot's status, saves the matrix and stops with
`state=input-drift-stop`; pre-slot drift stops before launching that slot.
Lifecycle safety stops still take precedence. The schedule, slot counts and
no-retry/no-replacement rules do not change.

The real `results/2026-10-16-v45-smoke/w1-final-freeze-guard-attempt02/` smoke completed
successfully with both new pre-slot and post-slot checks. It remains nonformal
and fills no assigned comparison slot.

The existing e2e helper still owns sparse-disk creation, the actual QEMU PID,
supervision, death proof and exact-inode cleanup. Its own scratch-root
restriction remains in force; choose `--work` accordingly. These drivers do
not alter or bypass that restriction. Every run invokes the helper exactly
once with a new disk, including groups which do not use disk swap. The guest
backend setup, not disk presence, determines which devices are activated.
Do not create a disk manually or delete a retained helper disk.

## Frozen v4.6 result-disk transport

`matrix.json` and plan JSON freeze `result_disk` as:

```json
{"transport":"MEMCOMP_DATA_V1","header_bytes":512,"data_mib":128,
 "serial":"memcomp-data","device":"/dev/vdb","swap":false}
```

Every assigned runner command explicitly receives `--data-mib 128`. The runner
allows 16..256 MiB (default 128); the formal matrix deliberately freezes 128 for
all groups. The existing helper's `--data-mb`/`--data-output` remain optional and
default off outside this path. No matrix resume/overwrite/replacement or extra
slot is introduced: the population stays exactly 965 assigned boots.

The independent sparse raw vdb is never formatted or activated as swap; serial
is `memcomp-data`. The first 512 bytes contain the ASCII line
`MEMCOMP_DATA_V1 bytes=N sha256=HEX status=0|1\n` followed by zero padding
(no leading space in the actual magic). Payload begins at byte 512. Only a
successful status and matching declaration may enter a valid result.

The order is fixed: actual QEMU death and process-group drain -> supervisor
sparse export -> exact-owned-inode cleanup -> ACK -> runner bytes/hash/zero-tail
and UART-declaration agreement -> gzip. Export is not death proof; a missing ACK
or unknown cleanup remains a lifecycle safety stop. No Python summary tries to
repair, substitute UART CSV, delete a retained disk or bypass these gates.
Data-disk vdb IO is transport overhead, not vda backing-store writeback traffic.

## Workloads and counts (C, D, E)

All formal groups use 1024 MiB guest RAM, 2048 MiB backing/logical zram size,
seed 1, the same workload arguments and guest CPU 1.

| Workload ID | Groups | Dataset / guest workload | Boots per group | Total |
| --- | ---: | --- | ---: | ---: |
| Eight `w1-corpus-<source>` IDs | 10 each | 16384 real pages + disjoint 16384-page rewrite prime | 10 | 800 |
| `w2` | 10 | 8 workers × 64 MiB; memory.max 256 MiB; 32 switches | 10 | 100 |
| `w3-anon-512m` | 10 | 512 MiB private anonymous + 512 MiB disjoint real-corpus pressure | 5 | 50 |
| `w4-832m` | 3 | 212992 pages = 832 MiB | 5 | 15 |
| **Total** | | | | **965** |

Every W1 command includes required `--rewrite`. Each existing guest measures
seq/rand/single/rewrite in each inner round (one warmup, five measured); rewrite
is not an extra workload ID, group, guest or replacement slot. All ten groups
and all eight application slots run it. `plan()` explicitly checks the per-label
800/100/50/15 counts and the total **965**. `w1_protocol` freezes fresh mapping,
seq-only fill-before memory baseline and random-prime-to-target rewrite.

W4 includes only `zram-writeback-zstd`, `zswap-zstd`, `memcomp-swap-zstd`.
The guest sets `COMPARE_WORKLOAD_LABEL=w4`; `/refault` then uses the existing
single-pass CSV path. There is no new W4 flag and no W1 round flags in W4.
The v4.8 W4 protocol below is uniform across these three groups. This
documentation edit claims no execution; the v4.5 evidence later in this
document is historical, not validation of this method.

All three W4 backends use the same owned `/compare-w4` memory subgroup;
the target process and pressure child share it. `memory.max`, `memory.high`
and `memory.swap.max` remain at default unlimited `max`, without limit or
reclaim-policy tuning. Sizes use configured RAM from `/guest-memory-bytes`,
not `MemTotal`, backend pool size or `MemAvailable`.

Hold page-aligned 7/8 of configured RAM: **896 MiB for a 1 GiB guest**. Every
held page is half seeded random bytes and half zero, not a mixture of whole
random and half-zero pages. Also touch a page-aligned 1/4-RAM all-random ramp
(**256 MiB**) during filling, then release only that ramp before
`pressure_ready`. Fixed **1 ms/MiB untimed pacing** applies during filling,
including the ramp. Keep the main held-pressure mapping alive through drain,
readback and full-page verification, then release/reap it. W3 stays all-random
and has no W4 ramp. CSV `pressure_bytes` includes held bytes only, not the ramp.

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

Zram's explicit idle writeback is disclosed, not equated with automatic reclaim;
backend counter units are not directly comparable.

W4 latency is the p50/p99 of **all verified first touches of the complete
segment after stable writeback**, reported as `post_writeback_readback_*`.
Retain the full per-touch latency distribution; do not narrow it to counted
refaults or inferred written-back target pages. Per-page writeback attribution
is unavailable. Do not emit the old strict-target N/A metrics or counted-refault
aliases. Pair the distribution with `pswpin`, `zswpin` and memcomp load-source
deltas, and report swap-device writes during **`read_begin -> read_end` for
all three backends**. Report that sector delta separately (bytes = sectors *
512) from the `pressure_begin -> read_begin` write volume. Readback can itself
create reclaim pressure and fresh writes; that is real backend behavior, not
a reason to discard otherwise valid full-segment latency distributions.

Common write volume is the physical swap/backing **vda** `sectors_written`
difference in `/proc/diskstats` from the exact `pressure_begin` snapshot to
`read_begin`, multiplied by **512**, regardless of guest logical sector size.
Never use data-transport vdb traffic or compare the native backend units above.
The window intentionally excludes zram's earlier explicit idle writeback.
`run.py` validates vda identity (including zram `backing_dev`) and records
`swap_write_device`, `swap_write_window`, `swap_write_sectors_before`,
`swap_write_sectors_after`, `swap_write_bytes`, `w4_diskstats_valid/error`,
`writeback_pressure_to_stable_ns` and `writeback_gate_open_at_read_begin` in
status. Gate state is descriptive only and never excludes readback latency.

`eligible_for_comparison` still admits a structurally valid cap-expired guest
for write volume. `writeback_latency_excluded` and
`eligible_for_readback_comparison` carry the separate latency decision.
Matrix coverage separately reports `readback_valid_pairs`,
`readback_invalid_pairs` and their round lists, plus
`w4_unstable_readback_boots`; never discard all volume pairs because one
readback is unstable.

New W4 runner metadata and matrix/plan JSON freeze `w4_protocol` from
`run.py:W4_PROTOCOL`: shared default-unlimited scope, every-page half-zero
held pattern, 7/8 held RAM, 1/4 random ramp, release-before-ready,
1000 us/MiB pacing, held lifetime through full verification and
`phase_level_not_per_target_page` attribution. Four exact unique markers for
the owned group, workload membership, pressure pattern and ramp release are
mandatory (runner.md). Missing/duplicate evidence fails `w4_scope_valid`;
`w4_scope_error` records why. The CSV schema and other validity gates are unchanged.

### Historical v4.5 ramp smoke decisions (unchanged)

All three final `w4-{zram-writeback,memcomp-swap,zswap}-zstd-mglru-ramp`
smokes passed 212992 complete rows each, matching bytes/SHA256, actual QEMU
death/group drain and owned-disk cleanup, and MGLRU boot mask `7`. Captured
`memory.events` has `oom=0` and `oom_kill=0` throughout. Memcomp records the
allowed positive-delta 60 s deadline (timeout=1, stable polls=0), **not stable
writeback**; zram and zswap have stable polls=3. Zswap's one phase-start cached
page is automatically disclosed in its result README and excluded, leaving
212991 eligible/counted touches. Strict written-back-target latency stays N/A.

The newly added scope gate passes all three final logs on offline replay.
Their original saved status/metadata predate those fields and are not rewritten
or claimed to include them. New runner executions record/enforce the gate and
protocol directly. All three remain `kind=smoke`,
`eligible_for_comparison=false`, never formal comparison samples.

These are historical v4.5 smokes, not v4.6 validation; they fill none
of the 15 formal W4 slots or 965 total slots. Earlier
root failures, subgroup zswap +247 success/zram OOM and all older one-in-eight/
one-in-four/one-in-two modes are retained and never reused (README.md).

W2 worker loss is an availability outcome, not an invitation to replace workers
or discard the run. Only completed successful switches enter latency statistics.

### Deterministic ordering and pairing (A, E)

Rounds are 1-based. In each round, visit the eight W1 applications, W2, and (in
rounds 1–5) W3 and W4. For each workload, run every eligible variant once.
The fixed cyclic base variant order is:

1. zram-writeback-zstd
2. zswap-zstd
3. swap-none
4. memcomp-swap-zstd
5. zram-zstd
6. memcomp-noswap-zstd
7. zram-lz4
8. memcomp-swap-lz4
9. zswap-crosscheck-zstd
10. memcomp-noswap-lz4

For workload base offset b, round r rotates that list left by
`(b + r - 1) % 10`. On even rounds it reverses direction **while keeping the
rotated first group first**. Reversing the entire list would visit only one
parity of slots in ten rounds. The eight W1 offsets are 0, 2, 4, 6, 8, 0, 2 and 4; W2 is
8, W3 is 0 and W4 is 1. W4 filters the resulting order to its three groups.
Each full ten-boot W1/W2 population visits every position exactly once.

The offsets avoid consecutive identical groups, including workload and
round boundaries. Different variants may share a backend family; family
separation is not required by the plan and is not claimed. `matrix.py`
checks the identical-group invariant rather than adding guests or changing
workload parameters.

Every destination has exactly one `status.json` entry with `repeat=1`.
`run.py --repeats` is retained for compatibility but accepts only 1.
Destinations are `round-NN/<workload_id>/<group_id>/`. Both per-boot metadata
and `matrix.json` use `method=v4.6`, `kind=interleaved-comparison`,
`host_boot_round`, `workload_id`, and `group_id` for pairing.

A failed or timed-out guest is retained in its assigned slot; subsequent
slots continue after helper lifecycle completion. There are no replacement
runs, survivor selection, resume, overwrite, or reuse of any old-method results. Result
roots must not exist. `matrix.json` reports valid, invalid and still-pending
same-round pairs, including the zswap environment crosscheck. If actual QEMU
death or owned-disk cleanup is **unknown**, stop for safety instead of
launching another heavy task; never infer death from the sudo/relay PID.
Ctrl-C also stops the matrix, not just the current workload.

## Host safety and boot gate (B)

* All QEMU/vCPU threads inherit `taskset --cpu-list 4-7`; SMP is 2 and the
  workload uses guest CPU 1. Other vCPU work remains available to reclaim and
  workqueues. These affinity values are enforced, not just defaults.
* A per-user lock shared by runs with the same workspace parent prevents
  concurrent invocations. Keep every attempt under that one parent.
  Before **each** guest starts, wait for `/proc/loadavg`'s 1-minute value to
  be at most 1.0 and for recognized VM/build processes to disappear. Do not
  run other compiles, randconfig or VMs for the duration of the matrix.
  Process-name detection is an additional guard, not a resource-isolation
  guarantee; the owner must also exclude unrecognized heavy jobs.
* Record initial/final load, sample count, detected processes, threshold and
  elapsed wait in `metadata.json` under `host_load_gate`. Update during the
  wait so Ctrl-C leaves evidence. Load wait has no deadline and is
  interruptible; guest execution is independently bounded.
* Preserve at least guest RAM **plus 2 GiB** host MemAvailable, and **32 GiB**
  free on the explicitly supplied mounted host volume (`--host-volume`).
  On WSL this must be the host volume housing the VM/scratch storage, not a
  substitute filesystem selected to evade the existing floor. There is no
  CLI to lower either threshold. Check them during load waits and before
  launching. The volume path is explicit rather than hardcoded.
* Per-guest timeout defaults to 1800 seconds and must be 5–3600 seconds.
  The existing helper supervises the actual guest timeout and owned-disk lifecycle.
  An outer watchdog only signals its relay to request cancellation; it never
  kills an arbitrary supervisor PID or unlinks a disk.
* Disk `cache=unsafe` is unchanged: disk refault measures host page-cache
  backed virtual disk IO, not durable device IO. WSL nested KVM comparisons
  are relative measurements, not physical device latency or energy.

## CSV and kernel/data validity

`run.py` keeps verified payload/snapshot framing plus short original UART bytes
in `run1.log.gz`, and extracts workload CSV to `run1.csv.gz`. CSV and full snapshots
do **not** traverse UART. The existing telemetry parser still consumes the full
COMPARE_SNAPSHOT / COMPARE_RAW_BEGIN/END interface in that reconstructed log.
If supervisor completion is unknown, retain original live logs/output; do not
compress/unlink a file still receiving supervisor output. Failed preparation
retains metadata/status and available diagnostics, never a fabricated payload.
Exit zero alone never means complete PASS: require helper lifecycle proof,
transport bytes/hash/zero-tail/UART agreement, exactly one successful COMPARE_DONE,
complete valid CSV and no kernel/data failures.
The failure regex includes Kernel panic, BUG, KASAN, DATA_MISMATCH,
`rcu.*detected stalls`, `rcu.*self-detected stall`, soft lockup, WARNING and
UBSAN. In particular real r2 W3 RCU stalls must not be filtered away or
reclassified as harmless serial output. No diagnostic-disabling boot flags
are added.

W1 retains exactly one header with the existing snapshot/drain and
v4.7 rewrite-prime fields plus the three v4.8 first-pass/drain-delta fields.
Validate the frozen producer/reader schema, not the old 46-column header.
W3/W4 have 45 columns (19 old plus 26 appended). Each touch must verify
successfully and have the
expected CPU, round, warmup, mode, order, unique page index and contiguous
sequence **starting at 0 within each round/mode**. Each seq/rand/rewrite phase has
`pages` touches followed by one batch record; single has
`min(--single, pages)` touches and no batch record. Batch `verify_ok` and
`sequence` are empty by contract, **not verification failures**. Validate
batch counts, timings, drain flag and memory fields. Drain timeout is recorded
and prevents promotion to a complete comparative result.

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

For W1, expected data rows (excluding the sole header) are:

```
(rounds + warmup) * (3 * pages + min(single, pages) + 3)
```

Defaults are rounds=5, warmup=1, single=4096: **319506** rows per formal W1
boot. Round 0 is warmup for the defaults; with warmup=0, round 0 is measured.
Each W1 inner round recreates the mapping, emits `rN_seq_fresh_mapping`,
and captures its seq MemFree baseline before fill; only seq has comparable memory deltas. Rand/rewrite
memory-before fields must be `-1`. Rewrite has its own fresh random primer
(`rN_rewrite_prime_*`, no primer CSV), verified readback, target-content rewrite,
and measured second PAGEOUT (`rN_rewrite_*`). The primer seed XOR is
`0x7265777269746501`; the regular target generator/rotation is unchanged.

W3/W4 remain one row per page, with `swap_type_before` and PTE-first
classification. Anonymous W3 reports `refault_p50_ns/refault_p99_ns` from
C.3 frozen snapshot eligibility, with `eligible_count/refault_fraction`;
`nonpresent_first_touch_*` is descriptive only. W4 reports full-segment
post-writeback readback p50/p99 only when stable. Raw diagnostic columns are
retained in both, not redefined as new metrics. Type 30 memcomp observations must classify as `memcomp`, not
swap-cache hits. Only memcomp backends enable the type-30 classification.
Real swap classifications require the controlled active type-0 whitelist and
mincore=0 (nonresident) or mincore=1 (cached); memcomp-noswap has an empty real
swap whitelist. Other special swapped types must remain unknown, never
cached or eligible refaults. Their presence is recorded and makes the boot
ineligible, without dropping its raw rows. Touch page_kind must be one of the
actual six kinds, not the old rotated placeholder. An older v4.4 type probe is
historical evidence only; the main thread must confirm the current image/type
before the new formal execution. The driver does not fabricate or rerun a probe.
Present pages and pages already cached at the phase-start snapshot are not
refault latencies. Later readahead cache hits of snapshot-eligible pages remain
counted; emit readahead_hit_ratio and phase_start_cached_count separately. Keep unknown observations explicit. W2 validates the complete
init/switch/pid_exit record sequence, not just the number of switch lines.

## CLI usage: plan first, execute only after approval

From the repository root, substitute explicit existing paths:

```sh
python3 -B bench/compare/matrix.py \
  --tree <source-tree> --binaries <prebuilt-static-tools> \
  --corpus-image <approved-eight-application-corpus> \
  --kernels <six-selected-current-kernel-artifacts> --work <helper-accepted-scratch> \
  --dest <new-v45-results> --host-volume <mounted-host-volume> \
  --timeout 1800
```

This only prints the reproducible schedule; it does not make directories,
start guests, package initramfs files, build, or use old results. After the
owner's approval, use the same arguments plus `--execute --allow-pressure`.

For a single **nonformal** boot after separate execution authorization:

```sh
python3 -B bench/compare/matrix.py \
  --tree <source-tree> --binaries <prebuilt-static-tools> \
  --corpus-image <approved-eight-application-corpus> \
  --kernels <six-selected-current-kernel-artifacts> --work <helper-accepted-scratch> \
  --dest <new-smoke-results> --host-volume <mounted-host-volume> \
  --smoke --smoke-group memcomp-swap-zstd --timeout 1800 \
  --execute --allow-pressure
```

Smoke uses one W1 mixed boot, 64 pages, rounds=1, warmup=0 and single=32
(**162** data rows). It is permanently tagged `kind=smoke`, has no formal
host_boot_round, and `eligible_for_comparison=false` even when valid.
Do not relabel, copy into a formal destination or upgrade smoke data.

## Reporting contract (E; analyzer is separate)

This scheduler records raw evidence and pair coverage, not speedup claims.
The analyzer under the v4.8 contract must first take each guest's median over
measured rounds
(exclude warmup), then report group median, IQR and boot-to-boot spread
`(max - min) / median`. Compare only same-round complete paired boots:
memcomp zstd against zram-zstd/zswap-zstd, memcomp lz4 against zram-lz4,
and W4 memcomp-swap-zstd against zram-writeback-zstd/zswap-zstd. Report the
median of ratios and k/n direction agreement, explicitly counting invalid
pairs. At least 8/10 directions (or 5/5 for five-boot workloads) and a ratio
outside the reference's own boot spread are needed for a speedup/slowdown
claim; otherwise report within noise. Fewer complete pairs must not be
silently treated as the original full sample. zswap-crosscheck/zswap must
remain within noise before the matrix can be used. Kernel CPU time is
descriptive, not a backend ranking. No v4.5 or earlier result participates.
Readers stream raw gzip snapshots into compact guest/round output, never a
raw telemetry long table. Missing coverage and unstable latency pairs are
counted explicitly; native counter units are descriptive, not ratioed.

All comparison attempt work directories must share one durable workspace
parent. The per-user serialization lock lives in that parent, not /tmp;
heavy-task/load checks still run before every guest.

## Rotation interpretation and position audit

Reverse the traversal direction on even rounds **while preserving the rotated
first group**, so round r begins at the rth group of its fixed workload base.
A literal whole-list reversal would visit only one parity of positions in a
ten-group/ten-round matrix. Fixed cyclic workload-base offsets (recorded in
matrix.json) avoid identical adjacent groups across workload/round boundaries
without adding guests or changing any workload parameters. W1/W2 each group
visits all ten within-workload positions exactly once; the actual 965-slot
plan is checked for this and for no consecutive identical group. Different
variants of the same backend are not forbidden: they are distinct groups.

## Historical uniform MGLRU gate (v4.5; not v4.6 validation)

All six images, stock and candidate alike, require `CONFIG_LRU_GEN=y` and
`CONFIG_LRU_GEN_ENABLED=y`. Before a run, the runner checks the `bzImage` and
`.config` SHA256 against the artifact manifest, then checks both effective
config settings. Runner/matrix metadata and plan JSON freeze `reclaim`
(mode, required configs, boot path, required mask `1`).
Using only the verified payload's rebuilt log and existing snapshot reader,
the runner requires one complete boot raw `/sys/kernel/mm/lru_gen/enabled`
value with core bit `0x1` on; optional bits `0x2`/`0x4` are not required.
Missing/bad/core-off evidence fails the round. `status.json` records
`mglru_boot_valid`, `mglru_boot_mask` (integer/null), `mglru_boot_error`;
metadata repeats them as `reclaim_validation`.

Earlier MGLRU-off W3 smoke is historical transport evidence only, excluded
from this uniform-reclaim population; it does not validate final-MGLRU W3.

The final-MGLRU `memcomp-noswap-zstd` W3 smoke is now complete at
`results/2026-10-16-v45-smoke/w3-memcomp-noswap-zstd-mglru/`:
131072 data rows for 512 MiB tmpfs plus 512 MiB bounded anonymous pressure,
19977844 verified raw payload bytes, and 9.42 s runner elapsed time. Its saved
status records boot enabled mask `7` (core bit on), CSV and payload SHA256
integrity, safe lifecycle completion, and no reported RCU/kernel-data failure.
It remains `kind=smoke`, `eligible_for_comparison=false`: this validates the
exercised W3 integration, not the other groups or the formal comparison matrix.

Final W4 ramp smoke evidence and historical exclusions are in README.md.
The shared integration protocol passed; the formal five-boot paired population
is still pending. Memcomp's positive-delta deadline is not stable writeback;
zswap's one phase-start cached page is disclosed and not eligible. Strict
written-back-target latency remains N/A despite successful smoke gates.

### Retained v4.7 W1 present-page drain behavior

The first PAGEOUT can leave a memcomp compression-rejected page present:
the post-unmap failure returns it to the LRU with its incompressible mark.
W1 drain retries unchanged present pages as well as remapped real-swap cache
entries, using the existing four-round bound. It never reads a present page
while draining. Retry PAGEOUT time remains in `purge_ns`, outside the initial
`pageout_total_ns`; the final pagemap snapshot and rewrite-prime counts remain
observations, not a fabricated PASS. W3/W4 retain their original drain policy.
The protocol and measurement-source hashes distinguish this correction from
the failed first v4.7 smoke; old captures are not rewritten.

The W4 `pressure_begin` snapshot is taken after the target PAGEOUT syscall
returns but **before** the `pageout_returned` ACK/hook and pressure allocation.
This common window includes zram's explicitly requested idle writeback; its
native/physical write increments remain required, not assumed. The explicit
hook is not autonomous reclaim and is still reported separately.
