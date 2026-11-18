#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# Pack the existing E2E init with the kernel build's native gen_init_cpio.
set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "usage: $0 OUT" >&2
    exit 2
fi
e2e=$(cd "$(dirname "$0")/../e2e" && pwd)
out=$(cd "$1" && pwd)
if [ ! -f "$out/.config" ] || [ ! -x "$out/usr/gen_init_cpio" ] ||
   [ ! -f "$out/uapi/include/linux/userfaultfd.h" ]; then
    echo "run build-kernel.sh first: $out" >&2
    exit 1
fi

work=$(mktemp -d "$out/.initramfs.XXXXXX")
trap 'rm -rf "$work"' EXIT
cd "$work"
soft_dirty=0
if grep -Fxq 'CONFIG_MEM_SOFT_DIRTY=y' "$out/.config"; then soft_dirty=1; fi
zig cc -target aarch64-linux-musl -static -O2 -s -Wall -Wextra -Werror -pthread \
    -DMCWB_SOFT_DIRTY="$soft_dirty" -I"$out/uapi/include" "$e2e/mcwb_init.c" -o init
"$out/usr/gen_init_cpio" - <<'LIST' | gzip -9 > initramfs.gz
dir /dev 0755 0 0
nod /dev/console 0600 0 0 c 5 1
dir /proc 0755 0 0
dir /sys 0755 0 0
dir /tmp 1777 0 0
file /init init 0755 0 0
LIST
mv initramfs.gz "$out/initramfs.gz"
