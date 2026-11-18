#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Validate the two fixed, non-parameterized 6.12 debugfs KTAP streams."""

import argparse
from pathlib import Path
import re
import sys


SUITES = {
    "swap-memcomp-budget": tuple(
        "memcomp_test_budget_swapped_" + case
        for case in ("pageout", "refault1", "refault4", "fork", "exit")
    ),
    "swap-memcomp-packed-budget": tuple(
        "memcomp_test_budget_packed_" + case
        for case in ("writeback", "refault1", "refault4", "fork_exit", "exit")
    ),
}
PREFIX = "KUNIT_SWAP "
DIAGNOSTIC = re.compile(
    r"WARNING:|BUG:|Oops:|Kernel panic|Call [Tt]race:|KASAN:|UBSAN:|"
    r"bad page|Out of memory:|oom-kill:|page allocation failure:"
)
CASE = re.compile(r"    (ok|not ok) ([1-9][0-9]*) (\w+)(?: (# SKIP)(?: .*)?)?")
ASSERTION = re.compile(r"    # (\w+): (?:ASSERTION|EXPECTATION) FAILED at \S+:\d+")
ASSERTION_VALUE = re.compile(r"        \S.* ==(?: .+)?")


def require(condition, error):
    if not condition:
        raise ValueError(error)


def check_ktap(lines, suite):
    cases = SUITES[suite]
    require(lines[:5] == ["KTAP version 1", "1..1", "    KTAP version 1",
                          f"    # Subtest: {suite}", f"    1..{len(cases)}"],
            f"{suite}: wrong KTAP header or plan")
    counts = {"pass": 0, "skip": 0, "fail": 0}
    assertion = expected = False
    for line in lines[5:-1]:
        index = sum(counts.values())
        failure = ASSERTION.fullmatch(line)
        if failure:
            require(index < len(cases) and failure[1] == cases[index],
                    f"{suite}: assertion for unexpected case {failure[1]!r}")
            assertion, expected = True, False
            continue
        # Debugfs stores the assertion body without the KTAP comment prefix.
        if assertion and not expected and line.startswith("    Expected "):
            expected = True
            continue
        if expected and ASSERTION_VALUE.fullmatch(line):
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = CASE.fullmatch(line)
        require(match and index < len(cases) and
                match[2] == str(index + 1) and match[3] == cases[index],
                f"{suite}: unexpected case record {line!r}")
        ok, skip = match[1] == "ok", match[4] is not None
        require(not (ok and assertion or not ok and skip),
                f"{suite}: contradictory case verdict {line!r}")
        counts[("skip" if skip else "pass") if ok else "fail"] += 1
        assertion = expected = False
    require(sum(counts.values()) == len(cases), f"{suite}: incomplete cases")
    # 6.12 debugfs omits the suite-level SKIP directive. The cases decide.
    verdict = "not ok" if counts["fail"] else "ok"
    require(lines[-1] == f"{verdict} 1 {suite}", f"{suite}: invalid terminal verdict")
    return counts


def validate(console):
    diagnostic = DIAGNOSTIC.search(console)
    require(not diagnostic, f"kernel diagnostic: {diagnostic[0]}" if diagnostic else "")
    records = iter(line[len(PREFIX):] for line in console.splitlines()
                   if line.startswith(PREFIX))

    def expect(want):
        got = next(records, None)
        require(got == want, f"expected {want!r}; got {got!r}")

    def read_phase(phase, suite):
        expect(f"BEGIN {phase} {suite}")
        end = f"END {phase} {suite}"
        lines = []
        for record in records:
            if record == end:
                return check_ktap(lines, suite)
            require(record.startswith("KTAP "), f"missing {end!r}; got {record!r}")
            lines.append(record[len("KTAP "):])
        raise ValueError(f"missing {end!r}")

    evidence = []
    for suite, cases in SUITES.items():
        counts = read_phase("boot", suite)
        require(counts["skip"] == len(cases), f"{suite}: expected all cases to skip before swapon")
        evidence.append(("boot", suite, counts))
    expect("SWAPON /dev/vda")
    for suite in SUITES:
        expect(f"RUN {suite}")
        evidence.append(("live", suite, read_phase("live", suite)))
    expect("SWAPOFF /dev/vda")
    expect("DONE")
    expect(None)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("console", type=Path)
    args = parser.parse_args()
    try:
        evidence = validate(args.console.read_text(errors="replace"))
    except (OSError, ValueError) as error:
        print(f"KUNIT_SWAP RESULT FAIL: {error}", file=sys.stderr)
        return 1

    failed = skipped = 0
    for phase, suite, counts in evidence:
        verdict = "FAIL" if counts["fail"] else "SKIP" if counts["skip"] else "PASS"
        print(f"{phase} {suite}: {verdict} " + " ".join(f"{k}={v}" for k, v in counts.items()))
        if phase == "live":
            failed += counts["fail"]
            skipped += counts["skip"]
    print(f"KUNIT_SWAP RESULT {'FAIL' if failed else 'SKIP' if skipped else 'PASS'}")
    return 1 if failed else 4 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
