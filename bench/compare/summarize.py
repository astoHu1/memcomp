#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Stage 4 v4.6: per-guest medians and interleaved, same-host-round comparisons."""
import argparse
from collections import Counter, defaultdict
import csv
from contextlib import ExitStack
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import gzip
import json
import os
from pathlib import Path
import re
import zlib

from prime import PRIME_CONTRACT, PRIME_FIELDS, PRIME_SOURCE, prime_counts
from pageout import PAGEOUT_CONTRACT, first_pass_counts

D = Decimal
SOURCES = ("system_server", "systemui", "qq", "wechat", "chrome",
           "netease_music", "discord", "telegram")
W1_COMBINED = "w1-corpus-geomean"
METHOD = "v4.6"
EXCLUDED = {"2026-10-16-compare-v43", "2026-10-16-compare-v43-r2", "2026-10-16-compare-v44", "2026-10-16-compare-v45"}
CLASSES = ("memcomp", "swapped_nonresident", "cached_nonpresent", "present", "other_nonresident", "unknown")
ELIGIBLE = {"memcomp", "swapped_nonresident"}
IDENTITY = ["result_dir", "method", "kind", "workload_id", "group_id", "host_boot_round"]
W4_READBACK_SCOPE = (
    "W4 latency covers every verified touch in the full segment after stable writeback; "
    "not a per-page written-back subset; timed-out guests retain write volume only")
W3_SCOPE = (
    "Anonymous W3 refaults use C.3 frozen pagemap/swapcache eligibility and "
    "include read-ahead hits; nonpresent_first_touch is descriptive only")


W1_PHYSICAL_FREE_DELTA = (
    "fill_to_drained_delta:seq_fill_begin->seq_read_begin:"
    "memory.physical_free_MemFree_plus_PCP_kib")
W1_PHYSICAL_CONSUMED = "seq.physical_free_consumed_kib"
W1_PAGEOUT_FIELDS = ("pageout_attempt_ns", "pageout_ns",
                     "first_pass_nonpresent_fraction", "physical_free_consumed_kib")
W1_PAGEOUT_NOTE = (
    "Both timings use raw timed PAGEOUT total, before drain/retry; "
    "noswap vs zram: attempt time is primary; memcomp-swap vs zswap: "
    "first-nonpresent time is primary. zswap writes incompressible pages in "
    "the timed call, memcomp-swap may write them in a later untimed retry. "
    "Random noswap may reclaim no pages: undefined per-reclaimed time is not zero; "
    "zram raw-page storage is not memory saving. Memory consumption is "
    "start-minus-end MemFree+PCP from the shared seq fill-to-drained window, "
    "not a separate rand/rewrite measurement; both timings get their own E.4 test.")


@dataclass
class Metric:
    value: int | Decimal | None
    unit: str = "count"
    direction: str = ""  # Empty means descriptive only; never ranked.
    reason: str = ""
    source: str = ""
    provenance: str = ""
    note: str = ""


def quantile(values, probability):
    if not values:
        return None
    values = sorted(values)
    position = D(len(values) - 1) * D(str(probability))
    lo = int(position)
    hi = min(lo + 1, len(values) - 1)
    return D(values[lo]) + (D(values[hi]) - D(values[lo])) * (position - lo)


def distribution(values):
    median = quantile(values, '.5')
    q1, q3 = quantile(values, '.25'), quantile(values, '.75')
    return dict(median=median, q1=q1, q3=q3, iqr=q3 - q1 if values else None,
                spread=(D(max(values)) - D(min(values))) / median if median is not None and median > 0 else None)


def positive_int(value):
    return type(value) is int and value > 0


def json_file(path, expected):
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(result, expected):
            raise ValueError("unexpected root type")
        return result, ""
    except (OSError, ValueError, UnicodeError) as error:
        return expected(), f"{path.name}: {type(error).__name__}: {error}"


def excluded(path):
    return bool(EXCLUDED.intersection(path.parts))


def boot_count(label):
    return 10 if label in ("w1", "w2") else 5


def discover(root):
    """One invocation is one matrix root; retain status/log-only failed boots too."""
    found = set()
    if excluded(root):
        return [root]
    for directory, children, files in os.walk(root):
        children[:] = [name for name in children if name not in EXCLUDED]
        if any(name in files for name in ("metadata.json", "status.json", "run1.log.gz", "run1.csv.gz")):
            found.add(Path(directory))
    return sorted(found) or [root]


def load_boot(directory, expected=None):
    meta, error = json_file(directory / "metadata.json", dict)
    if expected:
        for name in ("method", "kind", "workload_id", "group_id", "host_boot_round"):
            if name in meta and meta[name] != expected.get(name):
                error = "; ".join(filter(None, (error, "matrix/metadata identity mismatch: " + name)))
        meta = {**expected, **meta}
    statuses, status_error = json_file(directory / "status.json", list)
    issues = [s for s in (error, status_error) if s]
    label = str(meta.get("label") or str(meta.get("workload_id", "")).split("-", 1)[0])
    host_round = meta.get("host_boot_round", meta.get("round"))
    if "host_boot_round" in meta and "round" in meta and meta["host_boot_round"] != meta["round"]:
        issues.append("conflicting host_boot_round/round metadata")
    if label not in ("w1", "w2", "w3", "w4"):
        issues.append("workload label missing/unknown")
    blocked = excluded(directory) or meta.get("method") != METHOD or bool(expected and expected.get("method") != METHOD)
    formal = not blocked and meta.get("method") == METHOD and meta.get("kind") == "interleaved-comparison"
    if formal and (not positive_int(host_round) or host_round > boot_count(label)):
        issues.append("host boot round missing/out of range")
    if formal and any(not isinstance(meta.get(k), str) or not meta[k] for k in ("group_id", "workload_id")):
        issues.append("group_id/workload_id missing")
    status = statuses[0] if len(statuses) == 1 and isinstance(statuses[0], dict) else {}
    if not (status.get("repeat") == 1 and status.get("valid") is True
            and status.get("qemu_helper_exit") == 0 and status.get("kernel_or_data_failure") is False):
        issues.append("one valid repeat=1 status required")
    if formal and not (status.get("csv_integrity") is True and status.get("guest_status") == [0]
                       and status.get("eligible_for_comparison") is True and status.get("lifecycle_safe_to_continue") is True):
        issues.append("formal integrity/lifecycle/eligibility status incomplete")
    if blocked:
        issues.append("blocked/excluded pre-v4.6, unknown or quarantined method; CSV/raw log not read")
    identity = dict(result_dir=str(directory), method=meta.get("method", ""), kind=meta.get("kind", ""),
                    workload_id=meta.get("workload_id", "") if isinstance(meta.get("workload_id", ""), str) else "",
                    group_id=meta.get("group_id", "") if isinstance(meta.get("group_id", ""), str) else "",
                    host_boot_round=host_round if positive_int(host_round) else None)
    return dict(identity=identity, meta=meta, status=statuses, label=label, formal=formal, blocked=blocked,
                issues=issues, rounds=[], metrics={})


def load_boots(root):
    expected = {}
    manifest_path = root / "matrix.json"
    manifest_error = ""
    if manifest_path.is_file():
        manifest, manifest_error = json_file(manifest_path, dict)
        jobs = manifest.get("jobs", [])
        if not isinstance(jobs, list):
            jobs, manifest_error = [], "matrix jobs must be an array"
        for job in jobs:
            if not isinstance(job, dict) or not isinstance(job.get("dest"), str):
                manifest_error = "matrix job missing destination identity"
                continue
            directory = Path(job["dest"]).resolve()
            if root != directory and root not in directory.parents:
                manifest_error = "matrix destination escapes input root"
                continue
            identity = {key: job.get(key) for key in ("workload_id", "group_id", "host_boot_round")}
            identity.update(method=manifest.get("method"), kind=manifest.get("kind"))
            if directory in expected:
                manifest_error = "duplicate assigned matrix destination"
            expected[directory] = identity
    found = set(discover(root))
    if expected and found == {root} and not any((root / name).exists() for name in ("metadata.json", "status.json", "run1.log.gz", "run1.csv.gz")):
        found.clear()
    for directory in sorted(found | expected.keys()):
        boot = load_boot(directory, expected.get(directory))
        if manifest_error:
            boot["issues"].append(manifest_error)
        yield boot


