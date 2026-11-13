#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Plan, or explicitly execute, the v4.98 real-corpus comparison matrix."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from run import (corpus_header, HERE, METHOD as RUNNER_METHOD, RECLAIM,
                 W1_PROTOCOL_V411 as W1_PROTOCOL, W4_PROTOCOL_V411 as W4_PROTOCOL,
                 cpu_set, digest, floor, interrupt, kernel_identity, write_json)

METHOD = "v4.6"
RESULT_DISK = {"transport": "MEMCOMP_DATA_V1", "header_bytes": 512, "data_mib": 128,
               "serial": "memcomp-data", "device": "/dev/vdb", "swap": False}

# Fixed cyclic bases make every full ten-boot population visit every slot.
# Workload offsets avoid an identical group at workload/round boundaries.
VARIANTS = (
    ("zram-writeback", "zstd", "zram-writeback"),
    ("zswap", "zstd", "zswap"),
    ("swap", "none", "swap"),
    ("memcomp-swap", "zstd", "memcomp"),
    ("zram", "zstd", "zram"),
    ("memcomp-noswap", "zstd", "memcomp"),
    ("zram", "lz4", "zram"),
    ("memcomp-swap", "lz4", "memcomp"),
    ("zswap-crosscheck", "zstd", "zswap-crosscheck"),
    ("memcomp-noswap", "lz4", "memcomp"),
)
WRITEBACK = {("zram-writeback", "zstd"), ("zswap", "zstd"), ("memcomp-swap", "zstd")}


def group_id(variant):
    return variant[0] + "-" + variant[1]


SOURCES = ("system_server", "systemui", "qq", "wechat", "chrome",
           "netease_music", "discord", "telegram")
W1_COMBINED = "w1-corpus-geomean"
BASE_OFFSETS = {**{"w1-corpus-" + name: (2 * index) % 10
                   for index, name in enumerate(SOURCES)},
                "w2": 8, "w3-anon-512m": 0, "w4-832m": 1}


def variant_order(round_id, workload_id):
    offset = (BASE_OFFSETS[workload_id] + round_id - 1) % len(VARIANTS)
    ordered = VARIANTS[offset:] + VARIANTS[:offset]
    # Preserve the rotated first group when reversing the direction. Reversing
    # the whole even list repeats only same-parity positions across ten boots.
    return ordered[:1] + ordered[:0:-1] if round_id % 2 == 0 else ordered


def workloads(smoke=False):
    common = ["--seed", "1", "--order", "rand", "--cpu", "1"]
    corpus = ["--corpus", "/dev/vdc", "--corpus-sampling", "uniform-v411"]
    for source in (SOURCES[:1] if smoke else SOURCES):
        yield "w1", "w1-corpus-" + source, [
            "/refault", "--rewrite", "--pages", "64" if smoke else "16384",
            *corpus, "--corpus-source", source,
            *common, "--settle-ms", "250", "--rounds", "1" if smoke else "5",
            "--warmup", "0" if smoke else "1", "--single", "32" if smoke else "4096"]
    if smoke:
        return
    yield "w2", "w2", [
        "/app_switch", "--allow-pressure", "--cgroup-parent", "/sys/fs/cgroup",
        "--workers", "8", "--worker-bytes", "67108864", "--memory-max", "268435456",
        "--switches", "32", "--swap-max", "max", "--seed", "1", "--cpu", "1",
        "--timeout-ms", "10000", *corpus]
    yield "w3", "w3-anon-512m", [
        "/shmem", "--anonymous", "--pages", "131072", *corpus,
        "--corpus-source", "combined", *common, "--settle-ms", "250",
        "--pressure-mib", "512"]
    yield "w4", "w4-832m", [
        "/refault", "--pages", "212992", *corpus, "--corpus-source", "combined",
        *common, "--settle-ms", "1000"]


def bind_corpus(args):
    """Use the runner's existing header/stat binding and approved source order."""
    header, identity, header_sha = corpus_header(args.corpus_image, with_identity=True)
    if tuple(source["label"] for source in header["sources"]) != SOURCES:
        raise ValueError("corpus source order differs from the eight approved applications")
    if any(source["pages"] < 32768 for source in header["sources"]) or header["total_pages"] < 507904:
        raise ValueError("corpus does not cover existing unique-index requirements")
    args.corpus_expected_identity = json.dumps(identity, separators=(",", ":"))
    args.corpus_header_sha256 = header_sha
    return {"image": str(args.corpus_image), "identity": identity,
            "header_sha256": header_sha, "sources": header["sources"],
            "total_pages": header["total_pages"], "sampling": "uniform-v411"}


