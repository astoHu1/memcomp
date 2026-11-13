# W2: stock-neutral multi-process application switching

## Status and scope

The static x86_64 and aarch64 builds pass with -Werror, and one small neutral
KVM cgroup-switch smoke completes. Exact records are in
results/2026-10-16-compare-userspace-build/ and
results/2026-10-16-compare-smoke/memcomp-swap-w2-small/.
This is not the planned five-run comparative measurement or exhaustive
OOM/signal/timeout validation. Unexpected non-SIGKILL worker signals invalidate
the run; their actual status and every unavailable switch remain recorded.

`app_switch.c` is a standalone Linux C program. It includes no project headers,
needs no Python or external process tools at runtime, and is intended to link
statically with libc. It neither sets up nor detects a backend. The same binary,
arguments, corpus, access order and cgroup limits must run unchanged across
stock swap, zram, zram-writeback, zswap and memcomp configurations. Backend setup
and backend-specific observation belong to the main runner/collector, not W2.
There are no memcomp ABI dependencies, backend count assertions, fixed source
revisions, boot-command-line opt-ins, or accounting-test changes.

This is a synthetic **full-working-set resume** workload, not a GUI launch or
phone app trace. The worker's data regeneration and checking are deliberately
part of measured CPU and wall time; these costs must not be represented as
backend-only refault cost.

## Safety contract

**Use only inside a disposable Linux VM. Do not run on a daily-use host/device.**
The CLI requires explicit `--allow-pressure`, an absolute cgroup parent, a worker
count, a per-worker size, a memory limit and a switch count. No arguments or
`--help` just prints help without creating groups or allocating working sets.
Malformed, incomplete or non-opted-in pressure invocations exit with code 2.
The program does not automatically judge a workload safe for the available RAM.
An operator must size it to the disposable VM and retain headroom for the
controller/kernel/collector outside the limited child.

The parent must already be a writable, delegated cgroup v2 directory with
`memory` present in **its `cgroup.subtree_control`**. Permission must include
creation/removal of a child, writing its controls, and worker self-migration
(including required common-ancestor migration permissions). W2 refuses to
silently enable controllers or mount cgroupfs. It requires `memory.swap.max` to
exist and be writable, even for a run with no active swap. Missing prerequisites
are infrastructure errors, not a fallback to uncontrolled pressure.

Only `app-switch.<controller-pid>.<monotonic-time-hex>.<attempt>` is created,
using exclusive `mkdirat` under the supplied parent. Existing names are never
reused. Parent and child are held through directory FDs; the leaf must not be
a symlink. Cleanup compares the child's saved device/inode identity before
removing that exact direct child with `unlinkat(..., AT_REMOVEDIR)`. There is no
recursive deletion, `cgroup.kill`, process-group kill, or cleanup of other runs.
Concurrent external mutation/migration into the owned group is unsupported;
an occupied or changed group is left intact with an explicit cleanup error.

The controller **never migrates into its child**. Every worker joins that common
child and reports JOIN before any working-set `mmap` or filling. The program
writes/reads back exactly the requested `memory.max` and `memory.swap.max` before
forking. It changes no parent/global settings, host swap state, swap priority,
OOM score, swappiness, reclaim policy, codec or allocator. The default
`memory.swap.max=max` is explicit, not an accidental `0`. `max` does not create
swap or override an ancestor's restriction; `--swap-max 0` intentionally forbids
swap only for the owned child. Raw parent `memory.max`, `memory.high`,
`memory.swap.max` and `memory.oom.group` are recorded when available.

The controller remains subject to pre-existing ancestor and system-wide memory
constraints. A parent/ancestor group-OOM domain can kill the controller too;
placing it outside its newly limited child is not immunity from global OOM.
Choose an isolated delegation with adequate ancestor/global headroom and keep
pre-existing ancestor policy consistent across runs. W2 never adjusts it.

## Build integration (main worker, not executed here)

The independent target can use a libc with static development libraries:

```sh
cc -O2 -std=c11 -Wall -Wextra -Werror -static \
  -o app_switch app_switch.c
```

No `common.h` or extra library is required on modern Linux libc. Main owns
Makefile integration and actual toolchain/link verification. This worker did
not edit the Makefile, `common.h`, or the comparison README.

## Invocation

The following is a **template for later use in the disposable guest**, not an
invocation performed during implementation. Supply decimal, native-page-aligned
byte counts selected for that VM; do not copy an unrelated machine's capacity.
The parent must already have been prepared by the main runner/operator.

```sh
./app_switch --allow-pressure --cgroup-parent "$CGROUP_PARENT" \
  --workers "$WORKERS" --worker-bytes "$WORKER_BYTES" \
  --memory-max "$LIMIT_BYTES" --swap-max max \
  --switches "$SWITCHES" --seed "$SEED" --timeout-ms "$HANDSHAKE_MS" \
  > switches.csv 2> switches.log
```

