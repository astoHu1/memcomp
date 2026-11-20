#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# Use the same TREE and OUT as build.sh. For CONFIG_TEST_HMM=m, build.sh
# must have produced OUT/lib/test_hmm.ko before packing the initramfs.
# INITRAMFS_OUT and KERNEL_UAPI_OUT can override the staging directories.
set -euo pipefail
source "$(dirname "$0")/env.sh"
SYSROOT="$ROOT/build/aarch64-musl"
DEST=${INITRAMFS_OUT:-$ROOT/build/macos-userspace}
UAPI=${KERNEL_UAPI_OUT:-$ROOT/build/kernel-uapi}
RUNTIME="$ROOT/build/compiler-rt/lib/linux/libclang_rt.builtins-aarch64.a"
MM="$TREE/tools/testing/selftests/mm"
hmm_module=
if [ -f "$OUT/.config" ] && grep -qx 'CONFIG_TEST_HMM=m' "$OUT/.config"; then
    hmm_module="$OUT/lib/test_hmm.ko"
    if [ ! -f "$hmm_module" ] || [ ! -s "$hmm_module" ]; then
        echo "missing $hmm_module; run build.sh with the same TREE and OUT" >&2
        exit 1
    fi
    module_info=$(file -b "$hmm_module")
    case "$module_info" in
        'ELF 64-bit LSB relocatable, ARM aarch64,'*) ;;
        *) echo "unexpected format for $hmm_module: $module_info" >&2; exit 1 ;;
    esac
fi

mkdir -p "$DEST" "$UAPI"
UAPI=$(cd "$UAPI" && pwd)
[ -f "$SYSROOT/lib/libc.a" ] || bash "$MACOS_DIR/build-userspace.sh"
[ -f "$RUNTIME" ] || bash "$MACOS_DIR/build-runtime.sh"
"${KMAKE[@]}" headers_install INSTALL_HDR_PATH="$UAPI"
CC=("$LLVM_BIN/clang" --target=aarch64-linux-musl --sysroot="$SYSROOT"
    --ld-path="$BREW_PREFIX/opt/lld/bin/ld.lld" -static -O2 -g
    -mno-outline-atomics -nostdlib -L"$SYSROOT/lib"
    -isystem "$UAPI/include" "$SYSROOT/lib/crt1.o" "$SYSROOT/lib/crti.o")
LIBS=(-Wl,--start-group -lc "$RUNTIME" -Wl,--end-group "$SYSROOT/lib/crtn.o")
"${CC[@]}" -Wall -Wextra -Werror -pthread \
    "$ROOT/bench/e2e/mcwb_init.c" "${LIBS[@]}" -o "$DEST/mcwb_init"
"${CC[@]}" "$MM/memcomp.c" "${LIBS[@]}" -o "$DEST/memcomp_selftest"
"${CC[@]}" -std=c11 -Wall -Wextra -Wpedantic \
    "$ROOT/bench/ksm_memcomp/ksm_memcomp_test.c" "${LIBS[@]}" -o "$DEST/ksm_memcomp_test"
if [ -n "$hmm_module" ]; then
    # The HMM and GUP test ioctl headers are private to the kernel tree.
    "${CC[@]}" -pthread -I"$TREE" "$MM/hmm-tests.c" "${LIBS[@]}" -o "$DEST/hmm-tests"
    if [ ! "$hmm_module" -ef "$DEST/test_hmm.ko" ]; then
        cp -- "$hmm_module" "$DEST/test_hmm.ko"
    fi
fi
/usr/bin/clang -O2 "$TREE/usr/gen_init_cpio.c" -o "$DEST/gen_init_cpio"
(
    cd "$DEST"
    executables=(mcwb_init memcomp_selftest ksm_memcomp_test)
    {
        printf '%s\n' \
            'dir /dev 0755 0 0' \
            'nod /dev/console 0600 0 0 c 5 1' \
            'dir /proc 0755 0 0' \
            'dir /sys 0755 0 0' \
            'dir /tmp 1777 0 0' \
            'file /init mcwb_init 0755 0 0' \
            'file /memcomp_selftest memcomp_selftest 0755 0 0' \
            'file /ksm_memcomp_test ksm_memcomp_test 0755 0 0'
        if [ -n "$hmm_module" ]; then
            printf '%s\n' 'file /hmm-tests hmm-tests 0755 0 0' \
                'file /test_hmm.ko test_hmm.ko 0644 0 0'
            executables+=(hmm-tests)
        fi
    } > initramfs.list
    ./gen_init_cpio initramfs.list | gzip -9 > initramfs.gz.tmp
    mv -- initramfs.gz.tmp initramfs.gz
    file "${executables[@]}"
    if [ -n "$hmm_module" ]; then
        shasum -a 256 test_hmm.ko
    fi
    shasum -a 256 "${executables[@]}" initramfs.gz
)
