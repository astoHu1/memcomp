# Phone test package: how the images and tools were built

Everything listed here was built on the host. Paths are relative to
the repository root; `<work>` is the maintainer's scratch directory and
is not published.

## Kernel sources

All three images are built from ACK android16-6.12 at
`738f95cebf1082e5acda1db41e2db7023a04a6e2`
("ANDROID: KVM: arm64: pviommu: Fix pinning for map_pages()").

The prerequisite layer has nine commits. They are separate from the
memcomp series and are described in the cover letter.

| Image | What it contains | Source head | Tree |
| --- | --- | --- | --- |
| Release | prerequisites + 18 series patches, production `gki_defconfig` | `c0547a607d7757c01061b2ce1ee2c741c9825b52` | `4b45de7370f4d25b667776d02e7675c774dd1d08` |
| A | Release source + 7 local boot patches | `3f30ddfa097c9576a19c90f96f5de706323cd3f6` | `a764a27936e3fda561b50e2f8219f7ed559645f6` |
| B | prerequisites + 9 local boot patches, no series | `9f9e23cb4c74a81c74e77096de0ed27a571ba35b` | `8ca1b4120edafb6cadfdb85a50a7c2644441115c` |

The Release image was built from the commits before signing. Signing
added Signed-off-by lines, a later rewrite removed the Assisted-by
trailers, and seven header-comment lines were removed before posting.
The signed ACK branch head `713abbe0490a37e4ff6717920833e826e80d946f` has tree
`d37a8c4e747f5803f954ec434f42b0d1f003d567`, which differs from the Release tree only by those
comment lines. The image was not rebuilt; line numbers in two headers
changed, so a rebuild would not be byte-identical.

The local boot patches make the image boot on the test phone. A and B
share these seven:

- module: Load all modules
- module: Always return success
- gki_defconfig: Enable CFI_PERMISSIVE
- ANDROID: GKI: Enable SYSTEM_DATA_VERIFICATION
- gki_defconfig, module: Build Rust properly
- module: Block qca_cld3_wcn7750
- arm64: defconfig: disable MTE and KASAN at boot

B adds "gki_defconfig: Built-in ZRAM" and "gki_defconfig: Enable
ZRAM_BACKEND_ZSTD" before the last one. The exported patches and their
SHA256 values are under
`results/2026-11-11-v4132-refreeze/device-preparation/patches/`.

B was built in an earlier round on an earlier copy of the prerequisite
layer, tip `b0311af4526c4281db8db8a66a852d4475f9a152`. The current layer,
tip `06aeaea4553f46f959a5bc1f79be3a382214e935`, differs from it in one
comment line of `mm/huge_memory.c`, which renamed the series in the
comment. Line numbers are unchanged, so the generated code is the same.
B was not rebuilt.

## Configuration

- **Release:** `MEMCOMP=y`, default compressor zstd, zram and zswap off.
  It has no local boot patches and is the image meant for publication.
- **A:** `MEMCOMP=y`, `ZRAM=n`.
- **B:** no memcomp, `ZRAM=y` with the LZ4, ZSTD and LZO backends. Its
  default compressor is lzo-rle, so a zstd comparison must select zstd
  explicitly and read the selection back.

Both A and B boot with `kasan=off arm64.nomte` on the command line.
`CONFIG_ARM64_MTE` and `CONFIG_KASAN` are still enabled. The complete
A-to-B configuration difference is
`results/2026-11-11-v4132-refreeze/device-build/device-ab-config.diff`
(SHA256 `9f312ae86302d15579ae0e3bb8d5764ee03046241d15e6546235bff9a1c85899`).
It covers memcomp, zram and one derived crypto option, `CRYPTO_ACOMP`.

## Build command

Each image was built serially in four steps, `gki_defconfig`,
`olddefconfig`, `prepare` and `Image`, plus `modules` for A and B:

```sh
make -C "$ACK" O="$OUT" ARCH=arm64 LLVM=1 LLVM_IAS=1 \
  CC="ccache clang" HOSTCC=clang HOSTCXX=clang++ -j4 \
  CLANG_AUTOFDO_PROFILE="$ACK/gki/aarch64/afdo/kernel.afdo" TARGET
```

- Toolchain: Android clang r536225 (clang 19.0.1), Android Rust
  1.82.0.p1 and bindgen 0.69.5. The SHA256 of each executable is in
  `inputs.json` next to the wrapper.
- AFDO profile SHA256:
  `aa7f2b8d4b25ed72371218265d6bb19b281a171552b29e38f8a5d9b52f1f1d5a`.
- Default warning level, no `W=1`; `CONFIG_WERROR=y`.
- Fixed build identity: user `nakixii`, host `memcomp`, version 1,
  timestamp `Thu, 5 Nov 2026 22:01:56 +0800`. The timestamp is a fixed
  input, not the build date.
- The wrapper and its pinned inputs are under
  `results/2026-11-13-v4137-scan-fixes/build-preparation/device-preparation/`.

The Release and A builds reused an existing output directory and
ccache. Their source trees are identical to the trees built and checked
in the previous round. Only the commit message of one prerequisite
commit changed, which changes the version string.

## Images

