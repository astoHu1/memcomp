#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Gate a single VZ guest console, independently of the runner's exit status."""

import argparse
import re
import sys
from dataclasses import dataclass, field


KUNIT_SUITES = {  # (total leaves, skipped leaves)
    "swap-memcomp-budget": (5, 5),
    "swap-memcomp-packed-budget": (5, 5),
    "memcomp": (484, 7),
    "memcomp-codec-scope": (1, 0),
}
DISKLESS_MARKERS = (
    "COVERAGE: SKIP (expected) writeback/refault/swapoff: NO_SWAP=1; resident integrity only",
    "COVERAGE: SKIP (expected) mincore writeback: NO_SWAP=1; pool check retained",
)
DIAGNOSTIC = re.compile(
    r"WARNING:|\bBUG:|Oops:|bad page|Kernel panic|general protection|Call Trace\b|"
    r"KASAN:|KCSAN:|UBSAN:|KFENCE:|Unable to handle kernel|Out of memory:|"
    r"oom-kill:|Huh VM_FAULT_OOM|pagefault_out_of_memory|memcomp-diag:|memcomp_debug_fault:",
    re.IGNORECASE,
)
PRINTK = re.compile(r"^(?:<[0-9]+>)?(?:\[\s*[0-9]+\.[0-9]+\] )?")
GUEST = re.compile(r"^\[mcwb[ \t]+[0-9]+\.[0-9]+\][ \t]+(.*)$")
RESULT = re.compile(r"ok[ \t]+([0-9]+)(?:[ \t]+(.*))?")
PLAN = re.compile(r"1\.\.([0-9]+)")
KTAP_TOTALS = re.compile(r"# Totals: pass:([0-9]+) fail:0 skip:([0-9]+) total:([0-9]+)")
TAP_TOTALS = re.compile(r"# Totals: pass:([0-9]+) fail:0 xfail:0 xpass:0 skip:([0-9]+) error:0")


