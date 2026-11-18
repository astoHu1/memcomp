# SPDX-License-Identifier: GPL-2.0

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


VALIDATOR = Path(__file__).resolve().parents[1] / "validate-log.py"

# Reduced from the slot-fix consoles: parameter groups, wholly skipped
# budget suites and their summaries, and a suite without a stats summary.
KTAP = """KTAP version 1
1..4
    KTAP version 1
    # Subtest: swap-memcomp-budget
    1..5
    ok 1 pageout # SKIP no swap at boot
    ok 2 refault1 # SKIP no swap at boot
    ok 3 refault4 # SKIP no swap at boot
    ok 4 fork # SKIP no swap at boot
    ok 5 exit # SKIP no swap at boot
# Totals: pass:0 fail:0 skip:5 total:5
ok 1 swap-memcomp-budget # SKIP
    KTAP version 1
    # Subtest: swap-memcomp-packed-budget
    1..5
    ok 1 pageout # SKIP no swap at boot
    ok 2 refault1 # SKIP no swap at boot
    ok 3 refault4 # SKIP no swap at boot
    ok 4 fork # SKIP no swap at boot
    ok 5 exit # SKIP no swap at boot
ok 2 swap-memcomp-packed-budget # SKIP
    KTAP version 1
    # Subtest: memcomp
    1..2
        KTAP version 1
        # Subtest: parameters
        ok 1 first
        ok 2 second # SKIP feature unavailable
    ok 1 parameters
    ok 2 stats
# Totals: pass:2 fail:0 skip:1 total:3
ok 3 memcomp
    KTAP version 1
    # Subtest: memcomp-codec-scope
    1..1
    ok 1 codec
ok 4 memcomp-codec-scope
"""

REGULAR = """[mcwb  12.62] writeback OK: 10 payloads written back, 8 swapped entries, swap used 1.0M, pool 10.0M
[mcwb  12.62] mincore writeback: A[3] vec0 token=0x831e unchanged; subsequent single-page read: content OK, io=1 cache=0 pswpin=1
"""
DISKLESS = """[mcwb  12.62] COVERAGE: SKIP (expected) writeback/refault/swapoff: NO_SWAP=1; resident integrity only
[mcwb  12.62] COVERAGE: SKIP (expected) mincore writeback: NO_SWAP=1; pool check retained
"""
MM_SKIP = "[mcwb   0.13] COVERAGE: SKIP SELFTEST: TAP reports skipped coverage (see captured results)\n"


def selftest(mode, skips):
    begin = "mode=swap swapoff_optin=1" if mode == "regular" else "mode=no-swap swapoff_optin=0"
    passed = 8 - skips
    rows = "".join(f"ok {n} case {n}\n" if n <= passed else f"ok {n} # SKIP case {n}\n" for n in range(1, 9))
    return (f"[mcwb   0.03] SELFTEST: BEGIN {begin}\n"
            "TAP version 13\n1..8\n" + rows +
            f"# Totals: pass:{passed} fail:0 xfail:0 xpass:0 skip:{skips} error:0\n"
            f"[mcwb   0.13] SELFTEST: END exit=0 pass={passed} skip={skips} fail=0 "
            f"result={'PARTIAL' if skips else 'PASS'}\n" + (MM_SKIP if skips else ""))


def console(mode="regular", kunit=True, mm_skips=2):
    diskless = mode == "diskless"
    e2e_skips = int(bool(mm_skips))
    return ((KTAP if kunit else "kunit: disabled\n") +
            "[mcwb   0.00] mcwb_init: 6.12.23+\n"
            "[mcwb   0.03] zero: required phases returned; consult FAIL records/final RESULT\n" +
            (selftest(mode, mm_skips) if mm_skips is not None else "") +
            (DISKLESS if diskless else REGULAR) +
            f"[mcwb  13.26] RESULT: PASS mismatches=0 writeback_payloads={0 if diskless else 10} "
            f"swapped_read_io={0 if diskless else 8} swapoff_entries=0 runtime=13s "
            f"coverage={'PARTIAL' if e2e_skips else 'COMPLETE'} skips={e2e_skips}\n"
            "[mcwb  13.41] mcwb_init: done, powering off\nreboot: Power down\n")