Release and A are in
`results/2026-11-13-v4137-scan-fixes/build-preparation/device-build/`;
B is in `results/2026-11-11-v4132-refreeze/device-build/B/`. Each
directory also holds the build logs, `effective.config` and
`summary.json`.

| Image | Bytes | SHA256 |
| --- | ---: | --- |
| Release | 40749568 | `b88baa72c0bc37f3c83066e9cf73aa9e66d8b97ad8a88c2493cbdb981532f121` |
| A | 40962560 | `4026d4fd73c24b399fcc777176224545fc48979e1026bf948f15e18458ad9312` |
| B | 40827392 | `66de4e1e93e32fec4ad7521f53e9aaf72ab2ababeca56dadcc338a0b90c96930` |

| Image | Version string suffix | Config SHA256 | vmlinux SHA256 |
| --- | --- | --- | --- |
| Release | `-gc0547a607d77` | `b30df665f1ebf131feb855ed4e5c9154f21ebbb1b02f8b2ba953a233ee830767` | `d5d551aa8ee9e19c78ac24f8adecee894a2b3fbe221cf8f968dbb8c5aff32409` |
| A | `-g3f30ddfa097c` | `b3b7a6a6e7feab30f3ba9e96f2d0d3689fb391e7b46e958bf97ab1066111e091` | `9bc431270c147e7a7f0df8173d07861fb239ce5f6479ddd9aca8868c18f73027` |
| B | `-g9f9e23cb4c74` | `96e39340dfe5b0e32ec7a61965b144fd7f7f6ad37a0f293edfe16da1b3f3bc94` | `9aed1d2c64db06a6e1076d8144c01854c12d5960d2054aec240030aa008a348d` |

The full version string of each is
`Linux version 6.12.23-4k-<suffix> (nakixii@memcomp) (Android (12833971,
+pgo, +bolt, +lto, +mlgo, based on r536225) clang version 19.0.1 ...
LLD 19.0.1) #1 SMP PREEMPT Thu, 5 Nov 2026 22:01:56 +0800`.

A and B each built 100 modules. `modules.sha256` in each image directory
lists them.

| Image | `modules.sha256` SHA256 |
| --- | --- |
| A | `1bede4458877dd5eafa317962d15febc70307d47f317b80b4f70cadc80a65bd0` |
| B | `343e03dad0ed51b7c6fcf3382d1c10c3a6d588175632ab1ac8af63f2f750c867` |

The modules themselves are not part of this package.

## Comparison with stock

Each image was compared with a stock build of the same configuration
without the series. The comparison covers the object list and the
compiler and linker diagnostics. All three passed: no diagnostic appears
that stock does not emit. memcomp adds `mm/memcomp.o`. Stock builds the
three zram objects, which the memcomp images disable. The records are
`build-preparation/diagnostics/comparison-{release,A}.json` in the
v4.137 results directory and
`device-stock-preparation/runs/B/comparison.json` in the v4.132 one.

Because of the ccache reuse, the Release and A comparisons show the
diagnostics the build emitted. A warning stored with a cached object is
not emitted again. Both trees were also checked in the previous round.

## Local paths in the images

The Release image contains 44 strings with home-directory paths, and A
and B 64 each. They are Rust panic locations that name the toolchain and the
kernel source directory. They reveal only the maintainer's user name.
Removing them needs a Rust path remapping flag and a rebuild of both the
image and the stock comparison.

## Userspace tools

The five tools in `bin/` are static AArch64 executables built with
aarch64-linux-gnu-gcc 13.3.0. `BUILD-mcbench.txt`, `BUILD-compare.txt`
and `BUILD-selftest.txt` record the inputs, flags, commands and output
hashes.

| Tool | Bytes | SHA256 |
| --- | ---: | --- |
| mcbench | 774152 | `c294614d3513912c26fb0c4e5b5e2ed7c2919d658c495840d09379935f235278` |
| refault | 848352 | `012b128819b34bc3ef42ffb27a48a664affecb82bbd75c83676a162f9e84d11a` |
| shmem | 847984 | `c6a5e5edb8ecaa26d8a5f27924c13ba699f2c0fbddaed8cfb9f22e24a8f70e5c` |
| app_switch | 777768 | `4f2f92bd7e5f90c7007b43f58fa2f258f0828335768ba9eb3225bebce7a99cf0` |
| memcomp_selftest | 847072 | `561c84b47f7a4b501f4e0d4a5a5617516860d27293cdfefd82aea9d67e2535c2` |

- mcbench: `-O2 -g0 -std=gnu11 -Wall -Wextra -Werror`, `-static`.
- refault, shmem and app_switch: the same flags plus `-Wformat=2
  -Wshadow`, built one at a time.
- memcomp_selftest: built from `tools/testing/selftests/mm` of the
  Release tree `4b45de7370f4d25b667776d02e7675c774dd1d08`, against arm64
  headers installed from the same tree.

None of the tools has an interpreter, a shared-library dependency or
debug sections. That does not show that they run under Android's libc
expectations or SELinux policy.

## Checking the package

```sh
cd device && sha256sum -c SHA256SUMS
```

`SHA256SUMS` covers the package files, the tools and the
`bench/compare` sources the tools are built from.