def option(boot, name, default):
    argv = boot["meta"].get("workload_argv", [])
    if not isinstance(argv, list):
        raise ValueError("workload_argv is not a list")
    if name not in argv:
        return default
    try:
        return int(argv[argv.index(name) + 1])
    except (IndexError, TypeError, ValueError) as error:
        raise ValueError("missing/invalid CLI option " + name) from error


def integer(row, name, minimum=0):
    text = row.get(name, "")
    if not re.fullmatch(r"-?\d+", text or "") or int(text) < minimum:
        raise ValueError("missing/invalid " + name)
    return int(text)


TOUCH_FIELDS = ("snapshot_swap_type", "snapshot_classification", "refault_eligible",
                "refault_counted", "readahead_hit")
DRAIN_FIELDS = ("drain_rounds", "drain_remaining_swapcache_pages", "drain_remapped_pages",
                "refault_eligible_pages", "refault_counted_pages", "readahead_hit_pages",
                "readahead_hit_ratio")
WRITEBACK_FIELDS = ("writeback_source", "writeback_before", "writeback_after", "writeback_delta",
                    "writeback_wait_ns", "writeback_wait_timed_out", "writeback_stable_polls",
                    "pressure_bytes", "pressure_ready", "writeback_explicit_idle", "writeback_proven")
# Different accounting points/units: all are descriptive, never cross-backend ratios.
WRITEBACK = {
    "memcomp-swap": ("memcomp_writeback_payloads", "payload_conversions",
                     "memcomp.packed_swap_events.writeback_payloads"),
    "zswap": ("zswpwb", "page_events", "vmstat.zswpwb"),
    "zram-writeback": ("zram_bd_writes", "units_4KiB", "zram.bd_writes"),
}


def bucket():
    return dict(count=0, classes=Counter(), snapshots=Counter(), eligible=0, counted=0,
                readahead=0, eligible_ns=0, srf=[], spo=[], nonpresent=0, snapshot_nonpresent=0,
                pages=set(), batch=None, warmup=None, swap_types=Counter())


def csv_rows(boot):
    # Defense in depth: even a direct caller must not open old CSV payloads.
    if boot["blocked"] or boot["meta"].get("method") != METHOD:
        raise ValueError("blocked method; CSV not opened")
    path = Path(boot["identity"]["result_dir"]) / "run1.csv.gz"
    with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("missing/duplicate CSV header")
        if boot["label"] != "w2":
            required = set(TOUCH_FIELDS + DRAIN_FIELDS + ("method",))
            if boot["label"] == "w1":
                required.update(PRIME_FIELDS)
            if boot["label"] in ("w3", "w4"):
                required.update(WRITEBACK_FIELDS + ("purge_ns", "drain_timed_out"))
            if not required <= set(reader.fieldnames):
                raise ValueError("missing v4.6 frozen-snapshot/drain/writeback fields")
        for row in reader:
            if None in row or None in row.values():
                raise ValueError("incomplete/extra CSV field")
            yield row


def observation(row, boot, snapshot=False):
    kind = row.get("snapshot_classification" if snapshot else "classification")
    if kind not in CLASSES:
        raise ValueError("unknown observation classification")
    names = (("snapshot_present", "snapshot_swapped", "snapshot_resident", "snapshot_swap_type")
             if snapshot else ("present_before", "swapped_before", "resident_before", "swap_type_before"))
    present, swapped, resident, swap_type = (integer(row, key, -1) for key in names)
    if any(value not in (-1, 0, 1) for value in (present, swapped, resident)) or swap_type > 31:
        raise ValueError("observation flag/type outside pagemap range")
    spec = boot["meta"].get("workload_spec", {})
    if not isinstance(spec, dict):
        raise ValueError("workload_spec is not an object")
    types = spec.get("active_swap_types")
    verified = isinstance(types, list) and all(type(t) is int and 0 <= t <= 31 for t in types)
    if kind == "memcomp":
        if (present, swapped, resident) != (0, 1, -1) or swap_type < 0:
            raise ValueError("memcomp PTE must be nonpresent/swapped; mincore must be unused")
        if not str(boot["meta"].get("backend", "")).startswith("memcomp-") or (verified and swap_type in types):
            raise ValueError("memcomp identity conflicts with backend/real swap whitelist")
    elif kind in ("swapped_nonresident", "cached_nonpresent"):
        expected = 0 if kind == "swapped_nonresident" else 1
        if (present, swapped, resident) != (0, 1, expected) or not verified or swap_type not in types:
            raise ValueError("real swap classification lacks verified type/cache observation")
    elif kind == "present" and present != 1:
        raise ValueError("present classification contradicts PTE")
    elif kind == "other_nonresident" and (present, swapped, resident) != (0, 0, 0):
        raise ValueError("other-nonresident classification contradicts observation")
    # Unknown special entries remain unknown; never infer a type from mincore.
    return kind


def touch_population(row, boot, population):
    snapshot, before = observation(row, boot, True), observation(row, boot)
    eligible = int(snapshot in ELIGIBLE)
    counted = int(eligible and before in (ELIGIBLE | {"cached_nonpresent"}))
    hit = int(counted and snapshot == "swapped_nonresident" and before == "cached_nonpresent")
    for name, expected in (("refault_eligible", eligible), ("refault_counted", counted), ("readahead_hit", hit)):
        if integer(row, name) != expected:
            raise ValueError(name + " contradicts frozen snapshot/PTE observation")
    population["classes"][before] += 1
    population["snapshots"][snapshot] += 1
    population["snapshot_nonpresent"] += row.get("snapshot_present") == "0"
    population["eligible"] += eligible
    population["counted"] += counted
    population["readahead"] += hit
    return bool(counted)


def population_metrics(population):
    metrics = {kind + "_count": Metric(population["classes"][kind]) for kind in CLASSES}
    for kind in CLASSES:
        name = "phase_start_cached_count" if kind == "cached_nonpresent" else "phase_start_" + kind + "_count"
        metrics[name] = Metric(population["snapshots"][kind])
    metrics.update(eligible_count=Metric(population["counted"]),
                   snapshot_eligible_count=Metric(population["eligible"]),
                   readahead_hit_pages=Metric(population["readahead"]),
                   readahead_hit_ratio=Metric(D(population["readahead"]) / population["counted"]
                                             if population["counted"] else None, "fraction", "",
                                             "no counted refaults" if not population["counted"] else ""))
    return metrics


def drain_metrics(row, population, pages):
    values = {name: integer(row, name) for name in DRAIN_FIELDS[:-1]}
    rounds, remaining, remapped = (values[name] for name in DRAIN_FIELDS[:3])
    # W1 also retries present pages without touching/remapping swapcache.
    # W3/W4 drain only cached-nonpresent pages, so each retry remaps >=1.
    retries_present = row.get("workload") == "w1"
    if (rounds > 4 or remaining > pages or remapped > 4 * pages or
            (not retries_present and remapped < rounds) or
            (rounds == 0 and remapped != 0) or (remaining and rounds != 4)):
        raise ValueError("invalid bounded drain rounds/remapped/remaining counts")
    for name, expected in (("refault_eligible_pages", population["eligible"]),
                           ("refault_counted_pages", population["counted"]),
                           ("readahead_hit_pages", population["readahead"])):
        if values[name] != expected:
            raise ValueError(name + " differs from per-touch frozen-snapshot flags")
    text = row.get("readahead_hit_ratio", "")
    if population["counted"]:
        try:
            ratio = D(text)
        except (InvalidOperation, ValueError) as error:
            raise ValueError("invalid readahead_hit_ratio") from error
        exact = D(population["readahead"]) / population["counted"]
        if not ratio.is_finite() or not 0 <= ratio <= 1 or abs(ratio - exact) > D("0.000000001"):
            raise ValueError("readahead_hit_ratio differs from counted population")
    elif text:
        raise ValueError("zero denominator hit ratio must be empty/N/A")
    metrics = {name: Metric(value) for name, value in values.items()}
    metrics["purge_ns"] = Metric(integer(row, "purge_ns"), "ns")
    timeout = integer(row, "drain_timed_out")
    if timeout not in (0, 1):
        raise ValueError("invalid drain timeout flag")
    metrics["drain_timed_out"] = Metric(timeout, "boolean")
    return metrics


