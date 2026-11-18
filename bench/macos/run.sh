#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# run.sh OUT: targeted natural-pressure smoke test on Apple silicon.
set -euo pipefail
umask 077

die() { echo "$*" >&2; exit 1; }
[[ $# == 1 && -n $1 ]] || { echo "usage: $0 OUT" >&2; exit 2; }
[[ $(uname -s):$(uname -m) == Darwin:arm64 ]] || die 'requires an Apple silicon Mac'
here=$(CDPATH= cd -- "$(dirname "$0")" && pwd -P)
out=$(CDPATH= cd -- "$1" && pwd -P)
kernel=$out/arch/arm64/boot/Image
initrd=$out/initramfs.gz
for input in "$kernel" "$initrd"; do
    [[ -f $input && -r $input && -s $input ]] || die "missing or empty input: $input"
done
# Probe the actual syntax used by validate-log.sh before booting the guest.
command -v rg >/dev/null || die 'validate-log.sh requires ripgrep (rg)'
tail -n +1 -- /dev/null >/dev/null || die 'put coreutils/libexec/gnubin on PATH for tail'

run=$(mktemp -d "$out/vz-run.XXXXXXXX")
cd "$run"
xcrun swiftc -target arm64-apple-macos12.0 -module-cache-path "$out/swift-cache" \
    "$here/vz-runner.swift" -o vz-runner
codesign --force --sign - --entitlements "$here/virtualization.entitlements" vz-runner
codesign --verify --strict vz-runner
echo "Guest console: $run/console.log" >&2
./vz-runner "$kernel" "$initrd" >console.log
rm -f swap.raw
exec "$here/../e2e/validate-log.sh" "$run/console.log"
