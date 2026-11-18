# Optional ARM64 QEMU TCG correctness runs on macOS

This is a separate path from the VZ runner. It uses QEMU software emulation
and the original guests in `bench/mte` and `bench/e2e`. It does not establish
host CPU, VZ, or HVF capabilities, and its timings are not benchmark results.

The runner needs Python 3.10+ and `qemu-system-aarch64` on `PATH` (or an explicit
`--qemu /path/to/qemu-system-aarch64`). The
QEMU binary must accept the ARM `virt` machine and the TCG options used by the
selected profile. A missing executable is an error before creating results.
The runner does not install dependencies or build guest artifacts.

## Prepare inputs

Use the original 6.12.23 memcomp kernel tree and the macOS cross toolchain
described in [README.md](README.md). From the repo root, use separate `OUT`
directories for the two profiles. To validate the merged configs only, add
`CONFIG_ONLY=1` to the `build.sh` commands below. That does not produce an
`Image`; remove it when building an image for an actual run.

```sh
OUT="$PWD/build/macos-tcg-mte" CONFIG_FRAGMENT="$PWD/bench/macos/tcg-mte.config" \
    bash bench/macos/build.sh --configure
bash bench/macos/tcg-build-mte.sh "$PWD/build/macos-tcg-mte/guest"

OUT="$PWD/build/macos-tcg-e2e" CONFIG_FRAGMENT="$PWD/bench/macos/tcg-e2e.config" \
    bash bench/macos/build.sh --configure
OUT="$PWD/build/macos-tcg-e2e" INITRAMFS_OUT="$PWD/build/macos-tcg-e2e/guest" \
    KERNEL_UAPI_OUT="$PWD/build/macos-tcg-e2e/uapi" bash bench/macos/mkinitramfs.sh
```

The MTE packer needs the existing static musl sysroot and compiler-rt builtins
from `build-userspace.sh` and `build-runtime.sh`. Its destination must be new.
The E2E initramfs must contain the original `mcwb_init` and `memcomp_selftest`.

## Run and interpret

```sh
mkdir -p build/macos-tcg-results
python3 -B bench/macos/tcg_run.py mte \
    --kernel build/macos-tcg-mte/arch/arm64/boot/Image \
    --config build/macos-tcg-mte/.config \
    --initramfs build/macos-tcg-mte/guest/initramfs.gz \
    --results build/macos-tcg-results/mte-01
python3 -B bench/macos/tcg_run.py e2e \
    --kernel build/macos-tcg-e2e/arch/arm64/boot/Image \
    --config build/macos-tcg-e2e/.config \
    --initramfs build/macos-tcg-e2e/guest/initramfs.gz \
    --results build/macos-tcg-results/e2e-01
```

- `mte`: single-node `virt,mte=on` with `-cpu max`, 2 vCPUs and 512 MiB of
  RAM. Runs `no-swap` without a disk and `swap-on` with a freshly formatted
  256 MiB disk. Both must exit cleanly and pass the original MTE marker checks.
- `e2e`: two equal RAM nodes and one virtual CPU socket per node (4 vCPUs,
  1024 MiB total by default). Uses the original workload and selftest,
  `EXPECT_NUMA=1`, and a blank 2048 MiB disk that `mcwb_init` formats. Pass
  requires the E2E and MM TAP gates (all eight selftest cases), exercised
  swapoff, and a successful node-1 migration witness from the guest. The
  launcher grants the original selftest's QEMU-only boot opt-in solely on this
  fresh one-disk VM. `coverage=PARTIAL` remains partial even if the
  migration passed. Set `REQUIRE_COMPLETE=1` to reject partial coverage.

Both profiles use `ttyAMA0`; when attached, the only guest disk is an emulated
virtio-MMIO block device (`/dev/vda`). No host block device, arbitrary QEMU
options, guest network, or monitor is exposed. `--memory-mb`, `--cpus`,
`--swap-mb`, and `--timeout` can be overridden; use even memory and CPU counts
for E2E. `--timeout` defaults to 1800 seconds per case (E2E's host process
gets an additional 120 seconds beyond the guest watchdog).

Use a trusted results parent. Each run requires a new private directory and
cannot write under `results/formal-965`. `inputs.json`, `command.json`,
`guest.log` (serial only), `qemu.log` (host diagnostics), and the receipts
record the invocation and checks. Swap images are retained even on failure or
interruption; remove a run directory only after QEMU has exited. The runner
never reuses or unlinks a disk.

Offline checks (no QEMU required): `python3 -B bench/macos/tcg_test.py`.