def writeback_metrics(row, boot):
    # Runtime import: run imports telemetry, which imports this module. No
    # parser runs during module initialization, so there is no import cycle.
    from run import W4_PROTOCOL_V411

    protocol = boot["meta"].get("w4_protocol")
    # Compare the complete JSON contract, including scalar types: True must
    # not masquerade as a one-second interval, nor 300.0 as the frozen cap.
    if (not isinstance(protocol, dict) or
            json.dumps(protocol, sort_keys=True) != json.dumps(W4_PROTOCOL_V411, sort_keys=True)):
        raise ValueError("missing/mismatched W4 stability protocol source binding")
    backend = boot["meta"].get("backend")
    if backend not in WRITEBACK:
        raise ValueError("W4 backend outside the three writeback groups")
    source, unit, _ = WRITEBACK[backend]
    if row.get("writeback_source") != source:
        raise ValueError("W4 writeback source/backend mismatch")
    values = {name: integer(row, name) for name in WRITEBACK_FIELDS[1:]}
    if values["writeback_after"] - values["writeback_before"] != values["writeback_delta"] or values["writeback_delta"] <= 0:
        raise ValueError("W4 requires a positive real writeback counter delta")
    if values["pressure_ready"] != 1 or values["writeback_proven"] != 1:
        raise ValueError("W4 requires pressure_ready=1 and writeback_proven=1")
    page_bytes = integer(row, "page_size", 1)
    if not values["pressure_bytes"] or values["pressure_bytes"] % page_bytes:
        raise ValueError("W4 pressure bytes must be positive/page-aligned")
    if values["writeback_explicit_idle"] != int(backend == "zram-writeback"):
        raise ValueError("zram explicit idle writeback must remain distinguished")
    if values["writeback_wait_timed_out"] not in (0, 1):
        raise ValueError("invalid W4 timeout flag")
    timeout = values["writeback_wait_timed_out"]
    cap_ns = W4_PROTOCOL_V411["wait_cap_seconds"] * 10**9
    if timeout != int(values["writeback_wait_ns"] >= cap_ns):
        raise ValueError("W4 timeout contradicts the 300-second cap")
    if not timeout and values["writeback_stable_polls"] < W4_PROTOCOL_V411["stable_intervals"]:
        raise ValueError("W4 requires three complete unchanged counter intervals")
    return {name: Metric(value, unit if name in ("writeback_before", "writeback_after", "writeback_delta")
                         else "ns" if name.endswith("_ns") else "bytes" if name == "pressure_bytes"
                         else "boolean" if name in ("pressure_ready", "writeback_proven", "writeback_explicit_idle", "writeback_wait_timed_out")
                         else "count") for name, value in values.items()}