class ConsoleGateTests(unittest.TestCase):
    def run_gate(self, log, mode="regular", enabled=True, run_selftest=True, skips=1, opts=()):
        workload = []
        if mode != "kunit":
            workload += ["--kunit-enabled", str(int(enabled)), "--run-selftest", str(int(run_selftest))]
        if skips is not None:
            workload += ["--e2e-skips", str(skips)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "console.log"
            path.write_bytes(log.encode())
            return subprocess.run(
                [sys.executable, str(VALIDATOR), str(path), "--mode", mode, *workload, *opts],
                capture_output=True, text=True, check=False,
            )

    def gate(self, log, reason=None, enabled=True, **kwargs):
        opts = kwargs.pop("opts", ())
        if enabled:
            opts = ("--kunit-suite", "swap-memcomp-budget=5/5",
                    "--kunit-suite", "swap-memcomp-packed-budget=5/5",
                    "--kunit-suite", "memcomp=3/1", "--kunit-suite", "memcomp-codec-scope=1/0", *opts)
        result = self.run_gate(log, enabled=enabled, opts=opts, **kwargs)
        if reason is None:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(reason, result.stderr)
            self.assertEqual(result.stdout, "")
        return result.stdout

    def test_regular_separate_reports_with_crlf_and_printk_prefixes(self):
        log = "\r\n".join(f"<6>[    0.124] {line}" for line in console().splitlines()) + "\r\n"
        result = self.gate(log)
        self.assertIn("E2E (regular): RESULT: PASS", result)
        self.assertIn("KTAP: pass=3 skip=11 suites=4", result)
        self.assertIn("MM selftest: PARTIAL pass=6 skip=2", result)

    def test_diskless_and_disabled_modes(self):
        self.assertIn("MM selftest: PARTIAL pass=3 skip=5", self.gate(console("diskless", mm_skips=5), mode="diskless"))
        log = console("diskless", kunit=False, mm_skips=None)
        result = self.gate(log, mode="diskless", enabled=False, run_selftest=False, skips=0)
        self.assertIn("coverage=COMPLETE skips=0", result)
        self.assertEqual(result.count("NOT RUN (disabled)"), 2)
        self.gate(log, "requires writeback", enabled=False, run_selftest=False, skips=0)
        self.gate(log.replace(DISKLESS, DISKLESS.splitlines(keepends=True)[0]),
                  "diskless skip sentinels", mode="diskless", enabled=False, run_selftest=False, skips=0)

    def test_expected_positive_swapoff(self):
        opts = ("--require-swapoff",)
        self.gate(console(), "swapoff: +", opts=opts)
        log = console().replace(REGULAR, REGULAR + "[mcwb  13.00] swapoff: +4 entries restored, +4 reads, +0 fail\n")
        self.gate(log.replace("swapoff_entries=0", "swapoff_entries=4"), opts=opts)

    def test_e2e_rejects_invalid_result_accounting_and_missing_sentinels(self):
        for old, new, reason in (
            ("RESULT: PASS", "RESULT: FAIL", "E2E: RESULT: FAIL"),
            ("RESULT: PASS", "RESULT: PASS skips=1", "invalid result field"),
            ("skips=1", "skips=2", "expected skips=1, got skips=2"),
            ("coverage=PARTIAL", "coverage=COMPLETE", "expected coverage=PARTIAL"),
            ("mismatches=0", "mismatches=1", "data mismatches"),
            (MM_SKIP, "", "counted skip records"),
            ("zero: required phases returned;", "zero: started;", "zero: required phases returned"),
            ("mcwb_init: done, powering off", "incomplete", "mcwb_init: done"),
            ("reboot: Power down\n", "", "reboot: Power down"),
            ("mincore writeback:", "test started:", "mincore writeback:"),
            ("pswpin=1", "pswpin=0", "invalid writeback or mincore witness"),
        ):
            with self.subTest(old=old):
                self.gate(console().replace(old, new), reason)

    def test_auto_skips_still_requires_summary_to_match_records(self):
        self.assertIn("coverage=PARTIAL skips=1", self.gate(console(), skips=None))
        self.gate(console().replace("skips=1", "skips=2"), "found 1 counted skip records", skips=None)
        self.gate(console().replace("coverage=PARTIAL", "coverage=COMPLETE"),
                  "expected coverage=PARTIAL", skips=None)

    def test_tap_failures_reject_even_with_e2e_pass(self):
        for text in ("not ok 1 failure", "    not ok 1 failure # SKIP", "Bail out! abort",
                     "# Totals: pass:6 fail:0 xfail:0 xpass:1 skip:2 error:0"):
            with self.subTest(text=text):
                self.gate(console() + f"<4>[   14.00] {text}\n", "TAP/KTAP failure")

    def test_ktap_requires_matching_suite_body_plan_and_skip_counts(self):
        for old, new, reason in (
            (KTAP, "", "missing header or unclosed suite"),
            ("ok 1 first", "not ok 1 first", "TAP/KTAP failure"),
            ("        ok 2 second # SKIP feature unavailable\n", "", "totals disagree"),
            ("    ok 2 stats\n", "", "plan=2, saw 1"),
            ("ok 2 stats", "ok 1 stats", "expected result 2"),
            ("ok 3 memcomp\n", "ok 3 unrelated\n", "subtest result disagrees"),
            ("pass:2 fail:0 skip:1 total:3", "pass:3 fail:0 skip:0 total:3", "totals disagree"),
            ("    # Subtest: memcomp-codec-scope", "    # Subtest: other", "subtest result disagrees"),
        ):
            with self.subTest(old=old):
                self.gate(console().replace(old, new), reason)
        shortened = console().replace(KTAP, KTAP[:KTAP.index("    KTAP version 1\n    # Subtest: memcomp-codec-scope")]
                                      .replace("1..4", "1..3", 1))
        self.gate(shortened, "expected suite (total, skip) counts")
        self.gate(console(), "with KUnit disabled", enabled=False)
        wrong_skips = console().replace("ok 2 second # SKIP feature unavailable", "ok 2 second")
        wrong_skips = wrong_skips.replace("pass:2 fail:0 skip:1 total:3", "pass:3 fail:0 skip:0 total:3")
        self.gate(wrong_skips, "expected suite (total, skip) counts")

    def test_ktap_rejects_dropped_leaf_even_with_consistent_plan_and_totals(self):
        log = console().replace("    # Subtest: memcomp\n    1..2", "    # Subtest: memcomp\n    1..1")
        log = log.replace("    ok 2 stats\n", "")
        log = log.replace("pass:2 fail:0 skip:1 total:3", "pass:1 fail:0 skip:1 total:2")
        self.gate(log, "expected suite (total, skip) counts")

    def test_kunit_only_requires_complete_ktap_and_shutdown_without_init(self):
        kwargs = dict(mode="kunit", skips=None)
        log = KTAP + "reboot: Power down\n"
        self.assertIn("E2E: NOT RUN (KUnit-only)", self.gate(log, **kwargs))
        self.gate(KTAP, "reboot: Power down", **kwargs)
        self.gate(console("diskless", mm_skips=None), "unexpected init", **kwargs)
        self.gate(log.replace("ok 1 codec", "not ok 1 codec"), "TAP/KTAP failure", **kwargs)

    def test_mm_requires_envelope_full_plan_totals_and_exit(self):
        for old, new, reason in (
            (selftest("regular", 2), MM_SKIP, "SELFTEST: BEGIN"),
            ("TAP version 13\n1..8", "TAP version 13\n1..7", "expected TAP 13, plan 1..8"),
            ("ok 7 # SKIP case 7", "ok 6 # SKIP case 7", "expected TAP result 7"),
            ("ok 7 # SKIP case 7", "ok 7 case 7", "expected 2 skips"),
            ("xpass:0 skip:2 error:0", "xpass:0 skip:1 error:0", "TAP totals disagree"),
            ("SELFTEST: END exit=0", "SELFTEST: END exit=4", "wrapper END disagrees"),
            ("mode=swap swapoff_optin=1", "mode=no-swap swapoff_optin=0", "BEGIN mode/opt-in mismatch"),
        ):
            with self.subTest(old=old):
                self.gate(console().replace(old, new), reason)
        self.gate(console(), "unwrapped TAP header", run_selftest=False)
        self.gate(console(mm_skips=0), skips=0, opts=("--selftest-skips", "0"))

    def test_diagnostics_are_rejected_after_result(self):
        for text in ("WARNING: at workingset_refault", "Call trace:", "Kernel panic",
                     "Huh VM_FAULT_OOM leaked", "pagefault_out_of_memory: 20 callbacks suppressed",
                     "memcomp-diag: actor=do_swap_page", "[mcwb  14.00] FAIL: unexpected",
                     "[mcwb  14.00] TEST CONFIG ERROR: counters missing", "[mcwb] FATAL signal 11"):
            with self.subTest(text=text):
                self.gate(console() + text + "\n", "diagnostic")


if __name__ == "__main__":
    unittest.main()