def plan(args):
    jobs = []
    previous = None
    for round_id in range(1, 2 if args.smoke else 11):
        for label, workload_id, command in workloads(args.smoke):
            ordered = variant_order(round_id, workload_id)
            if args.smoke:
                selected = args.smoke_group or "memcomp-swap-zstd"
                ordered = tuple(item for item in ordered if group_id(item) == selected)
            if label in ("w3", "w4") and round_id > 5:
                continue
            for variant in ordered:
                backend, algorithm, kernel = variant
                if label == "w4" and (backend, algorithm) not in WRITEBACK:
                    continue
                if previous == group_id(variant):
                    raise ValueError("schedule contains consecutive identical groups")
                previous = group_id(variant)
                destination = args.dest / ("smoke" if args.smoke else "round-%02d" % round_id) / workload_id / group_id(variant)
                argv = [sys.executable, "-B", str(HERE / "run.py"),
                        "--tree", str(args.tree), "--binaries", str(args.binaries),
                        "--kernel-dir", str(args.kernels / kernel), "--work", str(args.work),
                        "--dest", str(destination), "--host-volume", str(args.host_volume),
                        "--backend", backend, "--algorithm", algorithm, "--label", label,
                        "--cpus", args.cpus, "--smp", "2", "--memory", "1024",
                        "--backing-mib", "2048", "--data-mib", str(RESULT_DISK["data_mib"]),
                        "--repeats", "1", "--timeout", str(args.timeout),
                        "--workload-id", workload_id, "--group-id", group_id(variant),
                        "--allow-pressure", "--corpus-image", str(args.corpus_image),
                        "--corpus-expected-identity", args.corpus_expected_identity,
                        "--corpus-header-sha256", args.corpus_header_sha256]
                if args.smoke:
                    argv.append("--smoke")
                else:
                    argv.extend(("--host-boot-round", str(round_id)))
                argv.extend(("--", *command))
                jobs.append({"index": len(jobs) + 1, "host_boot_round": None if args.smoke else round_id,
                             "label": label, "workload_id": workload_id, "group_id": group_id(variant),
                             "kernel_artifact": kernel, "dest": str(destination), "argv": argv})
    # Explicit frozen population: rewrite is an inner W1 mode, never a slot.
    formal_counts = {"w1": 800, "w2": 100, "w3": 50, "w4": 15}
    expected = 1 if args.smoke else 965
    if not args.smoke and dict(Counter(job["label"] for job in jobs)) != formal_counts:
        raise ValueError("v4.98 requires exactly W1=800, W2=100, W3=50, W4=15 boots")
    if len(jobs) != expected or len({job["dest"] for job in jobs}) != expected:
        raise ValueError("incomplete or duplicate boot schedule")
    return jobs


