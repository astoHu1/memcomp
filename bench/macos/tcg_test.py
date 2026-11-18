#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Offline checks for the TCG launcher; never start QEMU."""
import argparse
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tcg_run import (PAGE, ROOT, SWAP, create_swap, qemu_command, result_directory,
                     run_case, validate_e2e)

sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))
from test_validate_log import console as e2e_console

MTE_LOG = (
    b"MTE_NUMA requested=n online=absent\n"
    b"MTE_SWAP requested=off active=off\n"
    b"MTE_CASE prot_mte=PASS tagged_write=PASS\n"
    b"MTE_COMPRESS stored_bytes_delta=1 pageout_delta=1 swapped_pages=64\n"
    b"MTE_PAGEOUT data=PASS tag=PASS pages=64\n"
    b"MTE_BAD_TAG probe=PASS signal=SIGSEGV si_code=SEGV_MTESERR\n"
    b"MTE_RESULT PASS\n"
)
E2E_MOVE = (b"[mcwb    1.50] split: move_pages of 5: ret=0 (ok) node1=5 node0=0 "
            b"-EBUSY=0 -EAGAIN=0 -ENOMEM=0 other=0\r\n")
E2E_RESULT = e2e_console(kunit=False, mm_skips=0).encode().replace(
    b"[mcwb  13.26] RESULT:",
    b"[mcwb  13.00] swapoff: +2 entries restored, +1 reads, +0 fail\n"
    b"[mcwb  13.01] COVERAGE: SKIP soft-dirty: unavailable\n[mcwb  13.26] RESULT:",
).replace(b"swapoff_entries=0", b"swapoff_entries=2").replace(
    b"coverage=COMPLETE skips=0", b"coverage=PARTIAL skips=1")


class TcgTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="tcg-test,")
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.args = argparse.Namespace(qemu="mock-qemu", kernel=self.work / "Image",
                                       initramfs=self.work / "initramfs.gz", cpus=4,
                                       memory_mb=1024, swap_mb=1, timeout=1)

    def test_command_uses_fixed_serial_and_disk(self):
        for case in ("no-swap", "swap-on", "e2e"):
            with self.subTest(case=case):
                command = qemu_command(self.args, case)
                self.assertIn("tcg,thread=multi", command)
                self.assertEqual(command[command.index("-nic") + 1], "none")
                cmdline = command[command.index("-append") + 1]
                self.assertIn("console=ttyAMA0", cmdline)
                if case == "e2e":
                    self.assertIn("EXPECT_NUMA=1 RUN_SELFTEST=1", cmdline)
                    self.assertIn("memcomp_selftest.allow_swapoff=1", cmdline)
                    self.assertIn("node,nodeid=1,memdev=mem1", command)
                    self.assertIn("cpu,node-id=1,socket-id=1", command)
                else:
                    self.assertIn("virt,gic-version=3,mte=on", command)
                    self.assertNotIn("-numa", command)
                if case == "no-swap":
                    self.assertNotIn("-drive", command)
                else:
                    self.assertIn(f"if=none,format=raw,file={SWAP},id=swapdisk", command)
                    self.assertIn("virtio-blk-device,drive=swapdisk", command)

    def test_swap_layout_and_exclusive_creation(self):
        stub = self.work / SWAP
        create_swap(stub, 1, formatted=True)
        self.assertEqual(stat.S_IMODE(stub.stat().st_mode), 0o600)
        self.assertEqual(stub.stat().st_size, 1 << 20)
        with stub.open("rb") as disk:
            head = disk.read(PAGE)
        self.assertEqual(struct.unpack_from("<III", head, 1024), (1, 255, 0))
        self.assertEqual(head[-10:], b"SWAPSPACE2")
        symlink = self.work / "link.raw"
        symlink.symlink_to(stub)
        for path in (stub, symlink):
            with self.assertRaises(FileExistsError):
                create_swap(path, 2, formatted=False)
        self.assertEqual(stub.stat().st_size, 1 << 20)
        blank = self.work / "blank.raw"
        create_swap(blank, 1, formatted=False)
        with blank.open("rb") as disk:
            self.assertEqual(disk.read(PAGE), bytes(PAGE))

    def test_new_private_result_directory(self):
        result = result_directory(self.work / "new")
        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o700)
        with self.assertRaises(FileExistsError):
            result_directory(result)
        (self.work / "published").symlink_to(ROOT / "results/formal-965")
        with self.assertRaisesRegex(ValueError, "formal-965"):
            result_directory(self.work / "published" / "new")

    def test_mte_pass_requires_successful_qemu_exit(self):
        def mock_run(command, *, stdout, timeout, **_kwargs):
            stdout.write(MTE_LOG)
            if fake_timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            return subprocess.CompletedProcess(command, 0)

        for fake_timeout in (False, True):
            work = self.work / f"mte-{fake_timeout}"
            with patch("tcg_run.subprocess.run", side_effect=mock_run):
                receipt = run_case(self.args, "no-swap", work)
            self.assertFalse(receipt["validation"]["failure"])
            self.assertEqual(receipt["status"], "FAIL" if fake_timeout else "PASS")
            self.assertFalse((work / SWAP).exists())

    def test_mte_swap_case_requires_writeback_marker(self):
        markers = MTE_LOG.replace(b"requested=off active=off", b"requested=on active=on")

        def mock_run(command, *, cwd, stdout, **_kwargs):
            with (cwd / SWAP).open("rb") as disk:
                self.assertEqual(disk.read(PAGE)[-10:], b"SWAPSPACE2")
            stdout.write(markers if not writeback else markers.replace(
                b"MTE_RESULT PASS",
                b"MTE_WRITEBACK payloads_delta=1 swapped_read_io_delta=1 data=PASS tag=PASS\n"
                b"MTE_RESULT PASS"))
            return subprocess.CompletedProcess(command, 0)

        for writeback in (False, True):
            work = self.work / f"swap-on-{writeback}"
            with patch("tcg_run.subprocess.run", side_effect=mock_run):
                receipt = run_case(self.args, "swap-on", work)
            self.assertEqual(receipt["status"], "PASS" if writeback else "FAIL")
            self.assertTrue((work / SWAP).exists())

    def test_e2e_needs_validator_and_numa_witness(self):
        console = self.work / "guest.log"
        with patch.dict("os.environ", {"REQUIRE_COMPLETE": "0"}):
            console.write_bytes(E2E_MOVE + E2E_RESULT)
            receipt = validate_e2e(console)
            self.assertTrue(receipt["passed"])
            self.assertIn("coverage=PARTIAL", receipt["result"])
            self.assertEqual(receipt["numa_migrated_pages"], 5)
            for log in (E2E_RESULT, E2E_MOVE, E2E_MOVE * 2 + E2E_RESULT,
                        E2E_MOVE + E2E_RESULT + b"Kernel panic\n"):
                console.write_bytes(log)
                self.assertFalse(validate_e2e(console)["passed"])
        with patch.dict("os.environ", {"REQUIRE_COMPLETE": "1"}):
            console.write_bytes(E2E_MOVE + E2E_RESULT)
            self.assertFalse(validate_e2e(console)["passed"])


if __name__ == "__main__":
    unittest.main()
