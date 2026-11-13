#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# validate-log.sh <log> [console-first-line]: guest result and diagnostics.
# Exit zero means integrity PASS, not necessarily complete coverage.
set -euo pipefail
LOG=$1
[ -f "$LOG" ] || { echo "missing guest log: $LOG" >&2; exit 1; }
# Provenance/caller environment and nested TAP must never count as a result.
FIRST=${2:-1}
if [[ ! $FIRST =~ ^[1-9][0-9]*$ ]]; then
    echo "invalid console first line: $FIRST" >&2
    exit 1
fi
console=$(tail -n +"$FIRST" -- "$LOG")
PREFIX='^\[mcwb[[:space:]]+[0-9]+\.[0-9]+\] RESULT:'
count=$(rg -c "$PREFIX" <<< "$console" || true)
if [ "$count" != 1 ]; then
    echo "expected one guest RESULT, found ${count:-0}: $LOG" >&2
    exit 1
fi
result=$(rg "$PREFIX" <<< "$console")
if rg -q '^\[mcwb[[:space:]]+[0-9]+\.[0-9]+\][[:space:]]+FAIL:' <<< "$console"; then
    echo "timestamped guest failure in console: $LOG" >&2
    exit 1
fi
if ! rg -q 'RESULT: PASS([[:space:]]|$)' <<< "$result"; then
    echo "guest did not pass: $result" >&2
    exit 1
fi
coverage_count=$(rg -o 'coverage=(COMPLETE|PARTIAL)' <<< "$result" | wc -l)
skips_count=$(rg -o 'skips=[0-9]+' <<< "$result" | wc -l)
if [ "$coverage_count" -ne 1 ] || [ "$skips_count" -ne 1 ]; then
    echo "duplicate or missing coverage/skips fields: $result" >&2
    exit 1
fi
coverage=$(rg -o 'coverage=(COMPLETE|PARTIAL)' <<< "$result" | cut -d= -f2)
skips=$(rg -o 'skips=[0-9]+' <<< "$result" | cut -d= -f2)
# Validate the qualification rather than upgrading PARTIAL to COMPLETE.
if [ "$coverage" = COMPLETE ] && [ "$skips" = 0 ]; then
    :
elif [ "$coverage" = PARTIAL ] && [ "$skips" -gt 0 ]; then
    if [ "${REQUIRE_COMPLETE:-0}" = 1 ]; then
        echo "complete coverage required: $result" >&2
        exit 1
    fi
else
    echo "missing or inconsistent coverage: $result" >&2
    exit 1
fi
if rg -q 'WARNING:|BUG:|Oops:|bad page|Kernel panic|general protection|Call Trace|KASAN:' <<< "$console"; then
    echo "kernel diagnostic in guest log: $LOG" >&2
    exit 1
fi
printf '%s\n' "$result"
