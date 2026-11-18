#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline VZ comparison host tests; no runner or guest is executed."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import compare
from run import DRAIN_FIELDS, PAGE_FIELDS, PAGE_PHASE_FIELDS, SAMPLE_FIELDS, corpus_touch_index


def w2_output(unavailable=False):
    records = ["record,sequence,worker,status,elapsed_ns,checked_words,"
               "init_ok,alive_before_cleanup,wait_kind"]
    records += [f"init,0,{i},ready,,,1,," for i in range(2)]
    records += [f"switch,{i + 1},{i % 2},ok,10,1048576,,," for i in range(4)]
    if unavailable:
        records[4] = "switch,2,1,unavailable,,,,,"
    records += [f"pid_exit,0,{i},reaped,,,,1,exit" for i in range(2)]
    return ["COMPARE_CSV " + record for record in records]


def disk_payload(path, payload):
    fields = f"bytes={len(payload)} sha256={hashlib.sha256(payload).hexdigest()} status=0"
    with path.open("r+b") as stream:
        stream.write(f"MEMCOMP_DATA_V1 {fields}\n".encode().ljust(512, b"\0"))
        stream.write(payload)
    return f"COMPARE_DATA {fields}\n"


def w4_fixture(backend, *, timed_out=False, missing_scope=False, counter_failure=None):
    # The real workload uses 212992 pages; exercise the same validators with
    # two complete touches and the original 1 GiB held/ramp sizes.
    command = ["/refault", "--pages", "2", "--order", "rand", "--cpu", "1",
               "--corpus", "/dev/vdc", "--corpus-sampling", "uniform-v411"]
    spec = compare.workload_spec("w4", command, smoke=True, backend=backend)
    descriptor = {"total_pages": 300000, "sources": [{"label": "app", "pages": 300000, "start_page": 0}]}
    compare.corpus_preflight(descriptor, spec, compare.MEMORY)
    spec["guest_memory_mib"] = compare.MEMORY

    fields = PAGE_FIELDS + SAMPLE_FIELDS + DRAIN_FIELDS + PAGE_PHASE_FIELDS + [
        "corpus_source", "corpus_page_index"]
    row = dict.fromkeys(fields, "0")
    row.update(workload="w4", content="corpus", seed="1", page_size="4096", order="rand",
               page_kind="real-unclassified", corpus_source="app", cpu_before="1", verify_ok="1",
               snapshot_present="1", present_before="1", snapshot_resident="1", resident_before="1",
               swap_type_before="-1", snapshot_swap_type="-1", classification="present",
               snapshot_classification="present", readahead_hit_ratio="", method="v4.6",
               writeback_source=compare.W4_SOURCES[backend], writeback_before="10", writeback_after="15",
               writeback_delta="5", writeback_wait_timed_out=str(int(timed_out)), pressure_bytes="939524096",
               writeback_wait_ns="300000000000" if timed_out else "7000000000",
               writeback_stable_polls="0" if timed_out else "3", pressure_ready="1", writeback_proven="1",
               writeback_explicit_idle=str(int(backend == "zram-writeback")))
    csv = ["COMPARE_CSV " + ",".join(fields)]
    for i in range(2):
        row.update(page_index=str(i), sequence=str(i), corpus_page_index=str(corpus_touch_index(spec, i)))
        csv.append("COMPARE_CSV " + ",".join(row[key] for key in fields))

    lines = []
    ns = 0

    def snapshot(phase, blocks):
        nonlocal ns
        ns += 10
        lines.append(f"COMPARE_SNAPSHOT monotonic_ns={ns} phase={phase}")
        for path, text in blocks.items():
            lines.extend([f"COMPARE_RAW_BEGIN monotonic_ns={ns} phase={phase} path={path}", *text,
                          f"COMPARE_RAW_END monotonic_ns={ns + 1} phase={phase} path={path}"])

    def counters(sectors, native):
        blocks = {"/proc/diskstats": [f"254 0 vda 0 0 0 0 1 0 {sectors} 0 0 0 0"]}
        if backend == "zram-writeback":
            blocks.update({"/sys/block/zram0/bd_stat": [f"0 0 {native}"],
                           "/sys/block/zram0/backing_dev": ["/dev/vda"]})
        else:
            blocks["/proc/swaps"] = ["Filename Type Size Used Priority", "/dev/vda partition 1 1 -2"]
            if backend == "zswap":
                blocks["/proc/vmstat"] = [f"zswpwb {native}"]
            else:
                blocks["/sys/kernel/mm/memcomp/packed_swap_events"] = [
                    f"writeback_payloads {native}", "writeback_gate_open 0"]
        return blocks

    snapshot("boot", {"/sys/kernel/mm/lru_gen/enabled": ["0x0007"]})
    snapshot("workload_start", {})
    snapshot("pressure_begin", counters(20, 10))
    snapshot("pressure_ready", {})
    terminal = "writeback_unstable" if timed_out else "writeback_settled"
    snapshot(terminal, counters(60, 15))
    snapshot("read_begin", counters(20 if counter_failure == "disk" else 60,
                                    10 if counter_failure == "native" else 15))
    snapshot("read_end", counters(60, 15))
    lines += csv + ["COMPARE_CGROUP path=/compare-w4 limits=default_unlimited",
                    "W4_CGROUP path=/compare-w4 limits=default_unlimited",
                    "PRESSURE pattern=real-corpus held_bytes=939524096 ramp_pattern=real-corpus ramp_bytes=268435456"]
    if not missing_scope:
        lines.append("PRESSURE ramp_released_bytes=268435456 held_bytes=939524096")
    lines.append("COMPARE_WORKLOAD_END pid=2 raw_status=0 status=0 stdout_eof=1"
                 " exec_confirmed=1 control_error=0 telemetry_error=0")
    snapshot("workload_end", {})
    return lines, spec


class CompareTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def test_transport(self):
        data = self.root / "data.raw"
        identity = compare.fresh_image(data, 4096)
        payload = b"COMPARE_CSV fixture\n"
        uart = disk_payload(data, payload)
        self.assertEqual(compare.read_payload(data, 4096, identity, uart), payload)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            compare.read_payload(data, 4096, identity, uart * 2)
        with self.assertRaisesRegex(ValueError, "inode"):
            compare.read_payload(data, 4096, (0, 0), uart)
        with data.open("r+b") as stream:
            stream.seek(4095)
            stream.write(b"x")
        with self.assertRaisesRegex(ValueError, "nonzero data"):
            compare.read_payload(data, 4096, identity, uart)
        with data.open("r+b") as stream:
            stream.seek(512)
            stream.write(b"x")
        with self.assertRaisesRegex(ValueError, "SHA256"):
            compare.read_payload(data, 4096, identity, uart)

    def test_profiles(self):
        for name in compare.PROFILES:
            with self.subTest(name=name):
                options = compare.fragment(name)
                for disabled in ("KUNIT", "KUNIT_DEFAULT_ENABLED", "KSM", "DEBUG_VM"):
                    self.assertEqual(options[f"CONFIG_{disabled}"], "n")
                self.assertEqual(options["CONFIG_MEMCOMP"], "y" if name == "memcomp" else "n")
                self.assertEqual(options["CONFIG_PSI"], "y")
                self.assertNotIn("CONFIG_SERIAL_8250_CONSOLE", options)
        self.assertEqual(compare.fragment("zswap-crosscheck"), compare.fragment("zswap"))
        self.assertEqual(compare.fragment("zswap-crosscheck")["CONFIG_ZSWAP"], "y")

    def test_w4_settings_and_short_corpus(self):
        args = argparse.Namespace(label="w4", backend="memcomp-swap", algorithm="zstd", timeout=None)
        self.assertEqual(compare.run_settings(args), (1800, 2048, 128))
        with self.assertRaisesRegex(ValueError, "360..3600"):
            compare.run_settings(argparse.Namespace(**{**vars(args), "timeout": 300}))
        with self.assertRaisesRegex(ValueError, "W4 requires"):
            compare.run_settings(argparse.Namespace(**{**vars(args), "backend": "zswap-crosscheck"}))
        command = compare.PRESETS["w4"] + ["--cpu", "1", "--corpus", "/dev/vdc",
                                          "--corpus-sampling", "uniform-v411"]
        spec = compare.workload_spec("w4", command, smoke=True, backend=args.backend)
        for pages in (507903, 507904):
            descriptor = {"total_pages": pages, "sources": [{"label": "app", "pages": pages, "start_page": 0}]}
            if pages == 507903:
                with self.assertRaises(ValueError):
                    compare.corpus_preflight(descriptor, spec, compare.MEMORY)
            else:
                compare.corpus_preflight(descriptor, spec, compare.MEMORY)
        self.assertEqual(spec["corpus_target_pages"], 212992)
        self.assertEqual(spec["corpus_extra_pages"], 294912)

    def test_w4_original_evidence_gates(self):
        cases = [(backend, {}, None) for backend in compare.W4_SOURCES] + [
            ("memcomp-swap", {"timed_out": True}, "did not stabilize"),
            ("zswap", {"missing_scope": True}, "scope or writeback"),
            ("zram-writeback", {"counter_failure": "disk"}, "scope or writeback"),
            ("zswap", {"counter_failure": "native"}, "scope or writeback"),
        ]
        log = self.root / "guest.log.gz"
        for backend, options, error in cases:
            with self.subTest(backend=backend, options=options):
                lines, spec = w4_fixture(backend, **options)
                with gzip.open(log, "wt") as stream:
                    stream.write("\n".join(lines + ["COMPARE_CONSOLE_BEGIN", "COMPARE_DONE status=0", ""]))
                status = {"eligible_for_comparison": False}
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        compare.validate_evidence(lines, log, spec, status)
                    self.assertFalse(status["smoke_accepted"])
                else:
                    self.assertEqual(len(compare.validate_evidence(lines, log, spec, status)), 3)
                    self.assertTrue(status["smoke_accepted"])
                self.assertFalse(status["eligible_for_comparison"])
                if options.get("timed_out"):
                    self.assertTrue(status["w4_diskstats_valid"])
                    self.assertTrue(status["writeback_latency_excluded"])
                    self.assertEqual(status["swap_write_bytes"], 40 * 512)
                    self.assertEqual(status["writeback_pressure_to_read_native_delta"], 5)

    def test_worker_loss_is_not_a_functional_pass(self):
        spec = {"label": "w2", "workers": 2, "switches": 4, "worker_bytes": 8388608}
        _, passed = compare.validate_workload(w2_output(), spec)
        self.assertTrue(passed["csv_integrity"])
        self.assertTrue(passed["w2_all_workers_ready_and_switches_verified"])
        _, lost = compare.validate_workload(w2_output(unavailable=True), spec)
        self.assertTrue(lost["csv_integrity"])
        self.assertFalse(lost["w2_all_workers_ready_and_switches_verified"])

    def test_launch_and_acceptance(self):
        config = self.root / "config"
        options = compare.fragment("zswap-crosscheck")
        options.update({"CONFIG_" + k: "y" for k in (
            "ARM64", "ARM64_4K_PAGES", "VIRTIO_CONSOLE", "MULTIUSER", "PROC_SYSCTL")})
        config.write_text("\n".join(f"{k}={v}" for k, v in options.items()))
        kernel = self.root / "Image"
        header = bytearray(64)
        header[24], header[56:60] = 2, b"ARMd"
        kernel.write_bytes(header)
        for name in (*compare.TOOLS, "runner", "corpus"):
            (self.root / name).touch()
        args = argparse.Namespace(backend="zswap-crosscheck", algorithm="zstd", timeout=120, label="w2",
                                  kernel_config=config, kernel=kernel, runner=self.root / "runner",
                                  binaries=self.root, corpus_image=self.root / "corpus", tree=self.root)
        descriptor = {"total_pages": 4096, "sources": [
            {"label": "a", "pages": 2048, "start_page": 0},
            {"label": "b", "pages": 2048, "start_page": 2048},
        ]}

        def stub_pack(_tree, _binaries, dest, backend, *_args):
            self.assertEqual(backend, "zswap-crosscheck")
            dest.mkdir()
            image = dest / "initramfs.gz"
            image.touch()
            return image

        def stub_runner(argv, **_kwargs):
            runner, image, initrd, primary, console, memory, cpus, timeout, cmdline, info, rw, ro = argv
            self.assertEqual((Path(runner), Path(image)), (args.runner, args.kernel))
            self.assertTrue(Path(initrd).is_file())
            self.assertTrue(Path(primary).is_file())
            self.assertEqual((memory, cpus, timeout), ("1024", "2", "140"))
            self.assertIn("console=hvc0 rdinit=/init", cmdline)
            self.assertIn("zswap.enabled=1 zswap.compressor=zstd zswap.zpool=zsmalloc", cmdline)
            self.assertEqual(ro, "ro:" + str(args.corpus_image))
            self.assertEqual(rw, "rw:" + str(Path(info).parent / "data.raw"))
            boot = ["COMPARE_RAW_BEGIN monotonic_ns=1 phase=boot path=/sys/kernel/mm/lru_gen/enabled",
                    "0x0007", "COMPARE_RAW_END monotonic_ns=2 phase=boot path=/sys/kernel/mm/lru_gen/enabled",
                    "COMPARE_SNAPSHOT monotonic_ns=3 phase=workload_start"]
            end = ["COMPARE_WORKLOAD_END pid=2 raw_status=0 status=0 stdout_eof=1"
                   " exec_confirmed=1 control_error=0 telemetry_error=0",
                   "COMPARE_SNAPSHOT monotonic_ns=4 phase=workload_end"]
            uart = disk_payload(Path(rw[3:]), ("\n".join(boot + w2_output() + end) + "\n").encode())
            Path(console).write_bytes((uart + "COMPARE_DONE status=0\n").replace("\n", "\r\n").encode())
            return subprocess.CompletedProcess(argv, self.runner_exit)

        for self.runner_exit in (0, 124):
            with (patch.object(compare, "ROOT", self.root),
                  patch.object(compare.platform, "system", return_value="Darwin"),
                  patch.object(compare.platform, "machine", return_value="arm64"),
                  patch.object(compare.os, "geteuid", return_value=1000),
                  patch.object(compare, "corpus_header", return_value=(descriptor, (), "fixture")),
                  patch.object(compare, "pack", side_effect=stub_pack),
                  patch.object(compare.subprocess, "run", side_effect=stub_runner)):
                if self.runner_exit:
                    with self.assertRaisesRegex(ValueError, "runner exited 124"):
                        compare.run(args)
                else:
                    compare.run(args)
        statuses = [json.loads(path.read_text()) for path in
                    self.root.glob("build/macos-compare/*/functional-status.json")]
        self.assertEqual({status["state"] for status in statuses}, {"passed", "failed"})
        self.assertTrue(all(not status["eligible_for_comparison"] for status in statuses))
        self.assertEqual(len(list(self.root.glob("build/macos-compare/*/workload.csv"))), 1)


if __name__ == "__main__":
    unittest.main()
