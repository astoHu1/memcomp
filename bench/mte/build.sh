#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# Build the standalone arm64 MTE initramfs guest.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
TREE=${1:?usage: $0 <kernel-tree> <output-dir>}
OUT=${2:?usage: $0 <kernel-tree> <output-dir>}
CC=${AARCH64_CC:-aarch64-linux-gnu-gcc}
HOST_CC=${HOST_CC:-cc}

mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)

"$CC" \
	-static -O2 -g -std=gnu11 -Wall -Wextra -Werror \
	-march=armv8.5-a+memtag \
	-o "$OUT/mte_guest" "$HERE/mte_guest.c"

"$HOST_CC" -O2 -Wall -Wextra -Wno-sign-compare \
	-o "$OUT/gen_init_cpio" "$TREE/usr/gen_init_cpio.c"

cat > "$OUT/initramfs.list" <<LIST
# The guest mounts proc/dev itself so the test has no host-side dependencies.
dir /dev 0755 0 0
nod /dev/console 0600 0 0 c 5 1
dir /proc 0755 0 0
dir /sys 0755 0 0
dir /tmp 1777 0 0
file /init $OUT/mte_guest 0755 0 0
LIST

"$OUT/gen_init_cpio" "$OUT/initramfs.list" | gzip -n -9 > "$OUT/initramfs.gz"

# Keep this check local to the new harness: a dynamic guest is not acceptable.
if readelf -l "$OUT/mte_guest" | rg -q 'INTERP'; then
	echo "build-mte: guest unexpectedly has an interpreter" >&2
	exit 1
fi

sha256sum "$OUT/mte_guest" "$OUT/initramfs.gz"
