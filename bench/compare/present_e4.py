#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Add v4.132 presentation fields to a frozen E.4 CSV."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

AFFECTED_COUNTERS = frozenset({
    "vmstat.pswpin",
    "vmstat.pswpout",
    "vmstat.zswpin",
    "vmstat.zswpout",
    "vmstat.zswpwb",
})


def affected_counter(row: dict[str, str]) -> bool:
    field = row.get("metric", "").rsplit(":", 1)[-1]
    return row.get("candidate", "").startswith("memcomp-") and field in AFFECTED_COUNTERS


def present(input_path: Path, output_path: Path) -> int:
    if input_path.resolve() == output_path.resolve():
        raise ValueError("input and output must be different files")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with input_path.open("r", encoding="utf-8", newline="") as source, \
            output_path.open("w", encoding="utf-8", newline="") as target:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            raise ValueError("input E.4 table has no header")
        fieldnames = list(reader.fieldnames) + [
            "presentation_decision",
            "presentation_note",
        ]
        writer = csv.DictWriter(target, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        count = 0
        for row in reader:
            output = dict(row)
            if affected_counter(row):
                output["presentation_decision"] = "descriptive_only"
                output["presentation_note"] = (
                    "memcomp candidate/reference counter accounting points differ; "
                    "report both sides only and do not infer direction"
                )
            else:
                output["presentation_decision"] = row.get("decision", "")
                output["presentation_note"] = ""
            writer.writerow(output)
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(present(args.input, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
