#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/env.sh"
SYSROOT="$ROOT/build/aarch64-musl"
mkdir -p "$ROOT/build/musl"
cd "$ROOT/build/musl"
if [ ! -f config.mak ]; then
    "$ROOT/kernel-src/musl/configure" --target=aarch64-linux-musl \
        --prefix="$SYSROOT" --disable-shared --disable-wrapper \
        "CC=$LLVM_BIN/clang --target=aarch64-linux-musl --ld-path=$BREW_PREFIX/opt/lld/bin/ld.lld" \
        "AR=$LLVM_BIN/llvm-ar" "RANLIB=$LLVM_BIN/llvm-ranlib" LIBCC=
fi
make -j"${JOBS:-8}"
make install