def pairing_report(jobs, results):
    """Count complete/invalid/pending same-round pairs; never select survivors."""
    lookup = {(job["host_boot_round"], job["workload_id"], job["group_id"]): job for job in jobs}
    report = []
    for workload_id in dict.fromkeys(job["workload_id"] for job in jobs):
        if workload_id.startswith("w4"):
            comparisons = [("memcomp-swap-zstd", reference) for reference in
                           ("zram-writeback-zstd", "zswap-zstd")]
        else:
            comparisons = [(backend + "-" + codec, reference)
                           for backend in ("memcomp-noswap", "memcomp-swap")
                           for codec, references in (("zstd", ("zram-zstd", "zswap-zstd")),
                                                     ("lz4", ("zram-lz4",)))
                           for reference in references]
            comparisons.append(("zswap-crosscheck-zstd", "zswap-zstd"))
        rounds = sorted({job["host_boot_round"] for job in jobs if job["workload_id"] == workload_id})
        for left, right in comparisons:
            valid, invalid, pending = [], [], []
            readback_valid, readback_invalid = [], []
            for round_id in rounds:
                pair = [lookup.get((round_id, workload_id, group)) for group in (left, right)]
                if not all(pair):
                    continue
                statuses = [results.get(item["index"]) for item in pair]
                if not all(statuses):
                    pending.append(round_id)
                elif all(status.get("eligible_for_comparison") for status in statuses):
                    valid.append(round_id)
                    if workload_id.startswith("w4"):
                        target = (readback_valid if all(status.get("eligible_for_readback_comparison")
                                  for status in statuses) else readback_invalid)
                        target.append(round_id)
                else:
                    invalid.append(round_id)
                    if workload_id.startswith("w4"):
                        readback_invalid.append(round_id)
            if valid or invalid or pending:
                report.append({"workload_id": workload_id, "numerator": left, "reference": right,
                               "valid_pairs": len(valid), "invalid_pairs": len(invalid),
                               "valid_rounds": valid, "invalid_rounds": invalid, "pending_rounds": pending,
                               **({"readback_valid_pairs": len(readback_valid),
                                   "readback_invalid_pairs": len(readback_invalid),
                                   "readback_valid_rounds": readback_valid,
                                   "readback_invalid_rounds": readback_invalid}
                                  if workload_id.startswith("w4") else {})})
    if not any(job["host_boot_round"] is None for job in jobs):
        for left, right in [(item["numerator"], item["reference"]) for item in report
                            if item["workload_id"] == "w1-corpus-" + SOURCES[0]]:
            rows = [item for item in report if item["workload_id"] in
                    {"w1-corpus-" + source for source in SOURCES}
                    and item["numerator"] == left and item["reference"] == right]
            valid, invalid, pending = [], [], []
            for round_id in range(1, 11):
                if len(rows) != len(SOURCES) or any(round_id in row["pending_rounds"] for row in rows):
                    pending.append(round_id)
                elif all(round_id in row["valid_rounds"] for row in rows):
                    valid.append(round_id)
                else:
                    invalid.append(round_id)
            report.append(dict(workload_id=W1_COMBINED, numerator=left, reference=right,
                               valid_pairs=len(valid), invalid_pairs=len(invalid),
                               valid_rounds=valid, invalid_rounds=invalid, pending_rounds=pending))
    return report


def measurement_sources():
    helper = HERE.parent / "e2e/tests/run_e2e_qemu.py"
    return {"workload_source_hashes": {file.name: digest(file) for file in HERE.iterdir()
                                      if file.suffix in (".c", ".h", ".py")},
            "qemu_helper_sha256": digest(helper)}


