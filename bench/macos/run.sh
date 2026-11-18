#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# run.sh [new-result-directory] [guest command-line words...]
set -euo pipefail
source "$(dirname "$0")/env.sh"
RESULT_ARG=${1:-}
if [ "$#" -gt 0 ]; then
    shift
fi
KERNEL="$OUT/arch/arm64/boot/Image"
INITRAMFS=${INITRAMFS:-$ROOT/build/macos-userspace/initramfs.gz}
RUNNER="$ROOT/build/macos-vz/vz-runner"
for input in "$KERNEL" "$OUT/.config" "$INITRAMFS" "$RUNNER"; do
    [ -f "$input" ] || { echo "missing $input" >&2; exit 1; }
done
VZ_MEMORY_MB=${MEMORY_MB:-1024}
VZ_CPUS=${CPUS:-4}
VZ_TIMEOUT=${TIMEOUT:-900}
VZ_SWAP_MB=${SWAP_MB:-2048}
KUNIT_ONLY=${KUNIT_ONLY:-0}
[[ $VZ_SWAP_MB =~ ^(0|[1-9][0-9]*)$ ]] || {
    echo 'SWAP_MB must be an integer MiB (0 for no disk)' >&2
    exit 2
}
KUNIT=0
if rg -qx 'CONFIG_MEMCOMP_KUNIT_TEST=y' "$OUT/.config"; then
    KUNIT=1
fi
GUEST_ARGS=("$@")
if [ "$KUNIT_ONLY" = 1 ]; then
    [ "$KUNIT" = 1 ] && [ "$#" -eq 0 ] || {
        echo 'KUNIT_ONLY=1 requires a memcomp KUnit kernel and no guest arguments' >&2
        exit 2
    }
    VZ_SWAP_MB=0
elif [ "$#" -eq 0 ]; then
    GUEST_ARGS=(RUN_KSM=1 RUN_SELFTEST=1)
    E2E_SKIPS=${E2E_SKIPS:-3}
    if [ "$VZ_SWAP_MB" != 0 ]; then
        if [[ $VZ_MEMORY_MB != 1024 || $VZ_SWAP_MB != 2048 ]]; then
            echo 'default VZ writeback profile uses MEMORY_MB=1024 and SWAP_MB=2048; pass explicit guest arguments for other sizes' >&2
            exit 2
        fi
        # Create natural reclaim pressure instead of releasing the fill early.
        GUEST_ARGS+=(FILL_MB=1792 PRESS_MB=64 CHUNK_MB=4 PAGEOUT_LAG=0
            PACE_US=3000 THP=never WB_WAIT_S=10 CYCLES=1 CONC_SECS=3
            SWAPOFF_MB=1792 SWAPOFF_PRESS_MB=64 TEST_TIMEOUT=820 HEARTBEAT_S=15)
    fi
fi
BOOT_ARGS=()
SELFTEST=0
REQUIRE_SWAPOFF=1
KSM=0
HMM=0
for arg in ${GUEST_ARGS[@]+"${GUEST_ARGS[@]}"}; do
    case "$arg" in
        ''|*[!A-Za-z0-9_.:,/@%+=-]*)
            echo "invalid guest kernel command-line token: $arg" >&2
            exit 2 ;;
        kunit.enable=0|kunit.enable=1) KUNIT=${arg#*=}; continue ;;
        RUN_SELFTEST=0|RUN_SELFTEST=1) SELFTEST=${arg#*=} ;;
        RUN_KSM=0|RUN_KSM=1) KSM=${arg#*=} ;;
        RUN_HMM=0|RUN_HMM=1) HMM=${arg#*=} ;;
        RUN_SELFTEST=*|RUN_KSM=*|RUN_HMM=*)
            echo "expected a boolean guest argument: $arg" >&2; exit 2 ;;
        SKIP_SWAPOFF=1) REQUIRE_SWAPOFF=0 ;;
        SKIP_SWAPOFF=0) REQUIRE_SWAPOFF=1 ;;
        NO_SWAP=1)
            [ "$VZ_SWAP_MB" = 0 ] || {
                echo 'use SWAP_MB=0 for NO_SWAP=1' >&2; exit 2;
            }
            continue ;;
        NO_SWAP=*|kunit.enable=*|kunit_shutdown=*|kunit.filter*|memcomp_selftest.allow_swapoff=*|console=*|rdinit=*|init=*|panic=*)
            echo "guest boot argument is controlled by this launcher: $arg" >&2
            exit 2 ;;
    esac
    BOOT_ARGS+=("$arg")
done
if [ "$KUNIT" = 1 ] && ! rg -qx 'CONFIG_MEMCOMP_KUNIT_TEST=y' "$OUT/.config"; then
    echo 'kunit.enable=1 requires a memcomp KUnit kernel' >&2
    exit 2
fi
if [ "$KUNIT_ONLY" = 1 ]; then
    BOOT_ARGS+=(kunit_shutdown=poweroff)
elif [ "$VZ_SWAP_MB" = 0 ]; then
    BOOT_ARGS+=(NO_SWAP=1)
