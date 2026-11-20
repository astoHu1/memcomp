#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Run one non-comparable W1/W2/W3/W4 workload in a disposable VZ VM."""
import argparse
import csv as csv_module
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
COMPARE = ROOT / "bench/compare"
sys.path.insert(0, str(COMPARE))
from mkinitramfs import pack
from run import (KERNEL_FAILURE, W4_PROTOCOL_V411, W4_SOURCES, config_swap_types,
                 corpus_header, corpus_preflight, digest, phase_snapshot_evidence,
                 smoke_acceptance, validate_csv, w4_scope_evidence, workload_spec)

MIB = 1024**2
MEMORY, CPUS, BACKING, DATA = 1024, 2, 256, 32  # MiB except CPUS
TOOLS = ("guest", "refault", "shmem", "app_switch", "gen_init_cpio")
PROFILES = ("memcomp", "swap", "zram", "zram-writeback", "zswap", "zswap-crosscheck")
BACKENDS = ("memcomp-noswap", "memcomp-swap", *PROFILES[1:])
ZSWAP = ("zswap", "zswap-crosscheck")
PRESETS = {
    "w1": ["/refault", "--pages", "256", "--rounds", "1", "--warmup", "0",
           "--single", "64", "--rewrite"],
    "w2": ["/app_switch", "--allow-pressure", "--cgroup-parent", "/sys/fs/cgroup",
           "--workers", "2", "--worker-bytes", "8388608", "--memory-max", "12582912",
           "--switches", "4"],
    "w3": ["/shmem", "--anonymous", "--pages", "256", "--pressure-mib", "16",
           "--order", "rand"],
    # Keep the original pressure-inducing target, order and settle interval.
    "w4": ["/refault", "--pages", "212992", "--corpus-source", "combined",
           "--seed", "1", "--order", "rand", "--settle-ms", "1000"],
}


def profile(backend):
    return "memcomp" if backend.startswith("memcomp-") else backend


def fragment(name):
    configs = COMPARE / "configs"
    name = "zswap" if name == "zswap-crosscheck" else name
    options = {}
    for path in (configs / "common.fragment", configs / (name + ".fragment")):
        for line in path.read_text().splitlines():
            if line.startswith("CONFIG_"):
                key, value = line.split("=", 1)
                options[key] = value
    # Keep the VZ console from arm64-vz.config; the 8250 serial port is QEMU-only.
    options.pop("CONFIG_SERIAL_8250")
    options.pop("CONFIG_SERIAL_8250_CONSOLE")
    options["CONFIG_KUNIT_DEFAULT_ENABLED"] = "n"
    options["CONFIG_NET"] = "y"  # UNIX depends on NET under allnoconfig.
    options["CONFIG_VM_EVENT_COUNTERS"] = "y"
    if name != "memcomp":
        options["CONFIG_MEMCOMP_DEBUG_STATS"] = "n"
    if name == "swap":
        options["CONFIG_ZSMALLOC"] = "n"  # No compressed-memory consumer under allnoconfig.
    return options


def check_config(text, backend):
    required = fragment(profile(backend))
    required.update({"CONFIG_" + k: "y" for k in (
        "ARM64", "ARM64_4K_PAGES", "VIRTIO_CONSOLE", "MULTIUSER", "PROC_SYSCTL")})
    actual = dict(line.split("=", 1) for line in text.splitlines() if line.startswith("CONFIG_"))
    mismatches = [f"{key}={value} (found {actual.get(key, 'n')})"
                  for key, value in required.items() if actual.get(key, "n") != value]
    if mismatches:
        raise ValueError("kernel config does not match the VZ comparison profile: " +
                         ", ".join(mismatches))


def fresh_image(path, capacity):
    with path.open("xb") as stream:
        stream.truncate(capacity)
        st = os.fstat(stream.fileno())
    return st.st_dev, st.st_ino


