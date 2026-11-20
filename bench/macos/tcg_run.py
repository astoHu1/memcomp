#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Run the original ARM64 guests using only QEMU TCG and private swap."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
E2E_VALIDATOR = HERE / "validate-log.py"
sys.path.insert(0, str(ROOT / "bench/mte"))
from run_mte import CASES, guest_markers, preflight_config, sha256

SWAP = "swap.raw"
PAGE = 4096
E2E_MIGRATION = re.compile(
    r"^\[mcwb +[0-9]+\.[0-9]+\] split: move_pages of (?P<pages>[1-9][0-9]*): "
    r"ret=0 \(ok\) node1=(?P=pages) node0=0 -EBUSY=0 -EAGAIN=0 -ENOMEM=0 other=0$",
    re.MULTILINE,
)


def write_json(path: Path, data: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")


def create_swap(path: Path, size_mb: int, *, formatted: bool) -> None:
    size = size_mb << 20
    if not 16 * PAGE <= size <= (1 << 32) * PAGE:
        raise ValueError("swap size is outside the 4 KiB swap header range")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as disk:
        disk.truncate(size)
        if formatted:
            # Same little-endian Linux swap v1 header as mcwb_init's setup_swap().
            page = bytearray(PAGE)
            struct.pack_into("<III", page, 1024, 1, size // PAGE - 1, 0)
            page[-10:] = b"SWAPSPACE2"
            disk.write(page)


def qemu_command(args: argparse.Namespace, case: str) -> list[str]:
    if case not in (*CASES, "e2e"):
        raise ValueError(f"unknown TCG case: {case}")
    numa = case == "e2e"
    swap = case != "no-swap"
    machine = "virt,gic-version=3" if numa else "virt,gic-version=3,mte=on"
    cmdline = "console=ttyAMA0 rdinit=/init panic=-1 kunit.enable=0"
    if numa:
        cmdline += f" EXPECT_NUMA=1 RUN_SELFTEST=1 TEST_TIMEOUT={args.timeout}"
        cmdline += " memcomp_selftest.allow_swapoff=1"
        smp = f"{args.cpus},sockets=2,cores={args.cpus // 2},threads=1"
    else:
        cmdline += f" mte_numa=n mte_swap={'on' if swap else 'off'}"
        smp = str(args.cpus)
    command = [str(args.qemu), "-machine", machine, "-cpu", "max",
               "-accel", "tcg,thread=multi", "-m", str(args.memory_mb), "-smp", smp,
               "-no-user-config", "-nodefaults", "-display", "none", "-monitor", "none",
               "-serial", "stdio", "-nic", "none", "-no-reboot",
               "-kernel", str(args.kernel), "-initrd", str(args.initramfs), "-append", cmdline]
    if numa:
        for node in (0, 1):
            command += ["-object", f"memory-backend-ram,size={args.memory_mb // 2}M,id=mem{node}",
                        "-numa", f"node,nodeid={node},memdev=mem{node}"]
        for node in (0, 1):
            command += ["-numa", f"cpu,node-id={node},socket-id={node}"]
    if swap:
        # Fixed basename in cwd: no key/value option injection from host paths.
        command += ["-drive", f"if=none,format=raw,file={SWAP},id=swapdisk",
                    "-device", "virtio-blk-device,drive=swapdisk"]
    return command


def check_config(args: argparse.Namespace) -> None:
    lines = set(args.config.read_text(encoding="utf-8").splitlines())
    required = ["ARM64", "ARM64_4K_PAGES", "SMP", "SERIAL_AMBA_PL011_CONSOLE",
                "VIRTIO_MMIO", "VIRTIO_BLK", "BLK_DEV_INITRD", "RD_GZIP", "DEVTMPFS",
                "BINFMT_ELF", "PROC_FS", "PROC_PAGE_MONITOR", "SYSFS", "SWAP", "MEMCOMP",
                "MEMCOMP_DEBUG_STATS", "LRU_GEN_ENABLED"]
    if args.profile == "e2e":
        required += ["NUMA", "MIGRATION", "COMPACTION", "TRANSPARENT_HUGEPAGE", "USERFAULTFD",
                     "DEBUG_FS"]
    missing = [f"CONFIG_{name}=y" for name in required if f"CONFIG_{name}=y" not in lines]
    if missing:
        raise ValueError(f"missing built-in kernel settings: {', '.join(missing)}")
    for option in ("ZRAM", "ZSWAP"):
        if any(f"CONFIG_{option}={value}" in lines for value in ("y", "m")):
            raise ValueError(f"CONFIG_{option} must be disabled")
    max_cpus = next((int(line.split("=", 1)[1]) for line in lines
                     if line.startswith("CONFIG_NR_CPUS=")), 0)
    if args.cpus > max_cpus:
        raise ValueError(f"--cpus exceeds CONFIG_NR_CPUS={max_cpus}")
    if args.profile == "mte":
        preflight_config(args.config, compressor=None, numa="n")


def result_directory(path: Path) -> Path:
    # Resolve ancestors for the published-data check, but never follow or reuse
    # an existing final component (even a dangling symlink).
    path = path.absolute()
    path = path.parent.resolve(strict=True) / path.name
    published = (ROOT / "results/formal-965").resolve()
    if path.is_relative_to(published):
        raise ValueError("TCG results must stay outside results/formal-965")
    path.mkdir(mode=0o700)
    return path


def validate_e2e(console: Path) -> dict:
    command = [sys.executable, "-B", str(E2E_VALIDATOR), str(console),
               "--mode", "regular", "--kunit-enabled", "0", "--run-selftest", "1",
               "--selftest-skips", "0", "--require-swapoff"]
    if os.environ.get("REQUIRE_COMPLETE") == "1":
        command += ["--e2e-skips", "0"]
    result = subprocess.run(command,
                            capture_output=True, text=True, errors="replace", check=False)
    witness = E2E_MIGRATION.findall(console.read_text(encoding="utf-8", errors="replace"))
    diagnostic = result.stderr.strip()
    if len(witness) != 1:
        missing = f"expected one successful node-1 migration witness, found {len(witness)}"
        diagnostic = f"{diagnostic}\n{missing}" if diagnostic else missing
    return {"passed": result.returncode == 0 and len(witness) == 1,
            "result": result.stdout.strip(), "diagnostic": diagnostic,
            "numa_migrated_pages": int(witness[0]) if len(witness) == 1 else None}


def run_case(args: argparse.Namespace, case: str, work: Path) -> dict:
    work.mkdir(mode=0o700)
    if case != "no-swap":
        create_swap(work / SWAP, args.swap_mb, formatted=case == "swap-on")
    command = qemu_command(args, case)
    write_json(work / "command.json", {"cwd": str(work), "argv": command})
    console = work / "guest.log"
    error = None
    timeout = args.timeout + 120 if case == "e2e" else args.timeout
    with console.open("xb") as output, (work / "qemu.log").open("xb") as diagnostics:
        try:
            child = subprocess.run(command, cwd=work, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=diagnostics, timeout=timeout,
                                   check=False)
            rc = child.returncode
        except subprocess.TimeoutExpired:
            rc, error = 124, f"QEMU timed out after {timeout}s"
        except OSError as exc:
            rc, error = 127, f"could not start QEMU: {exc}"
    validation = (validate_e2e(console) if case == "e2e" else
                  guest_markers(console, case, numa="n"))
    passed = validation["passed"] if case == "e2e" else not validation["failure"]
    if error is None and rc != 0:
        error = f"QEMU exited with status {rc}"
    if error is None and not passed:
        error = "guest validation failed; see receipt and guest.log"
    receipt = {"case": case, "status": "PASS" if error is None else "FAIL",
               "qemu_status": rc, "error": error, "validation": validation,
               "guest_log_sha256": sha256(console)}
    write_json(work / "receipt.json", receipt)
    return receipt


def interrupted(signum: int, _frame: object) -> None:
    # Unwind subprocess.run, which kills and waits for its direct child.
    raise KeyboardInterrupt(signal.Signals(signum).name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=("mte", "e2e"))
    parser.add_argument("--kernel", type=Path, required=True, help="built ARM64 Image")
    parser.add_argument("--config", type=Path, required=True, help="matching built .config")
    parser.add_argument("--initramfs", type=Path, required=True, help="original guest initramfs")
    parser.add_argument("--results", type=Path, required=True, help="new directory under an existing parent")
    parser.add_argument("--qemu", default="qemu-system-aarch64", help="executable name or path")
    parser.add_argument("--memory-mb", type=int, help="default: 512 (MTE) or 1024 (E2E)")
    parser.add_argument("--cpus", type=int, help="default: 2 (MTE) or 4 (E2E)")
    parser.add_argument("--swap-mb", type=int, help="default: 256 (MTE) or 2048 (E2E)")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="seconds per MTE case or E2E guest watchdog (host adds 120s)")
    args = parser.parse_args()
    numa = args.profile == "e2e"
    for name, default in (("memory_mb", 1024 if numa else 512),
                          ("cpus", 4 if numa else 2), ("swap_mb", 2048 if numa else 256)):
        if getattr(args, name) is None:
            setattr(args, name, default)
    if min(args.memory_mb, args.cpus, args.swap_mb, args.timeout) <= 0:
        parser.error("memory, CPUs, swap, and timeout must be positive")
    if numa and (args.memory_mb % 2 or args.cpus % 2):
        parser.error("E2E requires even memory and CPU counts for two equal nodes")
    if args.swap_mb > (1 << 24):
        parser.error("swap image exceeds the 4 KiB swap header range")
    qemu = shutil.which(args.qemu)
    if qemu is None:
        parser.error(f"QEMU executable not found: {args.qemu}; supply --qemu /path/to/qemu-system-aarch64")
    args.qemu = Path(qemu).resolve()
    os.umask(0o077)
    try:
        for name in ("kernel", "config", "initramfs"):
            path = getattr(args, name).resolve(strict=True)
            if not path.is_file():
                raise ValueError(f"{name} is not a regular file: {path}")
            setattr(args, name, path)
        check_config(args)
        validator = E2E_VALIDATOR if numa else ROOT / "bench/mte/run_mte.py"
        input_paths = {name: getattr(args, name)
                       for name in ("qemu", "kernel", "config", "initramfs")}
        input_paths.update(validator=validator, launcher=Path(__file__).resolve())
        inputs = {name: {"path": str(path), "sha256": sha256(path)}
                  for name, path in input_paths.items()}
        results = result_directory(args.results)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"TCG result directory: {results}", flush=True)
    cases = ("e2e",) if numa else CASES
    signal.signal(signal.SIGHUP, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        write_json(results / "inputs.json", {"profile": args.profile, "accelerator": "tcg",
                                             "inputs": inputs, "cases": cases})
        receipts = []
        for case in cases:
            receipt = run_case(args, case, results / case)
            receipts.append(receipt)
            print(f"{case}: {receipt['status']}", flush=True)
        passed = all(receipt["status"] == "PASS" for receipt in receipts)
        write_json(results / "receipt.json", {"status": "PASS" if passed else "FAIL",
                                              "cases": receipts})
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        print(f"TCG run interrupted or failed: {exc}; evidence retained in {results}",
              file=sys.stderr)
        return 1
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