fi
CMDLINE="console=hvc0 rdinit=/init panic=-1 kunit.enable=$KUNIT ${BOOT_ARGS[*]}"
"$RUNNER" --check
if [ -n "$RESULT_ARG" ]; then
    parent=$(cd -P -- "$(dirname -- "$RESULT_ARG")" && pwd)
    case "$parent/" in
        "$ROOT/results/formal-965/"*) echo 'cannot write inside published results' >&2; exit 2 ;;
    esac
    mkdir -- "$RESULT_ARG"
    RESULT=$(cd -P -- "$RESULT_ARG" && pwd)
else
    mkdir -p "$ROOT/results/macos-vz"
    RESULT=$(mktemp -d "$ROOT/results/macos-vz/run.XXXXXXXX")
fi
DISK=-
if [ "$VZ_SWAP_MB" != 0 ]; then
    # Only this newly created private image is exposed to guest swapon/swapoff.
    DISK="$RESULT/swap.raw"
    (umask 077; truncate -s "${VZ_SWAP_MB}M" "$DISK")
fi
touch "$RESULT/console.log"
# HEAD-to-worktree diffs include both staged and unstaged tracked changes.
git -C "$TREE" diff --no-ext-diff --no-textconv --binary HEAD -- > "$RESULT/kernel-dirty.diff"
git -C "$TREE" status --short --untracked-files=all > "$RESULT/kernel-status.txt"
git -C "$ROOT" diff --no-ext-diff --no-textconv --binary HEAD -- bench/e2e bench/macos \
    > "$RESULT/workspace-dirty.diff"
git -C "$ROOT" status --short --untracked-files=all -- bench/e2e bench/macos \
    > "$RESULT/workspace-status.txt"
# Hash untracked build helpers too; git diff does not include new files.
(
    cd "$ROOT"
    shasum -a 256 bench/e2e/mcwb_init.c bench/e2e/*.h \
        bench/macos/*.sh bench/macos/*.config bench/macos/*.swift \
        bench/macos/*.entitlements bench/macos/compat/*.h \
        bench/macos/validate-log.py \
        bench/ksm_memcomp/ksm_memcomp_test.c
    shasum -a 256 "$TREE/tools/testing/selftests/mm/memcomp.c" \
        "$TREE/usr/gen_init_cpio.c" "$ROOT/kernel-src/musl/include/elf.h"
) > "$RESULT/source-sha256.txt"
{
    date -u '+utc=%Y-%m-%dT%H:%M:%SZ'
    uname -a
    git -C "$TREE" rev-parse HEAD 'HEAD^{tree}'
    printf 'workspace_head=%s\n' "$(git -C "$ROOT" rev-parse HEAD)"
    printf 'source_snapshot=run-time\n'
    shasum -a 256 "$KERNEL" "$OUT/.config" "$INITRAMFS" "$RUNNER"
    shasum -a 256 "$RESULT/kernel-dirty.diff" "$RESULT/kernel-status.txt" \
        "$RESULT/workspace-dirty.diff" "$RESULT/workspace-status.txt" \
        "$RESULT/source-sha256.txt"
    printf 'memory_mb=%s\ncpus=%s\ntimeout_seconds=%s\nswap_mb=%s\n' \
        "$VZ_MEMORY_MB" "$VZ_CPUS" "$VZ_TIMEOUT" "$VZ_SWAP_MB"
    printf 'guest_cmdline=%s\n' "$CMDLINE"
} > "$RESULT/provenance.txt"
echo "VZ result directory: $RESULT"
set +e
"$RUNNER" "$KERNEL" "$INITRAMFS" "$DISK" "$RESULT/console.log" \
    "$VZ_MEMORY_MB" "$VZ_CPUS" "$VZ_TIMEOUT" "$CMDLINE" \
    "$RESULT/vz-info.txt" > "$RESULT/host.log" 2>&1
status=$?
set -e
cat "$RESULT/host.log"
[ "$status" -eq 0 ] || { echo "VZ failed: status=$status" >&2; exit "$status"; }
GATE=(python3 -B "$MACOS_DIR/validate-log.py" "$RESULT/console.log")
if [ "$KUNIT_ONLY" = 1 ]; then
    exec "${GATE[@]}" --mode kunit
fi
MODE=regular
if [ "$VZ_SWAP_MB" = 0 ]; then
    MODE=diskless
fi
GATE+=(--mode "$MODE" --run-selftest "$SELFTEST" --kunit-enabled "$KUNIT")
if [ "$MODE" = regular ] && [ "$REQUIRE_SWAPOFF" = 1 ]; then
    GATE+=(--require-swapoff)
fi
if [ "${REQUIRE_COMPLETE:-0}" = 1 ]; then
    GATE+=(--e2e-skips 0)
elif [ -n "${E2E_SKIPS:-}" ]; then
    GATE+=(--e2e-skips "$E2E_SKIPS")
fi
"${GATE[@]}"
if [ "$KSM" = 1 ]; then
    rg --crlf -xq '\[mcwb +[0-9]+\.[0-9]+\] KSM: END exit=0 pass=5 skip=0 fail=0 result=PASS' "$RESULT/console.log" || {
        echo 'requested KSM did not pass all five tests' >&2; exit 1;
    }
fi
if [ "$HMM" = 1 ]; then
    rg --crlf -xq '\[mcwb +[0-9]+\.[0-9]+\] HMM: END exit=0 pass=1 skip=0 fail=0 result=PASS' "$RESULT/console.log" || {
        echo 'requested filtered HMM test did not pass' >&2; exit 1;
    }
fi