def execute(args, jobs):
    if RUNNER_METHOD != METHOD:
        raise ValueError("matrix/runner method mismatch; rebuild/review v4.6 tools separately")
    if os.geteuid() == 0 or not args.allow_pressure:
        raise ValueError("unprivileged owner and explicit --allow-pressure are required")
    if args.dest.exists():
        raise ValueError("result root already exists; no old-run reuse, resume, replacement or overwrite")
    if cpu_set(args.cpus) != [4, 5, 6, 7]:
        raise ValueError("formal/smoke comparison affinity is fixed to host CPUs 4-7")
    floor(1024, args.host_volume)
    # Use the six artifact roles selected by --kernels; never call build.py.
    artifacts = {}
    for backend, _, kernel in VARIANTS:
        if kernel in artifacts:
            continue
        identity, hashes = kernel_identity(args.kernels / kernel, backend)
        artifacts[kernel] = {"source": identity[0], "sha256": hashes}
    tools = ("guest", "refault", "shmem", "app_switch", "gen_init_cpio")
    if any(not (args.binaries / name).is_file() for name in tools):
        raise ValueError("prebuild all static tools and gen_init_cpio separately")
    binaries = {name: digest(args.binaries / name) for name in tools}
    corpus = bind_corpus(args)
    frozen_sources = measurement_sources()
    args.dest.mkdir(parents=True)
    matrix = {"method": METHOD, "kind": "smoke" if args.smoke else "interleaved-comparison",
              "planned_boots": len(jobs), "variants": VARIANTS, "kernel_artifacts": artifacts,
              "binary_sha256": binaries, **frozen_sources, "memory_mib": 1024, "backing_mib": 2048,
              "cpus": [4, 5, 6, 7], "guest_vcpus": 2, "guest_refault_cpu": 1,
              "repeats_per_destination": 1, "timeout_seconds": args.timeout,
              "order": "fixed workload base, rotate by round-1, reverse direction preserving first on even rounds",
              "workload_base_offsets": BASE_OFFSETS,
              "w4_target_mib": 832, "resume_or_replacement": False,
              "result_disk": dict(RESULT_DISK), "reclaim": RECLAIM, "w1_protocol": W1_PROTOCOL, "w4_protocol": W4_PROTOCOL,
              "runner_sha256": digest(HERE / "run.py"), "matrix_sha256": digest(Path(__file__)),
              "corpus": corpus, "jobs": jobs, "state": "running"}
    results = {}

    def save():
        matrix["completed_boots"] = len(results)
        matrix["valid_boots"] = sum(bool(result.get("valid")) for result in results.values())
        matrix["failed_boots"] = len(results) - matrix["valid_boots"]
        matrix["w4_unstable_readback_boots"] = sum(
            bool(result.get("writeback_latency_excluded")) for result in results.values())
        matrix["pairing"] = [] if args.smoke else pairing_report(jobs, results)
        write_json(args.dest / "matrix.json", matrix)
        if not args.smoke:
            write_json(args.dest / "comparisons.json", {
                "kind": "same-round-slot-pairing", "rows": matrix["pairing"],
                "counts": dict(Counter(row["workload_id"] for row in matrix["pairing"]))})

    save()
    for job in jobs:
        # No heavy task is live at this boundary. Fail closed rather than
        # collecting a mixed-source population if any frozen input changes.
        try:
            if measurement_sources() != frozen_sources:
                raise ValueError("measurement source/helper hashes changed")
            if {name: digest(args.binaries / name) for name in tools} != binaries:
                raise ValueError("guest binary hashes changed")
            backend = next(item[0] for item in VARIANTS if group_id(item) == job["group_id"])
            identity, hashes = kernel_identity(args.kernels / job["kernel_artifact"], backend)
            if {"source": identity[0], "sha256": hashes} != artifacts[job["kernel_artifact"]]:
                raise ValueError("kernel artifact identity changed")
        except (OSError, ValueError) as exc:
            matrix.update(state="input-drift-stop", input_drift_reason=str(exc))
            save()
            return 2
        print("BOOT", job["index"], "OF", len(jobs), job["host_boot_round"],
              job["workload_id"], job["group_id"], flush=True)
        # The child owns the per-host lock, load gate, fresh disk and lifecycle.
        # No outer timeout: the runner's interruptible load wait has no deadline.
        try:
            child = subprocess.Popen(job["argv"])
        except OSError as exc:
            # No child was created, so recording the failed slot is safe.
            result = {"repeat": 1, "valid": False, "eligible_for_comparison": False,
                      "state": "failed", "lifecycle_safe_to_continue": True,
                      "error": "runner launch failed: " + str(exc)}
            destination = Path(job["dest"])
            destination.mkdir(parents=True)
            write_json(destination / "status.json", [result])
            write_json(destination / "metadata.json", {
                "method": METHOD, "kind": matrix["kind"],
                "host_boot_round": job["host_boot_round"], "workload_id": job["workload_id"],
                "group_id": job["group_id"], "boot_started": False,
                "result_disk": dict(RESULT_DISK), "reclaim": RECLAIM})
            results[job["index"]] = result
            job.update(runner_exit=1, status=result)
            save()
            continue
        interrupted = False
        try:
            code = child.wait()
        except KeyboardInterrupt:
            interrupted = True
            if child.poll() is None:
                try:
                    child.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass
            try:
                code = child.wait(timeout=75)
            except subprocess.TimeoutExpired:
                code = 125
        # argparse exits with 2 before the runner creates any job/VM resource.
        unprepared = code == 2 and not Path(job["dest"]).exists()
        try:
            statuses = json.loads((Path(job["dest"]) / "status.json").read_text())
            if len(statuses) != 1 or statuses[0].get("repeat") != 1:
                raise ValueError("runner must emit exactly one repeat=1 status")
            result = statuses[0]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result = {"repeat": 1, "valid": False, "eligible_for_comparison": False,
                      "lifecycle_safe_to_continue": unprepared, "error": str(exc)}
        drift = ""
        try:
            # Recheck live bytes after runner return as well: launch metadata
            # alone cannot detect a late change, especially in the final slot.
            if measurement_sources() != frozen_sources:
                raise ValueError("measurement source/helper hashes changed during slot")
            if {name: digest(args.binaries / name) for name in tools} != binaries:
                raise ValueError("guest binary hashes changed during slot")
            current_identity, current_hashes = kernel_identity(
                args.kernels / job["kernel_artifact"], backend)
            if {"source": current_identity[0], "sha256": current_hashes} != artifacts[job["kernel_artifact"]]:
                raise ValueError("kernel artifact identity changed during slot")
            actual = json.loads((Path(job["dest"]) / "metadata.json").read_text())
            if not isinstance(actual, dict):
                raise ValueError("runner metadata is not an object")
            expected = {**frozen_sources, "binary_sha256": binaries,
                        "kernel_sha256": hashes["bzImage"], "config_sha256": hashes[".config"],
                        "source": identity[0], "reclaim": RECLAIM,
                        "w1_protocol": W1_PROTOCOL if job["label"] == "w1" else None,
                        "w4_protocol": W4_PROTOCOL if job["label"] == "w4" else None}
            if any(actual.get(name) != value for name, value in expected.items()):
                drift = "runner metadata differs from frozen measurement inputs"
        except (OSError, ValueError, TypeError) as exc:
            drift = "runner input identity unavailable: " + str(exc)
        if code != 0 or drift:
            result = dict(result, valid=False, eligible_for_comparison=False,
                          eligible_for_readback_comparison=False)
        if drift:
            result["input_drift_error"] = drift
            Path(job["dest"]).mkdir(parents=True, exist_ok=True)
            write_json(Path(job["dest"]) / "status.json", [result])
        results[job["index"]] = result
        job.update(runner_exit=code, status=result)
        save()
        if interrupted or code == 130:
            matrix["state"] = "interrupted"
            save()
            return 130
        if not result.get("lifecycle_safe_to_continue"):
            matrix["state"] = "safety-stop"
            matrix["safety_stop_reason"] = "actual QEMU death/owned-disk cleanup is not proven; do not start another heavy task"
            save()
            return 125
        if drift:
            matrix.update(state="input-drift-stop", input_drift_reason=drift)
            save()
            return 2
        # Failed workload/timeout stays in its assigned slot. No retry or reuse.
    matrix["state"] = "complete-with-failures" if matrix["failed_boots"] else "complete"
    save()
    return 1 if matrix["failed_boots"] else 0


