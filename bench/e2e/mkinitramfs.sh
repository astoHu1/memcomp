#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# mkinitramfs.sh [tree] : build the static /init and pack initramfs.gz
# (cpio newc: /init, /dev/console, /dev /proc /sys /tmp mountpoints).
# gen_init_cpio from the kernel tree is used so /dev/console exists
# without root; the tree only matters for that host tool.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
TREE=${1:-$HERE/tree_base}
DEST=${INITRAMFS_OUT:-$HERE}
if [ -n "${HMM_TEST_MODULE:-}" ] && [ -z "${HMM_TEST_BIN:-}" ]; then
    echo "HMM_TEST_MODULE requires HMM_TEST_BIN" >&2
    exit 1
fi
if [ -n "${KSM_BIN:-}" ] && [ ! -f "$KSM_BIN" ]; then
    echo "KSM_BIN does not name a regular file: $KSM_BIN" >&2
    exit 1
fi
mkdir -p "$DEST"
gcc -static -O2 -g -Wall -Wextra -Werror -pthread \
	-o "$DEST/mcwb_init" "$HERE/mcwb_init.c"
gcc -O2 -o "$DEST/gen_init_cpio" "$TREE/usr/gen_init_cpio.c"
cat > "$DEST/initramfs.list" <<LIST
dir /dev 0755 0 0
nod /dev/console 0600 0 0 c 5 1
dir /proc 0755 0 0
dir /sys 0755 0 0
dir /tmp 1777 0 0
file /init $DEST/mcwb_init 0755 0 0
LIST
if [ -n "${SELFTEST_BIN:-}" ]; then
    if [ ! "$SELFTEST_BIN" -ef "$DEST/memcomp_selftest" ]; then
        cp -- "$SELFTEST_BIN" "$DEST/memcomp_selftest"
    fi
    echo "file /memcomp_selftest $DEST/memcomp_selftest 0755 0 0" >> "$DEST/initramfs.list"
    sha256sum "$DEST/memcomp_selftest"
fi
if [ -n "${HMM_TEST_BIN:-}" ]; then
    if [ ! "$HMM_TEST_BIN" -ef "$DEST/hmm-tests" ]; then
        cp -- "$HMM_TEST_BIN" "$DEST/hmm-tests"
    fi
    echo "file /hmm-tests $DEST/hmm-tests 0755 0 0" >> "$DEST/initramfs.list"
    sha256sum "$DEST/hmm-tests"
fi
if [ -n "${HMM_TEST_MODULE:-}" ]; then
    if [ ! "$HMM_TEST_MODULE" -ef "$DEST/test_hmm.ko" ]; then
        cp -- "$HMM_TEST_MODULE" "$DEST/test_hmm.ko"
    fi
    echo "file /test_hmm.ko $DEST/test_hmm.ko 0755 0 0" >> "$DEST/initramfs.list"
    sha256sum "$DEST/test_hmm.ko"
fi
if [ -n "${KSM_BIN:-}" ]; then
    if [ ! "$KSM_BIN" -ef "$DEST/ksm_memcomp_test" ]; then
        cp -- "$KSM_BIN" "$DEST/ksm_memcomp_test"
    fi
    echo "file /ksm_memcomp_test $DEST/ksm_memcomp_test 0755 0 0" >> "$DEST/initramfs.list"
    sha256sum "$DEST/ksm_memcomp_test"
fi
"$DEST/gen_init_cpio" "$DEST/initramfs.list" | gzip -9 > "$DEST/initramfs.gz"
ls -l "$DEST/mcwb_init" "$DEST/initramfs.gz"
