# Functional comparisons on Apple Silicon VZ

This path boots the original `bench/compare` PID 1 and workload executables in
one disposable arm64 guest per invocation. It is for functional bring-up, not
performance or formal smoke acceptance. It does not change the KVM matrix,
guest, workload code, input protocol or CSV schema. VZ scheduling and disk
caching are not controlled like the KVM host. Do not use these captures in
the matrix, or interpret timings and memory counters as comparative results.

## Prerequisites

- Apple Silicon macOS 12.3+, native Python 3.11+, and the signed `vz-runner` from
  `build-runner.sh`. The runner must accept the appended block specs and set
  the second disk's virtio identifier to `memcomp-data` for this layout.
- Existing AArch64 musl sysroot, compiler-rt builtins and exported Linux UAPI
  headers from the macOS toolchain setup in [README.md](README.md). The build
  below compiles only userspace and the native archive helper. It does not
  fetch dependencies, regenerate headers, build an Image or launch a guest.
- A matching, built arm64 4 KiB `Image` and `.config` from the Linux 6.12
  comparison-compatible tree. This port expects the original sysfs knobs
  and pagemap encoding, including the original zswap zpool/shrinker knobs.
- A private regular-file corpus image in the existing
  [format](../corpus/FORMAT.md). Use an absolute canonical path and keep the
  image unchanged during execution. It is attached read-only, not copied;
  the guest still requires a read-only block device and uses direct reads.
  There is no synthetic fallback.

## Build and run

From the repo root, generate a fragment for `memcomp`, `swap`, `zram`,
`zram-writeback`, `zswap` or `zswap-crosscheck`. The fragment keeps the original comparison config
and backend, with VZ-specific overrides for `allnoconfig`; `build.sh` merges it
on top of `arm64-vz.config`. Use a dedicated kernel output directory. This
example checks Kconfig only; omit `CONFIG_ONLY=1` to build the dedicated Image,
or use a matching, already built Image. Do not point this at a shared output.

```sh
mkdir -p build/macos-compare
python3 -B bench/macos/compare.py config memcomp > build/macos-compare/memcomp.fragment
OUT="$PWD/build/macos-compare/kernel-memcomp" CONFIG_ONLY=1 \
  CONFIG_FRAGMENT="$PWD/build/macos-compare/memcomp.fragment" \
  bash bench/macos/build.sh --configure

bash bench/macos/compare-build.sh
```

`compare-build.sh [new-tools-directory]` defaults to
`build/macos-compare/tools`. It accepts `TREE`, `BREW_PREFIX`, `MUSL_SYSROOT`,
`KERNEL_UAPI` (the headers-install prefix) and `BUILTINS` (the static archive).
Use `--binaries` at runtime for a non-default tools directory.

With a built Image and `CORPUS` set to your existing private corpus path:

```sh
KOUT="$PWD/build/macos-compare/kernel-memcomp"
python3 -B bench/macos/compare.py run --label w1 \
  --backend memcomp-noswap --algorithm zstd \
  --kernel "$KOUT/arch/arm64/boot/Image" --kernel-config "$KOUT/.config" \
  --corpus-image "$CORPUS"
```

The `memcomp` profile serves both `memcomp-noswap` and `memcomp-swap`. Use
`--algorithm none` for `swap`; other backends use `zstd` or `lz4`.
`zswap-crosscheck` uses the same config and boot parameters as `zswap`, but
its `Image` must be built from the memcomp-patched tree with `CONFIG_MEMCOMP=n`.
The wrapper checks the config and records hashes; it cannot distinguish the
two source trees by config alone. Crosscheck supports W1-W3, not W4.

Each boot uses 2 vCPUs and 1 GiB RAM. The guest timeout defaults to 120 seconds
for W1-W3 (`--timeout` accepts 15..600) and 1800 seconds for W4 (360..3600).
Presets pin the workload to guest CPU 1 and use the original `uniform-v411`
corpus sampling:

| Label | Functional coverage | Minimum corpus input |
| --- | --- | --- |
| `w1` | Refault/rewrite; 256 pages, one measured round, no warmup, 64 single-page touches | 512 combined pages, including the disjoint prime |
| `w2` | Two 8 MiB workers inside a 12 MiB cgroup, four verified switches | At least 2048 pages in each of the first two sources |
| `w3` | 256 anonymous pages, random readback, 16 MiB disjoint pressure allocation | 4352 combined pages |
| `w4` | 832 MiB target, 896 MiB held pressure, 256 MiB transient ramp, then verified random readback | 507904 combined pages (1984 MiB before image metadata) |

W2 requires an active swap backend for this preset. For W4, use `--label w4`
with `memcomp-swap`, `zswap` or `zram-writeback` and the matching kernel.
W4 keeps the original 2 GiB backing and 128 MiB data disk. It uses the
original pressure-scope, CSV/readback, native writeback counter, `/dev/vda`
sector and 300-second stability gates. A cap-expired run with positive writes
retains the write-volume evidence and marks readback latency excluded, but
does not pass the functional run. Zram's idle writeback is explicit, and the
write evidence is phase-level, not proof of writeback for particular pages.

Consistent-memory snapshots, arbitrary workload flags, repeated boots and
matrix automation are out of scope.

## Devices and results

Attach only these three devices in order: fresh `primary.raw` as RW `/dev/vda`,
fresh `data.raw` as RW `/dev/vdb` with serial `memcomp-data`, and the corpus as
RO `/dev/vdc`. The `memcomp-noswap` and `zram` presets keep an unused 16 MiB
primary image in the first slot so the other devices do not shift. Do not
replace those owned images with existing disks or use `-` for the primary.

Each run creates a private directory under ignored `build/macos-compare/`.
It retains the scratch images, console, runner log, input hashes and a
`functional-status.json` with `eligible_for_comparison=false`. The runner must
exit cleanly and the guest must report one successful `COMPARE_DONE`. The
wrapper checks the data image identity, header, declared length, SHA256 and
zero tail before writing `guest.log` (`guest.log.gz` for W4). It then checks MGLRU boot evidence and
the original CSV validator, rejects known kernel failures and incomplete
readback, and requires all W2 switches to succeed. `workload.csv` is only
written on a functional pass. A timeout or failed check leaves the original
logs and images intact. Keep outputs private and do not stage them as results.

Offline host test: `python3 -B bench/macos/compare-tests.py -v`.
