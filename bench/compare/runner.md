# Neutral comparison build and guest runner

## Current method: v4.8 documentation contract

The v4.8 release-plan revision supersedes the current W1 denominator, W4
stability and primary memory-accounting rules below. Historical evidence
sections retain their original methods and decisions; they are not v4.8
validation. The serialized top-level `method=v4.6` alone does not establish
v4.8 compatibility: freeze the updated W1/W4 protocols and source hashes.
This documentation update does not claim implementation, smoke, ACK selftest
or formal-matrix completion. See matrix.md for the prerequisites to freezing
new inputs and starting the 565 assigned slots.


These tools are Stage 4 implementation, not accepted measurements yet. They do
not use the memcomp-only correctness build.sh as a generic backend builder.
Heavy builds and VMs must run serially. Run the host tools as the checkout owner;
the existing QEMU lifecycle helper alone obtains the permission needed for KVM.

## Build layout and safety

Use one reused, independent O= cache for all performance configurations. Keep
per-backend bzImage, effective .config, source identity, input fragments, exact
commands, full logs and exits in separate small artifact directories. Stock
backends require exactly v6.12.23; memcomp and the MEMCOMP=n+zswap cross-check use
the completed series. The builder does not switch refs or alter source files.
An owner must select the clean intended checkout before invoking it.

```sh
python3 bench/compare/build.py --tree <source> --out <reused-cache> \
  --dest <new-artifact-dir> --backend zswap --llvm <clang-bin-prefix> --jobs 24
```

Available build backends: memcomp, zram, zram-writeback, zswap, swap,
zswap-crosscheck. zstd and lz4 runtime selections share the same image.
The common fragment compiles THP with a madvise default; the guest applies
never uniformly before the workload. No NUMA or KSM, sanitizers,
lockdep or KUnit, and no modules. Resolved symbols are checked rather than
assuming that merge_config requests succeeded. Memcomp diagnostic statistics
remain enabled and their cost must be disclosed. Allocation and codec sources
are stock; no fairness variant is inferred merely from a backend name.
The builder stops its own process group below 2 GiB available memory or 32 GiB
free on the WSL host volume. It never deletes another build or result.

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

Only after all prerequisites pass, freeze inputs and start **all 565 slots
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

## Frozen formal inputs

At matrix start, sources/helper, five guest tools and kernel artifacts are
frozen. Every slot rechecks sources/helper, all five binaries and its kernel
before and after the slot, then compares runner metadata with the freeze.
Post-slot mismatch is saved as slot `valid=false` / comparison-ineligible and
stops the matrix with `input-drift-stop`; no schedule/retry rule changes.
Do not modify measurement `.c`/`.h`/`.py`, helper or frozen binaries/artifacts
after formal execution starts. See matrix.md for full identity and safety rules.
The real `w1-final-freeze-guard-attempt02` smoke passed both new checks, not a formal slot.

## Workload tools and packages

Build static libc binaries for guest, refault, shmem and app_switch in a reused
userspace output directory. mkinitramfs.py packages those existing files, a
private console node and explicit inputs. It does not copy a host disk or run
a shell inside the guest. Its only writable backend device is the disposable
virtio disk created and lifetime-managed by the existing QEMU helper.

The runner uses fresh guests for every repetition. Explicit host CPU affinity
is inherited by QEMU and its vCPU threads; W1/W3/W4 must also pass --cpu 0 to
pin the actual refaulting process to a guest vCPU. Host CPUs, guest vCPU count,
RAM, disk cache mode, capacity, source, config and image hashes are recorded.
All runs are KVM; there is no silent TCG fallback. WSL2 nested virtualization
results are relative, not physical-device latency/energy measurements.

```sh
python3 bench/compare/run.py --tree <source> --kernel-dir <artifact-dir> \
  --binaries <static-binary-dir> --work <durable-scratch> --dest <new-results> \
  --backend zswap --algorithm zstd --label w1 --cpus 0-1 --smp 2 \
  --memory 1024 --backing-mib 2048 --repeats 5 --allow-pressure -- \
  /refault --pages 32768 --content mixed --seed 1 --order rand --cpu 0
```

This is an invocation example, not an execution claim. One-repeat smoke runs
must explicitly use --smoke; they cannot be promoted to five-run comparisons.
Every guest's raw console log and CSV remain separate, stored losslessly as
.log.gz and .csv.gz to avoid large duplicate plain-text datasets. A QEMU zero exit is not
a workload result: the actual guest status, output and kernel/data diagnostics
are also recorded. Failed runs are retained and stop the specification; they
are not rerun until a fast survivor appears. Formal reporting uses five runs'
median and IQR, separate from each run's per-touch latency percentiles.