def read_payload(path, capacity, identity, console):
    """Check the guest's raw-disk framing after the runner has exited cleanly."""
    declarations = re.findall(r"^COMPARE_DATA bytes=(0|[1-9][0-9]*) "
                              r"sha256=([0-9a-f]{64}) status=([01])$", console, re.M)
    if len(declarations) != 1 or declarations[0][2] != "0":
        raise ValueError("missing, duplicate or failed guest data declaration")
    length, sha256, status = declarations[0]
    size = int(length)
    if not 0 < size <= capacity - 512:
        raise ValueError("invalid guest payload length")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        st = os.fstat(stream.fileno())
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1 or
                (st.st_dev, st.st_ino) != identity or st.st_size != capacity):
            raise ValueError("VZ data image inode/type/owner/size mismatch")
        expected = f"MEMCOMP_DATA_V1 bytes={length} sha256={sha256} status={status}\n".encode()
        if stream.read(512) != expected.ljust(512, b"\0"):
            raise ValueError("VZ data image header differs from console declaration")
        payload = stream.read(size)
        if len(payload) != size or hashlib.sha256(payload).hexdigest() != sha256:
            raise ValueError("VZ data payload length or SHA256 mismatch")
        tail = 0
        while chunk := stream.read(MIB):
            if chunk.strip(b"\0"):
                raise ValueError("nonzero data past the declared guest payload")
            tail += len(chunk)
        if 512 + size + tail != capacity:
            raise ValueError("incomplete raw image read")
    return payload


def validate_workload(lines, spec):
    records = [line.removeprefix("COMPARE_CSV ") for line in lines
               if line.startswith("COMPARE_CSV ")]
    report = validate_csv(records, spec)
    if spec["label"] == "w2" and report["csv_integrity"]:
        # The matrix records worker loss as an outcome; a functional pass
        # must actually exercise all four verified switches.
        report["w2_all_workers_ready_and_switches_verified"] = all(
            row["status"] in ("ready", "ok", "reaped")
            for row in csv_module.DictReader(records))
    return records, report


def validate_evidence(lines, log, spec, status):
    label, backend = spec["label"], spec["backend"]
    records, report = validate_workload(lines, spec)
    reclaim, disk = phase_snapshot_evidence(log, label, backend, report["writeback_latency_excluded"])
    status.update(report, **reclaim)
    if label == "w4":
        status.update(disk)
        status.update(w4_scope_evidence(lines, spec["guest_memory_mib"], spec["real_corpus"]))
        status["writeback_latency_excluded"] |= report["writeback_latency_excluded"]
        evidence_valid = status["w4_scope_valid"] and status["w4_diskstats_valid"]
        valid = (evidence_valid and report["csv_integrity"] and reclaim["mglru_boot_valid"] and
                 not report["drain_timed_out_rows"] and not report["unknown_special_swapped_touches"])
        status.update(smoke_acceptance(label, backend, {**status, "valid": valid}))
    if not report["csv_integrity"] or not reclaim["mglru_boot_valid"]:
        raise ValueError("workload CSV or MGLRU boot check failed; inspect status and guest log")
    if report["drain_timed_out_rows"] or report["unknown_special_swapped_touches"]:
        raise ValueError("incomplete drain or unknown special swap entry; inspect guest log")
    if not report.get("w2_all_workers_ready_and_switches_verified", True):
        raise ValueError("W2 had an unavailable worker or switch; inspect guest log")
    if label == "w4":
        if not evidence_valid:
            raise ValueError("W4 pressure scope or writeback evidence failed; inspect status and guest log")
        if not status["smoke_accepted"]:
            raise ValueError("W4 writeback did not stabilize within 300 seconds; evidence retained, not a pass")
    return records


def write_json(path, data):
    staging = path.with_suffix(".tmp")
    staging.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    staging.replace(path)


def regular_input(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"not a regular file: {path}")
    return path.resolve(strict=True)


