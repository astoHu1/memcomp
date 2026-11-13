#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Stream compact Stage 4 phase telemetry; see telemetry.md for definitions."""
import errno
import argparse
from contextlib import contextmanager
import csv
from dataclasses import dataclass, field, replace
from decimal import Decimal
import gzip
import hashlib
import json
import os
import stat
from pathlib import Path
import re
import posixpath
import zlib
import summarize as stats
from collections import defaultdict

MEMCOMP = "/sys/kernel/mm/memcomp/"
ZRAM = "/sys/block/zram0/"
ZSWAP = "/sys/kernel/debug/zswap/"
# Independent bench/debug instrumentation, never a production public ABI.
MEMCOMP_ACCOUNT = "/sys/kernel/debug/memcomp/memory_accounting"
MEMCOMP_OCCUPANCY = "/sys/kernel/debug/memcomp/memory_occupancy"
MEMCOMP_COMPONENTS = ("slot_bytes", "side_bytes", "cluster_bytes", "key_bytes",
                      "counts_bytes", "overflow_bytes", "pending_bytes")
MEMCOMP_ACCOUNT_FIELDS = ("version", "page_bytes", "cluster_header_bytes", "xa_node_bytes",
                         "index_metadata_bytes", "cluster_xa_nodes", "cluster_xa_bytes",
                         "clusters", "keys", "pending_records", "retired_clusters", "retired_bytes",
                         "stored_nonzero_pages", "dead_slots", "used_slots", "span_records",
                         "span_members", "objcg_count") + MEMCOMP_COMPONENTS
MEMCOMP_ACCOUNT_METRICS = {"memcomp.metadata." + name for name in MEMCOMP_ACCOUNT_FIELDS}
MEMCOMP_ACCOUNT_METRICS.add("memcomp.metadata.total_bytes")
LOG_RAW_LIMIT = 64 << 20
LOG_DECODED_LIMIT = 128 << 20
LOG_LINE_LIMIT = 1 << 20
# stats.load_boot blocks every pre-v4.6/unknown method before raw logs are opened.
CORE = {"/proc/vmstat", "/proc/meminfo", "/proc/stat", "/proc/diskstats", "/proc/swaps"}
GAUGES = {
    "events": set("enabled comp_ctx_count comp_ctx_reserved comp_ctx_borrow_limit comp_ctx_free "
                  "comp_ctx_waiters comp_ctx_borrowed_active pending_pages swapped_entries "
                  "zs_huge_class_size folio_owner_pages samefill_pages raw_pages shmem_pages".split()),
    "packed_swap_events": set("writeback_pool_pages writeback_pressure_scopes writeback_gate_open".split()),
    "execution_events": set("comp_budget_limit comp_budget_background_limit comp_budget_direct_limit "
                            "comp_budget_active comp_budget_background_active comp_budget_direct_active "
                            "comp_budget_waiters direct_staging_count direct_staging_free direct_staging_active".split()),
}
PEAKS = {
    "events": {"comp_ctx_borrowed_peak"}, "packed_swap_events": set(),
    "execution_events": set("comp_budget_peak comp_budget_background_peak comp_budget_direct_peak "
                            "direct_staging_peak restore_max_ns".split()),
}
EVENTS = {
    "events": set("comp_ctx_wait ctx_busy reclaim_ctx_busy_pages reclaim_ctx_busy_reserved_free_pages "
                  "reclaim_ctx_busy_all_busy_pages reclaim_ctx_borrow_pages reclaim_ctx_borrow_limit_pages "
                  "reclaim_ctx_borrow_waiter_pages compress_fail incompressible zs_fast_fail zs_retry "
                  "zs_retry_success zs_retry_fail zs_fail zs_alloc_other_fail raw_store raw_candidate_huge_class "
                  "raw_candidate_ge_page raw_reject raw_reject_huge_class raw_reject_ge_page raw_reject_zs_fallback "
                  "raw_reject_net async_payloads async_fail reserve_entry_alloc_fail "
                  "refresh_state_fail refresh_payload_fail anon_unmap_fail_pages "
                  "refresh_preflight_fail_pages refresh_folio_busy_fail_pages refresh_arch_fail_pages "
                  "refresh_state_fail_pages refresh_payload_fail_pages "
                  "anon_finish_fail_pages incompressible_marked_pages incompressible_changed_pages "
                  "incompressible_folio_skip_pages shmem_store_ok_pages shmem_store_fail_pages "
                  "shmem_eligibility_fail_pages shmem_payload_fail_pages shmem_arch_fail_pages "
                  "bg_store_ok bg_store_fail direct_store_ok direct_store_fail pageout_store_ok pageout_store_fail "
                  "swapped_read_io swapped_read_error swapped_read_cached swapoff_entries swapoff_read_io swapoff_fail".split()),
    "packed_swap_events": set("writeback_pressure_events writeback_pressure_scans writeback_compression_throttled "
                              "writeback_limited_batches writeback_budget_granted writeback_payloads writeback_zs_reclaimed "
                              "writeback_zs_no_progress writeback_decompress_error writeback_write_error writeback_gate_opens "
                              "writeback_packed_slots writeback_packed_payloads writeback_defrag_moved".split()),
    "execution_events": set("reclaim_ctx_budget_pages direct_budget_reject direct_budget_reject_pages kswapd_budget_wait "
                            "direct_staging_miss direct_staging_detach restore_spans restore_ptes restore_missed_pages "
                            "restore_rewalks restore_ns split_refused split_refused_pages "
                            "refresh_cross_node_pages meta_shrinker_registered meta_shrinker_count_calls "
                            "meta_shrinker_scan_calls meta_shrinker_detached_headers retained_empty_headers "
                            "retained_empty_header_bytes".split()),
}
RAW = re.compile(r"COMPARE_RAW_(BEGIN|END|NA|LIMIT) monotonic_ns=(\d+) phase=(\S+) path=(\S+)(?: (.*))?")
SNAP = re.compile(r"COMPARE_SNAPSHOT monotonic_ns=(\d+) phase=(\S+)")
PROC = re.compile(r"/proc/([1-9]\d*)/(comm|stat)")
WB_NOTE = ("ABI accounting events, not distinct pages or physical IO; submitted slots, payload conversions and "
           "allocator footprint changes have different accounting points; swapped_read_io can hit swapcache")
PHASE = re.compile(r"r(\d+)_(seq|rand|single|rewrite)_(.+)")
IDENTITY = stats.IDENTITY
# Deliberately omit raw ABI inventories, peaks, thread snapshots and capture rows.
# Only these C.5/C.6 readings and compact memory scalars survive each snapshot.
COMMON = set("vmstat.pswpin vmstat.pswpout vmstat.zswpin vmstat.zswpout vmstat.zswpwb "
             "vmstat.allocstall_sum meminfo.MemFree_kib meminfo.MemAvailable_kib "
             "vda.read_bytes vda.write_bytes".split())
CONSISTENT_FREE = "memory.physical_free_buddy_plus_PCP_kib"
MEMORY_PROTOCOL_V1 = "v4.11-w1-memory-consistent-v1"
MEMORY_PROTOCOL_V2 = "v4.11-w1-memory-consistent-v2"
MEMORY_PROTOCOL = "v4.11-w1-memory-consistent-v3"
MEMORY_PROBE_MAX_RETRIES = 16
MEMORY_MEMFREE_CHECKS = {
    MEMORY_PROTOCOL_V1: "max_abs_before_after",
    MEMORY_PROTOCOL_V2: "between_buddy_with_tolerance",
    MEMORY_PROTOCOL: "between_buddy_with_tolerance",
}
MEMORY_RAW = "/compare/memory/v411/"
PHYSICAL_FREE = "memory.physical_free_MemFree_plus_PCP_kib"
MEMORY = {"zoneinfo.PCP_pages", "zoneinfo.PCP_kib", PHYSICAL_FREE}
# Compatibility name for callers of the historical optional-memory helper.
# Current measurement baselines require these same scalars in required_metrics.
OPTIONAL_MEMORY = MEMORY
COMMON.update(MEMORY)
MEMINFO_FIELDS = ("MemFree", "MemAvailable", "Slab", "SReclaimable", "SUnreclaim",
                  "PageTables", "Percpu", "VmallocUsed")
SLAB_CACHES = ("memcomp_entry", "radix_tree_node", "zs_handle", "zspage")
SLAB_FIELDS = {
    "active_objs": "objects", "num_objs": "objects", "object_size_bytes": "bytes",
    "objects_per_slab": "objects", "pages_per_slab": "pages", "active_slabs": "slabs",
    "num_slabs": "slabs", "allocated_pages": "pages", "allocated_bytes": "bytes",
}
SLAB_REQUIRED = ("active_objs", "num_objs", "object_size_bytes", "allocated_pages")
MEMORY_STATE = MEMORY | {f"meminfo.{name}_kib" for name in MEMINFO_FIELDS}
MEMORY_STATE.update(f"slabinfo.{cache}.{name}" for cache in SLAB_CACHES for name in SLAB_FIELDS)
MEMORY_STATE.update(MEMCOMP_ACCOUNT_METRICS)
COMMON.update(MEMORY_STATE)
SLAB_NOTE = ("named slab-cache observations, shared across users/pools; not exclusive backend "
             "metadata; allocated_pages = num_slabs * pages_per_slab, not rounded object bytes; "
             "object_size_bytes is slabinfo objsize (allocator slot size)")
MEMFREE_NOTE = ("raw MemFree includes PCP migration effects; retained separately as a "
                "secondary diagnostic, not the primary cross-backend memory indicator")
PHYSICAL_FREE_NOTE = ("physical-free = MemFree+PCP; its signed end-minus-start delta is the "
                      "primary cross-backend memory indicator; sequential, non-atomic source "
                      "samples, not backend-exclusive cost; signed deltas are not ratio-ranked")
COMMON.update(f"psi.{resource}.{level}_total_us" for resource in ("memory", "cpu", "io")
              for level in ("some", "full"))
POOLS = {
    "memcomp": set("memcomp.L_bytes memcomp.C_bytes memcomp.P_bytes memcomp.L_over_C memcomp.L_over_P memcomp.pool_pages".split()),
    "zram": set("zram.orig_data_size zram.compr_data_size zram.mem_used_total zram.logical_over_payload zram.logical_over_pool zram.pool_pages".split()),
    "zswap": set("zswap.stored_pages zswap.pool_total_size zswap.logical_bytes zswap.logical_over_pool zswap.pool_pages".split()),
}
READ_SOURCES = {
    "memcomp": set("memcomp.events.swapped_read_io memcomp.events.swapped_read_error memcomp.events.swapped_read_cached".split()),
    "zram": {"zram.bd_reads", "zram.bd_writes"},
    "zswap": set(),
}
WB_COUNTER = "memcomp.packed_swap_events.writeback_payloads"
WB_GATE = "memcomp.packed_swap_events.writeback_gate_open"


@dataclass
class Block:
    path: str
    begin: int
    end: int | None = None
    lines: list = field(default_factory=list)
    error: str = ""


@dataclass
class Snapshot:
    phase: str
    occurrence: int
    ns: int
    explicit: bool = False
    blocks: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    memory: dict = field(default_factory=dict)
    memory_attempts: list = field(default_factory=list)
    threads: tuple = field(default_factory=lambda: ({}, ["not sampled"]))


@dataclass
class Log:
    snapshots: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    clocks: list = field(default_factory=list)
    requests: list = field(default_factory=list)
    done: list = field(default_factory=list)
    workload_end: list = field(default_factory=list)
    required_last: dict = field(default_factory=dict)
    required_invalid: bool = False
    setup_readbacks: list = field(default_factory=list)


def attributes(line):
    return dict(re.findall(r"(?:^|\s)([a-zA-Z_]+)=([^\s]+)", line))


def memory_marker_attributes(line):
    fields = line.split()
    attrs = attributes(line)
    required = {
        "COMPARE_MEMORY_SNAPSHOT_BEGIN": "monotonic_ns phase version page_bytes tolerance_pages max_attempts memfree_check free_sample",
        "COMPARE_MEMORY_SNAPSHOT_END": "monotonic_ns phase version status attempts",
        "COMPARE_MEMORY_ATTEMPT_BEGIN": "monotonic_ns phase attempt",
        "COMPARE_MEMORY_ATTEMPT_END": "monotonic_ns phase attempt status errno buddy_before_pages pcp_before_pages buddy_after_pages pcp_after_pages memfree_pages free_drift_pages memfree_buddy_max_pages",
    }
    if not fields or fields[0] not in required or set(attrs) != set(required[fields[0]].split()) or len(attrs) != len(fields) - 1:
        raise ValueError("unknown, duplicate, missing, or extra memory marker fields")
    numeric = set(attrs) - {"phase", "version", "status", "memfree_check", "free_sample"}
    if any(not re.fullmatch(r"[0-9]+", attrs[name]) for name in numeric):
        raise ValueError("non-canonical numeric memory marker field")
    return attrs


