# Disposable comparison guest (PID 1)

## Current method: v4.8 documentation contract

The v4.8 release-plan revision supersedes the current W1 denominator, W4
stability and primary memory-accounting rules below. Historical evidence
sections retain their original methods and decisions; they are not v4.8
validation. The serialized top-level `method=v4.6` alone does not establish
v4.8 compatibility: freeze the updated W1/W4 protocols and source hashes.
This documentation update does not claim implementation, smoke, ACK selftest
or formal-matrix completion. See matrix.md for the prerequisites to freezing
new inputs and starting the 565 assigned slots.


## Delivery status

`guest.c` is self-contained Linux C, intended for static linking with libc and
Linux userspace headers. It does not include `common.h`, invoke a shell, execute
host tools, compile anything, or start helper daemons. Historical static x86_64/aarch64
-Werror builds and initial memcomp neutral KVM integration smokes are recorded
in results/2026-10-16-compare-smoke/; they predate the current v4.5 method.
The final-MGLRU W3 and final W4 ramp smokes below supply historical integration
evidence for their exercised paths, not v4.8 acceptance. Other applicable prerequisites and the
565-slot formal matrix remain pending; smoke is not a performance result or
physical-device validation.

## Authorization and inputs

Before creating mounts or touching a block device, the program requires both:

- `getpid() == 1`;
- `/COMPARE_DISPOSABLE_GUEST` exists as a regular non-symlink file.

Absent authorization prints a nonzero sentinel and returns without mounting,
formatting, swapping or rebooting. A PID namespace/marker alone is not proof of
VM isolation: **the host must boot a disposable QEMU VM with only its owned
scratch disk exposed as `/dev/vda`**. Never bind-mount host devices into this
program. Formatting destroys the selected guest device's existing contents.

The initramfs supplies regular, non-symlink text files, LF line endings:

| File | Contract |
| --- | --- |
| `/backend` | Exactly `memcomp-noswap`, `memcomp-swap`, `zram`, `zram-writeback`, `zswap`, `swap` or `zswap-crosscheck` |
| `/algorithm` | `zstd` or `lz4`; **only** the plain `swap` backend uses `none` |
| `/workload-label` | `w1`/`w4` with `/refault`; `w3` with `/shmem`; `w2` with `/app_switch` |
| `/command` | One literal argv per line, up to 64 arguments **including argv[0]**, each up to 4096 bytes. argv[0] exactly `/refault`, `/shmem` or `/app_switch` |
| `/timeout-seconds` | Optional decimal 1..86400, default 300; host runner should always supply its selected value |
| `/zram-bytes` | Required only for zram variants: decimal, positive native-page multiple, at least 16 native pages, representable by a version-1 swap header |

Numbers accept no signs/suffixes/whitespace/overflow. Config enums are exact;
no shell splitting, quoting, substitution, executable search or expansion occurs.
`/command` arguments preserve literal spaces and empty argument lines; NUL/CR,
overlong arguments/input and unknown executable/label combinations fail. A final
LF is a delimiter, not an extra argument. The requested label is metadata only:
**W4 selects `/refault`'s single-shot path with CSV workload `w4`; the CSV
schema is unchanged.**

Use identical argument vectors across backend variants. The W2 parent flag is
**`--cgroup-parent /sys/fs/cgroup`**, not `--parent`. Its own `--allow-pressure`
and required byte/count arguments remain mandatory; PID1 does not rewrite them.
For W3, main should select a workload path under `/mnt/test`. Do not assume a
`/tmp` directory or mountpoint exists.

Main owns static compilation and the initramfs. For reference, a standalone
compile command is `cc -O2 -std=gnu11 -Wall -Wextra -Wformat=2 -Wshadow -static
-o guest guest.c` (one command, not performed by this worker). No extra library
is intended on modern Linux libc; actual cross-toolchain linking remains to be
verified by main. Workload executables must also be compatible with the guest.

## Guest setup and actual state

Mount procfs `/proc`, sysfs `/sys`, devtmpfs `/dev`, cgroup v2
`/sys/fs/cgroup`, and debugfs `/sys/kernel/debug`. Existing system mounts may be
accepted only with matching filesystem type; do not prepopulate the disposable
guest with unrelated services. `/dev/console` is opened for PID1 stdin/stdout/
stderr. Memory is enabled and read-verified in root `cgroup.subtree_control`,
so app_switch can create its own constrained child while its controller stays
outside it. PID1 never removes app_switch's cgroups.