def w1(boot):
    buckets = defaultdict(bucket)
    pages = option(boot, "--pages", 4096)
    rounds, warmup = option(boot, "--rounds", 5), option(boot, "--warmup", 1)
    single = min(option(boot, "--single", 4096), pages)
    spec = boot["meta"].get("workload_spec", {})
    verified_type0 = isinstance(spec, dict) and spec.get("active_swap_types") == [0]
    protocol = boot["meta"].get("w1_protocol")
    if not isinstance(protocol, dict) or (protocol.get("prime") != PRIME_CONTRACT or protocol.get("pageout") != PAGEOUT_CONTRACT):
        raise ValueError("missing/mismatched W1 prime protocol source binding")
    if "--rewrite" not in boot["meta"].get("workload_argv", []):
        raise ValueError("v4.6 W1 requires its separate --rewrite phase")
    if min(pages, rounds, single) <= 0 or warmup not in (0, 1):
        raise ValueError("invalid W1 sizes/rounds")
    if boot["formal"] and (rounds != 5 or warmup != 1):
        boot["issues"].append("formal W1 requires one warmup plus five measurement rounds")
    expected_rows = ((r, mode, "touch" if i < count else "batch")
                     for r in range(rounds + warmup) for mode in ("seq", "rand", "single", "rewrite")
                     for count in (single if mode == "single" else pages,)
                     for i in range(count + int(mode != "single")))
    for row in csv_rows(boot):
        if (row.get("method") != METHOD or row.get("workload") != "w1" or
                row.get("mode") not in ("seq", "rand", "single", "rewrite")):
            raise ValueError("W1 v4.6 method/workload/mode missing")
        r, mode = integer(row, "round"), row["mode"]
        if next(expected_rows, None) != (r, mode, row.get("record")):
            raise ValueError("W1 round/mode/record sequence invalid")
        if r >= rounds + warmup:
            raise ValueError("unexpected inner round")
        b = buckets[r, mode]
        flag = integer(row, "warmup")
        if flag not in (0, 1) or flag != int(r < warmup) or b["warmup"] not in (None, flag):
            raise ValueError("inconsistent warmup flag")
        b["warmup"] = flag
        prime_counts(row, spec)
        if row.get("record") == "batch":
            if (mode == "single" or b["batch"] is not None or row.get("touch_ns") or
                    row.get("pageout_ns") or any(row.get(name) for name in TOUCH_FIELDS)):
                raise ValueError("duplicate/malformed batch record")
            b["batch"] = row
            continue
        if row.get("record") != "touch" or row.get("classification") not in CLASSES:
            raise ValueError("unknown W1 record/classification")
        index = integer(row, "page_index")
        if integer(row, "sequence") != b["count"] or index >= pages or index in b["pages"]:
            raise ValueError("touch sequence/page coverage invalid")
        if mode == "seq" and index != b["count"]:
            raise ValueError("sequential phase page order invalid")
        if any(row.get(name) for name in DRAIN_FIELDS):
            raise ValueError("W1 touch must not repeat batch drain totals")
        b["pages"].add(index)
        b["count"] += 1
        if row.get("verify_ok") != "1":
            boot["issues"].append(f"round {r}/{mode}: full-page verification failure")
        kind, ns = row["classification"], integer(row, "touch_ns")
        counted = touch_population(row, boot, b)
        swap_type = integer(row, "swap_type_before", -1)
        if swap_type > 31:
            raise ValueError("swap_type_before outside pagemap type range")
        b["swap_types"][swap_type] += 1
        nonpresent = row.get("present_before") == "0"
        b["nonpresent"] += nonpresent
        if counted:
            b["eligible_ns"] += ns
            if mode == "single":
                b["srf"].append(ns)
        if mode == "single":
            pageout = integer(row, "pageout_ns")
            # Pageout needs swap identity, not cache/refault eligibility.
            # Metadata or the PTE observation proves this controlled guest's type 0.
            # A failed cache observation may be unknown but still have known
            # real swap identity; unknown special types never qualify.
            real_swapped = (kind == "memcomp" or
                            (swap_type == 0 and (verified_type0 or
                             kind in ("swapped_nonresident", "cached_nonpresent"))))
            if nonpresent and row.get("swapped_before") == "1" and real_swapped:
                b["spo"].append(pageout)
        elif row.get("pageout_ns"):
            raise ValueError("non-single touch has pageout_ns")
    if next(expected_rows, None) is not None:
        boot["issues"].append("W1 row sequence ended before all assigned records")
    for r in range(rounds + warmup):
        for mode in ("seq", "rand", "single", "rewrite"):
            b = buckets[r, mode]
            expected = single if mode == "single" else pages
            if b["count"] != expected:
                boot["issues"].append(f"round {r}/{mode}: {b['count']}/{expected} touches")
            n = b["counted"]
            metrics = {mode + "." + name: metric for name, metric in population_metrics(b).items()}
            for swap_type in range(-1, 32):
                count = b["swap_types"][swap_type]
                metrics[mode + (".swap_type_unknown_count" if swap_type < 0 else f".swap_type_{swap_type}_count")] = Metric(count)
            metrics.update({mode + ".eligible_count": Metric(n), mode + ".touch_count": Metric(b["count"]),
                            mode + ".nonpresent_count": Metric(b["nonpresent"]),
                            mode + ".eligible_touch_ns": Metric(b["eligible_ns"], "ns"),
                            mode + ".nonpresent_fraction": Metric(D(b["nonpresent"]) / b["count"] if b["count"] else None, "fraction")})
            if mode == "single":
                metrics["single.spo_count"] = Metric(len(b["spo"]))
                metrics["single.srf_count"] = Metric(len(b["srf"]))
                for prefix, values in (("spo", b["spo"]), ("srf", b["srf"])):
                    for p in (50, 99):
                        # C.3 excludes plain-swap single refault latency from
                        # standard comparisons, even if a subset is noncached.
                        policy_na = prefix == "srf" and boot["meta"].get("backend") == "swap"
                        reason = ("C.3 policy exclusion: plain-swap single refault" if policy_na else
                                  "no known nonpresent real-swapped pages" if prefix == "spo" and not values else
                                  "no frozen-snapshot counted refaults" if not values else "")
                        metrics[f"single.{prefix}_p{p}_ns"] = Metric(
                            None if policy_na else quantile(values, D(p) / 100),
                            "ns", "lower", reason)
            else:
                metrics[mode + "rf_ns"] = Metric(D(b["eligible_ns"]) / n if n else None, "ns", "lower", "no frozen-snapshot counted refaults" if not n else "")
                batch = b["batch"]
                if batch is None:
                    boot["issues"].append(f"round {r}/{mode}: batch absent")
                    batch = {}
                req = integer(batch, "requested_pages") if batch else 0
                np = integer(batch, "nonpresent_pages") if batch else 0
                if batch and (req != pages or np > req or np != b["snapshot_nonpresent"]):
                    boot["issues"].append("batch requested/nonpresent snapshot count invalid")
                for name in ("pageout_total_ns", "purge_ns", "drain_timed_out"):
                    value = integer(batch, name) if batch else None
                    metrics[mode + "." + name] = Metric(value, "ns" if name.endswith("_ns") else "boolean")
                if batch and metrics[mode + ".drain_timed_out"].value != 0:
                    boot["issues"].append(f"round {r}/{mode}: drain timed out")
                if batch:
                    metrics.update({mode + "." + name: metric
                                    for name, metric in drain_metrics(batch, b, pages).items()})
                first, fraction, added = first_pass_counts(batch) if batch else (0, None, None)
                total = integer(batch, "pageout_total_ns") if batch else None
                metrics[mode + ".pageout_attempt_ns"] = Metric(
                    D(total) / req if total is not None and req > 0 else None,
                    "ns", "lower", "no requested pages/batch" if not req else "",
                    source="run1.csv.gz:pageout_total_ns;requested_pages", note=W1_PAGEOUT_NOTE)
                metrics[mode + ".pageout_ns"] = Metric(
                    D(total) / first if total is not None and first > 0 else None,
                    "ns", "lower", "no first-pass nonpresent pages/batch" if not first else "",
                    source="run1.csv.gz:pageout_total_ns;first_pass_nonpresent_pages", note=W1_PAGEOUT_NOTE)
                metrics[mode + ".first_pass_nonpresent_pages"] = Metric(first if batch else None)
                metrics[mode + ".first_pass_nonpresent_fraction"] = Metric(fraction, "fraction")
                metrics[mode + ".drain_additional_nonpresent_pages"] = Metric(added)
                metrics[mode + ".nonpresent_pages"] = Metric(np if batch else None)
                metrics[mode + ".requested_pages"] = Metric(req if batch else None)
                if mode == "rewrite" and batch:
                    prime = prime_counts(batch, spec)
                    note = ("v4.7 prime smoke verdict pending realguest review: memcomp-swap "
                            "should be near all requested pages (16384 at the planned size); "
                            "no numeric acceptance threshold is inferred")
                    for name, value in prime.items():
                        metrics["rewrite." + name] = Metric(value, source=PRIME_SOURCE, note=note)
                    metrics["rewrite.prime_real_swap_fraction"] = Metric(
                        D(prime["prime_real_swap_pages"]) / prime["prime_requested_pages"],
                        "fraction", source=PRIME_SOURCE, note=note)
                    if prime["prime_drain_timed_out"]:
                        boot["issues"].append(f"round {r}/rewrite prime: drain timed out")
                for family in (("memfree", "memavailable") if mode == "seq" else ()):
                    values = []
                    for point in ("before", "after"):
                        key = family + "_" + point + "_kib"
                        value = integer(batch, key, -1) if batch else -1
                        values.append(value if value >= 0 else None)
                        metrics[mode + "." + key] = Metric(values[-1], "KiB", reason="meminfo unavailable" if value < 0 else "")
                    metrics[mode + "." + family + "_delta_kib"] = Metric(values[1] - values[0] if None not in values else None, "KiB", "", "meminfo unavailable" if None in values else "")
            boot["rounds"].append((r, b["warmup"] if b["warmup"] is not None else int(r < warmup), metrics))


def physical_free_consumption(metrics):
    """Invert the existing signed delta; never infer PCP from raw MemFree."""
    delta = metrics.get(W1_PHYSICAL_FREE_DELTA, Metric(None, "KiB", reason="MemFree+PCP snapshot delta unavailable"))
    valid = delta.value is not None and not delta.reason and delta.unit == "KiB"
    return Metric(-delta.value if valid else None, "KiB", "",
                  "" if valid else delta.reason or "MemFree+PCP delta unavailable/invalid unit",
                  source=delta.source, provenance=delta.provenance, note=W1_PAGEOUT_NOTE)


def primary_pageout_metric(candidate, reference, mode):
    """Label the planned primary pair, without dropping either denominator."""
    if candidate.startswith("memcomp-noswap-") and reference.startswith("zram-"):
        return mode + ".pageout_attempt_ns"
    if candidate.startswith("memcomp-swap-") and reference == "zswap-zstd":
        return mode + ".pageout_ns"
    return ""


def pageout_comparison_context(workload, candidate, reference, name, summaries):
    mode, dot, suffix = name.partition(".")
    if mode not in {"seq", "rand", "rewrite"} or not dot or suffix not in {"pageout_ns", "pageout_attempt_ns"}:
        return {}
    primary = primary_pageout_metric(candidate, reference, mode)
    context = dict(primary_metric=primary,
                   comparison_role="primary" if name == primary else "secondary",
                   comparison_note=W1_PAGEOUT_NOTE)
    for side, group in (("candidate", candidate), ("reference", reference)):
        for field in W1_PAGEOUT_FIELDS:
            key = W1_PHYSICAL_CONSUMED if field == "physical_free_consumed_kib" else mode + "." + field
            context[side + "_" + field] = summaries.get((workload, group, key), {}).get("median")
    return context