class InvalidLog(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise InvalidLog(reason)


def one(records, prefix, gate):
    matches = [(n, text) for n, text in records if text.startswith(prefix)]
    require(len(matches) == 1, f"{gate}: expected one {prefix!r} record, found {len(matches)}")
    return matches[0]


def read_console(lines):
    guest, protocol, power_down = [], [], []
    for n, line in enumerate(lines, 1):
        line = PRINTK.sub("", line.rstrip(), count=1)
        match = GUEST.fullmatch(line)
        text = match[1] if match else line.lstrip()
        if DIAGNOSTIC.search(line) or re.match(
            r"(?:\[mcwb\]\s+FATAL\b|FAIL:|TIMEOUT\b|TEST CONFIG ERROR:)", text
        ):
            raise InvalidLog(f"diagnostic at line {n}: {text[:240]}")
        if re.match(r"(?:not[ \t]+ok\b|Bail out!)", text, re.IGNORECASE) or (
            re.match(r"# .+:\s*pass:", text) and
            re.search(r"\b(?:fail|xfail|xpass|error):0*[1-9][0-9]*\b", text)
        ):
            raise InvalidLog(f"TAP/KTAP failure at line {n}: {text[:240]}")
        if match:
            guest.append((n, text))
        elif text == "reboot: Power down":
            power_down.append((n, text))
        elif text.startswith(("TAP version", "KTAP version", "1..", "ok ", "ok\t", "# Subtest:", "# Totals:")):
            protocol.append((n, line))
    return guest, protocol, power_down


def validate_e2e(guest, power_down, mode, skips, swapoff):
    start, _ = one([(n, text) for n, text in guest if not text.startswith("mcwb_init: done")],
                   "mcwb_init: ", "E2E start")
    end, result = one(guest, "RESULT:", "E2E")
    done, text = one(guest, "mcwb_init: done", "E2E")
    off, _ = one(power_down, "reboot: Power down", "E2E")
    require(start < end < done < off and text == "mcwb_init: done, powering off",
            "E2E: result and shutdown sentinels out of order")
    body = [(n, text) for n, text in guest if start < n < end]
    one(body, "zero: required phases returned;", "E2E")

    status, _, fields_text = result[len("RESULT:"):].strip().partition(" ")
    require(status == "PASS", f"E2E: {result[:240]}")
    fields = {}
    for token in fields_text.split():
        name, sep, value = token.partition("=")
        require(sep and name and value and name not in fields, f"E2E: invalid result field {token!r}")
        fields[name] = value
    reported = fields.get("skips", "")
    require(re.fullmatch(r"0|[1-9][0-9]*", reported), "E2E: missing or invalid skips field")
    observed = int(reported)
    expected_coverage = "PARTIAL" if observed else "COMPLETE"
    require(fields.get("coverage") == expected_coverage,
            f"E2E: expected coverage={expected_coverage} for skips={observed}, got {result}")
    require(skips is None or skips == observed, f"E2E: expected skips={skips}, got skips={observed}")
    skip_rows = [n for n, text in guest if text.startswith("COVERAGE: SKIP ") and not
                 text.startswith("COVERAGE: SKIP (expected) ")]
    require(all(start < n < end for n in skip_rows), "E2E: skip record outside test body")
    require(len(skip_rows) == observed, f"E2E: skips={observed}, found {len(skip_rows)} counted skip records")
    counters = []
    for key in ("mismatches", "writeback_payloads", "swapped_read_io", "swapoff_entries"):
        value = fields.get(key, "")
        require(re.fullmatch(r"[0-9]+", value), f"E2E: missing or invalid {key}")
        counters.append(int(value))
    mismatches, writes, reads, restored = counters
    require(not mismatches, f"E2E: {mismatches} data mismatches")

    transport = [text for _, text in body if text.startswith(("writeback OK:", "mincore writeback:"))]
    diskless = [text for _, text in body if text.startswith((
        "COVERAGE: SKIP (expected) writeback/refault/swapoff:",
        "COVERAGE: SKIP (expected) mincore writeback:",
    ))]
    if mode == "diskless":
        require(not any((writes, reads, restored)) and not transport,
                "E2E: diskless run logged swap transport")
        require(sorted(diskless) == sorted(DISKLESS_MARKERS), "E2E: missing or invalid diskless skip sentinels")
    else:
        require(writes and reads and not diskless, "E2E: regular run requires writeback and swap reads")
        write_at, write = one(body, "writeback OK:", "E2E")
        witness_at, witness = one(body, "mincore writeback:", "E2E")
        require(write_at < witness_at and re.fullmatch(
            r"writeback OK: [1-9][0-9]* payloads written back, [1-9][0-9]* swapped entries, .*", write
        ) and re.fullmatch(
            r"mincore writeback: \S+ vec0 token=0x[0-9a-f]+ unchanged; subsequent single-page read: "
            r"content OK, io=1 cache=0 pswpin=[1-9][0-9]*", witness
        ), "E2E: invalid writeback or mincore witness")
    if swapoff:
        _, text = one(body, "swapoff: +", "E2E")
        require(restored and re.fullmatch(
            r"swapoff: \+[1-9][0-9]* entries restored, \+[1-9][0-9]* reads, \+0 fail", text
        ), "E2E: invalid swapoff witness")
    return start, end, result


def result_row(text):
    match = RESULT.fullmatch(text)
    require(match, f"invalid TAP/KTAP result: {text}")
    desc, _, directive = (match[2] or "").partition("#")
    skipped = bool(re.match(r"\s*SKIP(?:\s|$)", directive, re.IGNORECASE))
    require(not re.match(r"\s*TODO\b", directive, re.IGNORECASE), f"unexpected TODO/XPASS: {text}")
    return int(match[1]), re.sub(r"^-\s+", "", desc.strip()), skipped


@dataclass
class KtapBlock:
    indent: int
    name: str = ""
    plan: int = None
    rows: int = 0
    passed: int = 0
    skipped: int = 0
    totals: tuple = None
    suites: dict = field(default_factory=dict)

    def finish(self):
        # Older KUnit parameter groups omit their own plan (the suite does not).
        require(self.rows and (self.plan == self.rows or self.plan is None and self.indent > 4),
                f"KTAP: {self.name or 'root'} plan={self.plan}, saw {self.rows} results")
        if self.totals is not None:
            require(self.totals == (self.passed, self.skipped, self.passed + self.skipped),
                    f"KTAP: {self.name} totals disagree with result rows")


def validate_kunit(protocol, start, enabled, expected):
    boot = [(n, line) for n, line in protocol if n < start]
    require(not any(n >= start and line.lstrip().startswith("KTAP version") for n, line in protocol),
            "KTAP: unexpected header after guest init")
    if not enabled:
        require(not boot, "KTAP: test output present with KUnit disabled")
        return "KTAP: NOT RUN (disabled)"
    stack = []
    for n, line in boot:
        text = line.lstrip()
        indent = len(line) - len(text)
        try:
            if text == "KTAP version 1":
                require(indent == (stack[-1].indent + 4 if stack else 0), "misplaced or duplicate header")
                stack.append(KtapBlock(indent))
                continue
            require(stack, "missing KTAP version 1 header")
            block = stack[-1]
            if indent == block.indent and text.startswith("# Subtest:"):
                require(indent and not block.name and block.plan is None and not block.rows,
                        "misplaced or duplicate subtest name")
                block.name = text.split(":", 1)[1].strip()
                require(block.name, "empty subtest name")
            elif indent == block.indent and PLAN.fullmatch(text):
                require(block.plan is None, "duplicate plan")
                block.plan = int(PLAN.fullmatch(text)[1])
            elif indent == block.indent - 4 and text.startswith("# Totals:"):
                totals = KTAP_TOTALS.fullmatch(text)
                require(totals and block.totals is None, "invalid or duplicate totals")
                block.totals = tuple(map(int, totals.groups()))
            else:
                number, name, skipped = result_row(text)
                if indent == block.indent - 4:
                    block.finish()
                    require(name and name == block.name and skipped == (block.passed == 0),
                            f"subtest result disagrees with body: {name!r}")
                    child = stack.pop()
                    block = stack[-1]
                    block.passed += child.passed
                    block.skipped += child.skipped
                    if indent == 0:
                        require(name not in block.suites, f"duplicate suite {name!r}")
                        block.suites[name] = (child.passed + child.skipped, child.skipped)
                else:
                    require(indent == block.indent and indent, "result without matching suite body")
                    block.passed += not skipped
                    block.skipped += skipped
                block.rows += 1
                require(number == block.rows, f"expected result {block.rows}, got {number}")
        except InvalidLog as error:
            raise InvalidLog(f"KTAP line {n}: {error}") from None
    require(len(stack) == 1, "KTAP: missing header or unclosed suite")
    root = stack[0]
    root.finish()
    require(root.suites == expected, f"KTAP: expected suite (total, skip) counts {expected}, got {root.suites}")
    require(root.passed, "KTAP: no passing tests")
    return f"KTAP: pass={root.passed} skip={root.skipped} suites={len(root.suites)}"


def validate_selftest(guest, protocol, start, end, mode, enabled, expected_skips):
    sentinels = [(n, text) for n, text in guest if text.startswith("SELFTEST:")]
    skip_rows = [(n, text) for n, text in guest if text.startswith("COVERAGE: SKIP SELFTEST:")]
    # HMM is optional and uses a separate guest_binary capture; never count it as MM TAP.
    hmm_begin = [n for n, text in guest if text.startswith("HMM: BEGIN ")]
    hmm_end = [n for n, text in guest if text.startswith("HMM: END ")]
    captures = [(hmm_begin[0], hmm_end[0])] if len(hmm_begin) == len(hmm_end) == 1 else []
    if enabled and len(sentinels) == 2:
        captures.append((sentinels[0][0], sentinels[1][0]))
    require(not any(n >= start and line.lstrip().startswith("TAP version") and
                    not any(a < n < b for a, b in captures) for n, line in protocol),
            "MM selftest: unwrapped TAP header")
    if not enabled:
        require(not sentinels and not skip_rows, "MM selftest: output present with RUN_SELFTEST=0")
        return "MM selftest: NOT RUN (disabled)"
    begin, begin_text = one(sentinels, "SELFTEST: BEGIN", "MM selftest")
    finish, end_text = one(sentinels, "SELFTEST: END", "MM selftest")
    require(len(sentinels) == 2 and start < begin < finish < end,
            "MM selftest: invalid begin/end sentinels")
    guest_mode = "mode=no-swap swapoff_optin=0" if mode == "diskless" else "mode=swap swapoff_optin=1"
    require(begin_text == f"SELFTEST: BEGIN {guest_mode}", "MM selftest: BEGIN mode/opt-in mismatch")

    tap = [line for n, line in protocol if begin < n < finish]
    # /memcomp_selftest always runs the full eight-case plan in this harness.
    require(len(tap) == 11 and tap[:2] == ["TAP version 13", "1..8"],
            "MM selftest: expected TAP 13, plan 1..8, eight results and totals")
    skipped = 0
    for i, text in enumerate(tap[2:-1], 1):
        number, _, skip = result_row(text)
        require(number == i, f"MM selftest: expected TAP result {i}, got {number}")
        skipped += skip
    passed = 8 - skipped
    require(passed and skipped == expected_skips,
            f"MM selftest: expected {expected_skips} skips and some passes, got pass={passed} skip={skipped}")
    totals = TAP_TOTALS.fullmatch(tap[-1])
    require(totals and tuple(map(int, totals.groups())) == (passed, skipped),
            "MM selftest: TAP totals disagree with result rows")
    status = "PARTIAL" if skipped else "PASS"
    require(end_text == f"SELFTEST: END exit=0 pass={passed} skip={skipped} fail=0 result={status}",
            "MM selftest: wrapper END disagrees with TAP or exit != 0")
    require(len(skip_rows) == bool(skipped) and all(finish < n < end for n, _ in skip_rows),
            "MM selftest: E2E skip accounting disagrees with TAP")
    return f"MM selftest: {status} pass={passed} skip={skipped}"


def nonnegative(value):
    if not re.fullmatch(r"[0-9]+", value):
        raise argparse.ArgumentTypeError("expected a nonnegative integer")
    return int(value)


def suite_expectation(value):
    match = re.fullmatch(r"([A-Za-z0-9_.-]+)=([0-9]+)/([0-9]+)", value)
    if not match:
        raise argparse.ArgumentTypeError("expected NAME=TOTAL/SKIPS")
    name, count, skips = match.groups()
    total, skipped = int(count), int(skips)
    if not total or skipped > total:
        raise argparse.ArgumentTypeError("expected 0 <= SKIPS <= TOTAL and TOTAL > 0")
    return name, (total, skipped)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("console", help="raw guest console.log (not host.log or provenance)")
    parser.add_argument("--mode", required=True, choices=("regular", "diskless", "kunit"),
                        help="regular swap, diskless E2E (NO_SWAP=1), or standalone boot KTAP")
    parser.add_argument("--run-selftest", choices=("0", "1"), help="effective RUN_SELFTEST (required for E2E)")
    parser.add_argument("--kunit-enabled", choices=("0", "1"), help="effective kunit.enable (required for E2E)")
    parser.add_argument("--e2e-skips", type=nonnegative,
                        help="exact counted E2E skips (0 requires COMPLETE); default: verify and report observed count")
    parser.add_argument("--selftest-skips", type=nonnegative, help="exact TAP skips (default regular=2, diskless=5)")
    parser.add_argument("--kunit-suite", metavar="NAME=TOTAL/SKIPS", action="append", type=suite_expectation,
                        help="override the exact KUnit suite set and leaf counts; repeat for each suite")
    parser.add_argument("--require-swapoff", action="store_true", help="require exercised E2E swapoff in regular mode")
    args = parser.parse_args()
    if args.mode == "kunit":
        if args.kunit_enabled == "0" or args.run_selftest == "1" or args.e2e_skips is not None:
            parser.error("--mode kunit expects enabled KUnit, no MM selftest and no E2E skips option")
    elif args.kunit_enabled is None or args.run_selftest is None:
        parser.error("E2E modes require --kunit-enabled and --run-selftest")
    suites = dict(args.kunit_suite) if args.kunit_suite else KUNIT_SUITES
    if args.kunit_suite and (args.kunit_enabled == "0" or len(suites) != len(args.kunit_suite)):
        parser.error("--kunit-suite requires enabled KUnit and unique suite names")
    if args.selftest_skips is not None and (args.run_selftest != "1" or args.selftest_skips >= 8):
        parser.error("--selftest-skips requires RUN_SELFTEST=1 and a count from 0 to 7")
    if args.require_swapoff and args.mode != "regular":
        parser.error("--require-swapoff requires regular mode")
    skips = args.selftest_skips
    if skips is None:
        skips = 5 if args.mode == "diskless" else 2
    try:
        with open(args.console, encoding="utf-8", errors="replace") as log:
            guest, protocol, power_down = read_console(log)
        if args.mode == "kunit":
            off, _ = one(power_down, "reboot: Power down", "KTAP-only")
            require(not guest and all(n < off for n, _ in protocol), "KTAP-only: unexpected init or trailing test output")
            e2e, selftest = "E2E: NOT RUN (KUnit-only)", "MM selftest: NOT RUN (KUnit-only)"
            kunit = validate_kunit(protocol, off, True, suites)
        else:
            start, end, result = validate_e2e(guest, power_down, args.mode, args.e2e_skips, args.require_swapoff)
            e2e = f"E2E ({args.mode}): {result}"
            kunit = validate_kunit(protocol, start, args.kunit_enabled == "1", suites)
            selftest = validate_selftest(guest, protocol, start, end, args.mode, args.run_selftest == "1", skips)
    except (OSError, ValueError) as error:
        print(f"{args.console}: {error}", file=sys.stderr)
        return 1
    print(f"{e2e}\n{kunit}\n{selftest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
