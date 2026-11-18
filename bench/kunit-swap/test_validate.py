# SPDX-License-Identifier: GPL-2.0
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from validate import PREFIX, SUITES


def console(live_status="pass", details=False):
    def phase(stage, name, status):
        cases = SUITES[name]
        yield f"BEGIN {stage} {name}"
        for line in ("KTAP version 1", "1..1", "    KTAP version 1",
                     f"    # Subtest: {name}", "    1..5"):
            yield f"KTAP {line}"
        for i, case in enumerate(cases, 1):
            outcome = "not ok" if status == "fail" and i == 1 else "ok"
            directive = " # SKIP requires real swap" if status == "skip" else ""
            if details and outcome == "not ok":
                for kind in ("EXPECTATION", "ASSERTION"):
                    yield f"KTAP     # {case}: {kind} FAILED at mm/test.h:42"
                    yield "KTAP     Expected ret == VM_FAULT_NOPAGE, but"
                    yield "KTAP         ret == 260 (0x104)"
                    yield "KTAP         VM_FAULT_NOPAGE == 256 (0x100)"
            yield f"KTAP     {outcome} {i} {case}{directive}"
        verdict = "not ok" if status == "fail" else "ok"
        yield f"KTAP {verdict} 1 {name}"
        yield f"END {stage} {name}"

    records = []
    for name in SUITES:
        records.extend(phase("boot", name, "skip"))
    records.append("SWAPON /dev/vda")
    for name in SUITES:
        records.append(f"RUN {name}")
        records.extend(phase("live", name, live_status))
    records.extend(("SWAPOFF /dev/vda", "DONE"))
    return "\r\n".join(PREFIX + record for record in records) + "\r\n"


class ValidationTest(unittest.TestCase):
    def run_validator(self, log):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "console.log"
            path.write_text(log)
            return subprocess.run(
                [sys.executable, str(Path(__file__).with_name("validate.py")), str(path)],
                capture_output=True, text=True, check=False,
            )

    def test_verdicts(self):
        for status, code in (("pass", 0), ("skip", 4), ("fail", 1)):
            with self.subTest(status=status):
                result = self.run_validator(console(status))
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn(f"KUNIT_SWAP RESULT {status.upper()}", result.stdout)

    def test_multiline_assertions_count_failed_cases_once(self):
        result = self.run_validator(console("fail", details=True))
        self.assertEqual(result.returncode, 1)
        for suite in SUITES:
            self.assertIn(f"live {suite}: FAIL pass=4 skip=0 fail=1", result.stdout)
        self.assertIn("KUNIT_SWAP RESULT FAIL", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_assertion_details_do_not_hide_malformed_records(self):
        failed = console("fail", details=True)
        first = next(iter(SUITES.values()))[0]
        for log in (failed.replace("    not ok 1 " + first, "    ok 1 " + first, 1),
                    failed.replace(f"    # {first}: EXPECTATION", "    # wrong_case: EXPECTATION", 1),
                    failed.replace("        ret == 260 (0x104)", f"        ok 1 {first}", 1),
                    console().replace(f"    ok 1 {first}",
                                      f"    Expected ret == 0, but\r\n{PREFIX}KTAP     ok 1 {first}", 1)):
            result = self.run_validator(log)
            self.assertEqual(result.returncode, 1)
            self.assertIn("KUNIT_SWAP RESULT FAIL:", result.stderr)
            self.assertNotIn("live swap-memcomp-budget:", result.stdout)

    def test_incomplete_or_inconsistent_evidence(self):
        good = console()
        for log in (good.replace("SWAPON /dev/vda", "INVALID"),
                    good.replace("    1..5", "    1..4", 1),
                    good.replace("    ok 2 ", "    ok 1 "),
                    good.replace(f"{PREFIX}DONE\r\n", ""),
                    good + f"{PREFIX}DONE\n", good + "WARNING: CPU: 0\n"):
            with self.subTest(log=log[-80:]):
                result = self.run_validator(log)
                self.assertEqual(result.returncode, 1)
                self.assertIn("KUNIT_SWAP RESULT FAIL:", result.stderr)


if __name__ == "__main__":
    unittest.main()
