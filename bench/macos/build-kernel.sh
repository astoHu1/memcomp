#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# Native Darwin host tools, arm64 Linux target. Use a dedicated case-sensitive OUT.
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: $0 TREE OUT" >&2
    exit 2
fi
dir=$(cd "$(dirname "$0")" && pwd -P)
tree=$(cd "$1" && pwd -P)
brew=${BREW_PREFIX:-$(brew --prefix)}
export PATH="$brew/opt/make/libexec/gnubin:$brew/opt/coreutils/libexec/gnubin:$brew/opt/gnu-sed/libexec/gnubin:$brew/opt/findutils/libexec/gnubin:$brew/opt/bison/bin:$brew/opt/flex/bin:$brew/opt/llvm/bin:$PATH"
out=$(realpath -m -- "$2")
case "$out/" in "$tree/"*) echo 'OUT must be outside TREE' >&2; exit 1;; esac
export TMPDIR="$out/tmp"
inc="$out/host-include"
mkdir -p "$inc" "$TMPDIR"
if [ "$inc" -ef "$out/HOST-INCLUDE" ] ||
   [ "$tree/include/uapi/linux/netfilter/xt_MARK.h" -ef "$tree/include/uapi/linux/netfilter/xt_mark.h" ]; then
    echo 'TREE and OUT must be on case-sensitive filesystems for headers_install' >&2
    exit 1
fi

# Expose only elf.h, never musl's libc headers, to the Darwin compiler.
zig_libc="$brew/opt/zig/lib/zig/libc"
elf="$zig_libc/include/generic-musl/elf.h"
[ -f "$elf" ] || elf="$zig_libc/musl/include/elf.h"
if [ ! -f "$elf" ]; then
    echo 'no usable ELF header; install Homebrew zig' >&2
    exit 1
fi
ln -sfn "$elf" "$inc/elf.h"
echo "Host elf.h: $elf"

kmake=(make -C "$tree" O="$out" ARCH=arm64 LLVM="$brew/opt/llvm/bin/"
    LD="$brew/opt/lld/bin/ld.lld" HOSTCC=/usr/bin/clang HOSTCXX=/usr/bin/clang++
    "HOSTCFLAGS=-I$inc -I$dir/compat" "HOSTCFLAGS_file2alias.o=-include $dir/compat/host.h")
"${kmake[@]}" KCONFIG_ALLCONFIG="$dir/arm64-vz.config" allnoconfig
while IFS= read -r option; do
    case "$option" in
        CONFIG_*=*) ;;
        *) continue ;;
    esac
    grep -Fxq "$option" "$out/.config" || { echo "configuration rejected: $option" >&2; exit 1; }
done < "$dir/arm64-vz.config"

"${kmake[@]}" -j"${JOBS:-8}" Image
"${kmake[@]}" -j"${JOBS:-8}" INSTALL_HDR_PATH="$out/uapi" headers_install
test -s "$out/uapi/include/linux/userfaultfd.h"
file "$out/arch/arm64/boot/Image"
printf 'UAPI: %s\n' "$out/uapi/include"
