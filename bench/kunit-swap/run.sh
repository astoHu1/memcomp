#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
umask 077

die() { echo "kunit-swap: $*" >&2; exit 1; }
if [[ $# != 1 ]]; then
    echo "Usage: $0 --build-only | /path/to/arm64/Image" >&2
    exit 2
fi

dir=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$dir/../.." && pwd)
brew=${BREW_PREFIX:-/opt/homebrew}
sysroot="$root/build/aarch64-musl"
runtime="$root/build/compiler-rt/lib/linux/libclang_rt.builtins-aarch64.a"
cpio="$root/kernel-src/linux-mainline/usr/gen_init_cpio.c"
runner="$root/build/macos-vz/vz-runner"
clang="$brew/opt/llvm/bin/clang"
lld="$brew/opt/lld/bin/ld.lld"

if [[ $1 != --build-only ]]; then
    [[ -f $1 ]] || die "missing kernel Image: $1"
    image=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
    [[ $(file -b "$image") == *'Linux kernel ARM64 boot executable Image'* ]] ||
        die "expected an uncompressed ARM64 Image: $image"
    [[ -x $runner ]] || die "missing signed VZ runner: $runner"
fi
for input in "$clang" "$lld" "$sysroot/lib/libc.a" "$sysroot/lib/crt1.o" \
             "$sysroot/lib/crti.o" "$sysroot/lib/crtn.o" "$runtime" "$cpio"; do
    [[ -f $input ]] || die "missing existing build input: $input"
done

# Never install dependencies or write to shared build directories.
result=$(mktemp -d /tmp/memcomp-kunit-swap.XXXXXXXX)
echo "kunit-swap output: $result"
export TMPDIR="$result"
"$clang" --target=aarch64-linux-musl --sysroot="$sysroot" --ld-path="$lld" \
    -static -nostdlib -mno-outline-atomics -O2 -Wall -Wextra -Werror \
    "$sysroot/lib/crt1.o" "$sysroot/lib/crti.o" "$dir/init.c" \
    -Wl,--start-group "$sysroot/lib/libc.a" "$runtime" -Wl,--end-group \
    "$sysroot/lib/crtn.o" -o "$result/init"
/usr/bin/clang -O2 "$cpio" -o "$result/gen_init_cpio"
(
    cd "$result"
    printf '%s\n' 'dir /dev 0755 0 0' 'nod /dev/console 0600 0 0 c 5 1' \
        'dir /proc 0555 0 0' 'dir /sys 0555 0 0' 'file /init init 0755 0 0' \
        > initramfs.list
    ./gen_init_cpio initramfs.list | gzip -n -9 > initramfs.gz
)
[[ $1 != --build-only ]] || exit 0

# No caller-supplied disks, boot flags, or additional guest payloads.
disk="$result/swap.raw"
printf 'memcomp-kunit-swap-scratch-v1\n' > "$disk"
truncate -s 536870912 "$disk"
touch "$result/console.log"
cmdline='console=hvc0 rdinit=/init panic=-1 kunit.enable=1 kunit_swap_disposable=1'
timeout=${TIMEOUT:-180}
{
    printf 'utc=%s\ncmdline=%s\nmemory_mb=1024\ncpus=4\ntimeout_seconds=%s\nswap_mb=512\n' \
        "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$cmdline" "$timeout"
    shasum -a 256 "$image" "$result/initramfs.gz" "$runner" \
        "$dir/init.c" "$dir/run.sh" "$dir/validate.py"
} > "$result/provenance.txt"

status=0
"$runner" "$image" "$result/initramfs.gz" "$disk" "$result/console.log" \
    1024 4 "$timeout" "$cmdline" "$result/vz-info.txt" \
    > "$result/host.log" 2>&1 || status=$?
cat "$result/host.log"
[[ $status == 0 ]] || die "VZ runner failed (status $status); see $result"
python3 "$dir/validate.py" "$result/console.log" 2>&1 | tee "$result/verdict.txt"