Add `--cpu "$GUEST_CPU"` to pin all workers to one guest logical CPU. Without it,
they inherit the controller's affinity. W2 does not change controller affinity
or host/vCPU affinity; the main runner records and fixes those separately.
Redirect both streams to writable, non-stalling regular files (or a reliably
drained serial/log sink). Blocking on an externally stalled output sink or a
hung filesystem is not covered by pipe-handshake deadlines.

| Option | Accepted values and defaults |
| --- | --- |
| `--allow-pressure` | Required explicit consent; no implicit pressure mode |
| `--cgroup-parent` | Required absolute path, shorter than `PATH_MAX`, no control characters; existing v2 memory-enabled parent |
| `--workers` | Required, 1..64; original worker IDs are 0..N-1 |
| `--worker-bytes` | Required, positive decimal bytes, native-page multiple, at most `min(SIZE_MAX, PTRDIFF_MAX)` |
| `--memory-max` | Required, positive decimal bytes, native-page multiple, at most `INT64_MAX` |
| `--swap-max` | Default `max`; or 0..`INT64_MAX` decimal bytes, native-page multiple |
| `--switches` | Required, 1..1,000,000 planned commands/attempts, not just successful ACKs |
| `--seed` | Default 1; decimal 0..`UINT64_MAX`; controls both content and scheduling |
| `--cpu` | Optional single CPU, 0..`CPU_SETSIZE-1`; must be in inherited allowed mask; affinity failure is not silently ignored |
| `--timeout-ms` | Default 10,000; 1..3,600,000 for EACH controller handshake, including initialization |

Numbers do not accept signs, suffixes, whitespace, fractions or overflow. The
program checks total worker bytes before multiplication against both `SIZE_MAX`
and `INT64_MAX`. Bounds are representational/operational limits, not a hardcoded
device capacity. Native page size is queried, not assumed to be 4096. Page size
must be a positive multiple of 32. A too-short timeout can itself evict healthy
workers from the measurement; report the selected value and all timeouts.

## Working set and command protocol

1. Create/configure the owned group. Allocate all nonblocking CLOEXEC pipe pairs
   before forking. Each child retains only its own command-read/reply-write
   endpoints; it closes ALL peer endpoints and the controller's parent-directory
   FD. The controller closes each child's endpoints immediately after its fork,
   so later forks cannot inherit those writers/readers. This prevents false EOF
   or EPIPE suppression. SIGPIPE is ignored, so EPIPE is a recorded I/O outcome.
2. Each child sets parent-death SIGKILL and checks the original parent's PID to
   close the fork/prctl race. It optionally sets guest affinity, joins the owned
   group using its PID, closes the group FD, then reports JOIN. No workload
   mapping is inherited: each child later obtains its own private anonymous VMA.
3. Controller attempts every JOIN with a real timeout, then snapshots
   `joined_before_allocation`. EOF/EPIPE/timeout before INIT also retires that
   worker as unavailable (including a very early OOM); it does not discard the
   remaining schedule. An explicit join-permission/affinity/protocol error is
   infrastructure failure. INIT is sent sequentially in original worker-ID
   order. INIT does `mmap`, applies VMA-local `MADV_NOHUGEPAGE` uniformly, and
   fills the fixed region. An unsupported/failed madvise is explicit error, not
   a silent policy difference. Earlier workers remain idle while later INITs
   establish pressure. OOM during INIT is retained as unavailable workers.
4. The page-indexed mixed corpus uses the seed, worker ID, page number and
   64-bit word offset. Seven of every eight native pages repeat a pseudorandom
   quarter-page; one has non-repeating pseudorandom words. Each page/worker has
   its own keyed stream. No expected-data mapping, resident verification copy,
   Python generator, or page-sized reference buffer exists. The repeat geometry
   is fixed; actual compression ratio is NOT asserted or inferred from it.
5. After `after_initialization`, controller schedules deterministic epochs.
   Each epoch is a seeded Fisher-Yates permutation of **all original IDs**, not
   just survivors. For N>1, the first two IDs are exchanged when necessary to
   avoid immediately repeating the previous epoch's last foreground worker.
   A partial final epoch is retained. N=1 is supported as a resume-only control,
   not a multi-process switch. There is no adaptive retry or respawn.
6. TOUCH always sweeps the same **entire** region in ascending 64-bit-word order,
   regenerates expected values without a replica, compares every word, and
   writes the same value back using volatile access. It also accumulates a
   diagnostic checksum of observed values. No PAGEOUT, discard, resizing,
   background-touch process, or artificial sleep is added. On first mismatch,
   capture word index/expected/observed, report corruption and stop that worker.
   Initialization fills but does not read-verify; `init_ok=1` alone is NOT a data
   integrity result. Every successful TOUCH verifies `worker_bytes/8` words.
