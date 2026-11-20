# Standalone memcg collectors on macOS arm64

`vz.py` uses the original PID 1 collectors, independently of the shared E2E
init. `minimal` runs the 64 MiB anonymous no-swap charge/release test; `s08`
runs the 64 MiB tmpfs source-to-root refault test. Run them separately, each
with its own disposable VM and new output directory. The helper does not build
the kernel, configure swap, or provision disks. The historical x86/KVM `run.py`
launcher and `s08.py pack/judge` commands retain their own binding and receipt
checks.

## Run

Use `s08` for the supported tmpfs check. `minimal` is an opt-in diagnostic:
it has failed the anonymous `memory.current` lower bound on the RFC v1 VZ
kernel and is not part of the default macOS test run.

Use a prebuilt arm64 `Image`, its corresponding `.config` and
`include/config/kernel.release` in the same build output, and the matching
MC16 source tree. Use the existing Homebrew LLVM/LLD, aarch64-musl sysroot,
and compiler-rt builtins under `build/`. `BREW_PREFIX` can override
`/opt/homebrew`. The signed VZ runner must support `-` as its disk argument
and report `block_devices=0` in the VZ setup receipt.

From the repository root, for example:

```sh
python3 -B bench/memcg/vz.py s08 \
    --kernel-tree kernel-src/linux-mainline --kernel-out build/macos-e2e \
    --out /tmp/memcg-vz-s08
```

Add `--pack-only` to compile a static AArch64 init and pack its initramfs
without invoking the runner. `PACKED_NOT_RUN` is not a guest verdict.
`--runner` overrides `build/macos-vz/vz-runner`. A launch uses one vCPU,
1 GiB RAM, `hvc0`, no block device, and the collector's own boot opt-in.
`result.json`, the console/host logs, VZ receipt, binding, and hashes stay
in the specified directory. The source snapshot and before/after checks
detect input drift; they do not certify that the supplied Image was built
from that source snapshot. Use a separately verified build for formal evidence.

## Kernel prerequisites and verdicts

Both need built-in `ARM64_4K_PAGES`, `CGROUPS`, `MEMCG`, `MEMCOMP`, `SWAP`,
`CRYPTO_ZSTD`, `TRANSPARENT_HUGEPAGE`, and `ADVISE_SYSCALLS`, plus the VZ
initramfs/console and pseudo-filesystem settings checked by the helper.
The kernel needs swap support for the memcomp path and `memory.swap.current`,
but neither launch attaches a disk or activates swap. The collectors need
cgroup v2 `memory.current`, `memory.stat`, `memory.events` and membership,
and `/sys/kernel/mm/memcomp/{compressor,stat}` with zstd and the logical,
payload, and pool byte counters.

- `minimal` uses the original phase, compression, cgroup membership,
  and release checks. MC16 replaces the historical 80-byte entry with a
  64-byte record header and a 24-byte member for each order-0 page. Accounting
  charges `ksize()` (96 or 128 bytes for this arm64 SLUB profile), not 88
  requested bytes. The judge subtracts separately reported kernel components,
  including accounted slot/shadow slab, and requires both residual counters
  to include payload and record charges within the original 80-page
  tolerance. That tolerance covers the small shared key charge; it does not
  permit payload-only accounting or establish per-key attribution.
- `s08` additionally needs `TMPFS`, `SHMEM`, `PROC_PAGE_MONITOR`, and
  `VM_EVENT_COUNTERS`, plus the memcomp shmem store/refault and `mincore`
  hooks (the rc3 anon-only port is insufficient). It keeps the original
  online-source, root reader, full readback, and cleanup checks. Root
  `memory.stat:pgmajfault` is required by the major-fault subtest, even though
  some root memory observations in ordinary samples are optional. The
  major-fault deltas must be exactly 16,384 each for task/global/root and zero
  for source A. The root counter is hierarchical, not a root-only usage proof.

## Trace-based E2E path

`bench/e2e/tests/run_memcg_guest.py` is a separate source-bound verifier, not
a substitute for either collector or a VZ launch mode. It binds an x86
`bzImage` to its build receipt, Git commit, source and symlink manifests,
then launches `qemu-system-x86_64`. Its init authorizes only an x86 CPUID
KVM/TCG guest and refuses arm64 before reaching the verifier.

The checkout is missing `bench/e2e/memcomp_account_trace.h`, which the
launcher hashes and snapshots before compiling `memcomp_memcg.c`. Restoring
the matching header alone would not port the test: the verifier requires the
loss-audited `events/memcomp/memcomp_account` trace ABI and
`/sys/kernel/slab/memcomp_entry/object_size` as an independent metadata-size
witness. Neither is provided by the local MC16 kernel trees. `TRACEPOINTS`,
`TRACING`, and `FTRACE` alone cannot provide them. A genuine port needs the
matching parser/trace contract and independent MC16 metadata witnesses, an
arm64 producer receipt, and a VZ-aware disposable-guest guard. This path is
left unchanged rather than weakening its gates.