def other_workload(boot):
    rows, delays = 0, []
    first_touches, nonpresent_touches, refaults, readback = [], [], [], []
    switches, successful, exits = 0, 0, {}
    population, phase_record, seen = bucket(), None, set()
    pages = option(boot, "--pages", 4096) if boot["label"] != "w2" else 0
    for row in csv_rows(boot):
        rows += 1
        if boot["label"] == "w2":
            if row.get("record") == "switch":
                switches += 1
                if row.get("status") == "ok":
                    if integer(row, "checked_words") == 0 or row.get("mismatch_word"):
                        raise ValueError("successful switch lacks complete verification")
                    successful += 1
                    delays.append(integer(row, "elapsed_ns"))
            elif row.get("record") == "pid_exit":
                worker = integer(row, "worker")
                if worker in exits:
                    raise ValueError("duplicate worker exit record")
                exits[worker] = integer(row, "alive_before_cleanup")
        else:
            if row.get("method") != METHOD or row.get("workload") != boot["label"]:
                raise ValueError("W3/W4 v4.6 method/workload mismatch")
            if integer(row, "sequence") != rows - 1:
                raise ValueError("touch sequence invalid")
            index = integer(row, "page_index")
            if row.get("order") not in ("seq", "rand"):
                raise ValueError("touch order must be seq or rand")
            if index >= pages or index in seen or (row.get("order") == "seq" and index != rows - 1):
                raise ValueError("touch page coverage/order invalid")
            seen.add(index)
            if row.get("verify_ok") != "1":
                boot["issues"].append("full-page verification failure")
            counted = touch_population(row, boot, population)
            repeated = DRAIN_FIELDS + ("purge_ns", "drain_timed_out") + WRITEBACK_FIELDS
            if phase_record is None:
                phase_record = row
            elif any(row.get(name) != phase_record.get(name) for name in repeated):
                raise ValueError("inconsistent repeated phase drain/writeback totals")
            ns = integer(row, "touch_ns")
            if boot["label"] == "w4":
                readback.append(ns)
            if boot["label"] == "w3":
                first_touches.append(ns)
                if counted:
                    refaults.append(ns)
                if row.get("present_before") == "0":
                    nonpresent_touches.append(ns)
    metrics = {"raw_rows": Metric(rows)}
    if boot["label"] == "w3":
        for name, values in (("first_touch", first_touches),
                             ("nonpresent_first_touch", nonpresent_touches)):
            metrics[name + "_count"] = Metric(len(values))
            for percent in (50, 99):
                metrics[f"{name}_p{percent}_ns"] = Metric(
                    quantile(values, D(percent) / 100), "ns", "",
                    "no observed touches" if not values else "")

        for percent in (50, 99):
            metrics[f"refault_p{percent}_ns"] = Metric(
                quantile(refaults, D(percent) / 100), "ns", "lower",
                "no C.3 eligible refaults" if not refaults else "",
                source="run1.csv.gz:touch_ns", note=W3_SCOPE)
        metrics["refault_fraction"] = Metric(
            D(len(refaults)) / len(first_touches) if first_touches else None,
            "fraction", note=W3_SCOPE)
        metrics["nonpresent_first_touch_fraction"] = Metric(
            D(len(nonpresent_touches)) / len(first_touches) if first_touches else None,
            "fraction", source="run1.csv.gz:present_before", note=W3_SCOPE)

    if boot["label"] == "w2":
        if switches != option(boot, "--switches", 32) or len(exits) != option(boot, "--workers", 8):
            boot["issues"].append("missing switches/worker exit records")
        metrics.update(switches=Metric(switches), ack_ok=Metric(successful), unavailable=Metric(switches-successful),
                       availability=Metric(D(successful)/switches if switches else None,"fraction","higher"),
                       alive_before_cleanup=Metric(sum(exits.values()),"workers","higher"))
        for p in (50, 99):
            metrics[f"ack_conditional_p{p}_ns"] = Metric(quantile(delays,D(p)/100),"ns","lower","no acknowledged fully verified switches" if not delays else "")
    else:
        expected = boot["status"][0].get("expected_workload_rows") if len(boot["status"]) == 1 and isinstance(boot["status"][0],dict) else None
        if positive_int(expected) and rows != expected:
            boot["issues"].append("touch rows differ from runner expected_workload_rows")
        if rows != pages or len(seen) != pages:
            boot["issues"].append("W3/W4 incomplete unique page coverage")
        metrics.update(population_metrics(population))
        if phase_record is not None:
            metrics.update(drain_metrics(phase_record, population, pages))
            if metrics["drain_timed_out"].value != 0:
                boot["issues"].append("drain timed out")
            if boot["label"] == "w4":
                metrics.update(writeback_metrics(phase_record, boot))
            elif any(phase_record.get(name) for name in WRITEBACK_FIELDS):
                raise ValueError("W3 must not fabricate W4 writeback fields")
        elif boot["label"] == "w4":
            boot["issues"].append("W4 missing pressure/positive-writeback evidence")
        if boot["label"] == "w4":
            timeout = metrics.get("writeback_wait_timed_out", Metric(None)).value
            status = boot["status"][0] if len(boot["status"]) == 1 and isinstance(boot["status"][0], dict) else {}
            excluded = timeout != 0 or status.get("writeback_latency_excluded") is True
            if type(status.get("writeback_latency_excluded")) is not bool:
                boot["issues"].append("W4 missing runner readback exclusion decision")
                excluded = True
            metrics["writeback_latency_excluded"] = Metric(int(excluded), "boolean")
            metrics["post_writeback_readback_count"] = Metric(len(readback), note=W4_READBACK_SCOPE)
            for p in (50, 99):
                metrics[f"post_writeback_readback_p{p}_ns"] = Metric(
                    None if excluded else quantile(readback, D(p) / 100), "ns", "lower",
                    "writeback timeout/unstable read-begin gate; latency excluded" if excluded else
                    "no verified full-segment readback" if not readback else "",
                    source="run1.csv.gz:touch_ns", note=W4_READBACK_SCOPE)
    boot["rounds"].append((1,0,metrics))


def guest_medians(boot):
    expected = option(boot, "--rounds", 5) if boot["label"] == "w1" else 1
    measured = [(r, metrics) for r,warm,metrics in boot["rounds"] if not warm]
    keys = set().union(*(metrics.keys() for _,metrics in measured)) if measured else set()
    for key in keys:
        # W1 keeps independent seq/rand/single/rewrite dictionaries per round.
        # Only the matching mode contributes one observation of this metric.
        cells = [metrics[key] for _,metrics in measured if key in metrics]
        template = next(m for m in cells if m is not None)
        values = [m.value for m in cells if m is not None and m.value is not None and not m.reason]
        complete = len(cells) == len(values) == expected
        boot["metrics"][key] = Metric(quantile(values,'.5') if complete else None, template.unit, template.direction,
                                      "" if complete else f"{len(values)}/{expected} usable measurement rounds; " + "; ".join(sorted({m.reason for m in cells if m and m.reason})),
                                      source=template.source, provenance="per-round.csv:" + key, note=template.note)


def metric_at(slots, round_number, name):
    entries = slots.get(round_number, [])
    if len(entries) != 1:
        return None, "missing boot" if not entries else "duplicate boot; no replacement/selection"
    boot = entries[0]
    if boot["issues"] or not boot["formal"]:
        return None, "; ".join(boot["issues"]) or "nonformal boot"
    metric = boot["metrics"].get(name)
    if metric is None or metric.value is None or metric.reason:
        return None, metric.reason if metric and metric.reason else "metric unavailable"
    return metric.value, ""


def groups_for(boots):
    groups = defaultdict(lambda: defaultdict(list))
    for boot in boots:
        if boot["formal"]:
            i = boot["identity"]
            groups[i["workload_id"],i["group_id"]][i["host_boot_round"]].append(boot)
    add_w1_combined(groups)
    return groups


def geomean(values):
    """Geometric mean of a complete nonnegative population."""
    values = [D(value) for value in values]
    if not values or any(value < 0 for value in values):
        raise ValueError("geomean requires nonnegative values")
    if any(value == 0 for value in values):
        return D(0)
    if all(value == values[0] for value in values):
        return values[0]
    return (sum(value.ln() for value in values) / len(values)).exp()