7. The controller samples `CLOCK_MONOTONIC` just before sending a command and
   just after receiving the complete reply. That is `elapsed_ns`, including pipe
   send, scheduling/refault/touch and ACK return. One absolute deadline covers
   both send and receive, and is recomputed after EINTR/EAGAIN rather than reset
   on every poll. Worker `worker_wall_ns` uses CLOCK_MONOTONIC; `worker_cpu_ns`
   uses CLOCK_PROCESS_CPUTIME_ID. `getrusage` deltas include user/system time,
   faults and context switches. Generation/checksum/verification costs stay in
   both measured worker times. CSV/log formatting and snapshots are outside the
   switch interval, although their between-switch perturbation still exists.
8. Worker idle reads deliberately have no short timeout: a worker may wait for
   many other handshakes. Controller exchanges have finite deadlines and children
   die on parent death. OOM, EOF, EPIPE or a controller deadline cannot leave the
   controller indefinitely waiting for that child's reply.

## CSV and failure semantics

Stdout has one header and three record kinds: `init`, `switch`, `pid_exit`.
Records use a common 32-column schema. Sequence is 1-based for switches and 0
otherwise. Worker/PID identifies the original process; PIDs are never recycled
or replaced by the workload. All numeric columns are unsigned decimal except
`errno`, which is a nonnegative Linux error number. Empty numeric fields mean
**unavailable/not applicable**, never an invented zero-duration success.

- `record,sequence,worker,pid,status,reason,errno`: outcome and provenance.
- `elapsed_ns`: attempted controller exchange duration. Present even for a
  failed exchange that ran until EOF/timeout; this is NOT completed latency.
  Empty for a future scheduled switch to an already unavailable worker.
- `worker_wall_ns,worker_cpu_ns`: measured work interval; INIT covers map/fill,
  TOUCH covers verification/read/write. Missing without a valid reply.
- `user_us,system_us,minflt,majflt,nvcsw,nivcsw`: per-operation `getrusage`
  deltas for INIT/TOUCH; **whole-process cumulative values** for `pid_exit`,
  obtained by `wait4` (the waitpid-family call with final rusage). These are
  different scopes and must not be summed across record kinds.
- `maxrss_kib`: process lifetime high-water RSS in Linux KiB, not a delta and not
  compressed-pool or cgroup resident size.
- `checked_words,checksum`: verification count and diagnostic 64-bit XOR digest.
  INIT has zero verified words/digest; only TOUCH compares data. The exact
  word-by-word comparison, not the checksum alone, determines correctness.
- `mismatch_word,expected,observed`: only present on corruption; zero-based
  64-bit-word index (byte offset = index * 8), regenerated and actual word values.
- `wait_kind,exit_code,term_signal,core_dumped`: final wait status on `pid_exit`;
  `wait_kind=unreaped_or_unknown` if no final status could safely be obtained.
- `controller_kill`: `none` or controller reason for actually sending SIGKILL;
  separates timeout/cleanup actions from external signals or possible OOM kill.
- `init_ok`: INIT ACK was successful, even if the worker died later.
- `alive_before_cleanup`: on `pid_exit`, whether the original initialized,
  usable child was not reaped at the post-switch observation; empty otherwise.
  This is a sampled lifecycle observation, not an assertion about a runnable
  process or a worker that already timed out but has not yet been reaped.
- `attempts,ok_switches,unavailable_switches`: cumulative original-ID counters;
  use final `pid_exit` values for per-worker totals. `ok_switches` counts only
  complete successful verification ACKs, never INIT or QUIT.

Successful switches have `status=ok`. Failures use `unavailable` (EOF, EPIPE,
`timeout`, `allocation_failed`, `exited`, etc.), `corrupt` (actual mismatch), or
`error` (invalid protocol or a worker/system-call error). Corrupt/error switch
rows also contribute to unavailable totals. A timed-out child is retired,
SIGKILLed only if still owned/unreaped, and all its later planned IDs remain in
the schedule with empty timings. A child killed during someone else's INIT or
TOUCH likewise retains all its scheduled rows. No survivor-only renumbering,
latency imputation, filtering, hidden restart or correctness threshold is used.
For an uninterrupted measurement, emitted switch rows equal `--switches`, even
if all workers die. A setup/fork failure, actual JOIN setup/protocol error, interrupted run or
failed output may be partial; exit status and planned/emitted summary expose that explicitly.

