#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Run exactly one v4.6 workload in a fresh, CPU-pinned x86 KVM guest."""
import argparse
import csv as csv_module
from collections import Counter
from bisect import bisect_right
import fcntl
import gzip
import hashlib
import io
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time

from mkinitramfs import BACKENDS, pack
import telemetry as telem
from telemetry import parse_log
from prime import PRIME_CONTRACT, PRIME_FIELDS, prime_counts
from pageout import FIRST_PASS_FIELDS, PAGEOUT_CONTRACT, first_pass_counts

HERE = Path(__file__).resolve().parent
MIB = 1024**2
METHOD = "v4.6"
CORPUS_SAMPLING_V411 = "uniform-v411"
CORPUS_SAMPLING_PROTOCOL = "corpus-page-uniform-disjoint-v411"
UINT64_MAX = (1 << 64) - 1
RECLAIM = {"mode": "mglru",
           "required_config": {"CONFIG_LRU_GEN": "y", "CONFIG_LRU_GEN_ENABLED": "y"},
           "boot_enabled_path": "/sys/kernel/mm/lru_gen/enabled", "boot_required_mask": 1}
W1_PROTOCOL = {
    "modes": ["seq", "rand", "single", "rewrite"],
    "fresh_mapping_each_round": True, "fresh_mapping_phase": "rN_seq_fresh_mapping",
    "memory_baseline": "seq_fill_before_to_drain_after",
    "rewrite": "fresh_random_pageout_readback_then_target_overwrite_pageout_readback",
    "rewrite_prime_seed_xor": "0x7265777269746501", "extra_boot_slots": 0,
    "prime": PRIME_CONTRACT, "pageout": PAGEOUT_CONTRACT,
    "drain_present_retry": "w1_only_unchanged_present_pages_after_initial_pageout",
    "drain_retry_cap": 4, "drain_retry_time": "purge_ns_outside_pageout_total_ns",
}
W4_PROTOCOL = {
    "memory_cgroup": "/compare-w4", "limits": "default_unlimited",
    "held_pattern": "half_random_half_zero_each_page", "held_ram_fraction": [7, 8],
    "ramp_pattern": "random", "ramp_ram_fraction": [1, 4],
    "ramp_release": "before_pressure_ready", "fill_pacing_us_per_mib": 1000,
    "held_until": "after_drain_readback_full_verification",
    "writeback_attribution": "phase_level_not_per_target_page",
    "wait_cap_seconds": 300, "stable_poll_seconds": 1, "stable_intervals": 3,
    "all_backend_wait": "three_complete_unchanged_writeback_and_pgscan_intervals_with_positive_writeback",
    "memcomp_gate": "descriptive_only_never_latency_exclusion",
    "poll_record": "WRITEBACK_POLL",
    "timeout_policy": "retain_write_volume_exclude_readback_latency_only",
    "swap_device": "vda", "diskstats_sector_bytes": 512,
    "write_window": ["pressure_begin", "read_begin"],
    "write_window_begin": "after_target_pageout_before_explicit_zram_writeback_and_pressure_allocation",
    "readback_population": "all_verified_segment_first_touches",
    "readback_write_window": ["read_begin", "read_end"],
}


# Versioned real-input semantics; historical constants remain fixture contracts.
REAL_W1_PROTOCOL = {**W1_PROTOCOL,
    "input": "real-corpus-v1", "generated_measurement_content": False,
    "rewrite": "fresh_disjoint_real_slice_pageout_readback_then_target_overwrite_pageout_readback",
    "rewrite_prime_seed_xor": None,
    "rewrite_prime_range": "target_end_to_target_end_plus_target_pages_same_source"}
REAL_W4_PROTOCOL = {**W4_PROTOCOL,
    "input": "real-corpus-v1", "generated_measurement_content": False,
    "held_pattern": "disjoint_real_corpus_suffix", "ramp_pattern": "next_disjoint_real_corpus_suffix",
    "shortage_policy": "fail_no_repetition_no_synthesis_no_silent_size_change"}

W1_PROTOCOL_V411 = {**REAL_W1_PROTOCOL,
    "input": CORPUS_SAMPLING_PROTOCOL,
    "rewrite": "ordered_source_complement_prime_then_uniform_target_overwrite",
    "rewrite_prime_range": "first_target_pages_from_ordered_source_complement"}
W4_PROTOCOL_V411 = {**REAL_W4_PROTOCOL,
    "input": CORPUS_SAMPLING_PROTOCOL,
    "held_pattern": "first_ordered_source_complement_pages",
    "ramp_pattern": "next_ordered_source_complement_pages"}


KERNEL_FAILURE = re.compile(
    r"Kernel panic|BUG:|KASAN:|DATA_MISMATCH|rcu.*detected stalls|"
    r"rcu.*self-detected stall|soft lockup|WARNING:|UBSAN:", re.IGNORECASE)
PAGE_FIELDS = (
    "workload,content,seed,page_size,order,sequence,page_index,page_kind,"
    "snapshot_present,snapshot_swapped,snapshot_resident,present_before,"
    "swapped_before,resident_before,classification,touch_ns,verify_ok,"
    "cpu_before,swap_type_before").split(",")
W1_FIELDS = (
    "method,record,round,warmup,mode,pageout_ns,pageout_total_ns,purge_ns,"
    "nonpresent_pages,requested_pages,drain_timed_out,memfree_before_kib,"
    "memfree_after_kib,memavailable_before_kib,memavailable_after_kib").split(",") + list(FIRST_PASS_FIELDS)
SAMPLE_FIELDS = ("snapshot_swap_type,snapshot_classification,refault_eligible,"
                 "refault_counted,readahead_hit").split(",")
DRAIN_FIELDS = ("drain_rounds,drain_remaining_swapcache_pages,drain_remapped_pages,"
                "refault_eligible_pages,refault_counted_pages,readahead_hit_pages,"
                "readahead_hit_ratio").split(",")
WRITEBACK_FIELDS = ("writeback_source,writeback_before,writeback_after,writeback_delta,"
                   "writeback_wait_ns,writeback_wait_timed_out,writeback_stable_polls,"
                   "pressure_bytes,pressure_ready,writeback_explicit_idle,writeback_proven").split(",")
PAGE_PHASE_FIELDS = ["method", "purge_ns", "drain_timed_out"] + WRITEBACK_FIELDS
W4_SOURCES = {"memcomp-swap": "memcomp_writeback_payloads", "zswap": "zswpwb",
              "zram-writeback": "zram_bd_writes"}