## Required workload matrix and W4 interpretation

W1 runs mixed, text, random and same content without filtering same-filled
pages. W2 uses the same explicit worker allocation, memory.max, swap.max,
switch count, seed and access pattern across backends. W3 uses the owned tmpfs
path /mnt/test and a separately opted-in bounded pressure worker.

W4 reuses the single-shot pageout/refault path with an 832 MiB mixed target.
Set label=w4 and retain all states; W1 rows are not relabeled independent cold
IOs. Memcomp's policy counts MC_STORED LRU payload rather than logical pages;
each backend still requires its own actual writeback counter. PAGEOUT return
or positive phase-level writeback does not prove a given target page read disk.

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

For zram-writeback only, the guest's phase barrier allows explicit idle=all,
writeback=idle after PAGEOUT and before refault. The backing device is assigned
BEFORE zram disksize; it is never formatted or swapon'ed separately. This is a
fixed disclosed zram policy, not an assertion that its policy equals memcomp
or zswap. zswap uses zsmalloc and its shrinker; all actual selections are read
back. memcomp's compressor may fall back and must be observed read-only.

Equal logical capacities are not equal physical RAM caps. Keep the explicit
guest RAM, dataset and backing capacities identical and report backend pool
policies separately. Disk cache=unsafe reports guest block IO, not host durable
writes. Pool/data/metadata and compression-ratio definitions differ; unavailable
attribution (especially shared kworker writeback CPU) is N/A, not zero. Raw
snapshots allow vmstat, PSI, block sectors, pool counts and CPU tick deltas
without pretending that attempts count distinct pages.

The collector's sole-console-writer repair is mandatory for formal data.
First-attempt direct child stderr interleaved some CSV frames and lost one
extracted row. That entire attempt is ineligible, despite its older runner
status saying valid=true. Corrected runs use a new result root and fresh
initramfs identity. Raw rows are validated, never repaired or fabricated.

## Uniform MGLRU gate (v4.5)

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

## Frozen W4 protocol and scope gate

New W4 runner metadata and matrix/plan JSON freeze `w4_protocol` from
`run.py:W4_PROTOCOL`: shared default-unlimited scope, every-page half-zero
held pattern, 7/8 held RAM, 1/4 random ramp, release-before-ready,
1000 us/MiB pacing, held lifetime through full verification and
`phase_level_not_per_target_page` attribution. Four exact unique markers for
the owned group, workload membership, pressure pattern and ramp release are
mandatory (shown below). Missing/duplicate evidence fails `w4_scope_valid`;
`w4_scope_error` records why. The CSV schema and other validity gates are unchanged.

The runner requires each exact marker once (1 GiB example):

```text
COMPARE_CGROUP path=/compare-w4 limits=default_unlimited
W4_CGROUP path=/compare-w4 limits=default_unlimited
PRESSURE pattern=halfzero held_bytes=939524096 ramp_pattern=random ramp_bytes=268435456
PRESSURE ramp_released_bytes=268435456 held_bytes=939524096
```

They record PID1's owned group, verified workload membership, pressure pattern
and ramp release. Scope evidence supplements, not replaces, payload/hash,
guest status, CSV, MGLRU and lifecycle checks.

### Historical v4.5 ramp wait and smoke decisions (not v4.8 rules)

Readiness retains its 30-second bound. After readiness, require a positive
backend writeback delta and either three stable one-second polls or a recorded
60 s deadline with `writeback_wait_timed_out=1`. A positive-delta deadline may
proceed to readback, but is not proof of stability. Zero/reset/missing counters,
early pressure-child death/OOM or failed verification invalidate the guest,
even if CSV rows and some writeback exist. Transport, MGLRU and lifecycle gates
also remain mandatory.

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

A separate `w4-zswap-zstd-mglru-ramp-final-runner` smoke directly passed the
final runner with 212992 rows, `w4_scope_valid=true`, `w4_protocol` metadata
and 38.59 s elapsed. It is not mixed into the original three-run counters/times
and remains nonformal.

Exact counter units, deltas and wall times are in README.md. Earlier root
zero-writeback, subgroup zswap +247/zram OOM and old one-in-eight/one-in-four/
one-in-two modes remain retained, non-reusable diagnostics.

## W1 first-pass timing and memory reporting (v4.8)

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
