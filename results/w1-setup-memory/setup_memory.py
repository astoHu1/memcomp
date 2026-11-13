#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Report W1 setup-window memory per backend from the formal 965 logs.

The setup window runs from before_backend_setup to workload_start. The W1
memory window used in the paper starts later, at rN_seq_fill_begin, so
fixed setup costs such as zram's disksize-sized table are outside it.
"""
import gzip
import re
import statistics
import sys
from pathlib import Path

GROUPS = ("zram-lz4", "zram-zstd", "zram-writeback-zstd", "memcomp-noswap-lz4",
          "memcomp-noswap-zstd", "memcomp-swap-lz4", "memcomp-swap-zstd",
          "zswap-zstd")
FIELDS = ("MemFree", "VmallocUsed", "Slab")


def setup_meminfo(log):
    phase = path = None
    seen = {}
    for line in gzip.open(log, "rt", errors="replace"):
        m = re.match(r"COMPARE_RAW_BEGIN .* phase=(\S+) path=(\S+)", line)
        if m:
            phase, path = m.groups()
            if phase.endswith("_seq_fresh_mapping"):
                break
            continue
        if line.startswith("COMPARE_RAW_END"):
            phase = None
            continue
        if phase in ("before_backend_setup", "workload_start") and path == "/proc/meminfo":
            m = re.match(r"(\w+):\s+(\d+) kB", line)
            if m and m.group(1) in FIELDS:
                seen.setdefault(phase, {})[m.group(1)] = int(m.group(2))
    return seen


def main():
    root = Path(sys.argv[1])
    print("group boots field median_kib min_kib max_kib  (MemFree: loss; others: growth)")
    for group in GROUPS:
        values = {f: [] for f in FIELDS}
        for log in sorted(root.glob(f"round-*/w1-corpus-*/{group}/run1.log.gz")):
            s = setup_meminfo(log)
            a, b = s.get("before_backend_setup"), s.get("workload_start")
            if not a or not b:
                continue
            values["MemFree"].append(a["MemFree"] - b["MemFree"])
            values["VmallocUsed"].append(b["VmallocUsed"] - a["VmallocUsed"])
            values["Slab"].append(b["Slab"] - a["Slab"])
        for f in FIELDS:
            x = values[f]
            if x:
                print(f"{group} {len(x)} {f} {statistics.median(x):g} {min(x)} {max(x)}")


if __name__ == "__main__":
    main()
