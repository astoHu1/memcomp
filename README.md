# memcomp

memcomp is a compressed backing store for Linux. When the kernel reclaims
an anonymous or shmem page, memcomp compresses it, keeps it in memory and
puts a token in the page table entry. A later fault decompresses the page
and maps it again. It uses the kernel's own compressors and zsmalloc, and
it needs no block device. If swap is configured, cold entries can be
written to it.

This repository holds RFC series v1 for Linux 6.12.23, the whitepaper,
the test and comparison code, the comparison results and an Android
phone test package. All results come from KVM guests; no phone results
are reported.

## Results in brief

The comparison ran 965 fresh guest boots under nested KVM, on anonymous
memory captured from eight Android applications. Each memcomp group was
paired with zram, zswap or zram writeback in the same round, and a row
counts as faster or slower only under a rule fixed before collection.

- Without real swap, single-page refault p99 was 10.7 µs against 20.7 µs
  for zram with lz4, and 22.0 µs against 27.3 µs with zstd.
- After writeback of an 832 MiB target, readback p50 was 11.9 µs against
  105.5 µs for zswap and 114.0 µs for zram writeback, with 70-76% fewer
  bytes written before readback.
- With real swap, single-page refault p99 was 4.8 times zram's with lz4
  and about 1.5 times zram's and zswap's with zstd. Sequential refault was
  30-31% slower than zswap.

The [whitepaper](whitepaper/memcomp-whitepaper.md) has the design, the
method, every decision and the limits. A shorter Chinese summary is in
[memcomp-post.zh.md](whitepaper/memcomp-post.zh.md). No phone results
are reported, because the variables on a phone could not be controlled;
all evaluation data come from KVM guests.

## Kernel series

| Branch | Base | Commits |
| --- | --- | --- |
| `memcomp/series-v1` | Linux 6.12.23 | 8 prerequisite commits, then the 16 series patches |
| `memcomp/android16-6.12` | ACK android16-6.12 | 9 prerequisite commits, then 18 patches: the 16 above and 2 ANDROID patches for vendor hooks and gki_defconfig |

The prerequisite commits are backports and small fixes that let the
series apply and run on 6.12. They are not part of the proposal and will
be dropped on a newer base.

The source trees identify the series independently of commit
signatures:

- mainline series v1: tree `06b510f011f66cb14e8a5dd4d00d72a3a1a0a7a5`
- ACK series v1: tree `d37a8c4e747f5803f954ec434f42b0d1f003d567`

The signed branch heads are `07038a987384` for mainline and
`713abbe0490a` for ACK. Before posting, seven header-comment lines were
removed; the earlier trees, `8f5623ffce75` and `4b45de7370f4`, differ only
by those lines.

[patches/v1/](patches/v1/) contains the mainline series exported with
`git format-patch` and its cover letter. The patches are signed off.

## Configuration

Build a 64-bit kernel with `CONFIG_SWAP=y` and `CONFIG_MEMCOMP=y`.
`CONFIG_ZRAM` and `CONFIG_ZSWAP` must be `n`; memcomp cannot run together
with either. `CONFIG_MEMCOMP_DEFAULT_COMPRESSOR` defaults to `zstd`, and
the boot option `memcomp.compressor=<name>` overrides it. Build the
chosen compressor into the kernel.

```sh
cat /sys/kernel/mm/memcomp/compressor
cat /sys/kernel/mm/memcomp/stat
```

`stat` prints logical, compressed and pool bytes. The first two include
entries already written to swap, so they are not a measure of RAM in use.
`CONFIG_MEMCOMP_DEBUG_STATS` adds further counters. The admin guide,
`Documentation/admin-guide/mm/memcomp.rst` in the series, describes the
options, the sysfs files and the known restrictions.

## Running the tests

Commands run from the kernel tree with the series applied, unless they
start with `bench/`, which is relative to this repository. Values in
angle brackets are paths you choose.

KUnit:

```sh
python3 tools/testing/kunit/kunit.py run --arch=x86_64 \
  --kunitconfig=mm/memcomp/tests/.kunitconfig \
  --build_dir=<kunit-out> 'memcomp*'
```

The MM selftest:

```sh
make -C tools/testing/selftests/mm OUTPUT=<selftest-out> \
  <selftest-out>/memcomp_selftest
```

Without arguments the selftest pages out and refaults its own memory and
checks swap-cache behavior. Its swapoff test runs only inside a QEMU guest
booted with `memcomp_selftest.allow_swapoff=1` and given
`--swap-device`, `--allow-swapoff` and `--swapon-flags`.

The end-to-end tests boot an x86_64 guest under KVM:

```sh
bench/e2e/build.sh <kernel-tree> <build-out>
bench/e2e/mkinitramfs.sh <kernel-tree>
bench/e2e/run.sh <build-out> <guest-log> "<guest test words>"
bench/e2e/validate-log.sh <guest-log>
```

Each script describes its arguments in its header. Run pressure and
swapoff tests only in disposable VMs.

## Rerunning the comparison

The comparison code is in [bench/compare/](bench/compare/), and
[matrix.md](bench/compare/matrix.md) describes the workloads, groups and
reporting rules. Kernels are built with `bench/compare/build.py`. The
whole matrix is planned and run with:

```sh
python3 -B bench/compare/matrix.py \
  --tree <source-tree> --binaries <static-tools> \
  --corpus-image <corpus-image> --kernels <kernel-artifacts> \
  --work <scratch> --dest <new-results> --host-volume <host-volume> \
  --timeout 1800
```

Without `--execute --allow-pressure` it only prints the schedule. The
captured application pages are private and are not published, so a rerun
needs a corpus of your own in the format described in
[bench/corpus/FORMAT.md](bench/corpus/FORMAT.md).

The figures and tables in the whitepaper are regenerated from the
published summary files:

```sh
python3 -B whitepaper/plot_results.py --summary results/formal-965
python3 -B whitepaper/plot_design.py
```

[results/](results/) holds the decision tables, the paired summaries, the
inputs of each round and a hash manifest of the raw guest records; its
README explains what is included and what is not.

## Phone test package

No phone results are reported. On a phone the variables that decide
the outcome could not be controlled: Android's low-memory killer takes
free swap into account, app behavior and kill decisions vary from boot
to boot, and thermal state drifts. A measured difference could not be
attributed to the kernel, so the evaluation uses KVM guests only.

[device/](device/) contains AArch64 userspace tools built on the host and
the instructions for an A/B comparison on a phone running ACK
android16-6.12: arm A runs memcomp, arm B runs stock zram. The kernel
images are built from the ACK branch; [device/BUILD.md](device/BUILD.md)
lists their sources and hashes. Nothing in the package flashes, reboots
or changes settings on a device by itself.

## Status

This is an RFC. The series is based on an LTS kernel and has to be
rebased onto mm-unstable before an upstream submission; Section 5 of the
whitepaper lists what else is missing. The code was written with AI
assistance and is reviewed and signed off by the maintainer.

## License

The code in this repository is licensed under the GNU General Public
License version 2; see [LICENSE](LICENSE). Each source file states its
terms in an SPDX header. One script whose hash is pinned by a published
result carries its header in a `.license` file beside it.

The whitepaper, the Chinese post, the evaluation tables and the figures
are available under GPL-2.0-only or the Creative Commons Attribution 4.0
International license, at your choice; see
[LICENSES/CC-BY-4.0](LICENSES/CC-BY-4.0). This follows the dual license
the Linux kernel uses for some of its documentation. The generated
tables and figures carry no header, so that regenerating them gives the
same bytes.
