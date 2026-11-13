# HMM module guest coverage

The v4 configuration gate requests `CONFIG_TEST_HMM=m` and
`CONFIG_HMM_MIRROR=y`. Use the HMM fragment after the normal E2E fragment;
it enables modules and leaves the dummy HMM device as a module, not a
built-in substitute. No production-kernel ABI or policy is added.

## Build and package

```sh
TREE="$HOME/memcomp-work/linux-6.12.23"
OUT="$HOME/memcomp-work/build/e2e-debug-numa-hmm"
PACK="$HOME/memcomp-work/e2e-debug-numa-hmm-initramfs"
HERE="$HOME/memcomp/bench/e2e"
LLVM_BIN="$HOME/toolchains/clang-r536225/bin" JOBS=6 \
  "$HERE/build.sh" "$TREE" "$OUT" "$HERE/debug.fragment" \
  "$HERE/numa.fragment" "$HERE/hmm.fragment"
PATH="$HOME/toolchains/clang-r536225/bin:/usr/local/bin:$PATH" \
  make -C "$TREE" LLVM=1 LLVM_IAS=1 ARCH=x86_64 O="$OUT" \
  -j6 lib/test_hmm.ko
mkdir -p "$PACK"
make -C "$TREE/tools/testing/selftests/mm" OUTPUT="$PACK" \
  LDFLAGS=-static "$PACK/hmm-tests" "$PACK/memcomp_selftest"
HMM_TEST_MODULE="$OUT/lib/test_hmm.ko" HMM_TEST_BIN="$PACK/hmm-tests" \
  SELFTEST_BIN="$PACK/memcomp_selftest" INITRAMFS_OUT="$PACK" \
  "$HERE/mkinitramfs.sh" "$TREE"
```

Verify the effective config has DEBUG_VM, KASAN, KASAN_GENERIC,
PROVE_LOCKING, KFENCE, NUMA, HMM_MIRROR and MODULES enabled, and TEST_HMM=m.
Preserve the exact config, commands, source commit and full build logs.
`run.sh` hashes the current source files; by itself this is not a detector
for an old initramfs built from different sources. Preserve pack-time input
hashes and verify the actual CPIO module/selftest members against their build
artifacts when reusing an image.
Packaging a module without the HMM test binary is rejected.

Add `RUN_HMM=1 HMM_MODULE=1 RUN_SELFTEST=1 kfence.sample_interval=1
loglevel=7` to the guest environment. `HMM_MODULE=1` calls finit_module on
`/test_hmm.ko` before the filtered `hmm.hmm_device_private.memcomp_read_write`
test. Open or load errors, including a duplicate module, are failures.
The test must observe actual memcomp pagemap tokens before each HMM read
and write operation and verify all bytes. A SKIP is not HMM acceptance.
There is no real GPU/device-DMA or physical-device coverage here.

The existing E2E owned-disk supervisor applies to this guest as well.
Use two explicit NUMA nodes for complete move_pages coverage. A single-node
run necessarily reports that subcase as SKIP; without swap, writeback and
swapoff are also inapplicable. Preserve PARTIAL and launcher failures
instead of changing them into COMPLETE. The full-pressure parameters and
all phase/owner checks remain unchanged.

`loglevel=7` exposes the KFENCE pool initialization message hidden by the
launcher's default loglevel=6. Sampling at 1 ms does not imply that every
allocation is guarded. Confirm initialization and the effective sample
interval; configuration alone is not an allocation-level witness.

## Host-only regression

```sh
python3 -B "$HOME/memcomp/bench/e2e/tests/test_hmm_module.py" \
  --work-dir "$HOME/memcomp-work/hmm-loader-host-tests" -v
```

These mock the module syscalls and test failure handling/packaging preflight;
they are not HMM kernel execution. The normal launcher regression is still
`tests/test_runner.py`, run independently of kernel builds.
