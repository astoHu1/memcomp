#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
MACOS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$MACOS_DIR/../.." && pwd)
BREW_PREFIX=${BREW_PREFIX:-/opt/homebrew}
export PATH="$BREW_PREFIX/opt/make/libexec/gnubin:$BREW_PREFIX/opt/coreutils/libexec/gnubin:$BREW_PREFIX/opt/gnu-sed/libexec/gnubin:$BREW_PREFIX/opt/findutils/libexec/gnubin:$BREW_PREFIX/opt/grep/libexec/gnubin:$BREW_PREFIX/opt/gnu-tar/libexec/gnubin:$BREW_PREFIX/opt/bison/bin:$BREW_PREFIX/opt/flex/bin:$BREW_PREFIX/opt/llvm/bin:$PATH"
LLVM_BIN="$BREW_PREFIX/opt/llvm/bin"
TREE=${TREE:-$ROOT/kernel-src/linux-mainline}
OUT=${OUT:-$ROOT/build/macos-arm64}
export TMPDIR="$OUT/tmp"
mkdir -p "$TMPDIR"
KMAKE=(make -C "$TREE" O="$OUT" ARCH=arm64 LLVM="$LLVM_BIN/" LLVM_IAS=1
    LD="$BREW_PREFIX/opt/lld/bin/ld.lld"
    HOSTCC=/usr/bin/clang HOSTCXX=/usr/bin/clang++
    "HOSTCFLAGS=-O2 -I$MACOS_DIR/compat -include $MACOS_DIR/compat/host.h" "HOSTLDFLAGS="
    KBUILD_BUILD_USER=memcomp KBUILD_BUILD_HOST=macos)
