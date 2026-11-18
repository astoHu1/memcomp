#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/env.sh"
mkdir -p "$ROOT/build/macos-vz/module-cache"
xcrun swiftc -O -module-cache-path "$ROOT/build/macos-vz/module-cache" \
    "$MACOS_DIR/vz-runner.swift" -o "$ROOT/build/macos-vz/vz-runner"
codesign --force --sign - --entitlements "$MACOS_DIR/virtualization.entitlements" \
    "$ROOT/build/macos-vz/vz-runner"
