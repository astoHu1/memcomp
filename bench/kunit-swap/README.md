# Post-swapon memcomp KUnit on VZ

This diagnostic path is not part of the default macOS test run. On the RFC v1
VZ kernel the live run passed five cases and failed five. Use it to investigate
those failures, not as a prerequisite for the supported VZ E2E path.

This standalone init runs `swap-memcomp-budget` and
`swap-memcomp-packed-budget` serially on the 6.12 memcomp writeback kernel.
There is no E2E payload, pressure workload, or concurrent suite execution
after swapon. The kernel tests are unchanged.

Supply an uncompressed ARM64 `Image` with the two built-in suites
(`CONFIG_MEMCOMP_KUNIT_TEST=y`) and `CONFIG_KUNIT=y`, `CONFIG_KUNIT_DEBUGFS=y`,
`CONFIG_DEBUG_FS=y`, `CONFIG_SWAP=y`, `CONFIG_MEMCG=y`,
`CONFIG_ADVISE_SYSCALLS=y`, and the usual VZ console, initramfs, devtmpfs and
virtio block support. Use the VZ KUnit config as the base, not the E2E fragment
that disables the suites. The launcher uses existing ARM64 musl and compiler
runtime artifacts and, for a run, the already built/signed VZ runner.

From the repository root:

```sh
OUT="$PWD/build/macos-kunit-swap" \
  CONFIG_FRAGMENT="$PWD/bench/macos/swap-kunit.config" \
  bash bench/macos/build.sh --configure
bash bench/kunit-swap/run.sh --build-only
bash bench/kunit-swap/run.sh "$PWD/build/macos-kunit-swap/arch/arm64/boot/Image"
```

`--build-only` creates no disk and starts no VM. Both commands pack only into
a new `/tmp/memcomp-kunit-swap.*` directory, without modifying shared build
outputs. A run uses 1 GiB RAM, four vCPUs, and only its own freshly marked
512 MiB image. `TIMEOUT` overrides the 180-second host watchdog. Do not add
disks or run this init outside the disposable guest.

Keep the launcher's unfiltered `kunit.enable=1` boot. In this kernel, disabling
KUnit or using its list action does not create the debugfs suite files.
Boot filters are also unsuitable: the executor frees the filtered suite
copies even though debugfs can retain their addresses. Other built-in suites
may run at boot; no KUnit or pressure process runs alongside the two live
triggers. The two target suites should each report five boot skips before
swap exists. The init reruns them after verifying that the scratch device
is the only entry in `/proc/swaps`.

The output directory contains `console.log` (raw kernel output plus separate
boot and live debugfs KTAP), `host.log`, `vz-info.txt`, `provenance.txt`, and
`verdict.txt` after a successful VZ poweroff. Recheck a log without VZ:

```sh
python3 bench/kunit-swap/validate.py /tmp/memcomp-kunit-swap.XXXXXXXX/console.log
```

Exit 0 requires five live passes per suite, no live skips/failures, complete
swap setup/teardown evidence, and no kernel diagnostics. Exit 4 reports live
skips with no failures; exit 1 reports a failure, incomplete evidence, or a
failed VZ invocation. Boot skips do not count toward the live result. A write
to debugfs `run` returning successfully is not treated as a test pass.