For W4, PID1 creates fresh owned `/sys/fs/cgroup/compare-w4` for every backend,
read-verifies default `memory.max`, `memory.high` and `memory.swap.max` are
`max`, and does not tune limits or reclaim policy. `memory.zswap.max` is also
checked as unlimited where available (required for zswap). The target enters
`/compare-w4` before exec and verifies membership; the pressure child inherits
the same group. PID1 remains outside. Old root-scope runs are not substitutes.

Create a new tmpfs at `/mnt/test` with an explicit byte limit of **90% of guest
`sysinfo().totalram * mem_unit`**, rounded down to a native page. This is a maximum,
not preallocated resident memory and not 90% of host RAM. Existing test mounts
are not reused/remounted. A separate owned tmpfs at `/run` is limited to 1% of
guest RAM (at least one page). No `/tmp` mount is required or created. Runtime
metadata and log handling must not be mistaken for workload resident size.

A single explicit THP policy, **never**, is applied for every backend. Anonymous
and shmem root policy files and available per-size `hugepages-*/enabled` and
`shmem_enabled` files are written/read-verified. Each policy is recorded; absent
THP files are explicitly N/A (e.g. THP compiled out), not invented settings. No
policy-selection axis, reclaim tuning, OOM-score changes, backend pool-cap or
allocator/codec optimization is added. Workload-local madvise behavior remains
owned by each workload.

All backends require an initially empty `/proc/swaps`. PID1 does not swapoff or
reuse any pre-existing swap. Backend matrix:

| Requested backend | Setup and only active swap target |
| --- | --- |
| `memcomp-noswap` | No active swap; observed memcomp compressor must match algorithm |
| `memcomp-swap` | Format/swapon `/dev/vda`; memcomp compressor observed read-only |
| `swap` | Format/swapon `/dev/vda`; zswap must actually be disabled/absent |
| `zswap`, `zswap-crosscheck` | Format/swapon `/dev/vda`; verify actual zswap enabled, compressor, zpool and shrinker parameters |
| `zram` | Select/read-verify comp_algorithm, then set/read-verify disksize, format/swapon **only `/dev/zram0`** |
| `zram-writeback` | Select/read-verify comp_algorithm; set backing_dev=`/dev/vda` **before disksize**; set/read-verify disksize; format/swapon **only `/dev/zram0`**, never the backing device |

Zram must initially have disksize=0. Missing built-in devices are waited for up
to five seconds, with the overall guest timeout still active. `/zram-bytes` is a
host-selected fixed byte count, not derived dynamically to favor a backend.
No reset of a pre-existing zram configuration is attempted. For writeback, actual
backing_dev is recorded before disksize. The setup writes no pool-cap knobs.

Formatting uses `fstat` block-device validation and `BLKGETSIZE64`, a real native-
page-sized swap header, version=1 at byte 1024, last_page at 1028, zero bad-page
count, and `SWAPSPACE2` in the final ten bytes. Page counts cannot overflow the
header's 32-bit last_page. The header is written fully, fsynced, then `swapon`
is called; the actual `/proc/swaps` target/count is checked. Only `/dev/vda` and
`/dev/zram0` exist on the formatting allowlist; `/dev/vda` is explicitly rejected
as a formatting/swapon target for zram-writeback.

Memcomp compressor and stat are read-only. Zswap parameters are also read-only:
main must select `zswap.enabled=1`, the requested compressor, `zpool=zsmalloc`
and `shrinker_enabled=1` using its kernel/boot configuration for the two zswap
variants. These four actual parameter files are read and checked, not inferred
from `.config`; all other variants require actual zswap disabled or compiled
out. Requested backend names are never themselves proof of active compression.
Raw memcomp nodes, zswap parameters and `/proc/cmdline` are recorded. Main retains
`.config`, `source.txt`, artifacts and QEMU command line; PID1 does not make a
claim about their contents or silently alter a compressor that mismatches.

## Phase barriers and zram writeback

PID1 creates dedicated notify and ACK pipes and exports their inherited FDs as
`COMPARE_PHASE_WRITE_FD` / `COMPARE_PHASE_READ_FD`. They are checked to be
distinct decimal FDs in 3..1023 before forking. The workload writes
`phase_name\n` and waits for one literal byte `'1'` without newline, using a
single 30-second deadline for the **total phase send plus ACK**.
Phase names are 1..63 ASCII alphanumeric/underscore/dot/hyphen characters.
A separate CLOEXEC startup-error pipe makes child fd/env/process-group/exec
failures explicit and nonzero; EOF on that pipe confirms exec only when no
startup-error payload was received.

