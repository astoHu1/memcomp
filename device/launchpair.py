#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Host-only app launch analysis; resample fixed bootpairs, never app/launch rows.

See launchpair.md for the single CSV schema and raw APP/RE field mapping.
Metadata references are operator assertions, not independent boot attestation.
"""

import argparse
import csv
import math
import os
from pathlib import Path
import random
import statistics
import sys
from collections import defaultdict


CATEGORIES = {
    "cold": ("alive", {"COLD"}),
    "hot": ("relaunch", {"HOT"}),
    "warm": ("relaunch", {"WARM"}),
    "hot_warm": ("relaunch", {"HOT", "WARM"}),
}
FIELDS = (
    "pair_id", "boot_id", "arm", "regime", "app", "phase", "attempt",
    "launch_state", "total_ms", "status", "metadata_status", "boot_metadata",
    "source_ref",
)
COUNTS = (
    "ok_attempts", "failed_attempts", "unavailable_attempts", "missing_rows",
    "state_mismatch", "zero_time_attempts", "absent_boot_cells", "missing_a_cells", "missing_b_cells",
    "missing_both_cells", "unpaired_rows",
)
OUTPUT = (
    "scope", "regime", "app", "category", "estimate_status", "planned_pairs",
    "metadata_eligible_pairs", "metadata_unverified_pairs", "usable_pairs",
    "apps_expected", "apps_used", *COUNTS,
    "ratio_a_over_b", "ci95_low", "ci95_high",
)


def load_input(path, apps):
    """Keep failed/unavailable rows; reject ambiguous or changing boot roles."""
    observations = defaultdict(list)
    boots = {}
    roles = defaultdict(dict)
    identities = {}
    attempts = set()
    source_refs = set()
    references = {}
    regimes = set()
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(FIELDS):
            raise ValueError("CSV header must exactly match launchpair.md")
        for line, raw in enumerate(reader, 2):
            if None in raw or any(value is None for value in raw.values()):
                raise ValueError(f"line {line}: wrong number of CSV fields")
            row = {key: value.strip() for key, value in raw.items()}
            if any("\n" in value or "\r" in value for value in row.values()):
                raise ValueError(f"line {line}: multiline fields are unsupported")
            boot, arm, regime = row["boot_id"], row["arm"], row["regime"]
            app, phase, pair = row["app"], row["phase"], row["pair_id"]
            if not boot or arm not in {"A", "B"} or not regime:
                raise ValueError(f"line {line}: boot_id, A/B arm and regime required")
            if app not in apps or phase not in {"alive", "relaunch"}:
                raise ValueError(f"line {line}: app not in roster or invalid phase")
            if not row["attempt"].isascii() or not row["attempt"].isdigit():
                raise ValueError(f"line {line}: positive integer attempt required")
            attempt = int(row["attempt"])
            if attempt < 1:
                raise ValueError(f"line {line}: positive integer attempt required")
            status = row["status"]
            if status not in {"ok", "failed", "unavailable", "missing"}:
                raise ValueError(f"line {line}: invalid status")
            total = None
            if row["total_ms"]:
                if not row["total_ms"].isascii() or not row["total_ms"].isdigit():
                    raise ValueError(f"line {line}: TotalTime must be integer ms")
                total = float(row["total_ms"])
                if not math.isfinite(total) or total < 0:
                    raise ValueError(f"line {line}: TotalTime must be nonnegative/finite")
            if status == "ok" and (total is None or not row["launch_state"]):
                raise ValueError(f"line {line}: ok requires state and TotalTime")
            ref, documented = row["boot_metadata"], row["metadata_status"]
            if documented not in {"documented", "unverified"}:
                raise ValueError(f"line {line}: metadata_status must be documented/unverified")
            if documented == "documented":
                if not pair or not ref:
                    raise ValueError(f"line {line}: documented boot needs pair_id and metadata")
                if ref not in references:
                    target = Path(ref)
                    if not target.is_absolute():
                        target = path.parent / target
                    references[ref] = target.is_file() and os.access(target, os.R_OK)
                if not references[ref]:
                    raise ValueError(f"line {line}: boot metadata reference unavailable")
            identity = (arm, pair)
            if boot in identities and identities[boot] != identity:
                raise ValueError(f"line {line}: same boot reused in another arm/pair")
            identities[boot] = identity
            key = (regime, boot)
            metadata = (pair, arm, documented, ref)
            if key in boots and boots[key] != metadata:
                raise ValueError(f"line {line}: inconsistent metadata within a boot/regime")
            boots[key] = metadata
            if pair:
                members = roles[(regime, pair)]
                if arm in members and members[arm] != boot:
                    raise ValueError(f"line {line}: multiple boots for one pair arm")
                members[arm] = boot
            unique = (regime, boot, app, phase, attempt)
            if unique in attempts:
                raise ValueError(f"line {line}: duplicate attempt")
            attempts.add(unique)
            source = row["source_ref"]
            if status == "missing" and (source or row["launch_state"] or total is not None):
                raise ValueError(f"line {line}: missing placeholder must have no observation")
            if status != "missing" and not source:
                raise ValueError(f"line {line}: observed attempt needs raw source_ref")
            if source:
                if source in source_refs:
                    raise ValueError(f"line {line}: raw source_ref reused")
                source_refs.add(source)
            observations[(regime, boot, app, phase)].append(
                (status, row["launch_state"], total)
            )
            regimes.add(regime)
    if not regimes:
        raise ValueError("empty observation CSV")
    return observations, boots, roles, sorted(regimes)


def percentile(values, probability):
    position = (len(values) - 1) * probability
    index = int(position)
    fraction = position - index
    return values[index] * (1 - fraction) + values[min(index + 1, len(values) - 1)] * fraction


def estimate(log_ratios, draws, seed):
    """Each element is ONE independent bootpair (or its fixed-panel mean)."""
    if len(log_ratios) < 3:
        return ("N/A", "N/A", "N/A")
    rng = random.Random(seed)
    n = len(log_ratios)
    samples = sorted(
        math.fsum(rng.choices(log_ratios, k=n)) / n for _ in range(draws)
    )
    values = (
        math.fsum(log_ratios) / n,
        percentile(samples, 0.025), percentile(samples, 0.975),
    )
    return tuple(format(math.exp(value), ".9g") for value in values)


def documented_pair(regime, members, boots):
    return all(
        arm in members and boots[(regime, members[arm])][2] == "documented"
        for arm in ("A", "B")
    )


def app_values(regime, app, phase, states, pairs, observations, boots):
    counts = dict.fromkeys(COUNTS, 0)
    medians = {}
    for (r, boot), (pair, _arm, _status, _ref) in boots.items():
        if r != regime:
            continue
        rows = observations.get((regime, boot, app, phase), [])
        if not pair:
            counts["unpaired_rows"] += len(rows)
        selected = []
        for status, state, total in rows:
            counts[{"ok": "ok_attempts", "failed": "failed_attempts",
                    "unavailable": "unavailable_attempts", "missing": "missing_rows"}[status]] += 1
            counts["zero_time_attempts"] += int(total == 0)
            if status == "ok":
                if state not in states:
                    counts["state_mismatch"] += 1
                elif total > 0:
                    selected.append(total)
        medians[boot] = statistics.median(selected) if selected else None
    logs = {}
    for pair, members in pairs:
        values = {}
        for arm in ("A", "B"):
            boot = members.get(arm)
            rows = observations.get((regime, boot, app, phase), [])
            counts["absent_boot_cells"] += int(not rows)
            values[arm] = medians.get(boot)
        missing_a, missing_b = values["A"] is None, values["B"] is None
        counts["missing_a_cells"] += int(missing_a)
        counts["missing_b_cells"] += int(missing_b)
        counts["missing_both_cells"] += int(missing_a and missing_b)
        if not missing_a and not missing_b and documented_pair(regime, members, boots):
            logs[pair] = math.log(values["A"]) - math.log(values["B"])
    return logs, counts


def report(observations, boots, roles, regimes, apps, draws, seed):
    writer = csv.DictWriter(sys.stdout, fieldnames=OUTPUT, lineterminator="\n")
    writer.writeheader()
    for regime in regimes:
        pairs = sorted((pair, members) for (r, pair), members in roles.items() if r == regime)
        eligible = [pair for pair, members in pairs if documented_pair(regime, members, boots)]
        common = dict(regime=regime, planned_pairs=len(pairs),
                      metadata_eligible_pairs=len(eligible),
                      metadata_unverified_pairs=len(pairs) - len(eligible))
        for category, (phase, states) in CATEGORIES.items():
            app_logs = {}
            totals = dict.fromkeys(COUNTS, 0)
            for app in apps:
                logs, counts = app_values(regime, app, phase, states, pairs, observations, boots)
                app_logs[app] = logs
                totals = {key: totals[key] + counts[key] for key in COUNTS}
                result = estimate(list(logs.values()), draws, seed)
                state = "estimated" if len(logs) >= 3 else "N/A_lt3_documented_bootpairs"
                writer.writerow(dict(common, scope="per_app", app=app, category=category,
                                     estimate_status=state, usable_pairs=len(logs),
                                     apps_expected=1, apps_used=int(bool(logs)), **counts,
                                     ratio_a_over_b=result[0], ci95_low=result[1], ci95_high=result[2]))
            # A fixed complete app panel across all metadata-eligible bootpairs.
            # Resample its PAIR means, keeping every app in a pair together.
            panel = [app for app in apps if eligible and all(pair in app_logs[app] for pair in eligible)]
            aggregate = [math.fsum(app_logs[app][pair] for app in panel) / len(panel)
                         for pair in eligible] if panel else []
            result = estimate(aggregate, draws, seed)
            state = "estimated" if len(aggregate) >= 3 else "N/A_lt3_documented_bootpairs"
            if not panel:
                state = "N/A_no_complete_app_panel"
            writer.writerow(dict(common, scope="overall", app="ALL", category=category,
                                 estimate_status=state, usable_pairs=len(aggregate),
                                 apps_expected=len(apps), apps_used=len(panel), **totals,
                                 ratio_a_over_b=result[0], ci95_low=result[1], ci95_high=result[2]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="normalized observations CSV; see launchpair.md")
    parser.add_argument("--apps", required=True, type=Path, help="expected app roster, one package per line")
    parser.add_argument("--draws", type=int, default=4000, help="bootpair bootstrap draws (default 4000)")
    parser.add_argument("--seed", type=int, default=1, help="bootstrap RNG seed (default 1)")
    args = parser.parse_args()
    if not 1000 <= args.draws <= 100000 or args.seed < 0:
        parser.error("draws must be 1000..100000 and seed nonnegative")
    try:
        apps = [line.strip() for line in args.apps.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not apps or len(apps) != len(set(apps)) or any(any(c.isspace() for c in app) for app in apps):
            raise ValueError("app roster must be nonempty, unique, one package per line")
        apps.sort()
        data = load_input(args.input, set(apps))
        print(f"method=paired-log-ratio bootstrap_unit=bootpair draws={args.draws} seed={args.seed} "
              "minimum_pairs=3 metadata=operator-documented-not-attested", file=sys.stderr)
        report(*data, apps, args.draws, args.seed)
    except (OSError, ValueError, OverflowError, csv.Error) as error:
        print(f"ERROR: {error}; partial stdout, if any, is not a complete report", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
