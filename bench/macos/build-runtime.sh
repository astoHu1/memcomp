#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/env.sh"
DEST="$ROOT/build/compiler-rt"
cmake -S "$ROOT/kernel-src/llvm-project/compiler-rt/lib/builtins" -B "$DEST" \
    -G 'Unix Makefiles' -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=aarch64 \
    -DCMAKE_C_COMPILER="$LLVM_BIN/clang" \
    -DCMAKE_CXX_COMPILER="$LLVM_BIN/clang++" \
    -DCMAKE_ASM_COMPILER="$LLVM_BIN/clang" \
    -DCMAKE_C_COMPILER_TARGET=aarch64-linux-musl \
    -DCMAKE_CXX_COMPILER_TARGET=aarch64-linux-musl \
    -DCMAKE_ASM_COMPILER_TARGET=aarch64-linux-musl \
    -DCMAKE_SYSROOT="$ROOT/build/aarch64-musl" \
    -DCMAKE_C_FLAGS=-mno-outline-atomics \
    -DCMAKE_AR="$LLVM_BIN/llvm-ar" -DCMAKE_RANLIB="$LLVM_BIN/llvm-ranlib" \
    -DLLVM_CMAKE_DIR="$BREW_PREFIX/opt/llvm/lib/cmake/llvm" \
    -DCOMPILER_RT_DEFAULT_TARGET_ONLY=ON -DCOMPILER_RT_INCLUDE_TESTS=OFF
cmake --build "$DEST" -j "${JOBS:-8}"
