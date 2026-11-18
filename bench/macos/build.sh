#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/env.sh"
REQUESTS="$OUT/requested.config"
fragments=("$MACOS_DIR/arm64-vz.config")
if [ -n "${CONFIG_FRAGMENT:-}" ]; then
    fragments+=("$CONFIG_FRAGMENT")
fi
# KCONFIG_ALLCONFIG accepts one input; the last request for each symbol wins.
awk '
    /^CONFIG_[A-Z0-9_]+=/ { key = $0; sub(/=.*/, "", key); opts[key] = $0 }
    /^# CONFIG_[A-Z0-9_]+ is not set$/ { opts[$2] = $0 }
    END { for (key in opts) print opts[key] }
' "${fragments[@]}" | LC_ALL=C sort > "$REQUESTS"
if [ "${1:-}" = "--configure" ] || [ ! -f "$OUT/.config" ]; then
    "${KMAKE[@]}" KCONFIG_ALLCONFIG="$REQUESTS" allnoconfig
fi
while IFS= read -r option; do
    case "$option" in
    '# CONFIG_'*' is not set'|CONFIG_*=n)
        symbol=${option#\# }
        symbol=${symbol%% *}
        symbol=${symbol%=n}
        if rg -q "^$symbol=[ym]$" "$OUT/.config"; then
            echo "required configuration rejected: $option" >&2
            exit 1
        fi ;;
    CONFIG_*=*)
        if ! rg -Fqx "$option" "$OUT/.config"; then
            echo "required configuration rejected: $option" >&2
            exit 1
        fi ;;
    esac
done < "$REQUESTS"
if rg -qx 'CONFIG_MEMCOMP=y' "$OUT/.config" &&
   rg -q '^CONFIG_(ZRAM|ZSWAP)=[ym]' "$OUT/.config"; then
    echo 'ZRAM and ZSWAP must be disabled when memcomp is enabled' >&2
    exit 1
fi
if [ "${CONFIG_ONLY:-0}" = 1 ]; then
    exit 0
fi
targets=(Image)
if rg -qx 'CONFIG_TEST_HMM=m' "$OUT/.config"; then
    targets+=(modules)
fi
"${KMAKE[@]}" -j"${JOBS:-8}" "${targets[@]}"
VMLINUX_INFO=$(file "$OUT/vmlinux")
IMAGE_INFO=$(file "$OUT/arch/arm64/boot/Image")
case "$VMLINUX_INFO" in
    *'ELF 64-bit LSB executable, ARM aarch64'*) ;;
    *) echo "unexpected vmlinux format: $VMLINUX_INFO" >&2; exit 1 ;;
esac
case "$IMAGE_INFO" in
    *'Linux kernel ARM64 boot executable Image, little-endian, 4K pages'*) ;;
    *) echo "unexpected Image format: $IMAGE_INFO" >&2; exit 1 ;;
esac
printf '%s\n' "$VMLINUX_INFO" "$IMAGE_INFO"
shasum -a 256 "$OUT/arch/arm64/boot/Image"
