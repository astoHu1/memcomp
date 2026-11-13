#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# build.sh <tree> <outdir> [extra.fragment...] : x86_64 bzImage with
# memcomp + swap for the KVM e2e harness.  Extra fragments are merged
# after x86.fragment (e.g. numa.fragment re-enables CONFIG_NUMA).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
TREE=$(cd "$1" && pwd)
mkdir -p "$2"
OUT=$(cd "$2" && pwd)
# Keep compiler scratch inside this invocation's output directory.
export TMPDIR="$OUT/tmp"
mkdir -p "$TMPDIR"
shift 2
EXTRA=()
for f in "$@"; do EXTRA+=("$(cd "$(dirname "$f")" && pwd)/$(basename "$f")"); done
if [ -n "${LLVM_BIN:-}" ]; then export PATH="$LLVM_BIN:$PATH"; fi
M=(make -C "$TREE" LLVM=1 LLVM_IAS=1 ARCH=x86_64 O="$OUT")
"${M[@]}" x86_64_defconfig
# merge_config creates temporary files in cwd; keep source inputs read-only.
(
    cd "$OUT"
    sh "$TREE/scripts/kconfig/merge_config.sh" -m -O "$OUT" \
        "$OUT/.config" "$HERE/x86.fragment" "${EXTRA[@]}"
)
"${M[@]}" olddefconfig
# Verify the options that the harness depends on survived olddefconfig.
fail=0
for opt in CONFIG_SWAP=y CONFIG_ZSMALLOC=y CONFIG_MEMCOMP=y CONFIG_KSM=y \
	   CONFIG_MEMCOMP_DEBUG_STATS=y CONFIG_CRYPTO_ZSTD=y \
	   CONFIG_LRU_GEN=y CONFIG_LRU_GEN_ENABLED=y CONFIG_VIRTIO_BLK=y \
	   CONFIG_VIRTIO_PCI=y CONFIG_DEBUG_VM=y CONFIG_BLK_DEV_INITRD=y \
	   CONFIG_DEVTMPFS=y CONFIG_SERIAL_8250_CONSOLE=y \
	   CONFIG_MEM_SOFT_DIRTY=y CONFIG_USERFAULTFD=y; do
	if ! rg -q -x "$opt" "$OUT/.config"; then
		echo "build.sh: missing $opt in $OUT/.config" >&2
		fail=1
	fi
done
for opt in CONFIG_ZRAM CONFIG_ZSWAP; do
	if rg -q "^$opt=[ym]" "$OUT/.config"; then
		echo "build.sh: $opt must be off" >&2
		fail=1
	fi
done
# Validate explicitly requested extra fragment values after dependency resolution.
for f in "${EXTRA[@]}"; do
    while IFS= read -r line; do
        case "$line" in
        CONFIG_*=*|"# CONFIG_"*" is not set")
            if ! rg -F -x -q -- "$line" "$OUT/.config"; then
                echo "build.sh: rejected fragment request: $line" >&2
                fail=1
            fi ;;
        esac
    done < "$f"
done
[ $fail = 0 ] || exit 1
"${M[@]}" -j"${JOBS:-4}" bzImage
ls -l "$OUT/arch/x86/boot/bzImage"