def add_w1_combined(groups):
    """Derived summaries add no guest slots; require all eight applications."""
    workloads = {"w1-corpus-" + source for source in SOURCES}
    names = {group for workload, group in groups if workload in workloads}
    for group in names:
        for r in range(1, 11):
            metrics = {}
            for name in W1_COMBINED_TIMINGS:
                cells = [(source, *metric_at(groups.get(("w1-corpus-" + source, group), {}), r, name))
                         for source in SOURCES]
                reasons = [source + ": " + reason for source, value, reason in cells if reason]
                if any(value is not None and value < 0 for _, value, _ in cells):
                    reasons.append("negative application timing")
                metrics[name] = Metric(None if reasons else geomean([value for _, value, _ in cells]),
                                       "ns", "lower", "; ".join(reasons),
                                       note="Derived eight-application geometric mean in one host round")
            groups[W1_COMBINED, group][r].append(
                dict(label="w1", formal=True, issues=[], metrics=metrics))


def combined_ratio(groups, candidate, reference, r, name):
    ratios, reasons = [], []
    for source in SOURCES:
        workload = "w1-corpus-" + source
        a, ae = metric_at(groups.get((workload, candidate), {}), r, name)
        b, be = metric_at(groups.get((workload, reference), {}), r, name)
        if ae or be or a < 0 or b <= 0:
            reasons.append(source + ": " + (ae or be or "invalid ratio denominator/value"))
        else:
            ratios.append(D(a) / D(b))
    return (None, "; ".join(reasons)) if reasons else (geomean(ratios), "")


def group_stats(groups):
    rows, lookup = [], {}
    for (workload,group),slots in sorted(groups.items()):
        entries = [b for values in slots.values() for b in values]
        label = entries[0]["label"]
        n = boot_count(label)
        keys = set().union(*(b["metrics"].keys() for b in entries)) or {"population"}
        for name in sorted(keys):
            values, reasons = [], []
            template = next((b["metrics"][name] for b in entries if name in b["metrics"]),Metric(None))
            for r in range(1,n+1):
                value, reason = metric_at(slots,r,name)
                if value is not None:
                    values.append(value)
                else:
                    reasons.append(f"round{r}: {reason}")
            row = dict(workload_id=workload,group_id=group,metric=name,unit=template.unit,direction=template.direction,
                       n_expected=n,n_valid=len(values),n_invalid=n-len(values),n_missing=sum(not slots.get(r) for r in range(1,n+1)),
                       n_latency_excluded=sum(len(slots.get(r, [])) == 1 and
                           slots[r][0]["metrics"].get("writeback_latency_excluded", Metric(0)).value == 1
                           for r in range(1, n + 1)),
                       quality="complete" if len(values)==n else "partial" if values else "unavailable",reason="; ".join(reasons),**distribution(values))
            rows.append(row)
            lookup[workload,group,name] = row
    return rows, lookup


def paired_population(slots, round_number, label, name):
    if label == "w3" and name.startswith("refault_p"):
        count, _ = metric_at(slots, round_number, "eligible_count")
        fraction, _ = metric_at(slots, round_number, "refault_fraction")
        return count, fraction
    if label == "w4" and name.startswith("post_writeback_readback_p"):
        count, _ = metric_at(slots, round_number, "post_writeback_readback_count")
        return count, None
    return None, None


# E.5 names the formal primary timing population, not arbitrary ns metrics.
W1_PRIMARY_TIMINGS = (
    "seqrf_ns", "randrf_ns", "rewriterf_ns",
    "seq.pageout_ns", "rand.pageout_ns", "rewrite.pageout_ns",
    "single.spo_p50_ns", "single.spo_p99_ns",
    "single.srf_p50_ns", "single.srf_p99_ns",
)
W1_COMBINED_TIMINGS = W1_PRIMARY_TIMINGS + tuple(
    mode + ".pageout_attempt_ns" for mode in ("seq", "rand", "rewrite"))
PRIMARY_TIMINGS = {
    **{("w1-corpus-" + source, name): 10
       for source in SOURCES
       for name in W1_PRIMARY_TIMINGS},
    **{("w2", f"ack_conditional_p{p}_ns"): 10 for p in (50, 99)},
    **{("w3-anon-512m", f"refault_p{p}_ns"): 5 for p in (50, 99)},
}


def primary_environment(comparisons):
    """One fail-closed E.5 gate shared by summary generation and plotting."""
    controls = defaultdict(list)
    for row in comparisons:
        key = row["workload_id"], row["metric"]
        if row["candidate"].startswith("zswap-crosscheck") and key in PRIMARY_TIMINGS:
            controls[key].append(row)
    if any(row["decision"] == "environment_drift" or row["environment"] == "drift"
           for rows in controls.values() for row in rows):
        return "drift"
    if controls.keys() != PRIMARY_TIMINGS.keys():
        return "unknown"
    for key, rows in controls.items():
        if len(rows) != 1:
            return "unknown"
        row, expected = rows[0], PRIMARY_TIMINGS[key]
        if (row["reference"] != "zswap-zstd" or row["unit"] != "ns" or
                int(row["n_expected"]) != expected or
                int(row["n_pair_valid"]) != expected or
                int(row.get("n_ratio_valid", row["n_pair_valid"])) != expected or
                row["environment"] != "within_noise" or
                row["decision"] != "within_noise"):
            return "unknown"
    return "within_noise"


def counter_resolution(name, unit):
    """Common telemetry counters only; never infer native/gauge semantics.

    snapshot_metrics() emits PSI us, vmstat event units and diskstats 512-byte
    sectors. Native payload conversions/4KiB units are not comparable traffic.
    Signed KiB memory deltas remain descriptive and are never ratio-ranked.
    """
    field = name.rsplit(":", 1)[-1]
    if (field in {f"psi.{resource}.{level}_total_us"
                  for resource in ("memory", "cpu", "io")
                  for level in ("some", "full")} and unit == "us"):
        return D(1000)
    events = {"vmstat." + event for event in
              ("pswpin", "pswpout", "zswpin", "zswpout", "zswpwb",
               "pgscan_kswapd", "pgscan_direct", "pgmajfault", "allocstall")}
    if ((field in events or field.startswith("vmstat.allocstall_")) and
            unit in {"page_events", "scan_events", "stall_events", "events", "count"}):
        return D(100)
    if (field in {"vda.read_bytes", "vda.write_bytes", "write_volume_bytes"}
            and unit == "bytes"):
        return D(1048576)
    if field in {"vda.read_sectors", "vda.write_sectors"} and unit == "sectors_512B":
        return D(2048)
    return None