def run_settings(args):
    w4 = args.label == "w4"
    if w4 and args.backend not in W4_SOURCES:
        raise ValueError("W4 requires memcomp-swap, zswap or zram-writeback")
    low, high, default = (360, 3600, 1800) if w4 else (15, 600, 120)
    timeout = default if args.timeout is None else args.timeout
    if not low <= timeout <= high:
        raise ValueError(f"{args.label.upper()} guest timeout must be {low}..{high} seconds")
    if (args.algorithm == "none") != (args.backend == "swap"):
        raise ValueError("use --algorithm none only with --backend swap")
    if args.label == "w2" and args.backend == "memcomp-noswap":
        raise ValueError("W2 preset exceeds its cgroup limit and requires a swap backend")
    return (timeout, 2048, 128) if w4 else (timeout, BACKING, DATA)


def run(args):
    if (platform.system(), platform.machine()) != ("Darwin", "arm64") or os.geteuid() == 0:
        raise ValueError("run as an unprivileged user on Apple Silicon macOS")
    timeout, backing_mib, data_mib = run_settings(args)
    w4 = args.label == "w4"

    config = regular_input(args.kernel_config)
    config_text = config.read_text()
    check_config(config_text, args.backend)
    kernel = regular_input(args.kernel)
    with kernel.open("rb") as image:
        header = image.read(64)
    if len(header) != 64 or header[56:60] != b"ARMd" or (header[24] & 7) != 2:
        raise ValueError("expected a little-endian 4 KiB arm64 Linux Image")
    runner = regular_input(args.runner)
    binaries = args.binaries.resolve(strict=True)
    for tool in TOOLS:
        regular_input(binaries / tool)
    corpus = regular_input(args.corpus_image)
    binding = corpus_header(corpus, with_identity=True)
    command = PRESETS[args.label] + ["--cpu", "1", "--corpus", "/dev/vdc",
                                     "--corpus-sampling", "uniform-v411"]
    spec = workload_spec(args.label, command, smoke=True, backend=args.backend)
    corpus_preflight(binding[0], spec, MEMORY)
    spec.update(config_swap_types(config_text), guest_memory_mib=MEMORY)

    base = ROOT / "build/macos-compare"
    if base.resolve() != base:
        raise ValueError("refusing symlinked results base")
    base.mkdir(parents=True, exist_ok=True)
    if w4 and shutil.disk_usage(base).free < (backing_mib + data_mib) * MIB:
        raise ValueError("insufficient free space for W4 scratch disks")
    dest = Path(tempfile.mkdtemp(prefix=f"{args.label}-{args.backend}.", dir=base))
    print(f"VZ functional result: {dest}", flush=True)
    status = {"kind": "macos-vz-functional", "eligible_for_comparison": False,
              "functional_pass": False, "state": "preparing"}
    write_json(dest / "functional-status.json", status)
    try:
        initrd = pack(args.tree, binaries, dest / "initrd", args.backend, args.algorithm,
                      args.label, command, backing_mib * MIB, timeout, data_mib, MEMORY)
        primary, data, console = (dest / name for name in ("primary.raw", "data.raw", "console.log"))
        # Keep the vda slot even when the backend never uses it.
        fresh_image(primary, (16 if args.backend in ("memcomp-noswap", "zram") else backing_mib) * MIB)
        identity = fresh_image(data, data_mib * MIB)
        console.touch(exist_ok=False)
        cmdline = "console=hvc0 rdinit=/init panic=-1 transparent_hugepage=madvise"
        if profile(args.backend) == "memcomp":
            cmdline += f" memcomp.compressor={args.algorithm}"
        if args.backend in ZSWAP:
            cmdline += (f" zswap.enabled=1 zswap.compressor={args.algorithm}"
                        " zswap.zpool=zsmalloc zswap.shrinker_enabled=1")
        argv = [str(runner), str(kernel), str(initrd), str(primary), str(console), str(MEMORY),
                str(CPUS), str(timeout + 20), cmdline, str(dest / "vz-info.txt"),
                "rw:" + str(data), "ro:" + str(corpus)]
        (dest / "kernel.config").write_text(config_text)
        write_json(dest / "functional-inputs.json", {
            **status, "backend": args.backend, "algorithm": args.algorithm, "workload": args.label,
            "guest_argv": command, "runner_argv": argv, "host": " ".join(os.uname()),
            "corpus_identity": binding[1], "corpus_header_sha256": binding[2],
            **({"w4_protocol": W4_PROTOCOL_V411} if w4 else {}),
            "sha256": {name: digest(path) for name, path in {
                "Image": kernel, "kernel.config": dest / "kernel.config", "vz-runner": runner,
                "initramfs.gz": initrd, **{tool: binaries / tool for tool in TOOLS},
            }.items()},
        })
        if corpus_header(corpus, with_identity=True) != binding:
            raise ValueError("corpus changed during preparation")
        status["state"] = "running"
        write_json(dest / "functional-status.json", status)
        with (dest / "host.log").open("wb") as stream:
            proc = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT,
                                  timeout=timeout + 30, check=False)
        status["runner_exit"] = proc.returncode
        if proc.returncode:
            raise ValueError(f"VZ runner exited {proc.returncode}; inspect host.log and console.log")
        if corpus_header(corpus, with_identity=True) != binding:
            raise ValueError("corpus changed during the run")
        console_bytes = console.read_bytes()
        uart = console_bytes.decode("utf-8", errors="replace").replace("\r\n", "\n")
        if re.findall(r"^COMPARE_DONE status=([0-9]+)$", uart, re.M) != ["0"]:
            raise ValueError("expected exactly one COMPARE_DONE status=0; inspect console.log")
        if re.search(r"^COMPARE_(?:CSV |RAW_|SNAPSHOT )", uart, re.M):
            raise ValueError("bulk guest output leaked onto the console")
        payload = read_payload(data, data_mib * MIB, identity, uart)
        status.update(data_bytes=len(payload), data_sha256=hashlib.sha256(payload).hexdigest())
        # Like the original runner, compress W4 so the shared telemetry parser's
        # compressed-input bound does not become a 64 MiB decoded-output bound.
        log = dest / ("guest.log.gz" if w4 else "guest.log")
        with (gzip.open if w4 else open)(log, "wb") as stream:
            stream.write(payload)
            stream.write(b"\nCOMPARE_CONSOLE_BEGIN\n")
            stream.write(console_bytes)
        lines = payload.decode("utf-8").splitlines()
        del payload
        if KERNEL_FAILURE.search(uart) or any(KERNEL_FAILURE.search(line) for line in lines):
            raise ValueError("guest/kernel failure marker; inspect guest and console logs")
        csv = validate_evidence(lines, log, spec, status)
        (dest / "workload.csv").write_text("\n".join(csv) + "\n")
        status.update(state="passed", functional_pass=True)
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        status.update(state="failed", error=str(exc) or type(exc).__name__)
        raise
    finally:
        write_json(dest / "functional-status.json", status)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    config = sub.add_parser("config", help="print a build.sh CONFIG_FRAGMENT to stdout")
    config.add_argument("profile", choices=PROFILES)
    smoke = sub.add_parser("run", help="boot exactly one non-formal guest")
    smoke.add_argument("--backend", choices=BACKENDS, required=True)
    smoke.add_argument("--algorithm", choices=("zstd", "lz4", "none"), required=True)
    smoke.add_argument("--label", choices=PRESETS, required=True)
    smoke.add_argument("--kernel", type=Path, required=True)
    smoke.add_argument("--kernel-config", type=Path, required=True)
    smoke.add_argument("--corpus-image", type=Path, required=True)
    smoke.add_argument("--tree", type=Path, default=ROOT / "kernel-src/linux-mainline",
                       help="tree path for the initramfs packer; does not verify Image provenance")
    smoke.add_argument("--binaries", type=Path, default=ROOT / "build/macos-compare/tools")
    smoke.add_argument("--runner", type=Path, default=ROOT / "build/macos-vz/vz-runner")
    smoke.add_argument("--timeout", type=int, help="guest timeout in seconds (120; W4: 1800)")
    args = parser.parse_args()
    if args.action == "config":
        for key, value in fragment(args.profile).items():
            print(f"{key}={value}")
        return 0
    os.umask(0o077)
    try:
        run(args)
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        print(f"VZ functional run failed: {exc or type(exc).__name__}", file=sys.stderr)
        return 1
    print("VZ functional PASS (not eligible for comparison)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
