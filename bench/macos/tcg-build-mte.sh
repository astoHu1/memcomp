#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# Package bench/mte's original static guest using the macOS cross toolchain.
set -euo pipefail
MACOS_DIR=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$MACOS_DIR/../.." && pwd)
if [ "$#" -ne 1 ]; then
    echo "usage: $0 <new-mte-initramfs-directory>" >&2
    exit 2
fi
TREE=${TREE:-$ROOT/kernel-src/linux-mainline}
BREW_PREFIX=${BREW_PREFIX:-/opt/homebrew}
LLVM_BIN="$BREW_PREFIX/opt/llvm/bin"
SYSROOT="$ROOT/build/aarch64-musl"
RUNTIME="$ROOT/build/compiler-rt/lib/linux/libclang_rt.builtins-aarch64.a"
for path in "$SYSROOT/lib/libc.a" "$RUNTIME" "$TREE/usr/gen_init_cpio.c"; do
    [ -f "$path" ] || { echo "missing build input: $path" >&2; exit 1; }
done
TREE=$(cd "$TREE" && pwd)

umask 077
dest=$1
case "$dest" in -*) dest="./$dest" ;; esac
mkdir "$dest"
cd "$dest"
"$LLVM_BIN/clang" --target=aarch64-linux-musl --sysroot="$SYSROOT" \
    --ld-path="$BREW_PREFIX/opt/lld/bin/ld.lld" -static -nostdlib \
    -O2 -g -std=gnu11 -Wall -Wextra -Werror -march=armv8.5-a+memtag \
    -mno-outline-atomics -L"$SYSROOT/lib" \
    "$SYSROOT/lib/crt1.o" "$SYSROOT/lib/crti.o" "$ROOT/bench/mte/mte_guest.c" \
    -Wl,--start-group -lc "$RUNTIME" -Wl,--end-group "$SYSROOT/lib/crtn.o" \
    -o mte_guest
elf=$("$LLVM_BIN/llvm-readelf" -h -l mte_guest)
if ! grep -q 'Machine:.*AArch64' <<< "$elf" || grep -q 'INTERP' <<< "$elf"; then
    echo 'expected a static AArch64 ELF guest' >&2
    exit 1
fi
/usr/bin/clang -O2 -o gen_init_cpio "$TREE/usr/gen_init_cpio.c"
printf '%s\n' \
    'dir /dev 0755 0 0' \
    'nod /dev/console 0600 0 0 c 5 1' \
    'dir /proc 0755 0 0' \
    'dir /sys 0755 0 0' \
    'dir /tmp 1777 0 0' \
    'file /init mte_guest 0755 0 0' > initramfs.list
./gen_init_cpio -t 0 initramfs.list | gzip -n -9 > initramfs.gz
shasum -a 256 mte_guest initramfs.gz
