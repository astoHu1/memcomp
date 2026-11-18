# arm64 writeback smoke test on macOS

Build the series-v1 Linux kernel on an Apple Silicon Mac, then boot it
directly with Apple's Virtualization framework. The guest uses the same
`mcwb_init.c` as the x86 KVM tests. No Linux build VM or root access is needed.

Install the Xcode Command Line Tools and Homebrew. Use a **case-sensitive**
volume for the kernel checkout and output; paths must not contain spaces.
From the root of this repository, with `work` on that volume:

```sh
brew install llvm lld make coreutils gnu-sed findutils bison flex ripgrep zig
work=/path/to/work
mkdir -p "$work"
git clone --depth=1 -b memcomp/series-v1 https://github.com/nakixii/linux.git "$work/linux"
bench/macos/build-kernel.sh "$work/linux" "$work/out"
bench/macos/mkinitramfs.sh "$work/out"
bench/macos/run.sh "$work/out"
```

Use a dedicated output directory. The kernel is `arch/arm64/boot/Image`
under that directory. This configuration tests real swap with the memcomp
KUnit fixtures **not compiled in**; it needs no changes to the kernel tree.

The runner gives the guest 4 vCPUs, 1 GiB of RAM and a new private 2 GiB
swap image per run. It prints the path to the guest console and checks it
with `bench/e2e/validate-log.sh`. A successful smoke run reports `RESULT:
PASS`, zero mismatches and nonzero writeback and swap readback counts.
Expect `coverage=PARTIAL`: this profile omits churn, retained mTHP, UFFD
stress, compat, cycles, concurrency and swapoff. It does not run KUnit or
the standalone MM selftest and is not a performance comparison.