The program does not claim that every SIGKILL is an OOM kill. `memory.events`
provides aggregate OOM evidence; per-PID signal and controller-kill provenance
are preserved, but aggregate events cannot unambiguously attribute a particular
external kill. Nonzero `max`, `oom`, `oom_kill`, swap events and unavailable
switches are measurements, never automatic whole-run correctness failure.

Unexpected signal termination other than SIGKILL (including SIGSEGV, SIGBUS,
SIGILL, SIGFPE and SIGABRT) invalidates the run as an infrastructure error,
while all unavailable rows and the actual signal remain recorded. SIGKILL
alone still does not prove OOM.

Worker exit codes: 0 clean QUIT, 10 mmap ENOMEM/resource unavailable,
11 harness/protocol/system-call failure, 12 detected content corruption.
Final controller exit codes:

| Exit | Meaning |
| --- | --- |
| 0 | Complete measurement, cleanup/output successful, no observed corruption or infrastructure error; **does not mean all/any workers survived** |
| 1 | Observed content corruption (takes precedence over other failures) |
| 2 | Invalid invocation, setup/protocol/required-stat/output/cleanup error, or incomplete non-interrupted schedule |
| 128 + signal | Controller handled SIGINT/SIGTERM/SIGHUP; partial run, unless corruption already takes precedence |

## Snapshots, teardown and analysis

Stderr has CONFIG (sizes/seed/page size/worker CPU/timeout), KERNEL,
OWNED_CGROUP (including read-back limits), raw snapshots and SUMMARY with planned,
emitted, ACK-success, unavailable, and alive-before-cleanup counts. Snapshot
phases: `joined_before_allocation`, `after_initialization`,
`after_switches_before_cleanup`, `after_reap`. Events accumulated during INIT
must not be silently discarded when reporting whole-run survivability.

Raw records use `RAW_BEGIN/RAW_END phase=... file=...`, with phase monotonic time.
They include cgroup memory current/peak/stat/events/local events/swap usage/swap
events/PSI, cpu.stat/PSI, io.stat/PSI, membership and lifecycle events; and
system /proc uptime/swaps/meminfo/vmstat/stat/diskstats/PSI, controller cgroup and
THP policy. The collector also discovers kswapd processes and retains their
raw /proc PID stat (CPU ticks). Missing optional files are `RAW_NA` with errno,
not zero. `memory.events` read failure is an infrastructure error; the values
inside it have no PASS threshold. Each file is capped at 256 KiB and explicit
`RAW_TRUNCATED` marks the limit. Snapshots are sequential, not atomic, and system
statistics include unrelated guest activity; retain raw logs and phase scope.

This permits neutral observation of PSI totals, allocstall, swap-page counters,
block-sector deltas, cgroup memory classes, and CPU time. System I/O is not
per-backend payload I/O, swap pages require the recorded native page size, and
block-layer sector units must not be confused with native pages. Neither this
program nor its schema invents a memcomp-specific ABI. Dedicated backend
writeback CPU, exact backend metadata cost/pool usage/compression ratio are
marked N/A here; the main runner may attach existing backend statistics, with
scope limitations stated. In particular slab/SUnreclaim is not a proxy for
memcomp metadata or evidence of a memcomp object leak.

Before normal teardown the controller observes survival, then sends bounded
QUIT exchanges. On interrupt/output failure or failed QUIT it kills only its
own unreaped PIDs. It uses nonblocking wait4 to avoid PID reuse and to preserve
exit reasons/rusage. After QUIT, a grace interval clamped to 1..5 seconds is
followed by SIGKILL for any still-owned children and one more bounded 1..5 second
reap interval. A child stuck uninterruptibly can outlast SIGKILL; report unknown
exit/cleanup error and leave an occupied group rather than hang or delete other
processes. Successful cleanup removes only the owned empty cgroup. An uncatchable
controller death cannot guarantee directory removal, although parent-death kill
bounds orphaned workers; the disposable VM/operator owns that exceptional case.

Report survival and unavailable/timeout fraction **beside** successful-ACK
latency distributions. Never call dead-worker empty timings zero latency or drop
failed attempts from the denominator. Cold/initialization and switch measurements
have separate record kinds. Main should use matched fresh guests, identical
scales/limits/swap limits/CPU policy/seed, five independent repeats, and report
run-level median/IQR with raw values. The WSL nested-virtualization setup supports
relative comparisons, not absolute phone/GUI latency or energy claims.

Pending main-worker verification: compile/link this exact source; no-opt-in
safety; a small bounded non-pressure/correctness run; a bounded memory-limited
OOM/unavailable run; actual timeout/EOF/EPIPE/interrupt cleanup; all planned rows
and final PID reasons; owned-group-only removal. These are verification tasks,
not claimed results and not a newly added test framework or benchmark gate.