def paired(groups, summaries):
    pair_rows, stats = [], []
    workloads = sorted({w for w, _ in groups})
    for workload in workloads:
        names = {g for w, g in groups if w == workload}
        label = next(b["label"] for (w, _), slots in groups.items() if w == workload
                     for entries in slots.values() for b in entries)
        comparisons = []
        for group in sorted(names):
            if group.startswith("memcomp-"):
                refs = (["zram-writeback-zstd", "zswap-zstd"] if label == "w4" and group == "memcomp-swap-zstd" else
                        [] if label == "w4" else ["zram-zstd", "zswap-zstd"] if group.endswith("-zstd") else
                        ["zram-lz4"] if group.endswith("-lz4") else [])
                comparisons.extend((group, ref, False) for ref in refs)
        if "zswap-zstd" in names or "zswap-crosscheck" in names or "zswap-crosscheck-zstd" in names:
            cross = "zswap-crosscheck" if "zswap-crosscheck" in names else "zswap-crosscheck-zstd"
            comparisons.insert(0, (cross, "zswap-zstd", True))
        for candidate, reference, crosscheck in comparisons:
            left, right = groups.get((workload, candidate), {}), groups.get((workload, reference), {})
            keys = {name for (w, g, name), row in summaries.items()
                    if w == workload and g in (candidate, reference) and
                    (row["direction"] or ("delta:" in name and
                     counter_resolution(name, row["unit"]) is not None))}
            for name in sorted(keys):
                n = boot_count(label)
                ref = summaries.get((workload, reference, name), {})
                template = summaries.get((workload, candidate, name), ref)
                unit = template.get("unit", "")
                floor = counter_resolution(name, unit)
                ranked = floor is not None or (unit in {"ns", "fraction", "workers"} and ":" not in name)
                resolution_reason = ""
                if floor is not None:
                    reference_values = [metric_at(right, r, name)[0] for r in range(1, n + 1)]
                    reference_median = ref.get("median")
                    if (any(value is None or value <= 0 for value in reference_values) or
                            reference_median is None or reference_median < floor):
                        resolution_reason = ("below_resolution: reference requires every round > 0 "
                                             f"and median >= {floor} {unit}")
                elif not ranked:
                    resolution_reason = "unknown counter/unit semantics; descriptive only; no ratio or direction"
                ratios, reasons = [], []
                pair_start = len(pair_rows)
                for r in range(1, n + 1):
                    a, ae = metric_at(left, r, name)
                    b, be = metric_at(right, r, name)
                    reason = "; ".join(s for s in ("candidate: " + ae if ae else "",
                                                   "reference: " + be if be else "",
                                                   resolution_reason) if s)
                    if not reason and (a < 0 or b <= 0):
                        reason = "ratio requires nonnegative candidate and positive reference"
                    value = None if reason else D(a) / D(b)
                    if workload == W1_COMBINED:
                        value, combined_reason = combined_ratio(groups, candidate, reference, r, name)
                        reason = "; ".join(filter(None, (reason, combined_reason)))
                        if reason:
                            value = None
                    observed = (not ae and not be and a >= 0 and
                                (b >= 0 if floor is not None else b > 0))
                    ac, af = paired_population(left, r, label, name)
                    bc, bf = paired_population(right, r, label, name)
                    pair_rows.append(dict(workload_id=workload, candidate=candidate, reference=reference, metric=name,
                                          candidate_count=ac, reference_count=bc, candidate_fraction=af, reference_fraction=bf,
                                          host_boot_round=r, candidate_value=a, reference_value=b, ratio=value,
                                          pair_valid=int(observed), ratio_valid=int(value is not None), reason=reason))
                    if value is not None:
                        ratios.append(value)
                    else:
                        reasons.append(f"round{r}: {reason}")
                med = quantile(ratios, '.5')
                less, greater, ties = (sum(v < 1 for v in ratios), sum(v > 1 for v in ratios), sum(v == 1 for v in ratios))
                k = less if med is not None and med < 1 else greater if med is not None and med > 1 else 0
                spread = ref.get("spread")
                complete = len(ratios) == n and ref.get("n_valid") == n
                raw_claim = (complete and med is not None and spread is not None and
                             abs(med - 1) > spread and k >= (8 if n == 10 else 5))
                primary = crosscheck and (workload, name) in PRIMARY_TIMINGS and unit == "ns"
                env = ("drift" if raw_claim else "within_noise" if complete else "unknown") if primary else "descriptive_only" if crosscheck else "unknown"
                decision = ("below_resolution" if floor is not None and resolution_reason else
                            "descriptive_only" if crosscheck and not primary or not ranked or not template.get("direction") else
                            "environment_drift" if primary and raw_claim else "within_noise")
                if crosscheck and not primary:
                    reasons.append("auxiliary crosscheck; descriptive only; excluded from primary timing environment gate")
                # Observed pairs and ratio-eligible pairs are distinct: a low
                # resolution counter still has valid absolute observations.
                observed_count = sum(pair["pair_valid"] for pair in pair_rows[pair_start:])
                observed_complete = observed_count == n and ref.get("n_valid") == n
                row = dict(workload_id=workload, candidate=candidate, reference=reference, metric=name, unit=unit,
                           claim=decision, n_expected=n, n_pair_valid=observed_count,
                           n_ratio_valid=len(ratios), n_pair_invalid=n-observed_count,
                           n_pair_missing=sum(not left.get(r) or not right.get(r) for r in range(1, n+1)),
                           lower_n=less, higher_n=greater, equal_n=ties, consistent_k=k, consistency=f"{k}/{n}",
                           candidate_median=summaries.get((workload, candidate, name), {}).get("median"),
                           reference_median=ref.get("median"), ratio_median=med, reference_spread=spread,
                           effect_percent=(med-1)*100 if med is not None else None, decision=decision, environment=env,
                           reason="; ".join(reasons + ([] if observed_complete else ["partial paired/reference population; invalid pairs remain counted"])))
                row.update(pageout_comparison_context(workload, candidate, reference, name, summaries))
                population = pair_rows[pair_start:]
                for side in ("candidate", "reference"):
                    for field in ("count", "fraction"):
                        observed = [pair[side + "_" + field] for pair in population if pair[side + "_" + field] is not None]
                        row[side + "_" + field + "_median"] = quantile(observed, '.5')
                stats.append(row)
    # E.5 uses the eight-application/W2/anonymous-W3 primary timing inventory.
    matrix_environment = primary_environment(stats)
    for row in stats:
        if row["candidate"].startswith("zswap-crosscheck"):
            continue
        row["environment"] = matrix_environment
        if row["decision"] not in {"below_resolution", "descriptive_only"}:
            row["decision"] = row["claim"] = "within_noise"
            median, spread = row["ratio_median"], row["reference_spread"]
            complete = row["n_ratio_valid"] == row["n_expected"]
            consistent = row["consistent_k"] >= (8 if row["n_expected"] == 10 else 5)
            if (matrix_environment == "within_noise" and complete and consistent and
                    median is not None and spread is not None and abs(median - 1) > spread):
                row["decision"] = "lower" if median < 1 else "higher"
                row["claim"] = (("faster" if median < 1 else "slower")
                                if row["unit"] == "ns" else row["decision"])
        if matrix_environment != "within_noise":
            row["reason"] = "; ".join(filter(None, (row["reason"],
                "matrix-wide W1/W2/W3 primary timing crosscheck " + matrix_environment +
                "; directional claim blocked")))
    return pair_rows, stats


def write_csv(path, rows, fields):
    with path.open("x",encoding="utf-8",newline="") as stream:
        writer=csv.DictWriter(stream,fields)
        writer.writeheader()
        writer.writerows(rows)