def w4_scope_evidence(lines, memory_mib, real_corpus=False):
    held = ((memory_mib * MIB // 4096) // 8 * 7) * 4096
    ramp = held // 4096 // 7 * 2 * 4096
    expected = (
        "COMPARE_CGROUP path=/compare-w4 limits=default_unlimited",
        "W4_CGROUP path=/compare-w4 limits=default_unlimited",
        (f"PRESSURE pattern=real-corpus held_bytes={held} ramp_pattern=real-corpus ramp_bytes={ramp}"
         if real_corpus else
         f"PRESSURE pattern=halfzero held_bytes={held} ramp_pattern=random ramp_bytes={ramp}"),
        f"PRESSURE ramp_released_bytes={ramp} held_bytes={held}",
    )
    prefixes = ("COMPARE_CGROUP ", "W4_CGROUP ", "PRESSURE pattern=",
                "PRESSURE ramp_released_bytes=")
    wrong = [prefix for prefix, marker in zip(prefixes, expected)
             if [line for line in lines if line.startswith(prefix)] != [marker]]
    return {"w4_scope_valid": not wrong,
            "w4_scope_error": "; ".join("missing/duplicate/mismatched " + prefix for prefix in wrong)}


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    # Keep the previous complete JSON until the new version is ready.
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def cpu_set(text):
    result = set()
    for item in text.split(","):
        if re.fullmatch(r"[0-9]+", item):
            result.add(int(item))
        elif re.fullmatch(r"[0-9]+-[0-9]+", item):
            lo, hi = map(int, item.split("-"))
            if lo > hi or hi > 4095:
                raise ValueError("invalid CPU interval")
            result.update(range(lo, hi + 1))
        else:
            raise ValueError("invalid CPU list")
    if not result or not result <= os.sched_getaffinity(0):
        raise ValueError("CPU list exceeds current affinity")
    return sorted(result)


def floor(memory_mib, host_volume):
    available = int(next(s.split()[1] for s in Path("/proc/meminfo").read_text().splitlines()
                         if s.startswith("MemAvailable:"))) * 1024
    if available < (memory_mib + 2048) * MIB:
        raise RuntimeError("insufficient available RAM with 2 GiB host reserve")
    if shutil.disk_usage(host_volume).free < 32 * 1024**3:
        raise RuntimeError("host volume has less than 32 GiB free")


def kernel_identity(directory, backend):
    identity = (directory / "source.txt").read_text().splitlines()
    requested = "memcomp" if backend.startswith("memcomp-") else backend
    if not identity or "backend=" + requested not in identity:
        raise ValueError("kernel artifact backend mismatch")
    hashes = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        sha, name = line.split("  ", 1)
        if (name not in ("bzImage", ".config") or name in hashes or
                not re.fullmatch(r"[0-9a-f]{64}", sha) or digest(directory / name) != sha):
            raise ValueError("kernel artifact hash mismatch")
        hashes[name] = sha
    if set(hashes) != {"bzImage", ".config"}:
        raise ValueError("kernel manifest must cover both bzImage and .config")
    config = (directory / ".config").read_text().splitlines()
    for name, value in RECLAIM["required_config"].items():
        settings = [line for line in config
                    if line.startswith(name + "=") or line == "# " + name + " is not set"]
        if settings != [name + "=" + value]:
            raise ValueError("all v4.6 backends require " + name + "=" + value)
    return identity, hashes


def option(command, name, default=None):
    count = command.count(name)
    if count > 1:
        raise ValueError("duplicate workload option " + name)
    if not count:
        return default
    index = command.index(name) + 1
    if index == len(command):
        raise ValueError("missing workload option value " + name)
    return command[index]


def config_swap_types(config_text):
    """Decode include/linux/swap.h reservations from the measured .config.

    PTE markers reserve one type in every frozen v6.12 image. Other special
    types are config-dependent; never infer migration from a sampled token.
    """
    enabled = {line.split("=", 1)[0] for line in config_text.splitlines()
               if line.startswith("CONFIG_") and line.endswith("=y")}
    device = 4 if "CONFIG_DEVICE_PRIVATE" in enabled else 0
    migration = 3 if "CONFIG_MIGRATION" in enabled else 0
    poison = 1 if "CONFIG_MEMORY_FAILURE" in enabled else 0
    memcomp = 1 if "CONFIG_MEMCOMP" in enabled else 0
    ordinary = (1 << 5) - device - migration - poison - memcomp - 1
    return {"migration_swap_types": list(range(ordinary + poison,
                                                ordinary + poison + migration)),
            "memcomp_swap_type": (ordinary + poison + migration + device
                                  if memcomp else None)}


def corpus_header(path, with_identity=False, with_digest=False):
    """Inspect the transport; optionally fingerprint its bytes without staging."""
    source = HERE.parent / "corpus" / "pack.py"
    spec = importlib.util.spec_from_file_location("memcomp_corpus_pack", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.read_header(path, with_identity=with_identity, with_digest=with_digest)


def corpus_expected_binding(path, identity_json, header_sha256):
    """Parse an optional external batch binding, never silently weaken it."""
    if (identity_json is None) != (header_sha256 is None):
        missing = "--corpus-expected-identity" if identity_json is None else "--corpus-header-sha256"
        raise ValueError("corpus expected identity and header SHA256 must be supplied together; missing " + missing)
    if identity_json is None:
        return None
    if path is None:
        raise ValueError("corpus expected binding requires --corpus-image")
    try:
        identity = json.loads(identity_json)
    except (TypeError, ValueError):
        raise ValueError("invalid expected corpus identity JSON") from None
    if (type(identity) is not list or len(identity) != 7 or
            any(type(value) is not int or value < 0 for value in identity)):
        raise ValueError("expected corpus identity must be a JSON array of seven nonnegative integers")
    if type(header_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", header_sha256) is None:
        raise ValueError("expected corpus header SHA256 must be 64 lowercase hex digits")
    return tuple(identity), header_sha256


def corpus_sample_index(n, target, slot):
    """Page-number-only v4.11 target then ordered complement, no page I/O.

    Python's arbitrary precision mirrors the C __uint128_t products. Indices
    are uint64_t; floats, byte content, hashes and codec ratios are not inputs.
    """
    if (any(type(value) is not int for value in (n, target, slot)) or
            not 1 <= target <= n <= UINT64_MAX or not 0 <= slot < n):
        raise ValueError("invalid uniform-v411 page domain")
    if slot < target:
        return slot * n // target
    return ((slot - target + 1) * n - 1) // (n - target)


def corpus_selection_index(selection, slot):
    if selection.get("protocol") != CORPUS_SAMPLING_PROTOCOL:
        raise ValueError("unknown corpus selection protocol")
    return selection["start_page"] + corpus_sample_index(
        selection["source_pages"], selection["target_pages"], slot)


def corpus_selection(source, target, groups, sources):
    """Describe every used set by ordered physical page IDs, never page bytes."""
    base, n = source["start_page"], source["pages"]
    if (type(base) is not int or type(n) is not int or
            base < 0 or n < 1 or base + n > UINT64_MAX):
        raise ValueError("invalid source page range")
    if (type(target) is not int or target < 1 or
            any(type(count) is not int or count < 0 for _, count in groups) or
            target + sum(count for _, count in groups) > n):
        raise ValueError("corpus too small for target and disjoint auxiliary sets")
    selection = {"protocol": CORPUS_SAMPLING_PROTOCOL, "start_page": base,
                 "source_pages": n, "target_pages": target,
                 "target_rule": "floor(k*N/T), 0<=k<T",
                 "auxiliary_rule": "remaining pages in original order",
                 "identity_digest_encoding": "ordered absolute page IDs, uint64 little-endian",
                 "sets": {}, "source_application_pages": {},
                 "content_statistics": "not inspected; requires separate private corpus analysis"}
    for item in sources:
        count = max(0, min(base + n, item["start_page"] + item["pages"]) -
                    max(base, item["start_page"]))
        if count:
            selection["source_application_pages"][item["label"]] = count
    if sum(selection["source_application_pages"].values()) != n:
        raise ValueError("source table does not cover selection")
    boundaries = [item["start_page"] for item in sources]
    first = 0
    for name, count in [("target", target), *groups]:
        if name in selection["sets"]:
            raise ValueError("duplicate corpus set")
        digest = hashlib.sha256()
        applications = Counter()
        for slot in range(first, first + count):
            absolute = corpus_selection_index(selection, slot)
            digest.update(absolute.to_bytes(8, "little"))
            applications[sources[bisect_right(boundaries, absolute) - 1]["label"]] += 1
        selection["sets"][name] = {
            "first_slot": first, "pages": count,
            "page_identity_sha256": digest.hexdigest(),
            "application_pages": dict(applications),
            "application_fractions": {name: pages / count for name, pages in applications.items()}}
        first += count
    return selection


def corpus_touch_index(spec, index):
    sampling = spec.get("corpus_sampling", "contiguous-v1")
    if sampling == CORPUS_SAMPLING_V411:
        selection = spec["corpus_selection"]
        if (selection["target_pages"] != spec["pages"] or
                selection["start_page"] != spec["corpus_first"]):
            raise ValueError("corpus selection does not match the target mapping")
        return corpus_selection_index(selection, index)
    if sampling != "contiguous-v1":
        raise ValueError("unknown corpus sampling protocol")
    return spec["corpus_first"] + index


def corpus_v411_preflight(header, spec, memory_mib):
    sources = header["sources"]
    if spec["corpus_offset"]:
        raise ValueError("uniform-v411 samples the entire source; offset must be zero")
    if spec["label"] == "w2":
        count, size = spec["workers"], spec["worker_bytes"]
        if size <= 0 or size % 4096 or count > len(sources):
            raise ValueError("W2 needs one real application per worker and whole pages")
        spec["corpus_workers"] = [
            {**source, "requested_pages": size // 4096,
             "selection": corpus_selection(source, size // 4096, [], sources)}
            for source in sources[:count]]
        return
    name = spec["corpus_source"]
    if spec["label"] in ("w3", "w4") and name != "combined":
        raise ValueError("uniform-v411 W3/W4 require the complete combined source")
    source = ({"start_page": 0, "pages": header["total_pages"]} if name == "combined"
              else next((item for item in sources if item["label"] == name), None))
    if source is None:
        raise ValueError("unknown uniform-v411 corpus source")
    target = spec["pages"]
    if spec["label"] == "w1":
        groups = [("prime", target)]
    elif spec["label"] == "w3":
        groups = [("pressure", spec["pressure_mib"] * 256)]
    else:
        held = memory_mib * 256 // 8 * 7
        groups = [("held", held), ("ramp", held // 7 * 2)]
    selection = corpus_selection(source, target, groups, sources)
    spec.update(corpus_first=source["start_page"], corpus_target_pages=target,
                corpus_extra_pages=sum(count for _, count in groups),
                corpus_sources=sources, corpus_selection=selection)


def corpus_preflight(header, spec, memory_mib):
    """Require disjoint real slices, refusing short inputs instead of padding."""
    if spec.get("corpus_sampling") == CORPUS_SAMPLING_V411:
        return corpus_v411_preflight(header, spec, memory_mib)
    sources = header["sources"]
    if spec["label"] == "w2":
        count, pages = spec["workers"], spec["worker_bytes"] // 4096
        if spec["worker_bytes"] % 4096 or count > len(sources):
            raise ValueError("W2 needs one different real application per worker")
        if any(source["pages"] < pages for source in sources[:count]):
            raise ValueError("W2 application corpus too small; replication is forbidden")
        spec["corpus_workers"] = [{**source, "requested_pages": pages}
                                  for source in sources[:count]]
        return
    name, offset = spec["corpus_source"], spec["corpus_offset"]
    selected = ({"start_page": 0, "pages": header["total_pages"]} if name == "combined"
                else next((source for source in sources if source["label"] == name), None))
    if selected is None or offset < 0:
        raise ValueError("unknown corpus source or negative offset")
    target = spec["pages"]
    if spec["label"] == "w1":
        extra = target  # a disjoint real rewrite-prime slice, not random generation
    elif spec["label"] == "w3":
        extra = spec["pressure_mib"] * 256
    else:
        held = memory_mib * 256 // 8 * 7
        extra = held + held // 7 * 2  # existing transient-ramp size, now real
    if offset + target + extra > selected["pages"]:
        raise ValueError("corpus has %d pages but target+disjoint pressure/prime need %d; "
                         "no generated or repeated padding is permitted" %
                         (selected["pages"], offset + target + extra))
    spec.update(corpus_first=selected["start_page"] + offset,
                corpus_target_pages=target, corpus_extra_pages=extra,
                corpus_sources=sources)


def workload_spec(label, command, smoke=False, backend=None):
    executable = {"w1": "/refault", "w2": "/app_switch", "w3": "/shmem", "w4": "/refault"}
    if not command or command[0] != executable[label]:
        raise ValueError("label does not match workload executable")
    cpu = int(option(command, "--cpu", "-1"))
    if cpu != 1:
        raise ValueError("the measurement process must be pinned to guest CPU 1")
    spec = {"label": label, "guest_cpu": cpu,
            "seed": int(option(command, "--seed", "1")),
            "order": option(command, "--order", "rand")}
    real = "--corpus" in command
    if "--functional-synthetic" in command and real:
        raise ValueError("corpus and functional generator are mutually exclusive")
    if real and (command.count("--corpus") != 1 or option(command, "--corpus") != "/dev/vdc" or
                 "--content" in command):
        raise ValueError("real input must be /dev/vdc with no generated content label")
    spec.update(real_corpus=real, corpus_source=option(command, "--corpus-source", "combined"),
                corpus_offset=int(option(command, "--corpus-offset", "0")))
    if spec["corpus_offset"] < 0:
        raise ValueError("negative corpus offset")
    sampling = option(command, "--corpus-sampling", "contiguous-v1")
    if any(arg.startswith("--corpus-sampling=") for arg in command):
        raise ValueError("use separate --corpus-sampling uniform-v411 arguments")
    if sampling not in ("contiguous-v1", CORPUS_SAMPLING_V411):
        raise ValueError("unknown corpus sampling version")
    if "--corpus-sampling" in command and sampling != CORPUS_SAMPLING_V411:
        raise ValueError("legacy sampling is selected by omitting the versioned opt-in")
    if sampling == CORPUS_SAMPLING_V411 and not real:
        raise ValueError("uniform-v411 requires real corpus")
    spec["corpus_sampling"] = sampling
    spec["backend"] = backend
    spec["active_swap_types"] = [0] if backend in BACKENDS and backend != "memcomp-noswap" else []
    spec["memcomp_swap_type"] = 30 if backend and backend.startswith("memcomp-") else None
    if not smoke and spec["seed"] != 1:
        raise ValueError("formal workloads require seed 1")
    if label != "w1" and "--rewrite" in command:
        raise ValueError("--rewrite is W1-only")
    if label == "w2":
        spec.update(worker_bytes=int(option(command, "--worker-bytes", "67108864")),
                    workers=int(option(command, "--workers", "8")),
                    switches=int(option(command, "--switches", "32")), workload_id="w2")
        if not 1 <= spec["workers"] <= 64 or not 1 <= spec["switches"] <= 1000000:
            raise ValueError("invalid W2 worker/switch count")
        if not smoke:
            fixed = {"--workers": "8", "--worker-bytes": "67108864",
                     "--memory-max": "268435456", "--switches": "32",
                     "--swap-max": "max", "--cgroup-parent": "/sys/fs/cgroup"}
            if any(option(command, key) != value for key, value in fixed.items()):
                raise ValueError("formal W2 requires the fixed application-switch workload")
            if "--allow-pressure" not in command:
                raise ValueError("W2 needs its guest pressure opt-in")
        return spec
    spec.update(pages=int(option(command, "--pages", "4096")),
                content="corpus" if real else option(command, "--content", "mixed"))
    if not 1 <= spec["pages"] <= 1048576 or spec["content"] not in ("mixed", "text", "random", "same", "corpus"):
        raise ValueError("invalid page count or content")
    if label == "w1":
        if command.count("--rewrite") != 1:
            raise ValueError("v4.6 W1 requires exactly one --rewrite (all groups, no extra slots)")
        spec["rewrite"] = True
        spec.update(rounds=int(option(command, "--rounds", "5")),
                    warmup=int(option(command, "--warmup", "1")),
                    single=min(int(option(command, "--single", "4096")), spec["pages"]),
                    workload_id="w1-" + ("corpus-" + spec["corpus_source"] if real else spec["content"]))
        if not 1 <= spec["rounds"] <= 9 or not 0 <= spec["warmup"] <= 1 or spec["single"] < 1:
            raise ValueError("invalid W1 rounds/warmup/single count")
        if not smoke and (spec["pages"], spec["rounds"], spec["warmup"], spec["single"]) != (16384, 5, 1, 4096):
            raise ValueError("formal W1 requires 16384 pages, 5 rounds, 1 warmup and 4096 single touches")
    elif label == "w3":
        if real and command.count("--anonymous") != 1:
            raise ValueError("v4.10 W3 real pages require explicit --anonymous; old shmem remains historical")
        spec.update(pressure_mib=int(option(command, "--pressure-mib", "0")),
                    mapping="private-anonymous" if real else "legacy-shmem",
                    workload_id="w3-anon-512m" if real else "w3-512m")
        if not smoke and (spec["pages"], spec["pressure_mib"]) != (131072, 512):
            raise ValueError("formal W3 requires 512 MiB tmpfs and 512 MiB pressure")
    else:
        if backend not in W4_SOURCES:
            raise ValueError("W4 requires memcomp-swap, zswap or zram-writeback")
        spec["workload_id"] = "w4-832m"
        if any(flag in command for flag in ("--rounds", "--warmup", "--single")):
            raise ValueError("W4 uses COMPARE_WORKLOAD_LABEL=w4, not W1 round flags")
        if not smoke and spec["pages"] != 212992:
            raise ValueError("formal W4 requires 832 MiB")
    return spec


def integer(value, minimum=0):
    if not re.fullmatch(r"-?[0-9]+", value or "") or int(value) < minimum:
        raise ValueError("invalid integer field: " + repr(value))
    return int(value)


def validate_csv(lines, spec):
    """Validate structure and *all* records; an exit status is not coverage."""
    report = {"csv_integrity": False, "workload_rows": 0,
              "expected_workload_rows": None, "drain_timed_out_rows": 0,
              "unknown_special_swapped_touches": 0, "migration_observed_touches": 0,
              "classification_counts": {}, "snapshot_classification_counts": {},
              "refault_eligible_touches": 0, "refault_counted_touches": 0,
              "readahead_hit_touches": 0, "phase_start_swapcache_phases": [],
              "phase_start_swapcache_pages": 0, "phase_start_swapcache_complete": False,
              "single_snapshot_cached_touches": 0,
              "writeback_latency_excluded": False, "writeback_wait_ns": None}
    if spec["label"] == "w1":
        report.update(rewrite_prime_phases=[], rewrite_prime_smoke_verdict="not_evaluated")
    classes, snapshot_classes = Counter(), Counter()
    try:
        reader = csv_module.DictReader(io.StringIO("\n".join(lines)), strict=True)
        fields = reader.fieldnames
        if not fields or len(set(fields)) != len(fields):
            raise ValueError("missing or duplicate CSV header fields")
        if spec["label"] != "w2":
            required = (PAGE_FIELDS + W1_FIELDS + SAMPLE_FIELDS + DRAIN_FIELDS + list(PRIME_FIELDS)
                        if spec["label"] == "w1" else
                        PAGE_FIELDS + SAMPLE_FIELDS + DRAIN_FIELDS + PAGE_PHASE_FIELDS)
            if spec.get("real_corpus"):
                required += ["corpus_source", "corpus_page_index"]
            if fields != required:
                raise ValueError("CSV header does not match the workload schema")
        else:
            required = {"record", "sequence", "worker", "status", "elapsed_ns",
                        "checked_words", "init_ok", "alive_before_cleanup", "wait_kind"}
            if not required <= set(fields):
                raise ValueError("incomplete W2 header")

        def rows():
            for row in reader:
                report["workload_rows"] += 1
                if None in row or None in row.values() or list(row.values()) == fields:
                    raise ValueError("ragged row or repeated CSV header")
                yield row

        def observation(row, snapshot=False):
            keys = (("snapshot_present", "snapshot_swapped", "snapshot_resident", "snapshot_swap_type")
                    if snapshot else ("present_before", "swapped_before", "resident_before", "swap_type_before"))
            present, swapped, resident, swap_type = (integer(row[key], -1) for key in keys)
            if any(value not in (-1, 0, 1) for value in (present, swapped, resident)) or swap_type > 31:
                raise ValueError("invalid PTE observation")
            memcomp = swapped == 1 and swap_type == spec.get("memcomp_swap_type")
            real_swap = swapped == 1 and swap_type in spec.get("active_swap_types", [])
            migrating = (present == 0 and swapped == 1 and
                         swap_type in spec.get("migration_swap_types", []))
            kind = ("present" if present == 1 else
                    "memcomp" if present == 0 and memcomp else
                    "migration" if migrating else
                    "swapped_nonresident" if present == 0 and real_swap and resident == 0 else
                    "cached_nonpresent" if present == 0 and real_swap and resident == 1 else
                    "other_nonresident" if present == 0 and swapped == 0 and resident == 0 else "unknown")
            field = "snapshot_classification" if snapshot else "classification"
            # Original guest CSV labels all special non-memcomp types unknown.
            # Preserve those bytes; recognition is a host-side v4.9 amendment.
            if row[field] != kind and not (migrating and row[field] == "unknown"):
                raise ValueError(field + " disagrees with PTE/swap-cache observation")
            return kind, (present == 0 and swapped == 1 and not memcomp and
                          not real_swap and not migrating)

        def cache_phase(round_id=None, mode=None, warmup=False):
            entry = {"workload": spec["label"], "round": round_id, "mode": mode,
                     "warmup": bool(warmup), "observed_pages": 0, "expected_pages": spec["pages"],
                     "snapshot_cached_pages": 0, "drain_remaining_swapcache_pages": None,
                     "complete": False}
            report["phase_start_swapcache_phases"].append(entry)
            return entry

        def touch(row, sequence, seen, mode=None, phase_cache=None):
            if integer(row["sequence"]) != sequence or row["verify_ok"] != "1":
                raise ValueError("touch sequence or data verification failed")
            index = integer(row["page_index"])
            if index >= spec["pages"] or seen[index]:
                raise ValueError("duplicate/out-of-range page index")
            seen[index] = 1
            if mode == "seq" and index != sequence:
                raise ValueError("sequential mode is not sequential")
            if (row["workload"] != spec["label"] or
                    row["content"] != spec["content"] or integer(row["seed"]) != spec["seed"] or
                    row["order"] != ("seq" if mode == "seq" else "rand" if mode else spec["order"]) or
                    integer(row["page_size"]) != 4096 or
                    integer(row["cpu_before"]) != spec["guest_cpu"]):
                raise ValueError("touch workload/content/page size/CPU mismatch")
            if spec.get("real_corpus"):
                if row["page_kind"] != "real-unclassified":
                    raise ValueError("real corpus must not inherit generated page kinds")
                absolute = corpus_touch_index(spec, index)
                source = next((item for item in spec["corpus_sources"]
                               if item["start_page"] <= absolute < item["start_page"] + item["pages"]), None)
                if source is None or row["corpus_source"] != source["label"] or integer(row["corpus_page_index"]) != absolute:
                    raise ValueError("touch corpus source/page identity mismatch")
            elif (row["page_kind"] not in ("zero", "same", "text", "record", "alpha", "random") or
                    (spec["content"] != "mixed" and row["page_kind"] != spec["content"])):
                raise ValueError("invalid actual page kind (not a rotated placeholder)")
            integer(row["touch_ns"])
            kind, unknown = observation(row)
            frozen, snapshot_unknown = observation(row, snapshot=True)
            if unknown or snapshot_unknown:
                report["unknown_special_swapped_touches"] += 1
            if kind == "migration" or frozen == "migration":
                report["migration_observed_touches"] += 1
            eligible = int(frozen in ("memcomp", "swapped_nonresident"))
            counted = int(eligible and kind in ("memcomp", "swapped_nonresident", "cached_nonpresent"))
            readahead = int(counted and frozen == "swapped_nonresident" and kind == "cached_nonpresent")
            values = (eligible, counted, readahead)
            if tuple(row[key] for key in SAMPLE_FIELDS[2:]) != tuple(str(value) for value in values):
                raise ValueError("snapshot eligibility/refault_counted/readahead_hit contradict observations")
            classes[kind] += 1
            snapshot_classes[frozen] += 1
            if mode == "single":
                # This is an immediate per-touch snapshot, not a drained phase.
                report["single_snapshot_cached_touches"] += int(frozen == "cached_nonpresent")
            elif phase_cache is not None:
                phase_cache["observed_pages"] += 1
                phase_cache["snapshot_cached_pages"] += int(frozen == "cached_nonpresent")
            for name, value in zip(("refault_eligible_touches", "refault_counted_touches",
                                    "readahead_hit_touches"), values):
                report[name] += value
            return values

        def phase(row, population=None):
            rounds, remaining, remapped, eligible, counted, readahead = (
                integer(row[key]) for key in DRAIN_FIELDS[:-1])
            if (rounds > 4 or remaining > spec["pages"] or remapped > 4 * spec["pages"] or
                    not 0 <= readahead <= counted <= eligible <= spec["pages"]):
                raise ValueError("impossible drain/population phase counters")
            if population is not None and (eligible, counted, readahead) != tuple(population):
                raise ValueError("phase population does not match actual snapshot/touch records")
            ratio = row["readahead_hit_ratio"]
            if counted:
                value = float(ratio)
                if (not math.isfinite(value) or not 0 <= value <= 1 or
                        not math.isclose(value, readahead / counted, rel_tol=0, abs_tol=1e-9)):
                    raise ValueError("invalid phase readahead hit ratio")
            elif ratio != "":
                raise ValueError("empty counted population must have N/A (empty) hit ratio")
            integer(row["purge_ns"])
            if row["drain_timed_out"] not in ("0", "1"):
                raise ValueError("invalid drain timeout flag")
            report["drain_timed_out_rows"] += int(row["drain_timed_out"])
            return tuple(row[key] for key in DRAIN_FIELDS + ["purge_ns", "drain_timed_out"])

        def writeback(row):
            source = W4_SOURCES.get(spec["backend"])
            if (source is None or row["writeback_source"] != source or
                    row["pressure_ready"] != "1" or row["writeback_proven"] != "1" or
                    row["writeback_explicit_idle"] != str(int(spec["backend"] == "zram-writeback"))):
                raise ValueError("W4 backend/source/pressure/writeback proof mismatch")
            before, after, delta = (integer(row[key]) for key in
                                    ("writeback_before", "writeback_after", "writeback_delta"))
            if delta <= 0 or after - before != delta:
                raise ValueError("W4 requires positive actual writeback delta matching counters")
            pressure = integer(row["pressure_bytes"], 4096)
            memory_mib = spec.get("guest_memory_mib")
            if type(memory_mib) is not int or not 256 <= memory_mib <= 4096:
                raise ValueError("W4 requires the configured guest_memory_mib, not observed MemTotal")
            page_size = integer(row["page_size"], 1)
            expected_pressure = memory_mib * MIB // page_size // 8 * 7 * page_size
            if pressure != expected_pressure:
                raise ValueError("W4 pressure_bytes does not match configured guest RAM")
            if row["writeback_wait_timed_out"] not in ("0", "1"):
                raise ValueError("invalid W4 writeback timeout flag")
            wait_ns = integer(row["writeback_wait_ns"])
            polls = integer(row["writeback_stable_polls"])
            timeout = row["writeback_wait_timed_out"] == "1"
            if timeout != (wait_ns >= W4_PROTOCOL["wait_cap_seconds"] * 10**9):
                raise ValueError("W4 timeout contradicts the 300-second cap")
            if not timeout and polls < 3:
                raise ValueError("W4 requires three complete unchanged counter intervals")
            report.update(writeback_latency_excluded=timeout, writeback_wait_ns=wait_ns)
            return tuple(row[key] for key in WRITEBACK_FIELDS)

        records = iter(rows())
        if spec["label"] == "w1":
            total = spec["rounds"] + spec["warmup"]
            report["expected_workload_rows"] = total * (3 * spec["pages"] + spec["single"] + 3)
            for round_id in range(total):
                for mode in ("seq", "rand", "single", "rewrite"):
                    count = spec["single"] if mode == "single" else spec["pages"]
                    seen = bytearray(spec["pages"])
                    population = [0, 0, 0]
                    phase_cache = (None if mode == "single" else
                                   cache_phase(round_id, mode, round_id < spec["warmup"]))
                    for sequence in range(count + (mode != "single")):
                        row = next(records)
                        batch = sequence == count
                        if (row["method"] != METHOD or integer(row["round"]) != round_id or
                                row["warmup"] != str(int(round_id < spec["warmup"])) or
                                row["mode"] != mode or row["record"] != ("batch" if batch else "touch") or
                                row["order"] != ("seq" if mode == "seq" else "rand")):
                            raise ValueError("wrong W1 method/round/warmup/mode/record/order")
                        prime = prime_counts(row, spec)
                        if prime is not None:
                            report["rewrite_prime_phases"].append({
                                "round": round_id, "warmup": round_id < spec["warmup"],
                                "source": row["prime_source"], **prime,
                                "prime_real_swap_fraction": prime["prime_real_swap_pages"] / spec["pages"],
                                "smoke_verdict": "pending_realguest_review"})
                            report["rewrite_prime_smoke_verdict"] = "pending_realguest_review"
                            report["drain_timed_out_rows"] += prime["prime_drain_timed_out"]
                        if not batch:
                            values = touch(row, sequence, seen, mode, phase_cache)
                            population = [a + b for a, b in zip(population, values)]
                            if any(row[key] for key in DRAIN_FIELDS + W1_FIELDS[6:]):
                                raise ValueError("W1 touch has batch-only phase fields")
                            if mode == "single":
                                integer(row["pageout_ns"])
                            elif row["pageout_ns"]:
                                raise ValueError("batch-mode touch has single pageout timer")
                            continue
                        # A batch is an aggregate, not an unverified touch.
                        if (row["workload"] != "w1" or row["content"] != spec["content"] or
                                integer(row["seed"]) != spec["seed"] or integer(row["page_size"]) != 4096):
                            raise ValueError("batch workload identity mismatch")
                        if any(row[key] for key in PAGE_FIELDS[5:] + SAMPLE_FIELDS):
                            raise ValueError("batch has touch-only fields")
                        if integer(row["requested_pages"]) != spec["pages"]:
                            raise ValueError("wrong batch page count")
                        if spec.get("real_corpus") and (row["corpus_source"] or row["corpus_page_index"]):
                            raise ValueError("batch has touch-only corpus fields")
                        first_pass_counts(row)
                        if integer(row["nonpresent_pages"]) > spec["pages"]:
                            raise ValueError("impossible batch nonpresent count")
                        for key in ("pageout_total_ns", "purge_ns"):
                            integer(row[key])
                        for key in ("memfree_before_kib", "memfree_after_kib",
                                    "memavailable_before_kib", "memavailable_after_kib"):
                            integer(row[key], -1)
                        if mode == "seq":
                            if any(integer(row[key], -1) < 0 for key in
                                   ("memfree_before_kib", "memavailable_before_kib")):
                                raise ValueError("seq requires a fresh fill-before memory baseline")
                        elif any(row[key] != "-1" for key in
                                 ("memfree_before_kib", "memavailable_before_kib")):
                            raise ValueError("rand/rewrite cannot reuse the fresh seq memory baseline")
                        if row["pageout_ns"]:
                            if not math.isfinite(float(row["pageout_ns"])) or float(row["pageout_ns"]) < 0:
                                raise ValueError("invalid normalized pageout time")
                        phase(row, population)
                        phase_cache["drain_remaining_swapcache_pages"] = integer(row["drain_remaining_swapcache_pages"])
                        phase_cache["complete"] = True
        elif spec["label"] in ("w3", "w4"):
            report["expected_workload_rows"] = spec["pages"]
            seen = bytearray(spec["pages"])
            population = [0, 0, 0]
            phase_record = writeback_record = None
            phase_cache = cache_phase(mode=spec["order"])
            for sequence in range(spec["pages"]):
                row = next(records)
                if row["method"] != METHOD:
                    raise ValueError("W3/W4 requires method=v4.6")
                values = touch(row, sequence, seen, phase_cache=phase_cache)
                population = [a + b for a, b in zip(population, values)]
                current = phase(row)
                if phase_record is not None and current != phase_record:
                    raise ValueError("inconsistent repeated read-phase diagnostics")
                phase_record = current
                # Repeated on every CSV row, but record it once per read phase.
                phase_cache["drain_remaining_swapcache_pages"] = integer(row["drain_remaining_swapcache_pages"])
                if spec["label"] == "w4":
                    current = writeback(row)
                    if writeback_record is not None and current != writeback_record:
                        raise ValueError("inconsistent repeated W4 writeback diagnostics")
                    writeback_record = current
                elif any(row[key] for key in WRITEBACK_FIELDS):
                    raise ValueError("W3 must not publish W4 writeback proof fields")
            if tuple(integer(row[key]) for key in DRAIN_FIELDS[3:6]) != tuple(population):
                raise ValueError("read-phase population differs from snapshot/touch records")
            phase_cache["complete"] = True
        else:
            workers, switches = spec["workers"], spec["switches"]
            report["expected_workload_rows"] = 2 * workers + switches
            for record, count in (("init", workers), ("switch", switches), ("pid_exit", workers)):
                for sequence in range(count):
                    row = next(records)
                    if row["record"] != record:
                        raise ValueError("missing, reordered or extra W2 record")
                    worker = integer(row["worker"])
                    if worker >= workers:
                        raise ValueError("invalid W2 worker")
                    expected_sequence = sequence + 1 if record == "switch" else 0
                    if integer(row["sequence"]) != expected_sequence or (record != "switch" and worker != sequence):
                        raise ValueError("invalid W2 sequence/worker coverage")
                    statuses = {"init": ("ready", "unavailable"), "switch": ("ok", "unavailable"),
                                "pid_exit": ("reaped",)}
                    if (row["status"] not in statuses[record] or
                            (record == "pid_exit" and row["wait_kind"] not in ("exit", "signal"))):
                        raise ValueError("W2 data/protocol/reap failure")
                    if record == "switch" and row["status"] == "ok":
                        integer(row["elapsed_ns"])
                        if integer(row["checked_words"]) != spec["worker_bytes"] // 8:
                            raise ValueError("incompletely verified successful W2 switch")
            # Worker loss is a measured outcome, not replacement with a survivor.
        if next(records, None) is not None:
            raise ValueError("extra workload rows")
        report["csv_integrity"] = True
    except (ValueError, TypeError, KeyError, IndexError, StopIteration, csv_module.Error) as exc:
        report["csv_error"] = str(exc) or "truncated workload CSV"
    report["classification_counts"] = dict(classes)
    report["snapshot_classification_counts"] = dict(snapshot_classes)
    report["phase_start_swapcache_pages"] = sum(
        entry["snapshot_cached_pages"] for entry in report["phase_start_swapcache_phases"])
    report["phase_start_swapcache_complete"] = report["csv_integrity"] and spec["label"] != "w2"
    # Count only frozen eligible members actually refaulted; a later cache hit
    # is included, while snapshot-present/cached pages never enter the cohort.
    report["eligible_refault_touches"] = report["refault_counted_touches"]
    return report


def phase_snapshot_evidence(path, label, backend, writeback_timed_out=False):
    """Use phase raw snapshots, not CSV/console guesses, for boot and W4 IO.

    parse_log streams snapshots to consume; drop raw blocks immediately after
    extracting these few scalars. Never retain a raw long-form telemetry table.
    """
    captured = {}
    wanted = {"boot"} | ({"pressure_begin", "pressure_ready", "writeback_settled",
                          "writeback_unstable", "read_begin", "read_end"} if label == "w4" else set())

    def consume(snapshot, _log):
        try:
            if snapshot.phase not in wanted:
                return
            if snapshot.phase in captured:
                captured[snapshot.phase] = {"error": "duplicate phase snapshot"}
                return
            values = {"ns": snapshot.ns}
            captured[snapshot.phase] = values

            def block(path):
                raw = snapshot.blocks.get(path)
                if raw is None or raw.error or raw.end is None:
                    raise ValueError("missing/incomplete raw block " + path)
                return raw.lines

            if snapshot.phase == "boot":
                value = "\n".join(block(RECLAIM["boot_enabled_path"])).strip()
                if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", value):
                    raise ValueError("malformed boot lru_gen/enabled mask")
                values["mask"] = int(value, 16 if value.lower().startswith("0x") else 10)
            elif snapshot.phase in ("pressure_begin", "read_begin", "read_end"):
                disks = [line.split() for line in block("/proc/diskstats")]
                rows = [row for row in disks if len(row) >= 3 and row[2] == "vda"]
                if len(rows) != 1 or len(rows[0]) < 14:
                    raise ValueError("swap backing vda missing/duplicate/malformed in diskstats")
                values["write_sectors"] = integer(rows[0][9])
                if backend == "memcomp-swap":
                    pairs = [line.split() for line in block(
                        "/sys/kernel/mm/memcomp/packed_swap_events")]
                    counts = [row[1] for row in pairs if len(row) == 2 and row[0] == "writeback_payloads"]
                elif backend == "zswap":
                    pairs = [line.split() for line in block("/proc/vmstat")]
                    counts = [row[1] for row in pairs if len(row) == 2 and row[0] == "zswpwb"]
                else:
                    fields = " ".join(block("/sys/block/zram0/bd_stat")).split()
                    counts = fields[2:3] if len(fields) >= 3 else []
                if len(counts) != 1:
                    raise ValueError("missing/duplicate native W4 writeback counter")
                values["writeback_count"] = integer(counts[0])
                if backend == "zram-writeback":
                    backing = "\n".join(block("/sys/block/zram0/backing_dev")).strip()
                    if backing != "/dev/vda":
                        raise ValueError("zram writeback backing device is not vda")
                else:
                    swaps = [line.split() for line in block("/proc/swaps")[1:] if line.strip()]
                    if len(swaps) != 1 or swaps[0][0] != "/dev/vda":
                        raise ValueError("W4 active swap device is not the sole vda")
            if backend == "memcomp-swap" and snapshot.phase in (
                    "writeback_settled", "writeback_unstable", "read_begin"):
                gate = [row[1] for line in block("/sys/kernel/mm/memcomp/packed_swap_events")
                        if len(row := line.split()) == 2 and row[0] == "writeback_gate_open"]
                values["gate_open"] = int(gate[0]) if len(gate) == 1 and gate[0] in ("0", "1") else None
        except (ValueError, IndexError, TypeError) as exc:
            captured[snapshot.phase]["error"] = str(exc)
        finally:
            snapshot.blocks.clear()

    observed = parse_log(path, consume)
    boot = captured.get("boot", {})
    mask = boot.get("mask")
    boot_error = boot.get("error", "")
    if mask is None:
        boot_error = boot_error or "missing boot snapshot/mask"
    elif not mask & RECLAIM["boot_required_mask"]:
        boot_error = "boot MGLRU core bit 0x1 is off"
    reclaim = {"mglru_boot_valid": not boot_error, "mglru_boot_mask": mask,
               "mglru_boot_error": boot_error}
    evidence = {}
    if label == "w4":
        evidence = {"w4_diskstats_valid": False, "w4_diskstats_error": "",
                    "swap_write_device": "vda", "swap_write_window": ["pressure_begin", "read_begin"],
                    "swap_write_sectors_before": None, "swap_write_sectors_after": None,
                    "swap_write_bytes": None, "writeback_pressure_to_stable_ns": None,
                    "writeback_latency_excluded": bool(writeback_timed_out),
                    "writeback_gate_open_at_terminal": None}
        try:
            before, after = (captured.get(phase, {}) for phase in ("pressure_begin", "read_begin"))
            for phase in (before, after):
                if phase.get("error"):
                    raise ValueError(phase["error"])
            a, b = before["write_sectors"], after["write_sectors"]
            if after["ns"] <= before["ns"] or b <= a:
                raise ValueError("W4 requires positive pressure-to-read disk write sectors")
            native = after["writeback_count"] - before["writeback_count"]
            if native <= 0:
                raise ValueError("W4 requires positive pressure-to-read native writeback delta")
            end = captured.get("read_end", {})
            if end.get("error") or end["ns"] <= after["ns"] or end["write_sectors"] < b:
                raise ValueError("invalid W4 readback diskstats phase")
            evidence.update(w4_diskstats_valid=True, swap_write_sectors_before=a,
                            swap_write_sectors_after=b, swap_write_bytes=(b - a) * 512,
                            writeback_gate_open_at_read_begin=after.get("gate_open"),
                            writeback_pressure_to_read_native_delta=native,
                            readback_swap_write_sectors=end["write_sectors"] - b,
                            readback_swap_write_bytes=(end["write_sectors"] - b) * 512)
            ready = captured.get("pressure_ready", {})
            outcome = "writeback_unstable" if writeback_timed_out else "writeback_settled"
            opposite = "writeback_settled" if writeback_timed_out else "writeback_unstable"
            waited = captured.get(outcome, {})
            if opposite in captured or ready.get("error") or waited.get("error"):
                raise ValueError("inconsistent W4 settled/unstable phase evidence")
            if not before["ns"] < ready["ns"] < waited["ns"] < after["ns"]:
                raise ValueError("invalid W4 pressure-to-read phase order")
            evidence["writeback_gate_open_at_terminal"] = waited.get("gate_open")
            # Gate is permission, not activity. Only the stability timeout excludes latency.
            evidence["writeback_latency_excluded"] = bool(writeback_timed_out)
            if not evidence["writeback_latency_excluded"]:
                evidence["writeback_pressure_to_stable_ns"] = waited["ns"] - ready["ns"]
        except (KeyError, ValueError, TypeError) as exc:
            evidence.update(w4_diskstats_valid=False, w4_diskstats_error=str(exc),
                            writeback_latency_excluded=True)
    # Structural raw framing problems must not silently certify snapshot IO.
    if observed.issues:
        reclaim.update(mglru_boot_valid=False, mglru_boot_error="; ".join(observed.issues[:8]))
        if label == "w4":
            evidence.update(w4_diskstats_valid=False, w4_diskstats_error="; ".join(observed.issues[:8]))
    return reclaim, evidence


def swapcache_note(report):
    """C.3 disclosure for this guest; no new artifact/receipt framework."""
    phases = report["phase_start_swapcache_phases"]
    if not any(entry["snapshot_cached_pages"] or entry["drain_remaining_swapcache_pages"]
               for entry in phases):
        return ""
    lines = ["", "## v4.6 C.3: swap cache remaining at read-phase start", "",
             "WARNING: swap-cache residue was observed after the bounded drain.",
             "Post-drain snapshots contain %d cached page-phase observations (not unique pages across phases)." %
             report["phase_start_swapcache_pages"],
             "Counts below use snapshot_classification=cached_nonpresent, never pre-touch cache hits or readahead_hit.",
             "The drain counter is its last scan, reported separately from the subsequent frozen snapshot.",
             "Snapshot-cached pages are excluded from refault eligibility; nonzero residue is disclosed, not silently relabelled.",
             "Count coverage: %s." % ("complete" if report["phase_start_swapcache_complete"] else
                                      "partial/unverified; CSV validation failed; counts are observed lower bounds"),
             "W1 single per-touch snapshots are not drained read phases and are excluded here (cached single touches: %d)." %
             report["single_snapshot_cached_touches"], "",
             "| workload | round | mode | warmup | observed/expected pages | snapshot cached pages | last drain remaining pages | phase complete |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for entry in phases:
        remaining = entry["drain_remaining_swapcache_pages"]
        lines.append("| %s | %s | %s | %s | %d/%d | %d | %s | %s |" % (
            entry["workload"], entry["round"] if entry["round"] is not None else "N/A", entry["mode"],
            int(entry["warmup"]), entry["observed_pages"], entry["expected_pages"],
            entry["snapshot_cached_pages"], remaining if remaining is not None else "N/A", int(entry["complete"])))
    return "\n".join(lines) + "\n"


def heavy_tasks():
    """Refuse known concurrent VM/build jobs; loadavg alone misses idle VMs."""
    found = []
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            name = (process / "comm").read_text().strip()
            if (name.startswith(("qemu-system", "cc1", "clang", "rustc", "ld.lld")) or
                    name in ("make", "gmake", "ninja", "ld", "lto1")):
                found.append({"pid": int(process.name), "comm": name})
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    return sorted(found, key=lambda item: item["pid"])


def wait_for_host(memory, volume, metadata, destination, poll_seconds=5):
    start = time.monotonic()
    metadata["host_load_gate"] = {"threshold_1m": 1.0, "samples": 0}
    while True:
        floor(memory, volume)
        load = float(Path("/proc/loadavg").read_text().split()[0])
        heavy = heavy_tasks()
        gate = metadata["host_load_gate"]
        gate.update(samples=gate["samples"] + 1, loadavg_1m=load,
                    wait_seconds=time.monotonic() - start, heavy_tasks=heavy)
        if "initial_loadavg_1m" not in gate:
            gate["initial_loadavg_1m"] = load
        gate["allowed"] = load <= 1.0 and not heavy
        write_json(destination / "metadata.json", metadata)
        if gate["allowed"]:
            return
        print("WAIT_HOST", json.dumps(gate), flush=True)
        time.sleep(poll_seconds)  # SIGINT remains enabled, including long waits.


def acquire_lock(work):
    # Share one durable workspace parent across comparison attempts.
    name = work.resolve().parent / ("memcomp-compare-" + str(os.getuid()) + ".lock")
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise RuntimeError("unsafe comparison lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


def invoke_helper(args, stream, timeout):
    child = subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT)
    interrupted = False
    try:
        return child.wait(timeout=timeout + 90), interrupted
    except (KeyboardInterrupt, subprocess.TimeoutExpired):
        interrupted = True
        # Signal only the relay. It asks its owned supervisor to cancel and reap.
        if child.poll() is None:
            try:
                child.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            return child.wait(timeout=60), interrupted
        except subprocess.TimeoutExpired:
            return 125, interrupted  # Do not kill a supervisor or delete its disk.


def read_data_output(path, console, capacity):
    """Verify actual exported bytes against UART, never metadata as evidence.

    Call only after the existing helper's actual-death AND cleanup handshake.
    The supervisor preserves the whole raw image, never a header-selected
    prefix. Validate its framing, actual payload bytes and unused zero tail
    before gzip. A panic/timeout/short header leaves the full image untouched.
    """
    report = {"data_integrity": False, "data_guest_status": None}
    payload = b""
    try:
        declarations = re.findall(
            r"^COMPARE_DATA bytes=(0|[1-9][0-9]*) sha256=([0-9a-f]{64}) status=([01])$",
            console, re.MULTILINE)
        exports = re.findall(
            r"^e2e-qemu: data_export=(\S+) dev=([0-9]+) inode=([0-9]+) "
            r"bytes=([0-9]+) format=(raw-disk)$", console, re.MULTILINE)
        if len(declarations) != 1 or len(exports) != 1:
            raise ValueError("missing/duplicate guest data declaration or supervisor export")
        expected_bytes, expected_hash, guest_status = declarations[0]
        name, dev, inode, exported_bytes, form = exports[0]
        report.update(data_expected_bytes=int(expected_bytes), data_expected_sha256=expected_hash,
                      data_guest_status=int(guest_status), data_export_format=form)
        if Path(name) != path or int(exported_bytes) != capacity:
            raise ValueError("not the requested whole-disk export")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            st = os.fstat(stream.fileno())
            if (not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (int(dev), int(inode)) or
                    st.st_uid != os.getuid() or st.st_nlink != 1 or st.st_size != capacity):
                raise ValueError("export inode/type/owner/size mismatch")
            header = stream.read(512)
            match = re.fullmatch(rb"MEMCOMP_DATA_V1 bytes=(0|[1-9][0-9]*) "
                                 rb"sha256=([0-9a-f]{64}) status=([01])\n", header.rstrip(b"\0"))
            if not match or match.groups() != tuple(value.encode("ascii") for value in declarations[0]):
                raise ValueError("raw header is missing/damaged or differs from UART declaration")
            count = int(expected_bytes)
            if count > capacity - 512:
                raise ValueError("declared payload exceeds raw-disk capacity")
            payload = stream.read(count)
            if len(payload) != count:
                raise ValueError("short actual payload read")
            # No header-selected prefix may hide bytes written beyond its end.
            tail_bytes = 0
            while chunk := stream.read(1024 * 1024):
                tail_bytes += len(chunk)
                if chunk.strip(b"\0"):
                    raise ValueError("nonzero raw data beyond declared payload; full image retained")
            if 512 + len(payload) + tail_bytes != capacity:
                raise ValueError("short/long actual raw-disk read")
        actual_bytes = len(payload)
        actual_hash = hashlib.sha256(payload).hexdigest()
        report.update(data_actual_bytes=actual_bytes, data_actual_sha256=actual_hash,
                      data_export_identity={"dev": st.st_dev, "inode": st.st_ino})
        if actual_bytes != int(expected_bytes) or actual_hash != expected_hash:
            raise ValueError("actual payload byte count/SHA256 does not match guest declaration")
        report["data_integrity"] = True
        return payload, report
    except (OSError, ValueError) as exc:
        report["data_error"] = str(exc)
        return b"", report


def interrupt(_signal, _frame):
    raise KeyboardInterrupt



def smoke_acceptance(label, backend, status):
    """A smoke stability gate, separate from retained write-volume validity."""
    if label != "w4":
        return {"smoke_semantic_acceptance": "pending_manual_review",
                "smoke_accepted": None, "smoke_reasons": []}
    reasons = []
    if status.get("valid") is not True:
        reasons.append("transport/data/lifecycle validity failed")
    if status.get("writeback_latency_excluded") is not False:
        reasons.append("writeback readback is unstable or unproven")
    wait = status.get("writeback_wait_ns")
    if type(wait) is not int or not 0 <= wait < W4_PROTOCOL["wait_cap_seconds"] * 10**9:
        reasons.append("stability did not finish within the original 300-second cap")
    if type(status.get("swap_write_bytes")) is not int or status["swap_write_bytes"] <= 0:
        reasons.append("pressure-to-read disk write bytes must be positive")
    native = status.get("writeback_pressure_to_read_native_delta")
    if type(native) is not int or native <= 0:
        reasons.append("pressure-to-read native writeback delta must be positive")
    accepted = not reasons
    return {"smoke_semantic_acceptance": "PASS_W4_STABILITY" if accepted else "FAIL_W4_STABILITY",
            "smoke_accepted": accepted, "smoke_reasons": reasons}


def main():
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGHUP, interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    for flag, help_text in (("tree", "source for gen_init_cpio; no checkout/build"),
                            ("kernel-dir", "existing build.py artifact directory"),
                            ("binaries", "prebuilt static tools AND gen_init_cpio"),
                            ("work", "durable scratch accepted by the existing e2e helper"),
                            ("dest", "new per-boot result directory"),
                            ("host-volume", "mounted host volume; retain the 32 GiB free-space floor")):
        parser.add_argument("--" + flag, type=Path, required=True, help=help_text)
    parser.add_argument("--corpus-image", type=Path, required=True,
                        help="existing private read-only real-page image; never copied to results")
    parser.add_argument("--corpus-expected-identity",
                        help="external batch stat7 JSON array; requires --corpus-header-sha256")
    parser.add_argument("--corpus-header-sha256",
                        help="external SHA256 of all 4096 header bytes; requires expected identity")
    parser.add_argument("--backend", choices=BACKENDS, required=True)
    parser.add_argument("--algorithm", choices=("zstd", "lz4", "none"), required=True)
    parser.add_argument("--label", choices=("w1", "w2", "w3", "w4"), required=True)
    parser.add_argument("--cpus", default="4-7")
    parser.add_argument("--smp", type=int, default=2)
    parser.add_argument("--memory", type=int, default=1024)
    parser.add_argument("--data-mib", type=int, default=128)
    parser.add_argument("--backing-mib", type=int, default=2048)
    parser.add_argument("--repeats", type=int, default=1, help="must be 1: the matrix owns repetition")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--host-boot-round", type=int)
    parser.add_argument("--workload-id")
    parser.add_argument("--group-id")
    parser.add_argument("--smoke", action="store_true", help="nonformal; never promote to comparison data")
    parser.add_argument("--allow-pressure", action="store_true")
    parser.add_argument("--corpus-sampling", choices=(CORPUS_SAMPLING_V411,),
                        help="opt-in page-number uniform targets and disjoint auxiliary sets")
    parser.add_argument("--snapshot-vmstat-refresh", action="store_true",
                        help="nonformal opt-in: stock counter fold before snapshots, may alter PCP")
    parser.add_argument("--snapshot-memory-consistent", action="store_true",
                        help="W1 smoke-only bounded buddy/PCP consistency protocol")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.snapshot_memory_consistent and (not args.smoke or args.label != "w1"):
            raise ValueError("consistent memory snapshots require W1 diagnostic smoke only")
        if args.snapshot_memory_consistent and args.snapshot_vmstat_refresh:
            raise ValueError("consistent memory snapshot excludes legacy refresh")
        if args.snapshot_vmstat_refresh and not args.smoke:
            raise ValueError("snapshot vmstat refresh is diagnostic smoke only; formal protocol is unfrozen")
        if args.snapshot_vmstat_refresh and args.label != "w1":
            raise ValueError("snapshot vmstat refresh requires W1 boundary ACKs; concurrent W2 unsupported")
        expected_corpus = corpus_expected_binding(args.corpus_image, args.corpus_expected_identity,
                                                 args.corpus_header_sha256)
        if os.geteuid() == 0 or not args.allow_pressure:
            raise ValueError("unprivileged owner and --allow-pressure are required")
        if not 16 <= args.data_mib <= 256:
            raise ValueError("data disk must be 16..256 MiB")
        if args.label == "w4" and args.timeout <= W4_PROTOCOL["wait_cap_seconds"]:
            raise ValueError("W4 guest timeout must exceed its 300-second writeback wait cap")
        if args.repeats != 1 or not 5 <= args.timeout <= 3600:
            raise ValueError("exactly one boot per invocation; timeout must be 5..3600 seconds")
        if not 256 <= args.memory <= 4096 or not 64 <= args.backing_mib <= 8192 or args.smp != 2:
            raise ValueError("guest memory/backing sizes out of bounds; smp must be 2")
        cpus = cpu_set(args.cpus)
        if cpus != [4, 5, 6, 7]:
            raise ValueError("v4.6 host affinity must be CPUs 4-7")
        if (args.backend == "swap") != (args.algorithm == "none"):
            raise ValueError("swap uses algorithm=none; compressed backends require a codec")
        if args.label == "w4" and (args.backend, args.algorithm) not in (
                ("zram-writeback", "zstd"), ("zswap", "zstd"), ("memcomp-swap", "zstd")):
            raise ValueError("W4 compares the three zstd writeback groups only")
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if args.corpus_sampling:
            declared = option(command, "--corpus-sampling")
            if declared is not None and declared != args.corpus_sampling:
                raise ValueError("runner and workload sampling versions differ")
            if declared is None:
                command += ["--corpus-sampling", args.corpus_sampling]
        if not args.smoke:
            if args.host_boot_round is None:
                raise ValueError("formal run requires --host-boot-round")
            if option(command, "--corpus-sampling") != CORPUS_SAMPLING_V411:
                raise ValueError("formal workload requires --corpus-sampling uniform-v411")
            if args.corpus_expected_identity is None:
                raise ValueError("formal run requires --corpus-expected-identity")
            if args.corpus_header_sha256 is None:
                raise ValueError("formal run requires --corpus-header-sha256")
        if "--functional-synthetic" in command or option(command, "--corpus") != "/dev/vdc":
            raise ValueError("comparison runs require real corpus /dev/vdc; no functional generator")
        spec = workload_spec(args.label, command, args.smoke, args.backend)
        descriptor, corpus_identity, corpus_header_page_sha256 = corpus_header(args.corpus_image, True)
        if expected_corpus is not None:
            if corpus_identity != expected_corpus[0]:
                raise ValueError("corpus identity differs from external expected binding")
            if corpus_header_page_sha256 != expected_corpus[1]:
                raise ValueError("corpus header SHA256 differs from external expected binding")
        corpus_preflight(descriptor, spec, args.memory)
        # Same configured RAM feeds QEMU, metadata, initramfs and CSV validation.
        spec["guest_memory_mib"] = args.memory
        group = args.backend + "-" + args.algorithm
        if not args.smoke and (args.backend, args.algorithm) not in (
                ("swap", "none"), ("zram", "zstd"), ("zram", "lz4"),
                ("zram-writeback", "zstd"), ("zswap", "zstd"), ("zswap-crosscheck", "zstd"),
                ("memcomp-noswap", "zstd"), ("memcomp-noswap", "lz4"),
                ("memcomp-swap", "zstd"), ("memcomp-swap", "lz4")):
            raise ValueError("not one of the ten formal variants")
        if args.group_id not in (None, group) or args.workload_id not in (None, spec["workload_id"]):
            raise ValueError("group/workload ID disagrees with the workload")
        if not args.smoke and (args.host_boot_round is None or not 1 <= args.host_boot_round <= (10 if args.label in ("w1", "w2") else 5)):
            raise ValueError("formal run requires its matrix host boot round")
        if not args.smoke and (args.memory, args.backing_mib) != (1024, 2048):
            raise ValueError("formal v4.6 requires 1024 MiB RAM and 2048 MiB backing")
        if args.smoke and args.host_boot_round is not None:
            raise ValueError("smoke has no formal host boot round")
        for key in ("tree", "binaries", "kernel_dir", "work", "dest", "host_volume", "corpus_image"):
            setattr(args, key, getattr(args, key).resolve())
        if args.dest.exists():
            raise ValueError("destination already exists: never reuse or replace results")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    args.dest.mkdir(parents=True)
    metadata = {"method": METHOD, "kind": "smoke" if args.smoke else "interleaved-comparison",
                "host_boot_round": args.host_boot_round, "workload_id": spec["workload_id"],
                "group_id": group, "backend": args.backend, "algorithm_requested": args.algorithm,
                "label": args.label, "workload_argv": command, "workload_spec": spec,
                "snapshot_vmstat_refresh": args.snapshot_vmstat_refresh,
                "snapshot_memory_consistent": args.snapshot_memory_consistent,
                "snapshot_counter_protocol": (telem.MEMORY_PROTOCOL if args.snapshot_memory_consistent
                    else "stock-vmstat-refresh-once-v1" if args.snapshot_vmstat_refresh else "unrefreshed-v1"),
                "snapshot_refresh_caveat": "outside touch timers; folds counters and may decay/drain PCP; non-atomic, not passive",
                "corpus_sampling_protocol": (CORPUS_SAMPLING_PROTOCOL
                    if spec.get("corpus_sampling", "contiguous") == CORPUS_SAMPLING_V411 else "contiguous-v1"),
                "corpus": descriptor, "corpus_transport": "read-only raw virtio vdc; guest O_DIRECT",
                "corpus_identity": corpus_identity, "corpus_header_page_sha256": corpus_header_page_sha256,
                "corpus_format_tool_sha256": digest(HERE.parent / "corpus/pack.py"),
                "guest_memory_mib": args.memory, "guest_vcpus": args.smp, "guest_refault_cpu": 1,
                "reclaim": RECLAIM, "w1_protocol": (W1_PROTOCOL_V411
                    if spec.get("corpus_sampling", "contiguous") == CORPUS_SAMPLING_V411 else REAL_W1_PROTOCOL)
                    if args.label == "w1" else None,
                "w4_protocol": (W4_PROTOCOL_V411
                    if spec.get("corpus_sampling", "contiguous") == CORPUS_SAMPLING_V411 else REAL_W4_PROTOCOL)
                    if args.label == "w4" else None,
                "host_affinity": cpus, "backing_mib": args.backing_mib,
                "zram_logical_mib": args.backing_mib, "timeout_seconds": args.timeout,
                "repeats": 1, "host_volume": str(args.host_volume),
                "block_cache": "cache=unsafe; guest block IO, not durable host writes",
                "virtualization": "x86 KVM; WSL2 nested results are relative comparisons only",
                "host_clock_ticks": os.sysconf("SC_CLK_TCK"),
                "raw_log_storage": "verified virtio payload then UART, lossless gzip; unverified originals retained",
                "raw_data_transport": "dedicated unformatted non-swap virtio-blk vdb",
                "data_disk_mib": args.data_mib,
                "fresh_boot_requested": True, "fresh_sparse_disk_requested": True,
                "fresh_sparse_disk": False}
    status = {"repeat": 1, "valid": False, "eligible_for_comparison": False,
              "state": "preparing", "lifecycle_safe_to_continue": True}
    write_json(args.dest / "metadata.json", metadata)
    write_json(args.dest / "status.json", [status])
    started = time.monotonic()
    lock = None
    exit_code = 1
    try:
        lock = acquire_lock(args.work)
        identity, hashes = kernel_identity(args.kernel_dir, args.backend)
        spec.update(config_swap_types((args.kernel_dir / ".config").read_text()))
        metadata.update(source=identity[0], kernel_sha256=hashes["bzImage"], config_sha256=hashes[".config"])
        tools = ("guest", "refault", "shmem", "app_switch", "gen_init_cpio")
        if any(not (args.binaries / name).is_file() for name in tools):
            raise ValueError("prebuild all userspace tools and gen_init_cpio; runner never compiles")
        metadata["binary_sha256"] = {name: digest(args.binaries / name) for name in tools}
        metadata["workload_source_hashes"] = {file.name: digest(file) for file in HERE.iterdir()
                                              if file.suffix in (".c", ".h", ".py")}
        helper = HERE.parent / "e2e/tests/run_e2e_qemu.py"
        metadata["qemu_helper_sha256"] = digest(helper)
        qemu = shutil.which("qemu-system-x86_64")
        if not qemu:
            raise ValueError("qemu-system-x86_64 missing")
        metadata["qemu_version"] = subprocess.check_output([qemu, "--version"], text=True, timeout=10).splitlines()[0]
        floor(args.memory, args.host_volume)
        args.work.mkdir(parents=True, exist_ok=True)
        image = pack(args.tree, args.binaries,
                     args.work / ("compare-init-" + hashlib.sha256(str(args.dest).encode()).hexdigest()[:20]),
                     args.backend, args.algorithm, args.label, command, args.backing_mib * MIB,
                     args.timeout - 2, args.data_mib, args.memory, args.snapshot_vmstat_refresh,
                     **({"snapshot_memory_consistent": True} if args.snapshot_memory_consistent else {}))
        metadata["initramfs_sha256"] = digest(image)
        data_output = image.parent / "run1.data"
        metadata["raw_data_output"] = str(data_output)
        boot = "transparent_hugepage=madvise"
        if args.backend.startswith("memcomp-"):
            boot += " memcomp.compressor=" + args.algorithm
        if args.backend in ("zswap", "zswap-crosscheck"):
            boot += " zswap.enabled=1 zswap.compressor=" + args.algorithm + " zswap.zpool=zsmalloc zswap.shrinker_enabled=1"
        helper_args = ["taskset", "--cpu-list", args.cpus, sys.executable, "-B", str(helper),
                       "--work", str(args.work), "--qemu", qemu,
                       "--kernel", str(args.kernel_dir / "bzImage"), "--initrd", str(image),
                       "--guest-env", boot, "--memory", str(args.memory),
                       "--swap-mb", str(args.backing_mib), "--smp", str(args.smp),
                       "--accelerator", "kvm", "--timeout", str(args.timeout),
                       "--data-mb", str(args.data_mib), "--data-output", str(data_output),
                       "--corpus-image", str(args.corpus_image),
                       "--corpus-expected-identity", json.dumps(corpus_identity),
                       "--corpus-header-sha256", corpus_header_page_sha256]
        shutil.copyfile(args.kernel_dir / ".config", args.dest / ".config")
        (args.dest / "command.txt").write_text(shlex.join(helper_args) + "\n")
        wait_for_host(args.memory, args.host_volume, metadata, args.dest)
        status.update(state="running", lifecycle_safe_to_continue=False)
        write_json(args.dest / "status.json", [status])
        log = args.dest / "run1.log"
        print("START", group, spec["workload_id"], args.host_boot_round, flush=True)
        with log.open("w") as stream:
            helper_exit, interrupted = invoke_helper(helper_args, stream, args.timeout)
        console_bytes = log.read_bytes()
        text = console_bytes.decode("utf-8", errors="replace")
        lines = text.replace("\r\n", "\n").splitlines()
        done = [int(line.split("=", 1)[1]) for line in lines
                if re.fullmatch(r"COMPARE_DONE status=[0-9]+", line)]
        disks = re.findall(r"^e2e-qemu: scratch=(\S+) dev=([0-9]+) inode=([0-9]+)$", text, re.MULTILINE)
        data_disks = re.findall(r"^e2e-qemu: data_scratch=(\S+) dev=([0-9]+) inode=([0-9]+)$",
                                text, re.MULTILINE)
        metadata["fresh_sparse_disk"] = len(disks) == 1
        metadata["sparse_disk_identity"] = [{"path": path, "dev": int(dev), "inode": int(inode)}
                                            for path, dev, inode in disks]
        metadata["fresh_data_disk"] = len(data_disks) == 1
        metadata["data_disk_identity"] = [{"path": path, "dev": int(dev), "inode": int(inode)}
                                          for path, dev, inode in data_disks]
        lifecycle = (helper_exit != 125 and
                     "actual_death_verified=1 group_drained=1" in text and
                     "e2e-qemu: scratch_cleanup=exact_inode_only" in text and
                     not re.search(r"scratch_retained|supervisor_completion_unknown|cleanup_failure", text))
        # Set this before I/O: data verification failure is not process death
        # uncertainty. Conversely, never even open a disk/output on unknown death.
        status["lifecycle_safe_to_continue"] = lifecycle
        if lifecycle:
            payload, transport = read_data_output(data_output, "\n".join(lines), args.data_mib * MIB)
        else:
            payload, transport = b"", {"data_integrity": False,
                                      "data_error": "lifecycle unknown; output and UART retained untouched"}
        raw_lines = payload.decode("utf-8", errors="replace").splitlines()
        csv = [line[len("COMPARE_CSV "):] for line in raw_lines if line.startswith("COMPARE_CSV ")]
        uart_raw = any(line.startswith(("COMPARE_CSV ", "COMPARE_RAW_", "COMPARE_SNAPSHOT "))
                       for line in lines)
        failure_lines = [line for line in raw_lines + lines if KERNEL_FAILURE.search(line)]
        validation = validate_csv(csv, spec)
        scope = (w4_scope_evidence(raw_lines, args.memory, spec["real_corpus"]) if args.label == "w4" else
                 {"w4_scope_valid": True, "w4_scope_error": ""})
        status.update(validation, **scope, phase_start_swapcache_readme=None)
        note = swapcache_note(validation)
        if note:
            # The result directory is new per guest. Preserve any existing
            # human notes; only append this guest's C.3 disclosure section.
            with (args.dest / "README.md").open("a", encoding="utf-8") as readme:
                readme.write(note)
            status["phase_start_swapcache_readme"] = "README.md"
        valid = (scope["w4_scope_valid"] and helper_exit == 0 and done == [0] and validation["csv_integrity"] and
                 not validation["drain_timed_out_rows"] and not validation["unknown_special_swapped_touches"] and
                 not failure_lines and lifecycle and transport["data_integrity"] and
                 transport.get("data_guest_status") == 0 and not uart_raw and
                 metadata["fresh_sparse_disk"] and metadata["fresh_data_disk"] and not interrupted)
        metadata["data_verification"] = transport
        reclaim = {"mglru_boot_valid": False, "mglru_boot_mask": None,
                   "mglru_boot_error": "verified payload/log unavailable"}
        if lifecycle and transport["data_integrity"]:
            # Archive the exact verified in-memory bytes, not a reopened path.
            # Keep legacy log framing for the summary reader: the original raw
            # text precedes a clearly delimited short console/kernel-log tail.
            with gzip.open(args.dest / "run1.log.gz", "wb") as target:
                target.write(payload)
                target.write(b"\nCOMPARE_CONSOLE_BEGIN\n")
                target.write(console_bytes)
            with gzip.open(args.dest / "run1.csv.gz", "wt", newline="") as target:
                target.write("\n".join(csv) + ("\n" if csv else ""))
            # Reuse the existing framed snapshot reader, only on the verified
            # payload/log archive. UART declarations are not boot-state evidence.
            reclaim, w4_disk = phase_snapshot_evidence(
                args.dest / "run1.log.gz", args.label, args.backend,
                validation["writeback_latency_excluded"])
            status.update(w4_disk)
            if args.label == "w4":
                valid = valid and w4_disk["w4_diskstats_valid"]
                # Terminal/read-begin gate reopening and timeout share one
                # latency-only decision. The existing final status update
                # drives matrix readback-pair/excluded-boot counts from it.
                validation["writeback_latency_excluded"] = bool(
                    validation["writeback_latency_excluded"] or w4_disk["writeback_latency_excluded"])
            valid = valid and reclaim["mglru_boot_valid"]
            if valid:
                current = data_output.lstat()
                identity = transport["data_export_identity"]
                if (not stat.S_ISREG(current.st_mode) or
                        (current.st_dev, current.st_ino) != (identity["dev"], identity["inode"])):
                    raise RuntimeError("export path changed after archive; retain it")
                data_output.unlink()  # Only the relay-created, verified export; never a QEMU disk.
                log.unlink()
            else:
                metadata["raw_log_storage"] += "; failed-run uncompressed output/UART also retained"
        else:
            metadata["raw_log_storage"] = "no gzip: raw output and run1.log retained; verification/lifecycle failed"
        metadata["reclaim_validation"] = reclaim
        status.update(reclaim)
        status.update(transport, raw_data_on_uart=uart_raw)
        status.update(validation, qemu_helper_exit=helper_exit, guest_status=done, csv_lines=len(csv),
                      kernel_or_data_failure=bool(failure_lines), kernel_failure_lines=failure_lines,
                      valid=valid, eligible_for_comparison=valid and not args.smoke,
                      eligible_for_readback_comparison=(valid and not args.smoke and
                          not validation["writeback_latency_excluded"]),
                      interrupted=interrupted, state="complete" if valid else "failed",
                      lifecycle_safe_to_continue=lifecycle)
        smoke_ok = True
        if args.smoke:
            status.update(smoke_acceptance(args.label, args.backend, status))
            smoke_ok = status["smoke_accepted"] is not False
            if valid and not smoke_ok:
                status["state"] = "smoke-rejected"
        exit_code = 130 if interrupted else 0 if valid and smoke_ok else 1 if lifecycle else 125
    except KeyboardInterrupt:
        status.update(state="interrupted", error="interrupted while preparing/waiting")
        exit_code = 130
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        status.update(state="failed", error=str(exc))
        exit_code = 1 if status["lifecycle_safe_to_continue"] else 125
    finally:
        status["elapsed_seconds"] = time.monotonic() - started
        write_json(args.dest / "metadata.json", metadata)
        write_json(args.dest / "status.json", [status])
        if lock is not None:
            os.close(lock)
    print("END", args.dest, json.dumps(status), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
