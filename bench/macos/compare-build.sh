#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Build only the original comparison userspace and native gen_init_cpio.
set -euo pipefail
umask 077
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BREW_PREFIX=${BREW_PREFIX:-/opt/homebrew}
LLVM_BIN="$BREW_PREFIX/opt/llvm/bin"
LLD="$BREW_PREFIX/opt/lld/bin/ld.lld"
TREE=${TREE:-$ROOT/kernel-src/linux-mainline}
SYSROOT=${MUSL_SYSROOT:-$ROOT/build/aarch64-musl}
UAPI=${KERNEL_UAPI:-$ROOT/build/kernel-uapi}/include
RUNTIME=${BUILTINS:-$ROOT/build/compiler-rt/lib/linux/libclang_rt.builtins-aarch64.a}
DEST=${1:-$ROOT/build/macos-compare/tools}

if [ "$#" -gt 1 ] || [ "$(uname -s)/$(uname -m)" != Darwin/arm64 ]; then
    echo "usage (Apple Silicon only): bash $0 [new-tools-directory]" >&2
    exit 2
fi
for prerequisite in "$LLVM_BIN/clang" "$LLD" "$TREE/usr/gen_init_cpio.c" \
    "$SYSROOT/lib/libc.a" "$SYSROOT/lib/crt1.o" "$SYSROOT/lib/crti.o" \
    "$SYSROOT/lib/crtn.o" "$UAPI/linux/fs.h" "$RUNTIME"; do
    [ -f "$prerequisite" ] || { echo "missing prerequisite: $prerequisite" >&2; exit 1; }
done
mkdir -p "$(dirname "$DEST")"
mkdir "$DEST"
DEST=$(cd "$DEST" && pwd)
mkdir "$DEST/tmp"
export TMPDIR="$DEST/tmp"
CC=("$LLVM_BIN/clang" --target=aarch64-linux-musl --sysroot="$SYSROOT"
    --ld-path="$LLD" -static -O2 -g -std=gnu11 -mno-outline-atomics
    -Wall -Wextra -Wformat=2 -Wshadow -Werror
    -nostdlib -isystem "$UAPI" -L"$SYSROOT/lib"
    "$SYSROOT/lib/crt1.o" "$SYSROOT/lib/crti.o")
LIBS=(-Wl,--start-group -lc "$RUNTIME" -Wl,--end-group "$SYSROOT/lib/crtn.o")
for tool in guest refault shmem app_switch; do
    "${CC[@]}" "$ROOT/bench/compare/$tool.c" "${LIBS[@]}" -o "$DEST/$tool"
done
/usr/bin/clang -O2 "$TREE/usr/gen_init_cpio.c" -o "$DEST/gen_init_cpio"
file "$DEST/guest" "$DEST/refault" "$DEST/shmem" "$DEST/app_switch" "$DEST/gen_init_cpio"