def outputs(output, boots, include_rounds=True):
    """Write compact rows as each guest arrives; retain only guest medians."""
    compact = []
    with ExitStack() as files:
        def writer(name, fields):
            stream = files.enter_context((output / name).open("x", encoding="utf-8", newline=""))
            result = csv.DictWriter(stream, fields)
            result.writeheader()
            return result
        rounds = writer("per-round.csv", IDENTITY + ["inner_round", "warmup", "metric", "value", "unit", "direction", "run_valid", "reason", "source_path", "provenance", "note"]) if include_rounds else None
        guests = writer("per-guest.csv", IDENTITY + ["metric", "value", "unit", "direction", "run_valid", "formal", "reason", "source_path", "provenance", "note"])
        pageout_fields = list(W1_PAGEOUT_FIELDS) + ["first_pass_nonpresent_pages",
                          "requested_pages", "nonpresent_pages", "drain_additional_nonpresent_pages", "purge_ns"]
        pageouts = writer("w1-pageout.csv", IDENTITY + ["scope", "inner_round", "warmup", "mode"] + pageout_fields +
                          ["memory_scope", "memory_reason", "memory_source_path", "run_valid", "note"])
        for boot in boots:
            identity = boot["identity"]
            valid = not boot["issues"]
            if boot["label"] == "w1":
                boot["metrics"][W1_PHYSICAL_CONSUMED] = physical_free_consumption(boot["metrics"])
                memory_rounds = {}
                for r, warm, metrics in boot["rounds"]:
                    if W1_PHYSICAL_FREE_DELTA in metrics:
                        key = r, warm
                        memory_rounds[key] = (physical_free_consumption(metrics) if key not in memory_rounds else
                            Metric(None, "KiB", reason="duplicate seq MemFree+PCP delta"))
                scopes = [("per-guest-median", "", "", boot["metrics"])]
                if include_rounds:
                    scopes += [("inner-round", r, warm, metrics) for r, warm, metrics in boot["rounds"]]
                for scope, r, warm, metrics in scopes:
                    for mode in ("seq", "rand", "rewrite"):
                        if mode + ".pageout_ns" not in metrics:
                            continue
                        values = {name: metrics.get(mode + "." + name, Metric(None)).value for name in pageout_fields}
                        memory = (boot["metrics"][W1_PHYSICAL_CONSUMED] if scope == "per-guest-median" else
                                  memory_rounds.get((r, warm), Metric(None, "KiB", reason="matching seq MemFree+PCP delta unavailable")))
                        values["physical_free_consumed_kib"] = memory.value
                        pageouts.writerow(dict(identity, scope=scope, inner_round=r, warmup=warm, mode=mode,
                            **values, memory_scope="seq_fill_begin->seq_read_begin (shared W1 memory baseline)",
                            memory_reason=memory.reason, memory_source_path=memory.source,
                            run_valid=int(valid), note=W1_PAGEOUT_NOTE +
                            " Guest columns are separate inner-round medians; never multiply medians to reconstruct totals."))
            if rounds is not None:
                for r, warm, metrics in boot["rounds"]:
                    for name, m in sorted(metrics.items()):
                        rounds.writerow(dict(identity, inner_round=r, warmup=warm, metric=name, value=m.value, unit=m.unit,
                            direction=m.direction, run_valid=int(valid), reason=m.reason, source_path=m.source or "run1.csv.gz",
                            provenance=m.provenance, note=m.note))
            # Put run-level errors/status/provenance once, not in every metric
            # row; repeated failure inventories would defeat compact output.
            note = W4_READBACK_SCOPE if boot["label"] == "w4" else W3_SCOPE if boot["label"] == "w3" else ""
            for name, m in sorted(boot["metrics"].items()):
                guests.writerow(dict(identity, metric=name, value=m.value, unit=m.unit, direction=m.direction,
                    run_valid=int(valid), formal=int(boot["formal"]), reason=m.reason, source_path=m.source or "run1.csv.gz",
                    provenance=m.provenance, note=m.note or note))
            guests.writerow(dict(identity, metric="run.status", run_valid=int(valid), formal=int(boot["formal"]),
                reason="; ".join(boot["issues"]), source_path="metadata.json;status.json", note=json.dumps(boot["status"], sort_keys=True)))
            guests.writerow(dict(identity, metric="run.provenance", run_valid=int(valid), formal=int(boot["formal"]),
                source_path="metadata.json;matrix.json", note=json.dumps(boot["meta"], sort_keys=True)))
            compact.append(dict(boot, rounds=[]))
    groups = groups_for(compact)
    summary, lookup = group_stats(groups)
    pair_rows, comparisons = paired(groups, lookup)
    write_csv(output / "summary.csv", summary, ["workload_id", "group_id", "metric", "unit", "direction", "n_expected", "n_valid", "n_invalid", "n_missing", "n_latency_excluded", "median", "q1", "q3", "iqr", "spread", "quality", "reason"])
    write_csv(output / "pairs.csv", pair_rows, ["workload_id", "candidate", "reference", "metric", "host_boot_round", "candidate_value", "reference_value", "candidate_count", "reference_count", "candidate_fraction", "reference_fraction", "ratio", "pair_valid", "ratio_valid", "reason"])
    write_csv(output / "paired-summary.csv", comparisons, ["workload_id", "candidate", "reference", "metric", "unit", "claim", "n_expected", "n_pair_valid", "n_ratio_valid", "n_pair_invalid", "n_pair_missing", "lower_n", "higher_n", "equal_n", "consistent_k", "consistency", "candidate_median", "reference_median", "ratio_median", "reference_spread", "effect_percent", "decision", "environment", "candidate_count_median", "reference_count_median", "candidate_fraction_median", "reference_fraction_median", "primary_metric", "comparison_role", "comparison_note"] +
              [side + "_" + field for side in ("candidate", "reference") for field in W1_PAGEOUT_FIELDS] + ["reason"])
    with (output / "comparisons.json").open("x", encoding="utf-8") as stream:
        json.dump({"kind": "metric-pairing", "rows": comparisons,
                   "counts": {"all_metric_comparisons": len(comparisons),
                              "primary_environment_timings": len(PRIMARY_TIMINGS),
                              "by_workload": dict(Counter(row["workload_id"] for row in comparisons))}},
                  stream, indent=2, default=str)
        stream.write("\n")
    return compact


def read_boots(root):
    for boot in load_boots(root):
        if not boot["blocked"]:
            try:
                w1(boot) if boot["label"] == "w1" else other_workload(boot)
                guest_medians(boot)
                # One streaming pass joins C.5 sources, C.6 pool baselines and
                # W4 disk bytes. No cross-backend native-unit conversion.
                from telemetry import extract_boot
                raw = dict(boot, rounds=[], metrics={}, issues=[])
                extract_boot(raw)
                boot["issues"].extend(raw["issues"])
                boot["extraction_failed"] = raw.get("extraction_failed", False)
                if boot["label"] == "w4":
                    csv_timeout = boot["metrics"].get("writeback_wait_timed_out", Metric(None)).value
                    raw_timeout = raw["metrics"].get("writeback_wait_timed_out", Metric(None)).value
                    if csv_timeout != raw_timeout:
                        boot["issues"].append("W4 CSV timeout/raw terminal-phase mismatch")
                    raw_excluded = raw["metrics"].get("writeback_latency_excluded", Metric(1)).value
                    excluded = bool(raw_excluded or boot["metrics"].get("writeback_latency_excluded", Metric(1)).value)
                    for _, _, metrics in boot["rounds"]:
                        if "writeback_latency_excluded" in metrics:
                            metrics["writeback_latency_excluded"] = Metric(int(excluded), "boolean")
                        if excluded:
                            for name in ("post_writeback_readback_p50_ns", "post_writeback_readback_p99_ns"):
                                if name in metrics:
                                    m = metrics[name]
                                    metrics[name] = Metric(None, m.unit, m.direction, "writeback stability timeout; latency excluded", m.source, m.provenance, m.note)
                                    boot["metrics"][name] = metrics[name]
                    raw["metrics"]["writeback_latency_excluded"] = Metric(int(excluded), "boolean", source="status.json;run1.log.gz")
                boot["metrics"].update(raw["metrics"])
                boot["rounds"].extend(raw["rounds"])
                if boot["label"] == "w1":
                    boot["metrics"][W1_PHYSICAL_CONSUMED] = physical_free_consumption(boot["metrics"])
            except (OSError, EOFError, UnicodeError, ValueError, TypeError, IndexError, KeyError, csv.Error, zlib.error) as error:
                boot["issues"].append(f"CSV/raw incomplete/invalid: {type(error).__name__}: {error}")
                boot["extraction_failed"] = True
        yield boot


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True,help="new output directory outside the input matrix")
    parser.add_argument("matrix",type=Path,help="one v4.6 matrix root, or one smoke boot for descriptive inspection")
    args=parser.parse_args()
    root,output=args.matrix.resolve(),args.output.resolve()
    if root==output or root in output.parents:
        parser.error("output must be outside the input matrix")
    try:
        output.mkdir(parents=True,exist_ok=False)
    except OSError as error:
        parser.error(str(error))
    boots = outputs(output, read_boots(root))
    if any(boot.get("extraction_failed") for boot in boots):
        raise SystemExit(1)


if __name__=="__main__":
    main()
