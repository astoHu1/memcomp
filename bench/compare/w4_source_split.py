#!/usr/bin/env python3
"""Derive the v4.133 W4 source split from the formal 965 inputs.

This reader is separate from E.4/E.5. It consumes exactly five formal W4
rounds and three named backends, streams each gzip CSV, and keeps backend
native writeback counters descriptive because their units differ.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable

BACKENDS = ("memcomp-swap-zstd", "zswap-zstd", "zram-writeback-zstd")
BACKEND_KIND = {
    "memcomp-swap-zstd": "memcomp-swap",
    "zswap-zstd": "zswap",
    "zram-writeback-zstd": "zram-writeback",
}
# Path recorded by the frozen 965 tools.
FROZEN_965_SYSFS = "/sys/kernel/mm/memcompress"
NATIVE = {
    "memcomp-swap": {
        "path": FROZEN_965_SYSFS + "/packed_swap_events",
        "paths": (
            FROZEN_965_SYSFS + "/packed_swap_events",
            "/sys/kernel/mm/memcomp/packed_swap_events",
        ),
        "event_paths": (
            FROZEN_965_SYSFS + "/events",
            "/sys/kernel/mm/memcomp/events",
        ),
        "field": "writeback_payloads",
        "unit": "payload_conversions",
        "note": "memcomp payload conversions; not physical bytes or guest pages",
    },
    "zswap": {
        "path": "/proc/vmstat",
        "field": "zswpwb",
        "unit": "page_events",
        "note": "zswap writeback page events; not physical bytes or unique pages",
    },
    "zram-writeback": {
        "path": "/sys/block/zram0/bd_stat",
        "field": "bd_writes",
        "unit": "units_4KiB",
        "note": "zram bd_writes in fixed 4 KiB units; not requests or guest pages",
    },
}
WORKINGSET_FIELDS = (
    "workingset_nodes", "workingset_refault_anon",
    "workingset_refault_file", "workingset_activate_anon",
    "workingset_activate_file", "workingset_restore_anon",
    "workingset_restore_file", "workingset_nodereclaim",
)
CSV_FIELDS = [
    "round", "backend", "workload_id", "csv_rows", "expected_pages",
    "writeback_source", "writeback_proven_values", "verified_rows",
    "snapshot_memcomp", "snapshot_swapped_nonresident",
    "snapshot_cached_nonpresent", "snapshot_present", "snapshot_other",
    "classification_memcomp", "classification_swapped_nonresident",
    "classification_cached_nonpresent", "classification_present",
    "classification_other", "classification_real_unclassified",
    "cached_nonpresent_readahead_hits", "touch_p50_ns", "touch_p99_ns",
    "touch_snapshot_memcomp_pages", "touch_snapshot_memcomp_p50_ns",
    "touch_snapshot_memcomp_p99_ns",
    "touch_snapshot_swapped_nonresident_pages",
    "touch_snapshot_swapped_nonresident_p50_ns",
    "touch_snapshot_swapped_nonresident_p99_ns",
    "touch_snapshot_cached_nonpresent_pages",
    "touch_snapshot_cached_nonpresent_p50_ns",
    "touch_snapshot_cached_nonpresent_p99_ns",
    "vda_read_requests_pressure_to_read", "vda_read_sectors_pressure_to_read",
    "vda_write_requests_pressure_to_read", "vda_write_sectors_pressure_to_read",
    "vda_write_bytes_pressure_to_read", "vda_read_requests_readback",
    "vda_read_sectors_readback", "vda_write_requests_readback",
    "vda_write_sectors_readback", "vda_write_bytes_readback",
    "native_counter_name", "native_counter_unit",
    "native_counter_pressure_begin", "native_counter_read_begin",
    "native_counter_read_end", "native_delta_pressure_to_read",
    "native_delta_readback", "writeback_accounting_note",
    "memcomp_swapped_read_io_readback",
    "memcomp_swapped_read_cached_readback",
    "memcomp_retrieval_events_readback",
    "memcomp_cache_hit_pct_readback",
    "memcomp_retrieval_events_per_vda_read_request",
    "memcomp_repeated_read_lower_bound",
    "workingset_nodes_readback_delta",
    "workingset_refault_anon_readback_delta",
    "workingset_refault_file_readback_delta",
    "workingset_activate_anon_readback_delta",
    "workingset_activate_file_readback_delta",
    "workingset_restore_anon_readback_delta",
    "workingset_restore_file_readback_delta",
    "workingset_nodereclaim_readback_delta",
    "readback_latency_attribution_note",
    "csv_file", "csv_sha256", "log_file", "log_sha256",
    "status_file", "status_sha256", "metadata_file", "metadata_sha256",
]
HEADER_RE = re.compile(r"^(?:COMPARE_RAW_BEGIN|COMPARE_RAW_END) (.*)$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def attrs(text: str) -> dict[str, str]:
    result = {}
    for item in text.split():
        key, separator, value = item.partition("=")
        if separator:
            result[key] = value
    return result


def parse_log(path: Path) -> dict[str, dict[str, list[str]]]:
    snapshots = {}
    declared_snapshots = set()
    current = None
    with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
        for raw in stream:
            line = raw.rstrip("\n")
            if line.startswith("COMPARE_SNAPSHOT "):
                phase = attrs(line.split(" ", 1)[1]).get("phase")
                if not phase or phase in declared_snapshots:
                    raise ValueError("duplicate or malformed snapshot phase")
                snapshots.setdefault(phase, {})
                declared_snapshots.add(phase)
                current = None
                continue
            match = HEADER_RE.match(line)
            if match:
                header = line.split(" ", 1)[0]
                values = attrs(match.group(1))
                phase, source = values.get("phase"), values.get("path")
                if not phase or not source:
                    raise ValueError("raw block has no phase or path")
                snapshots.setdefault(phase, {})
                if header == "COMPARE_RAW_BEGIN":
                    if current is not None:
                        raise ValueError("nested raw block")
                    current = (phase, source, [])
                else:
                    if current is None or current[0] != phase or current[1] != source:
                        raise ValueError("raw block end does not match begin")
                    if source in snapshots[phase]:
                        raise ValueError("duplicate raw path")
                    snapshots[phase][source] = current[2]
                    current = None
                continue
            if current is not None:
                current[2].append(line)
    if current is not None:
        raise ValueError("unterminated raw block")
    return snapshots


def integer(text: str, label: str) -> int:
    try:
        return int(text)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid integer for {label}: {text!r}") from error


def diskstats(lines: Iterable[str]) -> dict[str, int]:
    rows = []
    for line in lines:
        fields = line.split()
        if len(fields) >= 10 and fields[2] == "vda":
            rows.append(fields)
    if len(rows) != 1:
        raise ValueError("expected exactly one vda diskstats row")
    fields = rows[0]
    return {
        "read_requests": integer(fields[3], "vda read requests"),
        "read_sectors": integer(fields[5], "vda read sectors"),
        "write_requests": integer(fields[7], "vda write requests"),
        "write_sectors": integer(fields[9], "vda write sectors"),
    }


def native_counter(lines: Iterable[str], kind: str) -> int:
    spec = NATIVE[kind]
    if kind == "zram-writeback":
        fields = " ".join(lines).split()
        if len(fields) < 3:
            raise ValueError("zram bd_stat has no bd_writes field")
        return integer(fields[2], "zram bd_writes")
    matches = []
    for line in lines:
        fields = line.split()
        if len(fields) == 2 and fields[0] == spec["field"]:
            matches.append(fields[1])
    if len(matches) != 1:
        raise ValueError(f"expected one {spec['field']} counter")
    return integer(matches[0], spec["field"])


def percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5)))
    return ordered[index]


def counter_map(lines: Iterable[str]) -> dict[str, int]:
    result = {}
    for line in lines:
        fields = line.split()
        if len(fields) == 2:
            try:
                result[fields[0]] = integer(fields[1], fields[0])
            except ValueError:
                continue
    return result


def find_path(blocks: dict[str, list[str]], paths: Iterable[str]) -> str | None:
    for path in paths:
        if path in blocks:
            return path
    return None


def cohort_latency(touches: dict[str, list[int]], name: str) -> tuple[int, int | None, int | None]:
    values = touches.get(name, [])
    return len(values), percentile(values, 0.50), percentile(values, 0.99)


def text_number(value: object) -> str:
    return "" if value is None else str(value)


def parse_csv(path: Path) -> dict[str, object]:
    snapshots = Counter()
    classifications = Counter()
    writeback_sources = Counter()
    proven = Counter()
    touches = []
    touches_by_snapshot = {}
    cached_hits = 0
    real_unclassified = 0
    rows = 0
    verified = 0
    with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError("W4 CSV has no header")
        required = {
            "snapshot_classification", "classification", "readahead_hit",
            "touch_ns", "verify_ok", "writeback_source", "writeback_proven",
            "page_kind",
        }
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValueError(f"W4 CSV missing fields: {sorted(missing)}")
        for row in reader:
            rows += 1
            snapshots[row["snapshot_classification"]] += 1
            classifications[row["classification"]] += 1
            writeback_sources[row["writeback_source"]] += 1
            proven[row["writeback_proven"]] += 1
            verified += row["verify_ok"] == "1"
            cached_hits += row["classification"] == "cached_nonpresent" and row["readahead_hit"] == "1"
            real_unclassified += row["page_kind"] == "real-unclassified"
            if row["touch_ns"]:
                value = integer(row["touch_ns"], "touch_ns")
                if value < 0:
                    raise ValueError("negative touch_ns")
                touches.append(value)
                touches_by_snapshot.setdefault(
                    row["snapshot_classification"], []).append(value)
    return {
        "rows": rows,
        "verified": verified,
        "snapshots": snapshots,
        "classifications": classifications,
        "writeback_sources": writeback_sources,
        "proven": proven,
        "cached_hits": cached_hits,
        "touch_p50_ns": percentile(touches, 0.50),
        "touch_p99_ns": percentile(touches, 0.99),
        "touches_by_snapshot": touches_by_snapshot,
        "real_unclassified": real_unclassified,
    }


def phase_counters(log_path: Path, kind: str) -> dict[str, object]:
    snapshots = parse_log(log_path)
    required = {"pressure_begin", "read_begin", "read_end"}
    if not required <= snapshots.keys():
        raise ValueError(f"missing W4 phases: {sorted(required - snapshots.keys())}")
    spec = NATIVE[kind]
    native_paths = spec.get("paths", (spec["path"],))
    event_paths = spec.get("event_paths", ())
    values, disks, vmstats, events = {}, {}, {}, {}
    for phase in ("pressure_begin", "read_begin", "read_end"):
        blocks = snapshots[phase]
        if "/proc/diskstats" not in blocks:
            raise ValueError(f"{phase} has no diskstats")
        native_path = find_path(blocks, native_paths)
        if native_path is None:
            raise ValueError(f"{phase} has no native counter path {native_paths}")
        disks[phase] = diskstats(blocks["/proc/diskstats"])
        values[phase] = native_counter(blocks[native_path], kind)
        if "/proc/vmstat" not in blocks:
            raise ValueError(f"{phase} has no /proc/vmstat")
        vmstats[phase] = counter_map(blocks["/proc/vmstat"])
        event_path = find_path(blocks, event_paths)
        events[phase] = (counter_map(blocks[event_path])
                         if event_path is not None else {})
    return {
        "pressure_native": values["pressure_begin"],
        "read_native": values["read_begin"],
        "end_native": values["read_end"],
        "pressure_disk": disks["pressure_begin"],
        "read_disk": disks["read_begin"],
        "end_disk": disks["read_end"],
        "vmstat_pressure": vmstats["pressure_begin"],
        "vmstat_read": vmstats["read_begin"],
        "vmstat_end": vmstats["read_end"],
        "memcomp_events_pressure": events["pressure_begin"],
        "memcomp_events_read": events["read_begin"],
        "memcomp_events_end": events["read_end"],
    }


def expected_inputs(root: Path):
    for number in range(1, 6):
        for backend in BACKENDS:
            yield number, backend, root / f"round-{number:02d}" / "w4-832m" / backend


def input_identity(directory: Path) -> dict[str, object]:
    files = {}
    for name in ("run1.csv.gz", "run1.log.gz", "status.json", "metadata.json"):
        path = directory / name
        if not path.is_file():
            raise ValueError(f"missing formal W4 input: {path}")
        files[name] = {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    return files


def aggregate(root: Path) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
    root = root.resolve()
    rows, inputs = [], []
    for round_number, backend_name, directory in expected_inputs(root):
        identity = input_identity(directory)
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        kind = BACKEND_KIND[backend_name]
        csv_data = parse_csv(directory / "run1.csv.gz")
        phases = phase_counters(directory / "run1.log.gz", kind)
        source_counter = csv_data["writeback_sources"]
        expected_pages = integer(
            metadata.get("workload_spec", {}).get("pages", ""),
            "metadata workload pages",
        )
        if csv_data["rows"] != expected_pages:
            raise ValueError(
                f"{directory}: CSV rows {csv_data['rows']} != "
                f"metadata pages {expected_pages}"
            )
        if csv_data["verified"] != csv_data["rows"]:
            raise ValueError(f"{directory}: not all W4 rows verified")
        if set(csv_data["proven"]) != {"1"}:
            raise ValueError(f"{directory}: writeback proof is not uniformly 1")
        if len(source_counter) != 1:
            raise ValueError(
                f"{directory}: mixed writeback sources: "
                f"{sorted(source_counter)}"
            )
        source = next(iter(source_counter))
        pressure_disk = phases["pressure_disk"]
        read_disk = phases["read_disk"]
        end_disk = phases["end_disk"]
        disk_write = read_disk["write_sectors"] - pressure_disk["write_sectors"]
        disk_readback_write = end_disk["write_sectors"] - read_disk["write_sectors"]
        native_pressure_to_read = phases["read_native"] - phases["pressure_native"]
        native_readback = phases["end_native"] - phases["read_native"]
        read_disk_requests = end_disk["read_requests"] - read_disk["read_requests"]
        snap = csv_data["snapshots"]
        cls = csv_data["classifications"]
        event_read = phases["memcomp_events_read"]
        event_end = phases["memcomp_events_end"]
        read_io = (
            event_end["swapped_read_io"] - event_read["swapped_read_io"]
            if "swapped_read_io" in event_end and
            "swapped_read_io" in event_read else None
        )
        read_cached = (
            event_end["swapped_read_cached"] - event_read["swapped_read_cached"]
            if "swapped_read_cached" in event_end and
            "swapped_read_cached" in event_read else None
        )
        retrieval_total = (
            read_io + read_cached
            if read_io is not None and read_cached is not None else None
        )
        cache_pct = (
            100.0 * read_cached / retrieval_total
            if retrieval_total else None
        )
        per_disk_read = (
            retrieval_total / read_disk_requests
            if retrieval_total is not None and read_disk_requests else None
        )
        repeated_lower_bound = (
            max(0, retrieval_total - snap.get("memcomp", 0))
            if retrieval_total is not None else None
        )
        workingset_delta = {
            name: phases["vmstat_end"].get(name, 0) -
            phases["vmstat_read"].get(name, 0)
            for name in WORKINGSET_FIELDS
        }
        memcomp_pages, memcomp_p50, memcomp_p99 = cohort_latency(
            csv_data["touches_by_snapshot"], "memcomp")
        nonresident_pages, nonresident_p50, nonresident_p99 = cohort_latency(
            csv_data["touches_by_snapshot"], "swapped_nonresident")
        cached_pages, cached_p50, cached_p99 = cohort_latency(
            csv_data["touches_by_snapshot"], "cached_nonpresent")
        record = {
            "round": str(round_number),
            "backend": kind,
            "workload_id": str(metadata.get("workload_id", "")),
            "csv_rows": str(csv_data["rows"]),
            "expected_pages": str(expected_pages),
            "writeback_source": source,
            "writeback_proven_values": ",".join(sorted(csv_data["proven"])),
            "verified_rows": str(csv_data["verified"]),
            "snapshot_memcomp": str(snap.get("memcomp", 0)),
            "snapshot_swapped_nonresident": str(snap.get("swapped_nonresident", 0)),
            "snapshot_cached_nonpresent": str(snap.get("cached_nonpresent", 0)),
            "snapshot_present": str(snap.get("present", 0)),
            "snapshot_other": str(sum(v for k, v in snap.items() if k not in {"memcomp", "swapped_nonresident", "cached_nonpresent", "present"})),
            "classification_memcomp": str(cls.get("memcomp", 0)),
            "classification_swapped_nonresident": str(cls.get("swapped_nonresident", 0)),
            "classification_cached_nonpresent": str(cls.get("cached_nonpresent", 0)),
            "classification_present": str(cls.get("present", 0)),
            "classification_other": str(sum(v for k, v in cls.items() if k not in {"memcomp", "swapped_nonresident", "cached_nonpresent", "present"})),
            "classification_real_unclassified": str(csv_data["real_unclassified"]),
            "cached_nonpresent_readahead_hits": str(csv_data["cached_hits"]),
            "touch_p50_ns": text_number(csv_data["touch_p50_ns"]),
            "touch_p99_ns": text_number(csv_data["touch_p99_ns"]),
            "touch_snapshot_memcomp_pages": str(memcomp_pages),
            "touch_snapshot_memcomp_p50_ns": text_number(memcomp_p50),
            "touch_snapshot_memcomp_p99_ns": text_number(memcomp_p99),
            "touch_snapshot_swapped_nonresident_pages": str(nonresident_pages),
            "touch_snapshot_swapped_nonresident_p50_ns": text_number(nonresident_p50),
            "touch_snapshot_swapped_nonresident_p99_ns": text_number(nonresident_p99),
            "touch_snapshot_cached_nonpresent_pages": str(cached_pages),
            "touch_snapshot_cached_nonpresent_p50_ns": text_number(cached_p50),
            "touch_snapshot_cached_nonpresent_p99_ns": text_number(cached_p99),
            "vda_read_requests_pressure_to_read": str(read_disk["read_requests"] - pressure_disk["read_requests"]),
            "vda_read_sectors_pressure_to_read": str(read_disk["read_sectors"] - pressure_disk["read_sectors"]),
            "vda_write_requests_pressure_to_read": str(read_disk["write_requests"] - pressure_disk["write_requests"]),
            "vda_write_sectors_pressure_to_read": str(disk_write),
            "vda_write_bytes_pressure_to_read": str(disk_write * 512),
            "vda_read_requests_readback": str(end_disk["read_requests"] - read_disk["read_requests"]),
            "vda_read_sectors_readback": str(end_disk["read_sectors"] - read_disk["read_sectors"]),
            "vda_write_requests_readback": str(end_disk["write_requests"] - read_disk["write_requests"]),
            "vda_write_sectors_readback": str(disk_readback_write),
            "vda_write_bytes_readback": str(disk_readback_write * 512),
            "native_counter_name": NATIVE[kind]["field"],
            "native_counter_unit": NATIVE[kind]["unit"],
            "native_counter_pressure_begin": str(phases["pressure_native"]),
            "native_counter_read_begin": str(phases["read_native"]),
            "native_counter_read_end": str(phases["end_native"]),
            "native_delta_pressure_to_read": str(native_pressure_to_read),
            "native_delta_readback": str(native_readback),
            "writeback_accounting_note": NATIVE[kind]["note"],
            "memcomp_swapped_read_io_readback": text_number(read_io),
            "memcomp_swapped_read_cached_readback": text_number(read_cached),
            "memcomp_retrieval_events_readback": text_number(retrieval_total),
            "memcomp_cache_hit_pct_readback": text_number(
                round(cache_pct, 3) if cache_pct is not None else None),
            "memcomp_retrieval_events_per_vda_read_request": text_number(
                round(per_disk_read, 3) if per_disk_read is not None else None),
            "memcomp_repeated_read_lower_bound": text_number(repeated_lower_bound),
            **{f"{name}_readback_delta": str(value)
               for name, value in workingset_delta.items()},
            "readback_latency_attribution_note": (
                "overall and snapshot-cohort touch latency only; physical pool, "
                "disk, and same-slot attribution is unavailable"
            ),
            "csv_file": identity["run1.csv.gz"]["path"],
            "csv_sha256": identity["run1.csv.gz"]["sha256"],
            "log_file": identity["run1.log.gz"]["path"],
            "log_sha256": identity["run1.log.gz"]["sha256"],
            "status_file": identity["status.json"]["path"],
            "status_sha256": identity["status.json"]["sha256"],
            "metadata_file": identity["metadata.json"]["path"],
            "metadata_sha256": identity["metadata.json"]["sha256"],
        }
        rows.append(record)
        inputs.append({
            "round": round_number,
            "backend": kind,
            "directory": str(directory),
            "files": identity,
        })
    return rows, inputs


def write_outputs(root: Path, output_csv: Path, output_json: Path, output_md: Path) -> None:
    rows, inputs = aggregate(root)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    payload = {
        "schema": "v4.133-w4-source-split-v2",
        "formal_root": str(root.resolve()),
        "rounds": 5,
        "backends": [BACKEND_KIND[name] for name in BACKENDS],
        "rows": rows,
        "inputs": inputs,
        "method": {
            "csv": "streamed gzip CSV; counts are disjoint only where explicitly named",
            "snapshot_memcomp": (
                "snapshot_classification=memcomp means a memcomp PTE in the "
                "snapshot; it may have been written back and retrieved from "
                "disk or cache"
            ),
            "snapshot_nonresident_source": "snapshot_classification=swapped_nonresident",
            "readback_cached_source": "classification=cached_nonpresent and readahead_hit=1",
            "disk_window": "vda diskstats pressure_begin->read_begin and read_begin->read_end",
            "native_counter_rule": "backend-native counter deltas are descriptive and never cross-backend ratios",
            "readback_attribution": (
                "swapped_read_io and swapped_read_cached are retrieval events; "
                "disk requests are independent aggregate counters and cannot be "
                "assigned to individual pages"
            ),
            "script_sha256": sha256_file(Path(__file__).resolve()),
        },
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    with output_json.open("w", encoding="utf-8", newline="") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    with output_md.open("w", encoding="utf-8", newline="") as stream:
        stream.write("# W4 source split (formal 965)\n\n")
        stream.write("Descriptive derivation from five formal W4 rounds. It does not alter E.4 or E.5.\n\n")
        stream.write("Native counters retain backend units: memcomp payload conversions, zswap page events, and zram fixed 4 KiB bd_writes. The memcomp snapshot cohort is not a physical pool-source count.\n\n")
        stream.write("| round | backend | snapshot memcomp | snapshot nonresident | read disk requests | memcomp read io | memcomp cached | cache hit % | repeated read lower bound | touch p50/p99 ns | workingset readback deltas |\n")
        stream.write("|---:|---|---:|---:|---:|---:|---:|---:|---:|---|---|\n")
        for row in rows:
            working = "; ".join(
                f"{name}={row[f'{name}_readback_delta']}"
                for name in WORKINGSET_FIELDS
            )
            stream.write(
                "| {round} | {backend} | {snapshot_memcomp} | "
                "{snapshot_swapped_nonresident} | {vda_read_requests_readback} | "
                "{memcomp_swapped_read_io_readback} | "
                "{memcomp_swapped_read_cached_readback} | "
                "{memcomp_cache_hit_pct_readback} | "
                "{memcomp_repeated_read_lower_bound} | {touch_p50_ns}/{touch_p99_ns} | "
                "{working} |\n".format(working=working, **row)
            )
        stream.write("\n## Limits\n\n")
        stream.write("cached_nonpresent with readahead_hit=1 is a cache-hit subset, not a disjoint disk-read population. swapped_read_io and swapped_read_cached are successful retrieval events; they do not identify physical source per page. Readback disk requests and retrieval-events-per-request are aggregate measurements, and the repeated-read value is only a lower bound against the snapshot memcomp cohort. The readback window may include continued writeback; its bytes are reported separately and are not divided by the pressure-to-read volume.\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--csv", dest="output_csv", type=Path, required=True)
    parser.add_argument("--json", dest="output_json", type=Path, required=True)
    parser.add_argument("--md", dest="output_md", type=Path, required=True)
    args = parser.parse_args()
    write_outputs(args.root, args.output_csv, args.output_json, args.output_md)
    print(f"wrote {args.output_csv}, {args.output_json}, {args.output_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