The frozen W1 phases are `fill_begin`, `prepared`, `pageout_returned`,
`read_begin`, `read_end`. W3 uses `fill_begin`, `prepared`, `pressure_begin`,
`pressure_ready`, `read_begin`, `read_end`, or `pressure_disabled` instead of the
pressure pair. **`pageout_returned`** is the one W4 writeback trigger; the source
constant `W4_PAGEOUT_PHASE` uses exactly that name, with no alias.

At every phase, PID1 records a raw snapshot before ACK, outside workload timing
as defined by the caller. **Only** when `/workload-label=w4`, backend is
`zram-writeback`, and phase is `pageout_returned`, it first snapshots pre-writeback,
then synchronously writes `idle=all` followed by `writeback=idle`, records begin/
end timestamps, snapshots again, then ACKs. Duplicate triggers, failed sysfs
writes, missing expected W4 trigger, invalid/partial phase data or ACK failures
are control errors. There is no idle/writeback command for W1/W2/W3, including
W3 `pressure_ready`; therefore no ambiguous implicit W2/W3 writeback claim.
A successful writeback request does not assert any particular byte/page count;
retain actual bd_stat/diskstats and describe zero-write observations honestly.

PID1 budgets 25 seconds to service/ACK a phase. For W4 zram-writeback's
synchronous pageout_returned service, it uses 30 seconds to match the existing
workload barrier bound. The workload retains its own 30-second send/ACK
deadline; successful completion still requires both endpoints to finish in
time. The clocks and start points differ, so this is not a guaranteed margin.
The W4 300-second stability requirement is unchanged. Sysfs writeback is
synchronous and can stall inside the kernel. The guest alarm interrupts
interruptible operations, but cannot defeat
uninterruptible kernel I/O or a blocked console. **Host QEMU timeout remains the
primary outer bound.** Snapshot/writeback/serial overhead is excluded from
workload-marked timed intervals, but changes memory age/scheduling between them
and is an acknowledged experimental perturbation.

## Clean CSV stream and monitor lifecycle

Workload stdout goes to its **own pipe**, not directly to console. Stderr stays
on console. PID1 multiplexes stdout, phase notifications and startup errors with
poll. It forwards every complete stdout line (including CSV header) with exactly
`COMPARE_CSV ` prepended; no other parent output uses that prefix. It does not
parse, filter or rewrite the CSV payload. Normal stdout bytes/newlines are
preserved after removing that one transport prefix. `COMPARE_WORKLOAD_BEGIN`
and `COMPARE_WORKLOAD_END` delimit execution; telemetry may appear between CSV
records but never masquerades as CSV. Main extracts prefixed lines into a clean
raw CSV artifact and retains the whole console separately.

The stdout line limit is 16 KiB excluding newline. Overlong lines, NUL bytes or
a final nonterminated line are explicit infrastructure errors, never silently
truncated valid records. Rejected bytes are preserved losslessly in bounded
`COMPARE_STDOUT_REJECTED_HEX ` records rather than under the valid CSV prefix.
A forced outer timeout may prevent complete draining; it is reported as
`COMPARE_DRAIN_ERROR`, never a successful run with silently missing data.

After child exit, PID1 **continues draining stdout to EOF before the final
sentinel**. It never assumes child exit alone means all output was received.
The workload runs in its own process group; on guest timeout/control/telemetry
failure SIGTERM is followed after one second by SIGKILL. The drain deadline is
five seconds on failure, three seconds after normal main-child exit, with a
further bounded two-second reap interval. A surviving/adopted descendant,
unknown exit, missing exec confirmation or incomplete EOF is nonzero. PID1
reaps with waitpid and logs actual raw wait status/exit code/signal. It does not
perform foreign cgroup cleanup or spawn a workload shell. These bounds do not
replace host termination of a kernel stuck in D state or a stalled console.

## Telemetry and completion

Snapshots at boot/backend setup and workload start/end, and every W1/W3/W4 phase,
include `/proc/vmstat`, meminfo, stat, diskstats, swaps and pressure; zram mm_stat,
bd_stat and io_stat; memcomp stat/compressor; actual zswap parameters; and regular
readable debugfs/zswap counters. During W2, the PID1 monitor additionally samples
at a fixed **one-second** cadence (no catch-up burst after a delayed sample).
This is concurrent telemetry relative to the child, not an extra pressure task.