def buddyinfo_pages(b):
    """Strict stock buddyinfo, in base pages; topology is part of the basis."""
    if b is None or b.error or b.end is None:
        raise ValueError("buddyinfo missing/incomplete: " + (b.error if b else "absent"))
    zones, orders, total = set(), None, 0
    for line in b.lines:
        if not line.strip():
            continue
        m = re.fullmatch(r"Node\s+([0-9]+),\s+zone\s+(\S+)\s+([0-9]+(?:\s+[0-9]+)*)\s*", line)
        if not m:
            raise ValueError("malformed buddyinfo row")
        key, counts = (int(m[1]), m[2]), [int(v) for v in m[3].split()]
        if key in zones or not counts or len(counts) > 64 or (orders is not None and len(counts) != orders):
            raise ValueError("duplicate buddy zone/inconsistent order coverage")
        zones.add(key)
        orders = len(counts)
        total += sum(n << order for order, n in enumerate(counts))
        if total > 2**64 - 1:
            raise ValueError("buddyinfo page-count overflow")
    if not zones:
        raise ValueError("empty buddyinfo")
    basis = "buddy-zones=" + json.dumps(sorted(zones), separators=(",", ":")) + ";orders=" + str(orders)
    return total, basis, zones


def memory_attempt_evidence(attempt, page_bytes, request, version):
    """Recompute decisions from raw captures, never trust a success marker alone."""
    evidence = {"attempt": int(attempt.memory["begin"]["attempt"]), "status": "ERROR"}
    begin, end = attempt.memory["begin"], attempt.memory.get("end", {})
    evidence.update(begin_ns=attempt.ns, end_marker=dict(end))

    def source(path):
        b = attempt.blocks.get(path)
        if b is None or b.error or b.end is None or b.begin < attempt.ns or b.end < b.begin:
            raise ValueError("missing/incomplete/out-of-order " + path)
        return b

    if attempt.limits:
        raise ValueError("capture limit in memory attempt")
    if any(b.error and not b.error.startswith("unavailable errno=") for b in attempt.blocks.values()):
        raise ValueError("raw IO/parse error inside memory attempt")
    if end.get("status") == "ERROR":
        raise ValueError("guest memory attempt ERROR errno=" + end.get("errno", "missing"))
    refreshes = attempt.memory.get("refresh", [])
    if len(refreshes) != 1:
        raise ValueError("memory attempt requires exactly one stat_refresh")
    refresh = refreshes[0]
    if (refresh.get("status") != "ok" or refresh.get("errno") != "0" or
            not re.fullmatch(r"[1-9][0-9]*", refresh.get("attempts", ""))):
        raise ValueError("stat_refresh missing/failed/malformed")
    refresh_begin, refresh_end = int(refresh["begin_ns"]), int(refresh["end_ns"])
    end_ns = int(end["monotonic_ns"])
    if not attempt.ns <= refresh_begin <= refresh_end <= end_ns:
        raise ValueError("stat_refresh/attempt clock order invalid")
    if end.get("attempt") != begin["attempt"] or end.get("phase") != attempt.phase or end.get("errno") != "0":
        raise ValueError("mismatched memory attempt END")
    pairs, bases, pcp_bases = [], [], []
    for side in ("before", "after"):
        buddy = source(MEMORY_RAW + side + "/buddyinfo")
        zone = source(MEMORY_RAW + side + "/zoneinfo")
        pages, basis, zones = buddyinfo_pages(buddy)
        pcp, pcp_basis, error = zoneinfo_pcp(zone, "")
        # zoneinfo includes zero-present zones omitted by buddyinfo. The PCP
        # parser above still validates every empty zone and requires zero state.
        zone_keys, current_zone = set(), None
        for line in zone.lines:
            if (m := re.fullmatch(r"Node ([0-9]+), zone\s+(\S+)", line.strip())):
                current_zone = (int(m[1]), m[2])
            elif (m := re.fullmatch(r"present\s+([0-9]+)", line.strip())) and int(m[1]):
                if current_zone is None:
                    raise ValueError("populated zone without identity")
                zone_keys.add(current_zone)
        if error or zones != zone_keys:
            raise ValueError(error or "buddy/PCP zone coverage differs")
        if pcp < 0 or pages + pcp > 2**64 - 1:
            raise ValueError("PCP/free page-count overflow")
        pairs.append((pages, pcp))
        bases.append(basis)
        pcp_bases.append(pcp_basis)
    if len(set(bases)) != 1 or len(set(pcp_bases)) != 1:
        raise ValueError("buddy/PCP topology changed within memory attempt")
    # These must be adjacent reads, enclosing all classification sources.
    ordered = sorted(attempt.blocks.values(), key=lambda b: b.begin)
    expected_first = [MEMORY_RAW + "before/" + name for name in ("buddyinfo", "zoneinfo")]
    expected_last = [MEMORY_RAW + "after/" + name for name in ("buddyinfo", "zoneinfo")]
    if ([b.path for b in ordered[:2]] != expected_first or [b.path for b in ordered[-2:]] != expected_last or
            ordered[0].begin < refresh_end or ordered[-1].end is None or ordered[-1].end > end_ns):
        raise ValueError("memory read ordering/bracketing invalid")
    for a, b in zip(ordered, ordered[1:]):
        if a.end is None or a.end > b.begin:
            # Optional RAW_NA has no END; it remains legitimate if absent.
            if a.error.startswith("unavailable errno=") and a.begin <= b.begin:
                continue
            raise ValueError("overlapping/incomplete memory attempt raw clocks")
    mem = {}
    names = MEMINFO_FIELDS + ("AnonPages", "Cached", "Buffers", "SwapCached", "KernelStack",
                            "Active(anon)", "Inactive(anon)", "Active(file)", "Inactive(file)", "Unevictable")
    for name in names:
        matches = [line for line in source("/proc/meminfo").lines if line.startswith(name + ":")]
        m = re.fullmatch(re.escape(name) + r":\s+([0-9]+)\s+kB", matches[0]) if len(matches) == 1 else None
        if not m:
            raise ValueError("memory classification meminfo missing/duplicate/malformed: " + name)
        mem[name] = int(m[1]) * 1024
    if mem["Slab"] != mem["SReclaimable"] + mem["SUnreclaim"]:
        raise ValueError("inconsistent Slab classification")
    if mem["MemFree"] % page_bytes or mem["MemFree"] > 2**64 - 1:
        raise ValueError("MemFree page alignment/overflow")
    slab = source("/proc/slabinfo")
    if not slab.lines or slab.lines[0].strip() != "slabinfo - version: 2.1":
        raise ValueError("unsupported/missing slabinfo version")
    cache_names = set()
    for line in slab.lines[1:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.fullmatch(r"(\S+)\s+([0-9]+(?:\s+[0-9]+){4})\s+:\s+tunables\s+([0-9]+(?:\s+[0-9]+){2})\s+:\s+slabdata\s+([0-9]+(?:\s+[0-9]+){2})\s*", line)
        if not m or m[1] in cache_names:
            raise ValueError("malformed/duplicate slabinfo row")
        cache_names.add(m[1])
    vmstat, error = key_values(source("/proc/vmstat"), "")
    if error or any(name not in vmstat or vmstat[name] < 0 for name in ("nr_isolated_anon", "nr_isolated_file")):
        raise ValueError(error or "missing/negative isolated page gauges")
    for path in ("/proc/stat", "/proc/diskstats", "/proc/swaps"):
        source(path)
    swaps = source("/proc/swaps").lines
    if not swaps or swaps[0].split() != ["Filename", "Type", "Size", "Used", "Priority"]:
        raise ValueError("malformed swaps header")
    devices = set()
    for line in swaps[1:]:
        if not line.strip():
            continue
        fields = line.split()
        if (len(fields) != 5 or fields[0] in devices or fields[1] not in ("file", "partition") or
                any(not re.fullmatch(r"[0-9]+", v) for v in fields[2:4]) or
                not re.fullmatch(r"-?[0-9]+", fields[4])):
            raise ValueError("malformed/duplicate swap row")
        size, used = (int(v) * 1024 for v in fields[2:4])
        if not size or used > size or size % page_bytes or used % page_bytes:
            raise ValueError("invalid swap size/used/alignment")
        devices.add(fields[0])
    # Existing named-cache/pool evidence errors must not become INVALID merely
    # because the two consistency conditions also fail.
    candidate = replace(attempt, memory={}, memory_attempts=[])
    metrics = snapshot_metrics(candidate, page_bytes)
    framed = replace(candidate, memory={"version": version}) if request.get("workload") == "w1" else candidate
    if (version == MEMORY_PROTOCOL and request.get("workload") == "w1" and
            request.get("backend", "").startswith("memcomp-")):
        source(MEMCOMP_OCCUPANCY)
    for name in required_metrics({"label": "w1", "meta": {"backend": request.get("backend", "")}}, framed):
        if not (name.startswith("slabinfo.") or name in MEMCOMP_ACCOUNT_METRICS or
                name in set().union(*POOLS.values())):
            continue
        c = metrics.get(name)
        if c is None or c.value is None or c.reason or c.quality != "ok":
            raise ValueError("memory classification unavailable: " + name + ": " + (c.reason if c else "absent"))
    memfree = mem["MemFree"] // page_bytes
    drift = abs(sum(pairs[1]) - sum(pairs[0]))
    mismatch = max(abs(memfree - pair[0]) for pair in pairs)
    low, high = sorted(pair[0] for pair in pairs)
    interval_distance = max(low - memfree, memfree - high, 0)
    # The END max-distance field remains a v1-compatible diagnostic. v2
    # decisions use the interval distance recomputed from the raw captures.
    if version not in MEMORY_MEMFREE_CHECKS:
        raise ValueError("unknown memory protocol")
    distance = mismatch if version == MEMORY_PROTOCOL_V1 else interval_distance
    evidence.update(buddy_before_pages=pairs[0][0], pcp_before_pages=pairs[0][1],
                    buddy_after_pages=pairs[1][0], pcp_after_pages=pairs[1][1],
                    memfree_pages=memfree, free_drift_pages=drift, memfree_buddy_max_pages=mismatch,
                    memfree_buddy_interval_pages=interval_distance,
                    free_after_pages=sum(pairs[1]), buddy_basis=bases[1], pcp_basis=pcp_bases[1],
                    refresh=dict(refresh), raw_sources=[dict(path=b.path, begin_ns=b.begin, end_ns=b.end,
                                                           error=b.error) for b in ordered])
    evidence["status"] = "CONSISTENT" if drift <= 16 and distance <= 16 else "INCONSISTENT"
    if end.get("status") != evidence["status"] or any(
            end.get(key) != str(evidence[key]) for key in (
                "buddy_before_pages", "pcp_before_pages", "buddy_after_pages", "pcp_after_pages",
                "memfree_pages", "free_drift_pages", "memfree_buddy_max_pages")):
        raise ValueError("memory attempt marker/raw decision or counts mismatch")
    return evidence


def finish_memory_snapshot(s, log):
    if not s.memory:
        return
    info, request = s.memory, log.requests[0] if len(log.requests) == 1 else {}
    info["status"], info["attempts"] = "ERROR", []
    errors = []
    try:
        if info.get("framing_errors"):
            raise ValueError("; ".join(info["framing_errors"]))
        begin, end = info.get("begin", {}), info.get("end", {})
        page_bytes = int(begin.get("page_bytes", "0"))
        version = begin.get("version")
        if (version not in MEMORY_MEMFREE_CHECKS or end.get("version") != version or
                begin.get("tolerance_pages") != "16" or begin.get("max_attempts") != "10" or
                begin.get("memfree_check") != MEMORY_MEMFREE_CHECKS.get(version) or begin.get("free_sample") != "after" or
                begin.get("phase") != s.phase or end.get("phase") != s.phase or
                not valid_page_bytes(page_bytes) or request.get("page_bytes") != str(page_bytes) or
                request.get("workload") != "w1"):
            raise ValueError("unknown/incomplete memory protocol, geometry, or non-W1 request")
        if not s.ns <= int(begin["monotonic_ns"]) <= int(end["monotonic_ns"]):
            raise ValueError("memory snapshot marker clock order invalid")
        attempts = s.memory_attempts
        if not 1 <= len(attempts) <= 10 or end.get("attempts") != str(len(attempts)):
            raise ValueError("missing/excess/count mismatch memory attempts")
        last_end = int(begin["monotonic_ns"])
        for i, attempt in enumerate(attempts, 1):
            evidence = {"attempt": i, "status": "ERROR", "begin_marker": attempt.memory.get("begin", {}),
                        "end_marker": attempt.memory.get("end", {}),
                        "refresh": list(attempt.memory.get("refresh", [])),
                        "raw_sources": [dict(path=b.path, begin_ns=b.begin, end_ns=b.end, error=b.error)
                                        for b in attempt.blocks.values()]}
            try:
                if attempt.memory["begin"].get("attempt") != str(i) or attempt.ns < last_end:
                    raise ValueError("memory attempt ordinal/clock regression")
                evidence = memory_attempt_evidence(attempt, page_bytes, request, version)
                last_end = int(attempt.memory["end"]["monotonic_ns"])
                if last_end > int(end["monotonic_ns"]):
                    raise ValueError("memory attempt ends outside snapshot")
                if i != len(attempts) and evidence["status"] != "INCONSISTENT":
                    raise ValueError("retry after consistent/error memory attempt")
            except (KeyError, ValueError, TypeError) as exc:
                evidence.update(status="ERROR", error=str(exc))
                errors.append(str(exc))
            info["attempts"].append(evidence)
        if errors:
            raise ValueError("; ".join(errors))
        expected = "CONSISTENT" if info["attempts"][-1]["status"] == "CONSISTENT" else "INVALID"
        if (expected == "INVALID" and len(attempts) != 10) or end.get("status") != expected:
            raise ValueError("memory final status/attempt bound mismatch")
        # Only accepted classification is promoted to the snapshot. Keep all
        # rejected evidence separately, never merge repeated paths together.
        chosen = attempts[-1]
        if any(not path.startswith("/compare/swap_layout") for path in s.blocks):
            raise ValueError("unframed raw sources mixed with memory attempts")
        s.blocks.update(chosen.blocks)
        after = chosen.blocks[MEMORY_RAW + "after/zoneinfo"]
        s.blocks["/proc/zoneinfo"] = replace(after, path="/proc/zoneinfo")
        info.update(status=expected, version=version,
                    selected_attempt=len(attempts) if expected == "CONSISTENT" else None,
                    tolerance_pages=16, max_attempts=10, free_sample="after",
                    memfree_check=MEMORY_MEMFREE_CHECKS[version], page_bytes=page_bytes,
                    note="bounded sequential non-atomic observations, not proof of a root cause")
        if expected == "CONSISTENT":
            info.update(free_pages=info["attempts"][-1]["free_after_pages"],
                        basis=info["attempts"][-1]["buddy_basis"] + ";" + info["attempts"][-1]["pcp_basis"])
    except (KeyError, ValueError, TypeError) as exc:
        info.update(status="ERROR", error=str(exc))
        log.issues.append(s.phase + ": memory snapshot protocol/error: " + str(exc))
    # Raw text is already in the immutable log. Compact summaries retain every
    # attempt without expanding the streaming telemetry parser's memory bound.
    s.memory_attempts.clear()


def memory_snapshot_protocol(snapshots):
    """Return the actual marker version, never relabel a replay as the default."""
    versions = {s.memory.get("begin", {}).get("version") for s in snapshots if s.memory}
    return next(iter(versions)) if len(versions) == 1 else None


class _RawLimitReader:
    def __init__(self, source, limit):
        self.source, self.limit, self.used = source, limit, 0

    def read(self, size=-1):
        remaining = self.limit - self.used
        size = remaining + 1 if size is None or size < 0 else min(size, remaining + 1)
        data = self.source.read(size)
        self.used += len(data)
        if self.used > self.limit:
            raise ValueError("telemetry input exceeds compressed/input bound")
        return data

    def readline(self, size=-1):
        remaining = self.limit - self.used
        size = remaining + 1 if size is None or size < 0 else min(size, remaining + 1)
        data = self.source.readline(size)
        self.used += len(data)
        if self.used > self.limit:
            raise ValueError("telemetry input exceeds compressed/input bound")
        return data

    def tell(self):
        return self.used

    def readable(self):
        return True

    def seekable(self):
        return False


class _BoundedLines:
    def __init__(self, stream, decoded_limit, line_limit):
        self.stream, self.decoded_limit, self.line_limit = stream, decoded_limit, line_limit
        self.decoded = 0

    def __iter__(self):
        while True:
            raw = self.stream.readline(self.line_limit + 1)
            if not raw:
                return
            if len(raw) > self.line_limit:
                raise ValueError("telemetry line exceeds host bound")
            self.decoded += len(raw)
            if self.decoded > self.decoded_limit:
                raise ValueError("decoded telemetry exceeds host bound")
            yield raw.decode("utf-8", errors="strict")


@contextmanager
def open_log(path, raw_limit=LOG_RAW_LIMIT, decoded_limit=LOG_DECODED_LIMIT,
             line_limit=LOG_LINE_LIMIT):
    source_path = Path(path)
    try:
        source = source_path.open("rb")
    except FileNotFoundError:
        # Legacy parser fixtures replace gzip.open for virtual text paths.
        with gzip.open(path, "rt", encoding="utf-8", errors="strict") as stream:
            yield _BoundedLines(_TextLineAdapter(stream, raw_limit), decoded_limit, line_limit)
        return
    with source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("telemetry input is not a regular file")
        if info.st_size > raw_limit:
            raise ValueError("telemetry input exceeds compressed/input bound")
        magic = source.read(2)
        source.seek(0)
        bounded = _RawLimitReader(source, raw_limit)
        stream = gzip.GzipFile(fileobj=bounded, mode="rb") if magic == b"\x1f\x8b" else bounded
        try:
            yield _BoundedLines(stream, decoded_limit, line_limit)
        finally:
            if stream is not bounded:
                stream.close()


class _TextLineAdapter:
    def __init__(self, stream, raw_limit):
        self.stream, self.raw_limit, self.used = stream, raw_limit, 0

    def readline(self, size=-1):
        data = self.stream.readline(size).encode("utf-8")
        self.used += len(data)
        if self.used > self.raw_limit:
            raise ValueError("telemetry input exceeds compressed/input bound")
        return data


def parse_log(path, consume, *, raw_limit=LOG_RAW_LIMIT,
              decoded_limit=LOG_DECODED_LIMIT, line_limit=LOG_LINE_LIMIT,
              independent_setup_readbacks=False):
    """Stream raw snapshots; optionally separate zram setup readbacks.

    Summary treats the two policy readbacks as independent evidence, not memory
    boundaries. Default framing remains available to runner/accounting consumers.
    """
    log, counts, current, active = Log(), {}, None, None
    capture = None

    def snapshot(phase, ns, explicit=False):
        nonlocal current, capture
        if capture is not None and not explicit and current is not None and current.phase == phase:
            return capture
        if explicit or current is None or current.phase != phase:
            if current is not None:
                if capture is not None:
                    log.issues.append("unclosed memory attempt")
                    capture = None
                finish_memory_snapshot(current, log)
                consume(current, log)
            counts[phase] = counts.get(phase, 0) + 1
            current = Snapshot(phase, counts[phase], ns, explicit)
            log.snapshots.append(current)
        return current

    def store(s, b):
        if b.path in s.blocks:
            b.error = "duplicate raw source in snapshot"
        s.blocks[b.path] = b
        if b.error and not b.error.startswith("unavailable errno="):
            log.issues.append(f"{s.phase}/{b.path}: {b.error}")
        if b.path in CORE and b.error:
            log.issues.append("required core snapshot unavailable")

    try:
        with open_log(path, raw_limit, decoded_limit, line_limit) as stream:
            for raw in stream:
                line = raw.rstrip("\r\n")
                match = RAW.fullmatch(line)
                if match:
                    op, ns, phase, source, extra = match.groups()
                    ns = int(ns)
                    if op == "END":
                        if active is None:
                            log.issues.append("orphan RAW_END")
                            continue
                        s, b = active
                        if phase != s.phase or source != b.path or ns < b.begin:
                            b.error = "mismatched RAW_END or decreasing clock"
                        b.end = ns
                        store(s, b)
                        active = None
                        continue
                    if active is not None:
                        s, b = active
                        b.error = "missing RAW_END"
                        store(s, b)
                        active = None
                    setup_paths = {
                        "zram_algorithm_before_disksize": ZRAM + "comp_algorithm",
                        "zram_backing_before_disksize": ZRAM + "backing_dev",
                    }
                    if (independent_setup_readbacks and current is not None and
                            current.phase == "before_backend_setup" and not capture and
                            setup_paths.get(phase) == source):
                        counts[phase] = counts.get(phase, 0) + 1
                        s = Snapshot(phase, counts[phase], ns)
                        log.setup_readbacks.append(s)
                        if ns <= current.ns:
                            log.issues.append("non-increasing setup readback clock")
                    else:
                        s = snapshot(phase, ns)
                    if op == "BEGIN":
                        active = (s, Block(source, ns))
                    elif op == "NA":
                        store(s, Block(source, ns, error="unavailable errno=" + attributes(extra or "").get("errno", "unknown")))
                    else:
                        s.limits[source] = extra or "capture limit"
                    continue
                if line.startswith(("COMPARE_RAW_READ_ERROR", "COMPARE_RAW_TRUNCATED")):
                    if active is not None:
                        active[1].error = line
                    else:
                        log.issues.append(line)
                    continue
                if line.startswith("COMPARE_PROBE_READ_RETRY"):
                    if active is not None:
                        active[1].error = "observer retry marker inside raw block"
                        log.issues.append(active[1].error)
                        continue
                    try:
                        fields = line.split()
                        attrs = attributes(" ".join(fields[1:]))
                        retry = int(attrs["retry"])
                        maximum = int(attrs["max_retries"])
                        path = attrs["path"]
                        if (fields[0] != "COMPARE_PROBE_READ_RETRY" or
                                attrs.get("errno") != str(errno.EAGAIN) or
                                path not in (MEMCOMP_ACCOUNT, MEMCOMP_OCCUPANCY) or
                                not 1 <= retry <= maximum <= MEMORY_PROBE_MAX_RETRIES or
                                current is None or not current.explicit or
                                attrs.get("phase") != current.phase or
                                int(attrs["monotonic_ns"]) < current.ns):
                            raise ValueError("invalid observer retry marker")
                    except (KeyError, TypeError, ValueError) as exc:
                        log.issues.append("malformed observer retry marker: " + str(exc))
                    continue
                if line.startswith("COMPARE_RAW_"):
                    log.issues.append("malformed raw marker")
                    if active is not None:
                        active[1].error = "malformed raw marker"
                    continue
                if active is not None:
                    active[1].lines.append(line)
                    continue
                if line.startswith("COMPARE_MEMORY_"):
                    op = line.split()[0]
                    try:
                        attrs = memory_marker_attributes(line)
                        if current is None or not current.explicit or attrs.get("phase") != current.phase:
                            raise ValueError("memory marker outside explicit snapshot/phase")
                        ns = int(attrs["monotonic_ns"])
                        if ns < current.ns:
                            raise ValueError("memory marker clock regression")
                        if op == "COMPARE_MEMORY_SNAPSHOT_BEGIN":
                            if current.memory or capture is not None:
                                raise ValueError("duplicate memory BEGIN")
                            current.memory["begin"] = attrs
                        elif op == "COMPARE_MEMORY_ATTEMPT_BEGIN":
                            if not current.memory or "end" in current.memory or capture is not None:
                                raise ValueError("orphan/nested memory attempt BEGIN")
                            capture = Snapshot(current.phase, len(current.memory_attempts) + 1, ns, True)
                            capture.memory["begin"] = attrs
                            current.memory_attempts.append(capture)
                        elif op == "COMPARE_MEMORY_ATTEMPT_END":
                            if capture is None:
                                raise ValueError("orphan memory attempt END")
                            capture.memory["end"] = attrs
                            capture = None
                        elif op == "COMPARE_MEMORY_SNAPSHOT_END":
                            if not current.memory or "end" in current.memory or capture is not None:
                                raise ValueError("orphan/duplicate/unclosed memory END")
                            current.memory["end"] = attrs
                        else:
                            raise ValueError("unknown memory marker")
                    except (ValueError, KeyError) as exc:
                        log.issues.append("malformed memory marker: " + str(exc))
                        if current is not None and current.explicit:
                            current.memory.setdefault("framing_errors", []).append(str(exc))
                    continue
                if line.startswith("COMPARE_VMSTAT_REFRESH "):
                    if capture is not None:
                        capture.memory.setdefault("refresh", []).append(attributes(line))
                    elif current is not None and current.memory:
                        log.issues.append("stat_refresh outside a memory attempt")
                    continue
                match = SNAP.fullmatch(line)
                if match:
                    ns, phase = match.groups()
                    snapshot(phase, int(ns), True)
                elif line.startswith("COMPARE_CLOCK_TICKS "):
                    log.clocks.append(attributes(line).get("value", ""))
                elif line.startswith("COMPARE_REQUEST "):
                    log.requests.append(attributes(line))
                elif line.startswith("COMPARE_WORKLOAD_END "):
                    log.workload_end.append(attributes(line))
                elif line.startswith("COMPARE_DONE "):
                    log.done.append(attributes(line).get("status"))
                elif re.search(r"Kernel panic|BUG:|KASAN:|DATA_MISMATCH|rcu.*(?:stall|stuck)|soft lockup", line):
                    log.issues.append("kernel/data failure marker")
    except (OSError, EOFError, UnicodeError, zlib.error, ValueError) as error:
        log.issues.append(f"log unavailable/incomplete: {type(error).__name__}: {error}")
    if active is not None:
        s, b = active
        b.error = "EOF without RAW_END"
        store(s, b)
    if current is not None:
        finish_memory_snapshot(current, log)
        consume(current, log)
    if any(s.memory for s in log.snapshots):
        if memory_snapshot_protocol(log.snapshots) is None:
            log.issues.append("mixed or missing consistent-memory protocol versions")
        if any(s.explicit and not s.memory for s in log.snapshots):
            log.issues.append("mixed legacy and consistent-memory snapshots")
    if log.done != ["0"]:
        log.issues.append("missing, duplicate or unsuccessful COMPARE_DONE")
    if len(log.workload_end) != 1 or any(
            log.workload_end[0].get(k) != v for k, v in
            {"status": "0", "stdout_eof": "1", "exec_confirmed": "1", "control_error": "0", "telemetry_error": "0"}.items()):
        log.issues.append("missing or unsuccessful COMPARE_WORKLOAD_END")
    for phase in ("workload_start", "workload_end"):
        if sum(s.phase == phase for s in log.snapshots) != 1:
            log.issues.append("missing/duplicate " + phase)
    if any(b.ns <= a.ns for a, b in zip(log.snapshots, log.snapshots[1:])):
        log.issues.append("non-increasing snapshot clock/order")
    return log


@dataclass
class Cell:
    value: int | Decimal | None
    semantics: str
    unit: str
    source: str
    begin: int | None = None
    end: int | None = None
    quality: str = "ok"
    reason: str = ""
    note: str = ""
    basis: str = ""


def cell(value, semantics, unit, source, block=None, reason="", note="", basis=""):
    return Cell(value, semantics, unit, source, block.begin if block else None,
                block.end if block else None, "unavailable" if value is None else "ok", reason, note, basis)


def payload(s, path):
    b = s.blocks.get(path)
    if b is None:
        parent = path.rsplit("/", 1)[0]
        return None, "raw source absent" + ("; directory capture capped" if parent in s.limits else "")
    return b, b.error or ("missing RAW_END" if b.end is None else "")


def key_values(b, error):
    if error:
        return {}, error
    values = {}
    for line in b.lines:
        if not line.strip():
            continue
        pair = line.split()
        if len(pair) != 2 or not re.fullmatch(r"-?\d+", pair[1]) or pair[0] in values:
            return {}, "malformed/duplicate key-value field"
        values[pair[0]] = int(pair[1])
    return values, "" if values else "empty key-value source"


def numbers(b, error, count, exact=False):
    if error:
        return [], error
    fields = " ".join(b.lines).split()
    if (len(fields) != count if exact else len(fields) < count) or any(not re.fullmatch(r"\d+", v) for v in fields):
        return [], "malformed numeric source/field count"
    return [int(v) for v in fields], ""


def positive_marker(values):
    if len(values) == 1 and re.fullmatch(r"[1-9]\d*", values[0]):
        return int(values[0])
    return None


def ratio(a, b):
    reason = a.reason or b.reason or ("zero denominator" if b.value == 0 else "")
    value = Decimal(a.value) / Decimal(b.value) if not reason and a.value is not None and b.value else None
    return replace(a, value=value, semantics="ratio", unit="ratio", source=a.source + (";" + b.source if b.source != a.source else ""),
                   begin=min(a.begin, b.begin) if a.begin is not None and b.begin is not None else None,
                   end=max(a.end, b.end) if a.end is not None and b.end is not None else None,
                   quality="ok" if value is not None else "unavailable", reason=reason or ("missing operand" if value is None else ""))


def zoneinfo_pcp(b, error):
    """Sum complete Linux 6.12 pagesets; retain only a scalar/coverage digest."""
    if error:
        return None, "", error
    zones, zone, cpu = {}, None, None
    try:
        for lineno, raw in enumerate(b.lines, 1):
            line = raw.strip()
            if not line:
                continue
            header = re.fullmatch(r"Node (\d+), zone\s+(\S+)", line)
            if line.startswith("Node"):
                if header is None:
                    raise ValueError("malformed node/zone header")
                key = (int(header[1]), header[2])
                if key in zones:
                    raise ValueError("duplicate node/zone")
                zone = {"fields": {}, "cpus": {}, "pagesets": False}
                zones[key], cpu = zone, None
                continue
            if zone is None:
                raise ValueError("content before node/zone header")
            fields = zone["fields"]
            field = re.match(r"^(pages free|present|managed|node_unreclaimable:|start_pfn:)\s+(.*)$", line)
            if field:
                name, value = field.groups()
                if name in fields or not re.fullmatch(r"\d+", value):
                    raise ValueError("duplicate/malformed/negative " + name)
                fields[name] = int(value)
                continue
            # Recognized field labels must not silently become ignored text.
            if re.match(r"^(pages free|present|managed|node_unreclaimable|start_pfn)\b", line):
                raise ValueError("malformed zone field")
            if line.startswith("pagesets"):
                if line != "pagesets" or zone["pagesets"]:
                    raise ValueError("duplicate/malformed pagesets")
                zone["pagesets"] = True
                continue
            if re.match(r"^cpu\b", line):
                match = re.fullmatch(r"cpu:\s*(\d+)", line)
                if not zone["pagesets"] or "node_unreclaimable:" in fields or "start_pfn:" in fields or not match:
                    raise ValueError("malformed/out-of-pagesets cpu")
                cpu = int(match[1])
                if cpu in zone["cpus"]:
                    raise ValueError("duplicate cpu in zone")
                zone["cpus"][cpu] = {}
                continue
            match = re.match(r"^(count|high|batch)\s*:(.*)$", line)
            if match:
                name, value = match[1], match[2].strip()
                if cpu is None or "node_unreclaimable:" in fields or "start_pfn:" in fields:
                    raise ValueError("PCP field outside cpu block")
                values = zone["cpus"][cpu]
                if name in values or not re.fullmatch(r"\d+", value):
                    raise ValueError("duplicate/malformed/negative PCP " + name)
                values[name] = int(value)
            elif re.match(r"^(count|batch)\b|^high\s*:", line):
                raise ValueError("malformed PCP field")
        if not zones:
            raise ValueError("no node/zone headers")
        total, coverage, online = 0, [], None
        for (node, name), zone in sorted(zones.items()):
            fields, cpus = zone["fields"], zone["cpus"]
            if not {"pages free", "present", "managed"} <= fields.keys():
                raise ValueError(f"incomplete zone fields at node {node} zone {name}")
            if fields["present"] == 0:
                if (zone["pagesets"] or cpus or fields["managed"] or fields["pages free"] or
                        "node_unreclaimable:" in fields or "start_pfn:" in fields):
                    raise ValueError("inconsistent unpopulated zone")
            else:
                if not zone["pagesets"] or not cpus:
                    raise ValueError("populated zone missing pagesets/cpus")
                if not {"node_unreclaimable:", "start_pfn:"} <= fields.keys():
                    raise ValueError("incomplete populated zone tail")
                if any(set(values) != {"count", "high", "batch"} for values in cpus.values()):
                    raise ValueError("incomplete PCP count/high/batch")
                if online is not None and set(cpus) != online:
                    raise ValueError("inconsistent online cpu coverage across zones")
                online = set(cpus)
                total += sum(values["count"] for values in cpus.values())
            coverage.append((node, name, sorted(cpus)))
        digest = hashlib.sha256(json.dumps(coverage, separators=(",", ":")).encode()).hexdigest()
        return total, "pcp-node-zone-cpu-sha256=" + digest, ""
    except ValueError as exc:
        return None, "", f"zoneinfo PCP: {exc} (at/before line {lineno if b.lines else 0})"


def optional_memory(s, page_bytes, memfree):
    """Decode even historical optional captures; required baselines are checked separately."""
    if s.memory:
        info = s.memory
        b, error = payload(s, MEMORY_RAW + "after/zoneinfo")
        reason = "" if info.get("status") == "CONSISTENT" else "memory snapshot " + info.get("status", "ERROR")
        evidence = info.get("attempts", [{}])[-1] if info.get("attempts") else {}
        pages = evidence.get("pcp_after_pages") if not reason else None
        pcp = cell(pages, "gauge", "pages", MEMORY_RAW + "after/zoneinfo", b, reason,
                   "accepted v4.11 after PCP sample", evidence.get("pcp_basis", ""))
        kib = replace(pcp, value=None if reason else Decimal(pages) * page_bytes / 1024, unit="KiB")
        free = replace(kib, value=None if reason else Decimal(info["free_pages"]) * page_bytes / 1024,
                       source=MEMORY_RAW + "after/buddyinfo;" + MEMORY_RAW + "after/zoneinfo",
                       begin=s.blocks.get(MEMORY_RAW + "after/buddyinfo", b).begin if b else None,
                       basis=info.get("basis", "") + ";page_bytes=" + str(page_bytes),
                       note="v4.11 accepted buddy+PCP after pair; sequential, non-atomic; not backend-exclusive or root-cause proof")
        return {"zoneinfo.PCP_pages": pcp, "zoneinfo.PCP_kib": kib, CONSISTENT_FREE: free}
    b, error = payload(s, "/proc/zoneinfo")
    if not error and (b.begin < s.ns or b.end < b.begin):
        error = "zoneinfo sample clock unavailable/out of order"
    pages, basis, error = zoneinfo_pcp(b, error)
    note = "sum pagesets cpu count across captured nodes/zones; high/batch are not free pages"
    pcp = cell(pages, "gauge", "pages", "/proc/zoneinfo", b, error, note, basis)
    valid_size = (type(page_bytes) is int and page_bytes > 0 and
                  not page_bytes & (page_bytes - 1))
    reason = error or ("guest page_bytes missing/invalid" if not valid_size else "")
    kib = replace(pcp, value=None if reason else Decimal(pages) * page_bytes / 1024,
                  unit="KiB", quality="unavailable" if reason else "ok", reason=reason,
                  basis=basis + ";page_bytes=" + str(page_bytes))
    reason = reason or memfree.reason
    if not reason and (memfree.value is None or memfree.begin is None or
                       memfree.end is None or memfree.begin < s.ns or memfree.end < memfree.begin):
        reason = "MemFree value/sample clock unavailable/out of order"
    combined = replace(kib, value=None if reason else memfree.value + kib.value,
                       source="/proc/meminfo;/proc/zoneinfo", note=PHYSICAL_FREE_NOTE,
                       begin=min(memfree.begin, pcp.begin) if memfree.begin is not None and pcp.begin is not None else None,
                       end=max(memfree.end, pcp.end) if memfree.end is not None and pcp.end is not None else None,
                       quality="unavailable" if reason else "ok", reason=reason)
    return {"zoneinfo.PCP_pages": pcp, "zoneinfo.PCP_kib": kib,
            "memory.physical_free_MemFree_plus_PCP_kib": combined}


def slab_cache_family(name):
    if name in ("memcomp_entry", "memcomp_entry_cache"):
        return "memcomp_entry"
    for family in ("radix_tree_node", "zs_handle", "zspage"):
        if name == family or (family != "radix_tree_node" and name.startswith(family + "-")):
            return family
    return None


SLAB_SYSFS = "/sys/kernel/slab/"


def slab_alias_memory(s, page_bytes, family):
    """Missing-family fallback only: shared physical cache, never exclusive cost."""
    source = SLAB_SYSFS.rstrip("/")
    samples, caches, unique = [], {}, {}
    failure = ""

    def raw(path):
        b, error = payload(s, path)
        if not error and (b.begin < s.ns or b.end < b.begin):
            error = "sysfs alias sample clock unavailable/out of order"
        if error:
            raise ValueError(path + ": " + error)
        samples.append(b)
        return [line.strip() for line in b.lines if line.strip()]

    def number(path, numa=False):
        lines = raw(path)
        if len(lines) != 1:
            raise ValueError(path + ": missing/duplicate sysfs value")
        parts = lines[0].split()
        if not parts or not re.fullmatch(r"\d+", parts[0]):
            raise ValueError(path + ": malformed/negative sysfs value")
        nodes = {}
        for token in parts[1:]:
            match = re.fullmatch(r"N(\d+)=(\d+)", token) if numa else None
            if not match or int(match[1]) in nodes:
                raise ValueError(path + ": malformed/duplicate NUMA suffix")
            nodes[int(match[1])] = int(match[2])
        value = int(parts[0])
        if nodes and sum(nodes.values()) != value:
            raise ValueError(path + ": inconsistent NUMA total")
        return value

    try:
        inventory = raw(SLAB_SYSFS + "compare_aliases")
        if source in s.limits:
            raise ValueError("sysfs alias directory capture capped")
        if (len(inventory) != len(set(inventory)) or
                any(not re.fullmatch(r"[A-Za-z0-9_-]+", n) or not slab_cache_family(n)
                    for n in inventory)):
            raise ValueError("malformed/duplicate sysfs alias inventory")
        names = [n for n in inventory if slab_cache_family(n) == family]
        if not names:
            raise ValueError("named cache absent from sysfs alias inventory; not zero or N/A")
        for name in names:
            prefix = SLAB_SYSFS + name + "/"
            identity = {}
            for line in raw(prefix + "compare_identity"):
                parts = line.split()
                if len(parts) != 2 or parts[0] in identity:
                    raise ValueError(name + ": malformed/duplicate alias identity")
                identity[parts[0]] = parts[1]
            if (set(identity) != {"readlink", "target", "device", "inode"} or
                    not re.fullmatch(r"\d+", identity["device"]) or
                    not re.fullmatch(r"[1-9]\d*", identity["inode"]) or
                    not re.fullmatch(re.escape(SLAB_SYSFS) + r"[^/\s.][^/\s]*", identity["target"])):
                raise ValueError(name + ": missing/invalid actualtarget identity")
            link_target = (SLAB_SYSFS + name if identity["readlink"] == "." else
                           posixpath.normpath(posixpath.join(source, identity["readlink"])))
            if link_target != identity["target"]:
                raise ValueError(name + ": readlink/actualtarget identity mismatch")
            values = {field: number(prefix + field, field in ("objects", "total_objects", "slabs"))
                      for field in ("objects", "total_objects", "object_size", "slab_size",
                                    "objs_per_slab", "order", "slabs", "aliases")}
            if (not values["object_size"] or values["slab_size"] < values["object_size"] or
                    not values["objs_per_slab"] or values["order"] > 30 or
                    values["objects"] > values["total_objects"] or
                    values["total_objects"] > values["slabs"] * values["objs_per_slab"] or
                    bool(values["total_objects"]) != bool(values["slabs"])):
                raise ValueError(name + ": inconsistent sysfs counts/geometry")
            pages = 1 << values["order"]
            if valid_page_bytes(page_bytes) and values["slab_size"] * values["objs_per_slab"] > pages * page_bytes:
                raise ValueError(name + ": sysfs slots exceed slab pages")
            # An alias is a view of an entire cache. Two names with the same
            # stat identity must not multiply its objects or physical pages.
            key = identity["device"] + ":" + identity["inode"]
            if key in unique and unique[key] != (identity["target"], values):
                raise ValueError(name + ": shared target changed during non-atomic capture")
            unique[key] = (identity["target"], values)
            fields = dict(zip(SLAB_FIELDS, (
                values["objects"], values["total_objects"], values["slab_size"],
                values["objs_per_slab"], pages, None, values["slabs"], values["slabs"] * pages,
                values["slabs"] * pages * page_bytes if valid_page_bytes(page_bytes) else None)))
            fields.update(sysfs_object_size_bytes=values["object_size"],
                          allocator_slot_size_bytes=values["slab_size"], aliases=values["aliases"])
            caches[name] = (key, identity, fields)
    except ValueError as exc:
        failure = "sysfs alias fallback: " + str(exc)
    b = Block(source, min(x.begin for x in samples), max(x.end for x in samples)) if samples else None
    note = ("sysfs alias fallback; shared cache target, not exclusive backend metadata; "
            "object_size_bytes/allocator_slot_size_bytes = slab_size (allocator slot); "
            "sysfs_object_size_bytes = object_size (shared-cache object size, not original struct size); "
            "allocated_pages = slabs * 2**order; same target counted once per family; "
            "family/per-alias/cross-family views overlap inside global Slab, never extra addends; "
            "sequential non-atomic samples; aliases=" + ",".join(sorted(caches)))
    units = dict(SLAB_FIELDS, sysfs_object_size_bytes="bytes", allocator_slot_size_bytes="bytes", aliases="aliases")
    out, dedup = {}, {}
    for cache, (key, identity, fields) in caches.items():
        dedup[key] = fields
        if cache == family:
            continue
        for name, value in fields.items():
            reason = failure or ("sysfs does not expose active_slabs" if name == "active_slabs" else "")
            if name == "allocated_bytes" and not valid_page_bytes(page_bytes):
                reason = reason or "guest page_bytes missing/invalid"
            out[f"slabinfo.{cache}.{name}"] = cell(
                None if reason else value, "gauge", units[name], source, b, reason,
                note + "; readlink=" + identity["readlink"] + "; actualtarget=" + identity["target"] + "; identity=" + key,
                "slab-cache=" + cache + ";sysfs-target=" + key)
    geometry = {"object_size_bytes", "objects_per_slab", "pages_per_slab",
                "sysfs_object_size_bytes", "allocator_slot_size_bytes"}
    for name in SLAB_FIELDS.keys() | geometry:
        reason = failure or ("sysfs does not expose active_slabs" if name == "active_slabs" else "")
        values = [fields[name] for fields in dedup.values()]
        if not reason and name in geometry and len(set(values)) != 1:
            reason = "heterogeneous shared-cache geometry; use per-alias observations"
        if name == "allocated_bytes" and not valid_page_bytes(page_bytes):
            reason = reason or "guest page_bytes missing/invalid"
        value = None if reason else values[0] if name in geometry else sum(values)
        basis = "slab-family=" + family + ";sysfs-targets=" + ",".join(sorted(dedup))
        if name == "allocated_bytes":
            basis += ";page_bytes=" + str(page_bytes)
        out[f"slabinfo.{family}.{name}"] = cell(value, "gauge", units[name], source, b, reason, note, basis)
    return out


def slab_memory(s, page_bytes):
    """Decode slabinfo 2.1, including per-pool zsmalloc cache names."""
    path = "/proc/slabinfo"
    b, error = payload(s, path)
    rows = {name: {} for name in SLAB_CACHES}
    problems = defaultdict(list)
    if not error:
        if not b.lines or b.lines[0].strip() != "slabinfo - version: 2.1":
            error = "slabinfo unsupported/missing version (expected 2.1)"
        elif b.begin < s.ns or b.end < b.begin:
            error = "slabinfo sample clock unavailable/out of order"
    if not error:
        for line in b.lines[1:]:
            parts = line.split()
            if not parts or parts[0].startswith("#"):
                continue
            name = parts[0]
            family = slab_cache_family(name)
            if family is None:
                continue
            if name in rows[family]:
                problems[family].append("duplicate cache " + name)
                continue
            sections = line.split(":")
            try:
                if len(sections) != 3:
                    raise ValueError("expected tunables and slabdata sections")
                head, tunables, slabdata = (part.split() for part in sections)
                if (len(head) != 6 or len(tunables) != 4 or tunables[0] != "tunables" or
                        len(slabdata) != 4 or slabdata[0] != "slabdata" or
                        any(not re.fullmatch(r"\d+", value)
                            for value in head[1:] + tunables[1:] + slabdata[1:])):
                    raise ValueError("malformed/negative slabinfo fields")
                active, count, size, objects, pages = map(int, head[1:])
                active_slabs, slabs, _ = map(int, slabdata[1:])
                if (not size or not objects or not pages or active > count or
                        active_slabs > slabs or count > slabs * objects or
                        bool(count) != bool(slabs)):
                    raise ValueError("inconsistent slabinfo counts/geometry")
                if valid_page_bytes(page_bytes) and size * objects > pages * page_bytes:
                    raise ValueError("slab objects exceed allocated slab pages")
                rows[family][name] = dict(zip(SLAB_FIELDS, (
                    active, count, size, objects, pages, active_slabs, slabs,
                    slabs * pages, slabs * pages * page_bytes if valid_page_bytes(page_bytes) else None)))
            except ValueError as exc:
                problems[family].append(f"{name}: {exc}")
    out = {}
    geometry = {"object_size_bytes", "objects_per_slab", "pages_per_slab"}
    for family, caches in rows.items():
        reason = error or "; ".join(problems[family]) or (
            "named cache absent (possibly uncreated/merged); not zero" if not caches else "")
        note = SLAB_NOTE + "; observed caches=" + ",".join(sorted(caches))
        for name, unit in SLAB_FIELDS.items():
            values = [row[name] for row in caches.values()]
            field_reason = reason
            if not field_reason and name in geometry and len(set(values)) != 1:
                field_reason = "heterogeneous cache geometry; use per-cache observations"
            if not field_reason and name == "allocated_bytes" and not valid_page_bytes(page_bytes):
                field_reason = "guest page_bytes missing/invalid"
            value = None if field_reason else values[0] if name in geometry else sum(values)
            basis = "slab-family=" + family
            if name == "allocated_bytes":
                basis += ";page_bytes=" + str(page_bytes)
            out[f"slabinfo.{family}.{name}"] = cell(
                value, "gauge", unit, path, b, field_reason, note, basis)
        # Keep actual cache identities as well as family totals. A total is not
        # an attribution to the backend being benchmarked (notably for xarray).
        for cache, fields in caches.items():
            if cache == family:
                continue
            for name, value in fields.items():
                field_reason = error or "; ".join(problems[family])
                if name == "allocated_bytes" and not valid_page_bytes(page_bytes):
                    field_reason = field_reason or "guest page_bytes missing/invalid"
                basis = "slab-cache=" + cache
                if name == "allocated_bytes":
                    basis += ";page_bytes=" + str(page_bytes)
                out[f"slabinfo.{cache}.{name}"] = cell(
                    None if field_reason else value, "gauge", SLAB_FIELDS[name], path, b,
                    field_reason, SLAB_NOTE, basis)
        # Never use aliases to hide a malformed/present slabinfo family.
        if not error and not caches and not problems[family]:
            out.update(slab_alias_memory(s, page_bytes, family))
    return out


def memcomp_accounting(s, page_bytes):
    """Allocated object bytes, including pending and retired until actual free.

    Own cluster xarray nodes are separate from index_metadata_bytes. Shared
    radix_tree_node slab observations cannot establish this ownership. objcg 0
    represents uncharged/root allocations; IDs and zero tombstones are boot-stable.
    """
    b, error = payload(s, MEMCOMP_ACCOUNT)
    # raw_file() adds a newline before RAW_END after the source's own final
    # newline. Allow that one framing separator, never empty source rows.
    lines = b.lines[:-1] if b and b.lines and b.lines[-1] == "" else b.lines if b else []
    if not error and any(not re.fullmatch(r"[a-z0-9_.]+ [0-9]+", line) for line in lines):
        error = "metadata requires key space unsigned decimal integer on every line"
    values, error = key_values(b, error)
    if error:
        raise ValueError(error)
    missing = set(MEMCOMP_ACCOUNT_FIELDS) - values.keys()
    if missing:
        raise ValueError("missing metadata fields: " + ",".join(sorted(missing)))
    if any(type(v) is not int or v < 0 for v in values.values()):
        raise ValueError("negative/noninteger metadata field")
    if values["version"] != 1 or values["page_bytes"] != page_bytes:
        raise ValueError("metadata version/page size mismatch")
    if not values["cluster_header_bytes"] or values["xa_node_bytes"] != 576:
        raise ValueError("invalid cluster layout or expected 576-byte xa_node layout")
    if sum(values[k] for k in MEMCOMP_COMPONENTS) != values["index_metadata_bytes"]:
        raise ValueError("metadata components do not reconcile with lifetime total")
    if values["cluster_xa_bytes"] != values["cluster_xa_nodes"] * values["xa_node_bytes"]:
        raise ValueError("owned cluster xarray byte/node mismatch")
    if (values["retired_bytes"] > values["index_metadata_bytes"] or
            values["retired_clusters"] > values["clusters"] or
            values["cluster_bytes"] < values["clusters"] * values["cluster_header_bytes"]):
        raise ValueError("invalid retired subset/cluster allocation")
    for count, cost in (("clusters", "cluster_bytes"), ("keys", "key_bytes"),
                        ("pending_records", "pending_bytes")):
        if bool(values[count]) != bool(values[cost]):
            raise ValueError("allocation count/bytes mismatch: " + count)
    owners = {}
    for name, value in values.items():
        if name in MEMCOMP_ACCOUNT_FIELDS:
            continue
        match = re.fullmatch(r"objcg\.(0|[1-9][0-9]*)\.(payload_bytes|metadata_bytes)", name)
        if not match:
            raise ValueError("unknown metadata field: " + name)
        owners.setdefault(match[1], {})[match[2]] = value
    if (len(owners) != values["objcg_count"] or
            any(set(row) != {"payload_bytes", "metadata_bytes"} for row in owners.values())):
        raise ValueError("missing/partial objcg inventory")
    if sum(row["metadata_bytes"] for row in owners.values()) != values["index_metadata_bytes"]:
        raise ValueError("objcg metadata does not reconcile with lifetime total")
    return dict(values=values, objcgs=owners,
                total_bytes=values["index_metadata_bytes"] + values["cluster_xa_bytes"])


def memcomp_metadata_metrics(s, page_bytes):
    b, _ = payload(s, MEMCOMP_ACCOUNT)
    try:
        account = memcomp_accounting(s, page_bytes)
        values = dict(account["values"], total_bytes=account["total_bytes"])
        error = ""
    except ValueError as exc:
        values, error = {}, str(exc)
    out = {}
    for name in (*MEMCOMP_ACCOUNT_FIELDS, "total_bytes"):
        unit = "bytes" if name.endswith("bytes") else "count"
        out["memcomp.metadata." + name] = cell(values.get(name), "gauge", unit,
            MEMCOMP_ACCOUNT, b, error, "bench-only lifetime accounting; retired_bytes is a subset")
    for name, value in values.items():
        if name.startswith("objcg."):
            out["memcomp." + name] = cell(value, "gauge", "bytes", MEMCOMP_ACCOUNT, b)
    return out


def valid_page_bytes(page_bytes):
    return type(page_bytes) is int and page_bytes > 0 and not page_bytes & (page_bytes - 1)


def pool_pages(c, page_bytes):
    reason = c.reason or ("guest page_bytes missing/invalid" if not valid_page_bytes(page_bytes) else "")
    if not reason and (c.value is None or c.value < 0 or c.value % page_bytes):
        reason = "physical pool bytes missing/negative/not page-aligned"
    return replace(c, value=None if reason else c.value // page_bytes, unit="pages",
                   quality="unavailable" if reason else "ok", reason=reason,
                   basis=c.basis + ";page_bytes=" + str(page_bytes),
                   note=c.note + "; allocator pool bytes / guest page_bytes; not total backend cost")


def snapshot_metrics(s, page_bytes):
    out = memcomp_metadata_metrics(s, page_bytes)
    b, error = payload(s, "/proc/vmstat")
    values, error = key_values(b, error)
    zones = sorted(k for k in values if k.startswith("allocstall_"))
    for k in ["pswpin", "pswpout", "zswpin", "zswpout", "zswpwb",
              "pgscan_kswapd", "pgscan_direct"] + zones + (["allocstall"] if "allocstall" in values else []):
        v = values.get(k)
        reason = error or ("field absent/negative" if v is None or v < 0 else "")
        if k.startswith(("pswp", "zswp")):
            unit, note = "page_events", "base-page swap/zswap events, not physical IO"
        elif k.startswith("pgscan_"):
            unit, note = "scan_events", "reclaim scan events"
        else:
            unit, note = "stall_events", "zone allocation-stall events"
        out["vmstat." + k] = cell(None if reason else v, "counter", unit,
                                 "/proc/vmstat", b, reason, note)
    total = sum(values[k] for k in zones) if zones else values.get("allocstall")
    reason = error or ("allocstall zone/aggregate counters absent" if total is None else "")
    if any(values[k] < 0 for k in zones) or (total is not None and total < 0):
        reason = "negative counter"
    out["vmstat.allocstall_sum"] = cell(None if reason else total, "counter", "stall_events", "/proc/vmstat", b, reason,
                                            "sum zone counters only; aggregate never added twice", ",".join(zones) or "aggregate-only")
    if not zones:
        out["vmstat.allocstall_zones"] = cell(None, "unavailable", "stall_events", "/proc/vmstat", b, error or "per-zone fields absent")
    path = "/proc/meminfo"
    b, error = payload(s, path)
    for name in MEMINFO_FIELDS:
        matches = [] if error else [line.split() for line in b.lines if line.startswith(name + ":")]
        reason = error or ("meminfo field absent/malformed" if len(matches) != 1 or len(matches[0]) != 3 or matches[0][2] != "kB" or not matches[0][1].isdigit() else "")
        out["meminfo." + name + "_kib"] = cell(None if reason else int(matches[0][1]), "gauge", "KiB", path, b, reason)
    out["meminfo.MemFree_kib"].note = MEMFREE_NOTE
    out.update(optional_memory(s, page_bytes, out["meminfo.MemFree_kib"]))
    out.update(slab_memory(s, page_bytes))
    for name in ("Slab", "SReclaimable", "SUnreclaim", "PageTables", "Percpu", "VmallocUsed"):
        out[f"meminfo.{name}_kib"].note = "global memory gauge; not exclusive backend attribution"
    b, error = payload(s, "/proc/diskstats")
    rows = [] if error else [line.split() for line in b.lines if len(line.split()) >= 3 and line.split()[2] == "vda"]
    if not error and (len(rows) != 1 or len(rows[0]) < 10 or any(not re.fullmatch(r"\d+", rows[0][i]) for i in (5, 9))):
        error = "vda missing/duplicate/malformed; not zero"
    for name, index in (("read", 5), ("write", 9)):
        for suffix, unit, scale in (("sectors", "sectors_512B", 1), ("bytes", "bytes", 512)):
            out[f"vda.{name}_{suffix}"] = cell(None if error else int(rows[0][index]) * scale, "counter", unit, "/proc/diskstats", b, error,
                                                   "guest backing-device IO; includes all vda activity, not durable host IO or exclusive backend WB")
    for resource in ("memory", "cpu", "io"):
        path = "/proc/pressure/" + resource
        b, error = payload(s, path)
        for level in ("some", "full"):
            rows = [] if error else [line.split() for line in b.lines if line.startswith(level + " ")]
            totals = [] if len(rows) != 1 else [v[6:] for v in rows[0][1:] if v.startswith("total=")]
            reason = error or ("PSI total missing/duplicate/malformed" if len(totals) != 1 or not totals[0].isdigit() else "")
            out[f"psi.{resource}.{level}_total_us"] = cell(None if reason else int(totals[0]), "counter", "us", path, b, reason, "cumulative total only; avg10/60/300 are not differenced")
    path = MEMCOMP + "stat"
    b, error = payload(s, path)
    values, error = numbers(b, error, 3, exact=True)
    note = "L/C include swap-backed live entries; P is zsmalloc physical pool, not all backend memory"
    for i, name in enumerate(("L_bytes", "C_bytes", "P_bytes")):
        out["memcomp." + name] = cell(None if error else values[i], "gauge", "bytes", path, b, error, note)
    for name, denom in (("L_over_C", "C_bytes"), ("L_over_P", "P_bytes")):
        out["memcomp." + name] = ratio(out["memcomp.L_bytes"], out["memcomp." + denom])
        out["memcomp." + name].note = note + "; L/P is not a pure compression ratio"
    for group in EVENTS:
        path = MEMCOMP + group
        b, error = payload(s, path)
        gate_values, gate_error = {}, error
        if group == "packed_swap_events" and not error:
            # Gate data is descriptive: even a missing/malformed gate must not
            # poison an independently valid required writeback counter.
            gate_lines, counter_lines = [], []
            for line in b.lines:
                target = (gate_lines if line.split()[:1] == ["writeback_gate_open"]
                          else counter_lines)
                target.append(line)
            gate_values, gate_error = key_values(replace(b, lines=gate_lines), "")
            b_values = replace(b, lines=counter_lines)
        else:
            b_values = b
        values, error = key_values(b_values, error)
        for k in sorted(EVENTS[group] | GAUGES[group] | PEAKS[group] | values.keys()):
            kind = "counter" if k in EVENTS[group] else "gauge" if k in GAUGES[group] else "peak" if k in PEAKS[group] else "unknown"
            v = values.get(k)
            reason = error or ("field absent" if v is None else "negative counter" if kind == "counter" and v < 0 else "")
            if group == "packed_swap_events" and k == "writeback_gate_open":
                v = gate_values.get(k)
                reason = gate_error or ("writeback gate absent or not 0/1" if v not in (0, 1) else "")
            unit = "base_pages" if k.endswith("_pages") or k == "writeback_zs_reclaimed" else "ns" if k.endswith("_ns") else "ABI_count"
            if k == "zs_huge_class_size":
                unit = "bytes"
            elif k == "writeback_payloads":
                unit = "payload_conversions"
            note = ("descriptive gate state only; never a wait or latency-exclusion condition"
                    if k == "writeback_gate_open" else WB_NOTE)
            c = cell(None if reason else v, kind, "raw_integer" if kind == "unknown" else unit, path, b, reason, note)
            if kind == "unknown":
                c = replace(c, quality="unverified", reason="unknown field semantics; snapshot only")
            out[f"memcomp.{group}.{k}"] = c
    for file, names in (("mm_stat", ("orig_data_size", "compr_data_size", "mem_used_total")),
                        ("bd_stat", ("bd_count", "bd_reads", "bd_writes"))):
        path = ZRAM + file
        b, error = payload(s, path)
        values, error = numbers(b, error, 3)
        for i, name in enumerate(names):
            kind = "counter" if name in ("bd_reads", "bd_writes") else "gauge"
            unit = "units_4KiB" if file == "bd_stat" else "bytes"
            note = "fixed 4096-byte units, not requests; bd_count is current backing occupancy" if file == "bd_stat" else "first three mm_stat fields; pool includes allocator overhead"
            c = cell(None if error else values[i], kind, unit, path, b, error, note)
            out["zram." + name] = c
            if file == "bd_stat":
                out["zram." + name + "_bytes"] = replace(c, value=None if c.value is None else c.value * 4096, unit="bytes")
    out["zram.logical_over_payload"] = ratio(out["zram.orig_data_size"], out["zram.compr_data_size"])
    out["zram.logical_over_pool"] = ratio(out["zram.orig_data_size"], out["zram.mem_used_total"])
    for name, unit in (("stored_pages", "base_pages"), ("pool_total_size", "bytes")):
        path = ZSWAP + name
        b, error = payload(s, path)
        values, error = numbers(b, error, 1, exact=True)
        out["zswap." + name] = cell(None if error else values[0], "gauge", unit, path, b, error, "debugfs gauge; pool includes allocator overhead")
    c = out["zswap.stored_pages"]
    out["zswap.logical_bytes"] = replace(c, value=c.value * page_bytes if c.value is not None and page_bytes else None, unit="bytes",
                                              quality="ok" if c.value is not None and page_bytes else "unavailable", reason=c.reason or ("guest page_bytes missing/ambiguous" if not page_bytes else ""))
    out["zswap.logical_over_pool"] = ratio(out["zswap.logical_bytes"], out["zswap.pool_total_size"])
    out["zswap.logical_over_pool"].note = "logical/pool including overhead; not payload compression ratio; sequential non-atomic reads"
    for family, name in (("memcomp", "memcomp.P_bytes"), ("zram", "zram.mem_used_total"),
                         ("zswap", "zswap.pool_total_size")):
        out[family + ".pool_pages"] = pool_pages(out[name], page_bytes)
    for name, unit, reason in (("zswap.payload_ratio", "ratio", "compressed payload total not exposed by these gauges"),
                               ("attribution.backend_metadata_bytes", "bytes", "no object-level attribution; P-C is not pure metadata"),
                               ("attribution.dedicated_writeback_cpu_seconds", "seconds", "shared kworkers; dedicated writeback CPU unavailable")):
        out[name] = cell(None, "unavailable", unit, "", reason=reason)
    return out


def thread_sample(s):
    pairs, problems, selected = {}, [], {}
    for path, b in s.blocks.items():
        m = PROC.fullmatch(path)
        if m:
            pairs.setdefault(int(m[1]), {})[m[2]] = b
    if not pairs or "kernel_threads" in s.blocks or "kernel_threads" in s.limits or len(pairs) >= 64:
        problems.append("kernel_threads missing/capped (64) or failed")
    for pid, pair in pairs.items():
        try:
            comm, stat = pair["comm"], pair["stat"]
            if comm.error or stat.error or comm.end is None or stat.end is None:
                raise ValueError("incomplete comm/stat block")
            name = "\n".join(comm.lines).strip()
            text = " ".join(stat.lines).strip()
            head, tail = text.rsplit(")", 1)
            stat_pid, stat_name = head.split("(", 1)
            fields = tail.split()
            if int(stat_pid.strip()) != pid or stat_name != name or not name.startswith(("kswapd", "kworker")):
                raise ValueError("proc identity/name mismatch")
            utime, stime, start = (int(fields[i]) for i in (11, 12, 19))
            if min(utime, stime, start) < 0:
                raise ValueError("negative proc stat")
            if name.startswith("kswapd"):
                selected[(pid, start)] = (utime + stime, stat)
        except (KeyError, ValueError, IndexError):
            problems.append(f"PID {pid}: missing/malformed comm/stat")
    if not selected:
        problems.append("no complete kswapd identity observed")
    return selected, problems


def cpu_delta(a, b, clock):
    left, problems = a.threads
    right, other = b.threads
    problems = problems + other
    if left.keys() != right.keys():
        problems.append("kswapd PID/starttime sets changed; matched subtotal only")
    matched = left.keys() & right.keys()
    if not matched:
        problems.append("no same PID+starttime match")
    if any(right[k][0] < left[k][0] or right[k][1].begin < left[k][1].end for k in matched):
        matched = set()
        problems.append("matched CPU counter/clock decreased; no trusted subtotal")
    ticks = sum(right[k][0] - left[k][0] for k in matched) if matched else None
    reason = "; ".join(dict.fromkeys(problems))
    note = "same PID+starttime only; utime+stime ticks; observed census, not dedicated backend CPU"
    if matched:
        note += "; matched=" + ",".join(f"{pid}:{start}" for pid, start in sorted(matched))
    c = Cell(ticks, "matched_cpu_delta", "ticks", ";".join(f"/proc/{pid}/stat" for pid, _ in sorted(matched)),
             min((left[k][1].begin for k in matched), default=None), max((left[k][1].end for k in matched), default=None),
             "partial" if ticks is not None and problems else "ok" if ticks is not None else "unavailable", reason, note)
    ending = (min((right[k][1].begin for k in matched), default=None), max((right[k][1].end for k in matched), default=None))
    seconds = replace(c, value=Decimal(ticks) / clock if ticks is not None and clock else None, unit="seconds",
                      quality=c.quality if clock else "unavailable", reason=reason + ("; " if reason and not clock else "") + ("guest COMPARE_CLOCK_TICKS missing/invalid/ambiguous" if not clock else ""))
    return {"cpu.kswapd_ticks": (c, ending), "cpu.kswapd_seconds": (seconds, ending)}


def difference(a, b):
    c = a or b
    reason = ("endpoint metric absent" if a is None or b is None else a.reason or b.reason)
    if not reason and (a.value is None or b.value is None):
        reason = "endpoint value unavailable"
    if not reason and (a.semantics != b.semantics or a.unit != b.unit or a.basis != b.basis):
        reason = "endpoint metric definitions differ"
    if not reason and (a.end is None or b.begin is None or b.begin < a.end):
        reason = "endpoint sample clock unavailable/out of order"
    if not reason and c.semantics == "counter" and b.value < a.value:
        reason = "counter decreased/reset; no clamp/wrap inference"
    return replace(c, value=None if reason else b.value - a.value,
                   begin=a.begin if a else None, end=a.end if a else None,
                   semantics="counter_delta" if c.semantics == "counter" else "gauge_net_change",
                   quality="unavailable" if reason else "ok", reason=reason)



def phase_parts(snapshot):
    match = PHASE.fullmatch(snapshot.phase)
    if match:
        r, mode, stage = match.groups()
        return int(r), mode, mode + "_" + stage
    return None, "", snapshot.phase


def phase_windows(log, boot):
    by_name = defaultdict(list)
    for s in log.snapshots:
        by_name[s.phase].append(s)
    windows = []
    wb_phases = [name for name in ("writeback_settled", "writeback_unstable") if by_name.get(name)]
    wb_end = wb_phases[0] if len(wb_phases) == 1 else "writeback_settled"
    if boot["label"] == "w4" and len(wb_phases) != 1:
        log.issues.append("W4 requires exactly one stable-or-timeout phase")

    def add(scope, left, right, required=False):
        a, b = by_name.get(left,[]), by_name.get(right,[])
        if len(a)==len(b)==1 and a[0].ns < b[0].ns:
            windows.append((scope,a[0],b[0]))
        elif required:
            log.issues.append(f"missing/duplicate/out-of-order phase window {left}->{right}")

    add("backend_setup_delta", "before_backend_setup", "workload_start", True)
    add("global_delta","workload_start","workload_end",True)
    if boot["label"]=="w1":
        numbers = sorted({phase_parts(s)[0] for s in log.snapshots if phase_parts(s)[0] is not None})
        warmup = stats.option(boot, "--warmup", 1)
        rounds = stats.option(boot, "--rounds", 5)
        if warmup not in (0, 1) or rounds <= 0:
            raise ValueError("invalid W1 warmup/rounds")
        if boot["formal"] and (warmup != 1 or rounds != 5):
            log.issues.append("formal W1 requires one warmup plus five measurement rounds")
        if set(numbers) != set(range(warmup + rounds)):
            log.issues.append("raw round phases do not cover configured warmup/measurement rounds")
        for r in range(warmup + rounds):
            for mode in ("seq","rand","single","rewrite"):
                prefix = f"r{r}_{mode}_"
                add("read_delta",prefix+"read_begin",prefix+"read_end",boot["formal"])
                if mode!="single":
                    add("pageout_delta",prefix+"prepared",prefix+"read_begin",boot["formal"])
            add("fresh_mapping_gate",f"r{r}_seq_fresh_mapping",f"r{r}_seq_fill_begin",True)
            add("rewrite_mapping_gate",f"r{r}_rewrite_prime_fresh_mapping",f"r{r}_rewrite_prime_fill_begin",True)
            add("rewrite_primer_gate",f"r{r}_rewrite_prime_read_end",f"r{r}_rewrite_fill_begin",True)
            for mode in ("seq", "rewrite"):
                add("backend_to_drained_delta", "before_backend_setup", f"r{r}_{mode}_read_begin", boot["formal"])
            add("fill_to_drained_delta",f"r{r}_seq_fill_begin",f"r{r}_seq_read_begin",boot["formal"])
            add("rewrite_delta",f"r{r}_rewrite_fill_begin",f"r{r}_rewrite_read_begin",boot["formal"])
            add("round_delta",f"r{r}_seq_fill_begin",f"r{r}_rewrite_read_end",boot["formal"])
    else:
        add("backend_to_drained_delta", "before_backend_setup",
            "workload_end" if boot["label"] == "w2" else "read_begin", boot["formal"])
        add("read_delta", "read_begin", "read_end",
            boot["label"] == "w4" or (boot["formal"] and boot["label"] == "w3"))
        add("prepared_to_read_delta", "prepared", "read_begin")
        if boot["label"] == "w3" and stats.option(boot, "--pressure-mib", 0):
            add("prepared_to_pressure_delta", "prepared", "pressure_begin", True)
            add("pressure_allocation_delta", "pressure_begin", "pressure_ready", True)
            add("pressure_to_read_delta", "pressure_ready", "read_begin", True)
        if boot["label"] == "w4":
            # Preserve phase identities and the zram explicit-writeback hook.
            # The common window starts after target PAGEOUT, before explicit
            # zram idle IO and pressure allocation. Keep explicit IO separate
            # descriptively; do not confuse it with autonomous reclaim.
            add("pageout_delta", "prepared", "pageout_returned", True)
            add("write_window_to_pageout_ack_delta", "pressure_begin", "pageout_returned", True)
            add("pressure_allocation_delta", "pressure_begin", "pressure_ready", True)
            add("writeback_wait_delta", "pressure_ready", wb_end, True)
            add("pressure_delta", "pressure_begin", wb_end, True)
            add("write_volume_delta", "pressure_begin", "read_begin", True)
            add("writeback_to_read_delta", wb_end, "read_begin", True)
            add("writeback_evidence_delta", "pressure_begin", "read_begin", True)
            add("explicit_writeback_delta", "w4_before_explicit_writeback", "pageout_returned",
                boot["meta"].get("backend") == "zram-writeback")
    return windows


def w4_evidence(boot, log):
    """Require native conversions and device IO within pressure_begin->read_begin."""
    if boot["label"] != "w4":
        return None
    backend = boot["meta"].get("backend")
    if backend not in stats.WRITEBACK:
        log.issues.append("W4 backend has no defined writeback counter/unit")
        return None
    _, unit, name = stats.WRITEBACK[backend]
    endpoints = [[s for s in log.snapshots if s.phase == phase]
                 for phase in ("pressure_begin", "read_begin")]
    if any(len(samples) != 1 for samples in endpoints):
        log.issues.append("W4 writeback evidence endpoints missing/duplicate")
        return None
    left, right = (samples[0].metrics.get(name) for samples in endpoints)
    if left is None or right is None:
        log.issues.append("W4 writeback source missing; never substitute another backend")
        return None
    evidence = difference(left, right)
    if (evidence.quality != "ok" or evidence.reason or evidence.unit != unit or
            evidence.value is None or evidence.value <= 0):
        log.issues.append("W4 requires a positive observed writeback delta: " + name)
    left, right = (samples[0].metrics.get("vda.write_sectors") for samples in endpoints)
    if left is None or right is None:
        reason = "W4 pressure-to-read diskstats write sectors missing"
    else:
        disk = difference(left, right)
        reason = disk.reason or ("W4 requires positive pressure-to-read diskstats write sectors"
                                 if disk.value is None or disk.value <= 0 else "")
    if reason:
        log.issues.append(reason)
        return replace(evidence, value=None, quality="unavailable", reason=reason)
    return evidence


def backend_family(boot):
    backend = str(boot["meta"].get("backend", ""))
    return "memcomp" if backend.startswith("memcomp-") else "zram" if backend.startswith("zram") else "zswap" if backend == "zswap" else "swap"


def required_metrics(boot, snapshot):
    """Consumed C.5/C.6 and v4.10 memory baselines must be complete."""
    label = boot["label"]
    backend = boot["meta"].get("backend", "")
    family = backend_family(boot)
    match = PHASE.fullmatch(snapshot.phase)
    mode, stage = (match[2], match[3]) if match else ("", snapshot.phase)
    read = (stage in ("read_begin", "read_end") and label != "w2") or (
        label == "w2" and stage in ("workload_start", "workload_end"))
    if label == "w1":
        memory = mode in ("seq", "rewrite") and stage in ("fill_begin", "read_begin")
        pool = mode in ("seq", "rewrite") and stage == "read_begin"
    else:
        memory = pool = stage in (("workload_start", "workload_end") if label == "w2" else ("prepared", "read_begin"))
    if stage in ("before_backend_setup", "workload_start"):
        memory = True
        pool = stage == "workload_start"
    required = set()
    if read:
        required.update(("vmstat.pswpin", "vmstat.pswpout"))
        if family == "zswap":
            required.update(("vmstat.zswpin", "vmstat.zswpout", "vmstat.zswpwb"))
        elif family == "memcomp":
            required.update(READ_SOURCES[family])
        elif backend == "zram-writeback":
            required.update(("zram.bd_reads", "zram.bd_writes"))
        if backend in ("swap", "zswap", "memcomp-swap", "zram-writeback"):
            required.update(("vda.read_bytes", "vda.write_bytes"))
    if memory:
        required.update(f"meminfo.{name}_kib" for name in MEMINFO_FIELDS)
        required.update(MEMORY)
        caches = {"radix_tree_node"}
        # Before setup these named backend caches may not exist yet. Preserve
        # unavailable observations, never invent zero occupancy or a delta.
        if stage != "before_backend_setup":
            if family in POOLS:
                caches.update(("zs_handle", "zspage"))
        required.update(f"slabinfo.{cache}.{name}" for cache in caches for name in SLAB_REQUIRED)
    # The external probe is mandatory only for the J.4 W1 v2 memory flow.
    # Ordinary compare smoke and legacy W2/W3/W4 captures need no probe.
    if (family == "memcomp" and label == "w1" and snapshot.memory.get("version") == MEMORY_PROTOCOL and
            (memory or stage == "workload_end")):
        required.update(MEMCOMP_ACCOUNT_METRICS)
    if snapshot.memory and PHYSICAL_FREE in required:
        required.remove(PHYSICAL_FREE)
        required.add(CONSISTENT_FREE)
    if pool:
        if family in POOLS:
            required.add(family + ".pool_pages")
        required.update({
            "memcomp": {"memcomp.L_bytes", "memcomp.C_bytes", "memcomp.P_bytes"},
            "zram": {"zram.orig_data_size", "zram.compr_data_size", "zram.mem_used_total"},
            "zswap": {"zswap.stored_pages", "zswap.pool_total_size"},
        }.get(family, set()))
    if label == "w4":
        if stage in ("pressure_begin", "read_begin", "read_end"):
            required.update(("vda.write_bytes", "vda.write_sectors"))
        if stage in ("prepared", "pressure_begin", "pressure_ready", "writeback_settled", "writeback_unstable", "read_begin"):
            native = stats.WRITEBACK.get(backend)
            if native:
                required.add(native[2])
        if stage in ("pressure_ready", "writeback_settled", "writeback_unstable"):
            required.update(("vmstat.pgscan_kswapd", "vmstat.pgscan_direct"))
    return required


def retain_snapshot(s, log, family, boot=None):
    """Validate mandatory scalars, reduce the snapshot, then release raw text."""
    page_bytes = positive_marker([r.get("page_bytes", "") for r in log.requests])
    if page_bytes and page_bytes & (page_bytes - 1):
        page_bytes = None
    if boot is None:
        request = log.requests[0] if len(log.requests) == 1 else {}
        boot = {"label": request.get("workload", ""), "meta": {"backend": request.get("backend", family)}}
    selected = COMMON | ({CONSISTENT_FREE} if s.memory else set()) | POOLS.get(family, set()) | READ_SOURCES.get(family, set())
    if family != "memcomp":
        selected -= MEMCOMP_ACCOUNT_METRICS
    if boot["label"] == "w4":
        selected |= {"vda.read_sectors", "vda.write_sectors",
                     "vmstat.pgscan_kswapd", "vmstat.pgscan_direct"}
    if family == "memcomp":
        selected |= {WB_COUNTER, WB_GATE}
    elif family == "zram":
        selected |= {"zram.bd_count", "zram.bd_writes"}
    s.metrics = {name: value for name, value in snapshot_metrics(s, page_bytes).items() if name in selected or name.startswith(("slabinfo.", "memcomp.objcg."))}
    for name in sorted(required_metrics(boot, s)):
        if s.memory.get("status") == "INVALID" and (name == CONSISTENT_FREE or name in MEMORY_STATE or name.startswith("slabinfo.")):
            continue
        c = s.metrics.get(name)
        reason = "metric/source absent" if c is None else c.reason or (
            "required value unavailable" if c.value is None or c.quality != "ok" else "")
        if not reason and (c.begin is None or c.end is None or c.begin < s.ns or c.end < c.begin):
            reason = "required source sample clock unavailable/decreased"
        previous = log.required_last.get(name)
        if not reason and previous is not None:
            phase, before = previous
            if (c.semantics, c.unit, c.basis) != (before.semantics, before.unit, before.basis):
                reason = "required source definition changed since " + phase
            elif c.begin < before.end:
                reason = "required source sample clock decreased since " + phase
            elif c.semantics == "counter" and c.value < before.value:
                reason = "required counter decreased/reset since " + phase
        if reason:
            log.required_invalid = True
            log.issues.append(f"{s.phase}: required {name}: {reason}")
        else:
            # Gauges may legitimately shrink after release/writeback. Only their
            # sample clocks, not their values, must be monotonic.
            log.required_last[name] = (s.phase, c)
    threads, problems = thread_sample(s)
    s.threads = ({key: (ticks, replace(block, lines=[])) for key, (ticks, block) in threads.items()}, problems)
    s.blocks.clear()


def provenance(s, end, c, ending=(None, None)):
    return json.dumps(dict(phase=s.phase if s else None, end_phase=end.phase if end else None,
                          snapshot_ns=s.ns if s else None, end_snapshot_ns=end.ns if end else None,
                          sample_ns=[c.begin, c.end], end_sample_ns=list(ending),
                          semantics=c.semantics, quality=c.quality, basis=c.basis),
                      separators=(",", ":"), sort_keys=True)


def extract_boot(boot):
    directory = Path(boot["identity"]["result_dir"])
    if boot["blocked"]:
        return
    had_issues = bool(boot["issues"])
    family = backend_family(boot)
    log = parse_log(directory / "run1.log.gz",
                    lambda s, log: retain_snapshot(s, log, family, boot),
                    independent_setup_readbacks=True)
    boot["setup_readbacks"] = [
        dict(phase=s.phase, occurrence=s.occurrence, ns=s.ns,
             blocks={path: dict(begin=b.begin, end=b.end, lines=b.lines, error=b.error)
                     for path, b in s.blocks.items()}, limits=s.limits)
        for s in log.setup_readbacks]
    if any(s.memory for s in log.snapshots):
        boot["memory_snapshot_protocol"] = memory_snapshot_protocol(log.snapshots)
        boot["memory_snapshots"] = [dict(phase=s.phase, **s.memory) for s in log.snapshots if s.memory]
        states = [s.memory.get("status") for s in log.snapshots if s.memory]
        boot["memory_snapshot_status"] = "ERROR" if "ERROR" in states or log.issues else "INVALID" if "INVALID" in states else "CONSISTENT"
        boot["memory_snapshot_validity"] = {"CONSISTENT": "VALID", "INVALID": "INVALID", "ERROR": "ERROR"}[boot["memory_snapshot_status"]]
        boot["metrics"]["memory.snapshot_protocol"] = stats.Metric(
            None, "protocol", source="COMPARE_MEMORY_*",
            provenance=json.dumps({key: boot[key] for key in (
                "memory_snapshot_protocol", "memory_snapshot_validity", "memory_snapshots")}, separators=(",", ":")),
            note="per-phase raw-verified bounded attempts; INVALID invalidates memory only, not timing")
        if boot["memory_snapshot_status"] == "INVALID":
            for snapshot in log.snapshots:
                for name, metric in list(snapshot.metrics.items()):
                    if (name in MEMORY_STATE or name == CONSISTENT_FREE or name.startswith("slabinfo.") or
                            name in POOLS.get(family, set())):
                        snapshot.metrics[name] = replace(metric, value=None, quality="unavailable",
                                                        reason="boot memory result INVALID; timing/counters unaffected")
    if len(log.requests) != 1:
        log.issues.append("missing/duplicate COMPARE_REQUEST")
    else:
        request = log.requests[0]
        for raw_key, meta_key in (("backend", "backend"), ("algorithm", "algorithm_requested"), ("workload", "label")):
            if request.get(raw_key) != boot["meta"].get(meta_key):
                log.issues.append("COMPARE_REQUEST/metadata mismatch: " + raw_key)
    clock = positive_marker(log.clocks)
    warmup = stats.option(boot, "--warmup", 1) if boot["label"] == "w1" else 0
    rounds = stats.option(boot, "--rounds", 5) if boot["label"] == "w1" else 1
    measured = set(range(warmup, warmup + rounds))
    windows = phase_windows(log, boot)
    evidence = w4_evidence(boot, log)
    boot["issues"].extend(log.issues)
    boot["extraction_failed"] = log.required_invalid or (not had_issues and bool(log.issues))
    boot["metrics"]["run.raw_complete"] = stats.Metric(int(not log.issues), "boolean", source="run1.log.gz")
    boot["metrics"]["guest.clock_ticks"] = stats.Metric(clock, "ticks_per_second", reason="guest clock marker missing/ambiguous" if not clock else "", source="COMPARE_CLOCK_TICKS")
    collected = defaultdict(list)
    per_round = defaultdict(dict)

    def emit(scope, s, end, name, c, ending=(None, None), direction=""):
        r, mode, canonical = phase_parts(s)
        end_round, _, end_canonical = phase_parts(end) if end else (None, "", "")
        if r is None and end_round is not None:
            r = end_round
        key = scope + ":" + canonical + ("->" + end_canonical if end else "") + ":" + name
        metric = stats.Metric(c.value, c.unit, direction, c.reason if c.quality == "ok" else c.reason or c.quality,
                              c.source, provenance(s, end, c, ending), c.note)
        # Bad/partial values are never promoted to a trusted median.
        per_round[r if r is not None else -1][key] = metric
        if r is None or r in measured:
            collected[key].append((r, metric))

    for scope, a, b in windows:
        left, right = a.metrics, b.metrics
        # C.5 counters are read-window observations; pool representations are
        # endpoint gauges, not traffic, and are never ranked across backends.
        for name in sorted(left.keys() | right.keys()):
            x, y = left.get(name), right.get(name)
            c = x or y
            is_pool = name in POOLS.get(family, set())
            is_memory = name in MEMORY_STATE or name == CONSISTENT_FREE or name.startswith("slabinfo.")
            if scope == "read_delta" or (scope == "global_delta" and boot["label"] == "w2"):
                keep = c.semantics == "counter" or (scope == "global_delta" and
                    is_memory)
            elif scope in ("backend_setup_delta", "backend_to_drained_delta"):
                keep = is_memory or (is_pool and c.semantics == "gauge")
            elif scope == "prepared_to_read_delta":
                keep = is_memory
            elif scope == "write_volume_delta":
                keep = name in {"vda.write_bytes", "vda.write_sectors", "vmstat.zswpwb", "zram.bd_writes", WB_COUNTER}
            elif scope in ("fill_to_drained_delta", "rewrite_delta"):
                keep = is_memory or (is_pool and c.semantics == "gauge")
            elif scope == "writeback_evidence_delta":
                keep = name in {"vmstat.zswpwb", "zram.bd_writes", WB_COUNTER}
            elif scope in ("pageout_delta", "pressure_allocation_delta", "writeback_wait_delta"):
                keep = name in {"vda.write_bytes", "vmstat.allocstall_sum", "psi.memory.some_total_us", "psi.memory.full_total_us"}
                if scope == "writeback_wait_delta":
                    keep |= name in {"vmstat.pgscan_kswapd", "vmstat.pgscan_direct", "vda.write_sectors",
                                     "vmstat.zswpwb", "zram.bd_writes", WB_COUNTER}
            else:
                keep = False
            if keep:
                d = difference(x, y)
                direction = "lower" if name.startswith(("vda.", "psi.", "vmstat.allocstall")) and d.semantics == "counter_delta" else ""
                if (scope not in ("read_delta", "pageout_delta", "global_delta") or
                        boot["label"] == "w4" and scope == "read_delta" and name.startswith("vda.")):
                    direction = ""
                emit(scope, a, b, name, d, (y.begin, y.end) if y else (None, None), direction)
            if is_pool and scope in ("fill_to_drained_delta", "rewrite_delta") and y is not None:
                emit("drained_pool", b, None, name, y)
            if is_memory and scope == "fill_to_drained_delta":
                if x is not None:
                    emit("fresh_baseline", a, None, name, x)
                if y is not None:
                    emit("drained_memory", b, None, name, y)
        if scope in ("read_delta", "pageout_delta", "writeback_wait_delta") or (scope == "global_delta" and boot["label"] == "w2"):
            for name, (c, ending) in cpu_delta(a, b, clock).items():
                emit(scope, a, b, name, c, ending)
        if scope == "writeback_wait_delta":
            elapsed = cell(b.ns - a.ns, "elapsed", "ns", "COMPARE_SNAPSHOT", note="pressure_ready to stable/timeout endpoint; rate-limit policy, descriptive only")
            emit(scope, a, b, "writeback_wait_ns", elapsed)
    # Retain absolute observations at every captured phase, including both
    # memory baselines. Signed deltas alone cannot explain fixed setup costs.
    for endpoint in log.snapshots:
        for name, c in sorted(endpoint.metrics.items()):
            if name in MEMORY_STATE or name == CONSISTENT_FREE or name.startswith("slabinfo.") or name in POOLS.get(family, set()):
                emit("memory_state", endpoint, None, name, c)
    if boot["label"] == "w4":
        proven = int(evidence is not None and evidence.quality == "ok" and not evidence.reason and evidence.value is not None and evidence.value > 0)
        boot["metrics"]["writeback_proven"] = stats.Metric(proven, "boolean", source="run1.log.gz")
        timed_out = any(s.phase == "writeback_unstable" for s in log.snapshots)
        boot["metrics"]["writeback_wait_timed_out"] = stats.Metric(int(timed_out), "boolean", source="COMPARE_SNAPSHOT")
        read_begin = [s for s in log.snapshots if s.phase == "read_begin"]
        endpoints = [s for s in log.snapshots if s.phase in ("writeback_settled", "writeback_unstable")]
        if family == "memcomp":
            for scope, samples in (("writeback_state", endpoints), ("readback_state", read_begin)):
                c = samples[0].metrics.get(WB_GATE) if len(samples) == 1 else None
                if c is not None:
                    emit(scope, samples[0], None, WB_GATE, c)
        # Gate state is descriptive, including unavailable/invalid samples.
        # Only the counter/pgscan wait's timeout excludes readback latency.
        status = boot["status"][0] if len(boot["status"]) == 1 and isinstance(boot["status"][0], dict) else {}
        decision = status.get("writeback_latency_excluded")
        if type(decision) is not bool:
            boot["issues"].append("W4 runner readback exclusion decision missing")
        elif decision != timed_out:
            boot["issues"].append("W4 runner/raw timeout exclusion mismatch")
        boot["metrics"]["writeback_latency_excluded"] = stats.Metric(int(timed_out), "boolean", source="COMPARE_SNAPSHOT;status.json")
    for r, metrics in sorted(per_round.items()):
        boot["rounds"].append((r if r >= 0 else "", int(r >= 0 and r < warmup), metrics))
    for key, observations in collected.items():
        inner = {r for r, _ in observations if r is not None}
        expected = rounds if inner and boot["label"] == "w1" else 1
        cells = [metric for _, metric in observations]
        template = cells[0]
        values = [m.value for m in cells if m.value is not None and not m.reason]
        complete = len(values) == len(cells) == expected and (not inner or inner == measured)
        reasons = "; ".join(sorted({m.reason for m in cells if m.reason}))
        boot["metrics"][key] = stats.Metric(stats.quantile(values, '.5') if complete else None,
            template.unit, template.direction, "" if complete else f"{len(values)}/{expected} usable phase rounds; " + reasons,
            template.source, "per-round.csv:" + key, template.note)
    if boot["label"] == "w4":
        for key, metric in list(boot["metrics"].items()):
            if key.startswith("write_volume_delta:") and key.endswith(":vda.write_bytes"):
                boot["metrics"]["write_volume_bytes"] = replace(metric, direction="lower")
            elif key.startswith("read_delta:") and key.endswith(":vda.write_sectors"):
                boot["metrics"]["readback_write_sectors"] = replace(metric, direction="")
            elif key.startswith("read_delta:") and key.endswith(":vda.write_bytes"):
                boot["metrics"]["readback_write_bytes"] = replace(metric, direction="")
            elif key.startswith("writeback_evidence_delta:") and key.endswith(":" + stats.WRITEBACK.get(boot["meta"].get("backend"), ("", "", ""))[2]):
                boot["metrics"]["writeback_native_delta"] = replace(metric, direction="")
        for key, metric in list(boot["metrics"].items()):
            if key.startswith("writeback_wait_delta:") and key.endswith(":writeback_wait_ns"):
                boot["metrics"]["writeback_stability_wait_ns"] = replace(metric, direction="")
        status = boot["status"][0] if len(boot["status"]) == 1 and isinstance(boot["status"][0], dict) else {}
        volume = boot["metrics"].get("write_volume_bytes")
        if status.get("w4_diskstats_valid") is not True:
            boot["issues"].append("W4 runner did not validate swap-device diskstats")
        if volume is not None and volume.value is not None and status.get("swap_write_bytes") != volume.value:
            boot["issues"].append("W4 runner/raw pressure_begin-to-read_begin write-volume mismatch")
        for field, name in (("readback_swap_write_sectors", "readback_write_sectors"),
                            ("readback_swap_write_bytes", "readback_write_bytes")):
            metric = boot["metrics"].get(name)
            if field in status and metric is not None and metric.value is not None:
                if type(status[field]) is not int or status[field] != metric.value:
                    boot["issues"].append("W4 runner/raw read_begin-to-read_end mismatch: " + field)
        if not had_issues and boot["issues"]:
            boot["extraction_failed"] = True


def read_boots(root):
    for boot in stats.load_boots(root):
        try:
            extract_boot(boot)
        except (OSError, EOFError, UnicodeError, ValueError, TypeError, IndexError, KeyError, csv.Error, zlib.error) as error:
            reason = f"telemetry incomplete/invalid: {type(error).__name__}: {error}"
            boot["issues"].append(reason)
            boot["extraction_failed"] = True
            boot["metrics"]["run.raw_complete"] = stats.Metric(0, "boolean", reason=reason, source="run1.log.gz")
        yield boot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="new directory outside the input matrix")
    parser.add_argument("matrix", type=Path, help="one v4.6 matrix root (smoke is descriptive only)")
    args = parser.parse_args()
    root, output = args.matrix.resolve(), args.output.resolve()
    if root == output or root in output.parents:
        parser.error("output must be outside the input matrix")
    try:
        output.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        parser.error(str(error))
    boots = stats.outputs(output, read_boots(root))
    # A known failed/missing guest is a reportable identity, not a crash.
    # Unexpected corrupt/incomplete raw input from a valid boot remains fatal.
    if any(boot.get("extraction_failed") for boot in boots):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