def main():
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGHUP, interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("tree", "binaries", "kernels", "work", "dest", "host-volume", "corpus-image"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--cpus", default="4-7")
    parser.add_argument("--timeout", type=int, default=1800, help="per-guest 5..3600 seconds, not a load-wait deadline")
    parser.add_argument("--execute", action="store_true", help="actually start guests; default only prints a plan")
    parser.add_argument("--allow-pressure", action="store_true")
    parser.add_argument("--smoke", action="store_true", help="one 64-page W1 boot; permanently nonformal")
    parser.add_argument("--smoke-group", choices=tuple(group_id(item) for item in VARIANTS))
    args = parser.parse_args()
    try:
        if not 5 <= args.timeout <= 3600:
            raise ValueError("timeout must be bounded to 5..3600 seconds")
        if args.smoke_group and not args.smoke:
            raise ValueError("--smoke-group requires --smoke")
        for name in ("tree", "binaries", "kernels", "work", "dest", "host_volume", "corpus_image"):
            setattr(args, name, getattr(args, name).resolve())
        bind_corpus(args)
        jobs = plan(args)
        if not args.execute:
            print(json.dumps({"method": METHOD, "kind": "smoke" if args.smoke else "interleaved-comparison",
                              "executed": False, "planned_boots": len(jobs),
                              "result_disk": dict(RESULT_DISK), "reclaim": RECLAIM, "w1_protocol": W1_PROTOCOL, "w4_protocol": W4_PROTOCOL,
                              "counts_by_label": dict(Counter(job["label"] for job in jobs)), "jobs": jobs}, indent=2))
            return 0
        return execute(args, jobs)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