Every raw file record has a path and CLOCK_MONOTONIC timestamp in
`COMPARE_RAW_BEGIN/END`; snapshots are sequential, not atomic. Each file is
bounded to 256 KiB; debugfs collection is non-recursive and capped at 64 files.
Core proc reads/output errors are fatal telemetry errors. Optional absent files
are `COMPARE_RAW_NA` with errno, not zero. Kernel-thread collection is best effort,
retaining comm/stat for kswapd/kworker (up to 64 matches/65536 inspected PIDs),
with explicit limit markers. These CPU ticks do not isolate a shared worker's
memcomp/zram writeback work. Dedicated WB CPU and exact backend metadata/resource
attribution are explicitly N/A; pool/stat/IO scope must be explained by analysis,
not synthesized from slab growth. Console sampling has CPU/IO overhead and can
backpressure the child stdout; it must be kept identical across comparisons.

Finally PID1 syncs, prints one exact unprefixed line:

```text
COMPARE_DONE status=N
```

Status is zero **only** for successful setup, confirmed workload exec/zero exit,
successful required telemetry/phase control, complete stdout EOF and cleanup.
A workload's nonzero exit/signal is preserved when available; setup/control/
telemetry failure cannot turn a zero workload exit into success (uses 2).
Guest timeout is 124; handled external signals are 128+signal. The actual wait
status and control/telemetry flags remain in diagnostics even when timeout takes
precedence. The host must also reject missing/malformed sentinel, missing EOF,
QEMU timeout/crash or incomplete CSV artifact; it must not equate seeing a
requested backend/config name with a successful run.

After the sentinel PID1 requests `RB_POWER_OFF`, without swapoff/device reset or
unmounting/removing workload-owned groups. If poweroff fails it stays alive for
the host timeout. No post-sentinel userspace diagnostic is printed, though the
kernel may print its poweroff messages. The non-authorized path does not reboot.

The final-MGLRU W3 and final W4 ramp smokes exercised their real guest setups,
phase/CSV transport, payload bytes/hash checks and successful lifecycle cleanup.
They do not certify every other backend/workload setup, W2 controls or
unexercised startup-failure/timeout/reap paths. Those require their own evidence;
these smokes do not imply a complete formal matrix PASS.

Memcomp snapshots also retain the optional events, packed_swap_events and
execution_events files when DEBUG_STATS is enabled. These are accumulated
events/current gauges per their ABI, not a transactionally coherent page
index. In particular, swapped_read_io may be satisfied by swapcache and is
not physical block IO. Keep the matching /proc/diskstats observation for W4.

## Sole console writer and raw CSV integrity

PID1 is the only userspace console writer. Child stdout uses the framed CSV
pipe. Child stderr goes to an exclusively created, immediately unlinked file
on the owned /run tmpfs; it is replayed completely only after CSV is drained,
then truncated before the final snapshot. The file is bounded by the tmpfs
and a 16 MiB replay cap; write/replay/output failures invalidate the run.
No diagnostic is dropped to make a run pass. This prevents child diagnostic
writes from splitting a serial CSV frame. The host additionally checks the
actual complete row count, sequence and verification fields, not merely a
zero QEMU/guest exit. The first formal attempt exposed this real bug and is
retained as invalid data, never as a comparative result.

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

## Final W4 held-pressure and ramp protocol

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

New W4 runner metadata and matrix/plan JSON freeze `w4_protocol` from
`run.py:W4_PROTOCOL`: shared default-unlimited scope, every-page half-zero
held pattern, 7/8 held RAM, 1/4 random ramp, release-before-ready,
1000 us/MiB pacing, held lifetime through full verification and
`phase_level_not_per_target_page` attribution. Four exact unique markers for
the owned group, workload membership, pressure pattern and ramp release are
mandatory (runner.md). Missing/duplicate evidence fails `w4_scope_valid`;
`w4_scope_error` records why. The CSV schema and other validity gates are unchanged.

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

README.md records exact deltas/times and retained historical failures. Root
zero-writeback, subgroup zswap +247/zram OOM and all older one-in-eight/
one-in-four/one-in-two modes remain non-reusable diagnostics.

### W1 first-pass denominator and present-page drain (v4.8)

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

The v4.7 present-page drain behavior remains:


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
