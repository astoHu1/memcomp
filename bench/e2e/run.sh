#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# run.sh <outdir> <log> "<guest env words>" [memory MB] [swap MB; 0=none]
# A private scratch disk is removed only after actual QEMU death is proven.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$1
LOG=$2
ENVW=${3:-}
MEM=${4:-1024}
SWAPMB=${5:-2048}
KIMG=$OUT/arch/x86/boot/bzImage
INITRAMFS=${INITRAMFS:-$HERE/initramfs.gz}
[ -f "$KIMG" ] || { echo "no $KIMG" >&2; exit 1; }
[ -f "$INITRAMFS" ] || { echo "run mkinitramfs.sh first: $INITRAMFS" >&2; exit 1; }
WORK=${WORK_DIR:-$HOME/memcomp-work}
QEMU_HELPER=$HERE/tests/run_e2e_qemu.py
# Reject escaped scratch roots before even creating provenance temporaries.
python3 -B "$QEMU_HELPER" --check-work --work "$WORK"
TMPFILES=()
TMPIDENTS=()
cleanup_metadata()
{
    local i path identity
    for i in "${!TMPFILES[@]}"; do
        path=${TMPFILES[$i]}
        identity=$(stat -c '%d:%i' -- "$path" 2>/dev/null) || continue
        if [ ! -L "$path" ] && [ -f "$path" ] && [ "$identity" = "${TMPIDENTS[$i]}" ]; then
            rm -f -- "$path"  # Exact owned metadata inode, never a VM disk.
        else
            echo "retaining changed provenance temporary: $path" >&2
        fi
    done
}
new_metadata()
{
    local variable=$1 pattern=$2 path identity
    path=$(mktemp "$WORK/$pattern")
    identity=$(stat -c '%d:%i' -- "$path")
    TMPFILES+=("$path")
    TMPIDENTS+=("$identity")
    printf -v "$variable" '%s' "$path"
}
trap cleanup_metadata EXIT
# The relay observes this shell's death through pidfd and cancels via EOF.
# No shell signal/EXIT trap is ever allowed to unlink the QEMU disk.
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
mkdir -p "$WORK" "$(dirname "$LOG")"

digest_file()
{
    local path=$1 sum

    [ -f "$path" ] || {
        echo "missing provenance input: $path" >&2
        return 1
    }
    sum=$(sha256sum -- "$path") || {
        echo "cannot hash provenance input: $path" >&2
        return 1
    }
    printf '%s\n' "${sum%% *}"
}

provenance_file_line()
{
    local label=$1 path=$2 digest

    digest=$(digest_file "$path") || return 1
    printf 'provenance: %s=%s\n' "$label" "$digest"
}

{
    provenance_file_line mcwb_init_c "$HERE/mcwb_init.c"
    for source in memcomp_churn.h memcomp_compat.h memcomp_runtime.h memcomp_mincore_writeback.h \
                  memcomp_hmm_module.h \
                  run.sh validate-log.sh mkinitramfs.sh build.sh; do
        provenance_file_line "$source" "$HERE/$source"
    done
    provenance_file_line qemu_lifecycle_helper "$QEMU_HELPER"
    provenance_file_line initramfs "$INITRAMFS"
    if [ -n "${PROVENANCE_FILE:-}" ]; then
        provenance_file_line manifest "$PROVENANCE_FILE"
        cat -- "$PROVENANCE_FILE"
    fi
    echo "invocation: guest_env=$ENVW memory_mb=$MEM swap_mb=$SWAPMB smp=${SMP:-4} qemu_extra=${QEMU_EXTRA:-}"
    provenance_file_line kernel "$KIMG"
    provenance_file_line config "$OUT/.config"
    if [ -n "${KERNEL_TREE:-}" ]; then
        [ -d "$KERNEL_TREE" ] || {
            echo "invalid KERNEL_TREE: $KERNEL_TREE" >&2
            exit 1
        }
        kernel_head=$(git -C "$KERNEL_TREE" rev-parse --verify HEAD) || {
            echo "KERNEL_TREE is not a valid Git tree: $KERNEL_TREE" >&2
            exit 1
        }
        echo "provenance: kernel_head=$kernel_head"
        if [ -n "${KERNEL_SOURCE_DIGEST:-}" ]; then
            echo "provenance: kernel_source_digest=$KERNEL_SOURCE_DIGEST"
        else
            new_metadata diff_file .memcomp-kernel-diff.XXXXXXXX
            new_metadata untracked_file .memcomp-kernel-untracked.XXXXXXXX
            new_metadata untracked_sorted .memcomp-kernel-untracked-sorted.XXXXXXXX
            new_metadata untracked_digests .memcomp-kernel-untracked-digests.XXXXXXXX
            git -C "$KERNEL_TREE" diff --binary HEAD > "$diff_file" || {
                echo "cannot capture kernel diff: $KERNEL_TREE" >&2
                exit 1
            }
            provenance_file_line kernel_diff "$diff_file"
            git -C "$KERNEL_TREE" ls-files -o --exclude-standard -z > "$untracked_file" || {
                echo "cannot enumerate kernel untracked files: $KERNEL_TREE" >&2
                exit 1
            }
            sort -z "$untracked_file" > "$untracked_sorted" || {
                echo "cannot sort kernel untracked files: $KERNEL_TREE" >&2
                exit 1
            }
            : > "$untracked_digests"
            while IFS= read -r -d '' f; do
                digest=$(digest_file "$KERNEL_TREE/$f") || {
                    echo "cannot hash kernel untracked file: $KERNEL_TREE/$f" >&2
                    exit 1
                }
                printf '%s  %s\n' "$digest" "$f" >> "$untracked_digests"
            done < "$untracked_sorted"
            provenance_file_line kernel_untracked "$untracked_digests"
        fi
    else
        echo "provenance: kernel_head=${KERNEL_HEAD:-unavailable}"
        echo "provenance: kernel_source_digest=${KERNEL_SOURCE_DIGEST:-unavailable}"
    fi
} > "$LOG"
# Preserve the existing word-list/NUMA interface. The helper applies a
# fail-closed allowlist: no additional disk, networking or daemonization.
read -r -a EXTRA <<< "${QEMU_EXTRA:-}"
EXTRAARGS=()
for option in "${EXTRA[@]}"; do EXTRAARGS+=("--extra-word=$option"); done
QEMU=$(command -v qemu-system-x86_64) || {
    echo "qemu-system-x86_64 not found" >&2
    exit 1
}
start=$(date +%s)
# Ignore metadata even if its text resembles a timestamped guest RESULT.
printf '\nguest-console: begin\n' >> "$LOG"
CONSOLE_LINE=$(( $(wc -l < "$LOG") + 1 ))
set +e
python3 -B "$QEMU_HELPER" --work "$WORK" --qemu "$QEMU" \
    --kernel "$KIMG" --initrd "$INITRAMFS" --guest-env "$ENVW" \
    --memory "$MEM" --swap-mb "$SWAPMB" --smp "${SMP:-4}" \
    --accelerator "${ACCELERATOR:-kvm}" --timeout "${TIMEOUT:-900}" \
    "${EXTRAARGS[@]}" >> "$LOG" 2>&1 < /dev/null &
launcher=$!
wait "$launcher"
rc=$?
set -e
echo "qemu rc=$rc runtime=$(($(date +%s) - start))s" | tee -a "$LOG"
if [ "$rc" != 0 ]; then
    echo "QEMU/timeout failed: rc=$rc log=$LOG" >&2
    exit 1
fi
"$HERE/validate-log.sh" "$LOG" "$CONSOLE_LINE"
