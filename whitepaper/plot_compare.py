#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Draw descriptive v4.6 figures from one finished, gated formal matrix.

W3 uses C.3 anonymous-page refault populations. W4 shows whole-segment readback
only after stable writeback, with timeout exclusions retained in boot counts.
"""
import argparse
import csv
import json
import math
import sys
from pathlib import Path


# Keep plotting on exactly the same fail-closed 84-timing E.5 gate.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "compare"))
from summarize import (SOURCES, W1_PHYSICAL_CONSUMED,
                       primary_environment)  # noqa: E402


GROUPS = (
    ("swap-none", "swap"),
    ("zram-zstd", "zram / zstd"),
    ("zram-lz4", "zram / lz4"),
    ("zram-writeback-zstd", "zram writeback / zstd"),
    ("zswap-zstd", "zswap / zstd"),
    ("zswap-crosscheck-zstd", "zswap crosscheck / zstd"),
    ("memcomp-noswap-zstd", "memcomp no swap / zstd"),
    ("memcomp-noswap-lz4", "memcomp no swap / lz4"),
    ("memcomp-swap-zstd", "memcomp + swap / zstd"),
    ("memcomp-swap-lz4", "memcomp + swap / lz4"),
)


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def finished_inputs(matrix, summary):
    metadata = json.loads((matrix / "matrix.json").read_text())
    if (metadata["method"] != "v4.6" or
            metadata["kind"] != "interleaved-comparison" or
            metadata["state"] not in {"complete", "complete-with-failures"} or
            metadata["planned_boots"] != 965 or
            metadata["completed_boots"] != 965 or
            len(metadata["jobs"]) != 965):
        raise ValueError("requires all 965 assigned formal v4.6 slots to finish")
    for job in metadata["jobs"]:
        status = job.get("status", {})
        if ("runner_exit" not in job or status.get("state") == "running" or
                not status.get("lifecycle_safe_to_continue")):
            raise ValueError("a slot lacks terminal runner/cleanup evidence")
    statuses = {}
    for row in read_csv(summary / "per-guest.csv"):
        if row["metric"] != "run.status":
            continue
        key = row["workload_id"], row["group_id"], int(row["host_boot_round"])
        if key in statuses:
            raise ValueError("duplicate per-guest status")
        statuses[key] = row
    if len(statuses) != 965:
        raise ValueError("summary does not contain all 965 assigned slots")
    for job in metadata["jobs"]:
        key = job["workload_id"], job["group_id"], job["host_boot_round"]
        row = statuses[key]
        if (row["method"] != "v4.6" or
                row["kind"] != "interleaved-comparison" or row["formal"] != "1" or
                Path(row["result_dir"]).resolve() != Path(job["dest"]).resolve() or
                json.loads(row["note"]) != [job["status"]]):
            raise ValueError("summary status/identity does not match this matrix")
    comparisons = read_csv(summary / "paired-summary.csv")
    if (primary_environment(comparisons) != "within_noise" or
            not any(not row["candidate"].startswith("zswap-crosscheck")
                    for row in comparisons) or
            any(row["environment"] != "within_noise" for row in comparisons
                if not row["candidate"].startswith("zswap-crosscheck"))):
        raise ValueError("matrix-wide environment gate is not within noise")
    rows = {}
    for row in read_csv(summary / "summary.csv"):
        key = row["workload_id"], row["group_id"], row["metric"]
        if key in rows:
            raise ValueError("duplicate workload/group/metric in summary.csv")
        rows[key] = row
    return metadata, rows


def cell(rows, workload, group, metric, unit):
    row = rows.get((workload, group, metric))
    if row is None:
        return None, None
    n = int(row["n_valid"])
    expected = 5 if workload.startswith(("w3", "w4")) else 10
    if (int(row["n_expected"]) != expected or not 0 <= n <= expected or
            row["unit"] != unit):
        raise ValueError("unexpected metric population or unit")
    if n == 0:
        return None, 0
    values = tuple(float(row[key]) for key in ("q1", "median", "q3"))
    signed_memory = unit == "KiB" and metric == W1_PHYSICAL_CONSUMED
    if (not all(math.isfinite(value) for value in values) or
            not values[0] <= values[1] <= values[2] or
            not signed_memory and values[0] < 0):
        raise ValueError("invalid metric quartiles")
    if unit == "ns" and values[0] <= 0:
        raise ValueError("nonpositive latency quartile")
    return values, n


def pageout_columns(mode):
    """Always present both denominators, reclaimed fraction and shared memory."""
    if mode not in {"seq", "rand", "rewrite"}:
        raise ValueError("unknown W1 pageout mode")
    return (
        (mode + ".pageout_attempt_ns", "ns", 1000,
         "Timed PAGEOUT / requested pages", "ns/requested page (shown as us)"),
        (mode + ".pageout_ns", "ns", 1000,
         "Timed PAGEOUT / first nonpresent pages", "ns/first-nonpresent page (shown as us)"),
        (mode + ".first_pass_nonpresent_fraction", "fraction", 1,
         "First-pass nonpresent fraction", "First nonpresent / requested"),
        (W1_PHYSICAL_CONSUMED, "KiB", 1024,
         "Shared seq MemFree+PCP consumption", "MiB consumed; negative means free memory increased"),
    )


def pageout_cells(rows, workload, group, mode):
    columns = pageout_columns(mode)
    present = [(workload, group, metric) in rows for metric, *_ in columns]
    if any(present) and not all(present):
        raise ValueError("W1 pageout requires both timings, nonpresent fraction and MemFree+PCP together")
    return [cell(rows, workload, group, metric, unit) for metric, unit, *_ in columns]


def panel(ax, rows, workload, metric, unit, scale, title, xlabel, log=False):
    groups = tuple((group, label) for group, label in GROUPS if not workload.startswith("w4") or group in
                   {"memcomp-swap-zstd", "zram-writeback-zstd", "zswap-zstd"})
    expected = 5 if workload.startswith(("w3", "w4")) else 10
    for y, (group, _) in enumerate(groups):
        values, n = cell(rows, workload, group, metric, unit)
        if values is None:
            label = "Missing" if n is None else "N/A (see CSV)"
            ax.text(.03, y, label, transform=ax.get_yaxis_transform(),
                    va="center", color="#657482", fontsize=9)
        else:
            q1, median, q3 = (value / scale for value in values)
            if log and q1 <= 0:
                raise ValueError("nonpositive latency cannot use a log axis")
            color = "#176b91" if group.startswith("memcomp") else "#657482"
            if group.startswith("zswap-crosscheck"):
                color = "#aa6c16"
            ax.errorbar(median, y, xerr=[[median-q1], [q3-median]],
                        fmt="o", color=color, markersize=4.5, capsize=3,
                        elinewidth=1.6)
        count = f"?/{expected}" if n is None else f"{n}/{expected}"
        if workload.startswith("w4"):
            row = rows.get((workload, group, metric), {})
            count += "; excluded " + row.get("n_latency_excluded", "?")
        ax.text(1.025, y, count, transform=ax.get_yaxis_transform(),
                va="center", fontsize=8.5, color="#42566b")
    ax.set_yticks(range(len(groups)), [label for _, label in groups])
    ax.set_ylim(len(groups)-.4, -.6)
    if log:
        ax.set_xscale("log")
    else:
        from matplotlib.ticker import MaxNLocator
        if unit == "KiB" and metric == W1_PHYSICAL_CONSUMED:
            left, right = ax.get_xlim()
            margin = .025 * max(right - left, 1)
            ax.set_xlim(min(left, 0) - margin, max(right, 0) + margin)
        else:
            right = max(ax.get_xlim()[1], 1)
            ax.set_xlim(-.025 * right, right)
            if unit == "workers":
                ax.xaxis.set_major_locator(MaxNLocator(integer=True, min_n_ticks=1))
            ax.set_xticks([tick for tick in ax.get_xticks() if 0 <= tick <= right])
    ax.set_title(title, loc="left", fontsize=12, weight="bold", pad=10)
    ax.set_xlabel(xlabel, fontsize=10)
    ax.grid(axis="x", color="#dce3e9", linewidth=.7)
    ax.set_axisbelow(True)
    ax.tick_params(axis="both", labelsize=9)
    ax.tick_params(axis="y", length=0)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)


def caption(fig, metadata):
    fig.text(.5, .018,
             "Dot: median across boots; bar: interquartile range; right: valid metric boots. "
             "N/A: no valid metric; Missing: absent row (quality/reason in summary.csv).\n"
             "W1: separate normal/rewrite medians. W3: nonpresent count/fraction beside p50/p99. "
             "W4: full segment, unstable latency excluded.\n"
             "Descriptive distributions, not speedup claims: use paired-summary.csv for "
             "k/n, spread and decisions.\n"
             f"Source: summary.csv; formal v4.6; failed slots: {metadata['failed_boots']}/965. "
             "cache=unsafe: disk reads may hit host page cache.",
             ha="center", va="bottom", fontsize=8.5, color="#42566b", linespacing=1.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True,
                        help="finished formal matrix root")
    parser.add_argument("--summary", type=Path, required=True,
                        help="summarize.py output for that same matrix")
    parser.add_argument("--output-dir", type=Path,
                        default=Path(__file__).parent / "figures")
    parser.add_argument("--preview-dir", type=Path,
                        help="optional PNGs for visual review, not publication data")
    args = parser.parse_args()
    try:
        matrix = args.matrix.resolve()
        for directory in (args.output_dir, args.preview_dir):
            if directory is not None:
                resolved = directory.resolve()
                if resolved == matrix or matrix in resolved.parents:
                    raise ValueError("output must be outside the input matrix")
        metadata, rows = finished_inputs(matrix, args.summary)
        # Validate every plotted cell before creating files or importing pyplot.
        for content in SOURCES:
            for group, _ in GROUPS:
                for metric in ("randrf_ns", "rewriterf_ns"):
                    cell(rows, "w1-corpus-"+content, group, metric, "ns")
                for mode in ("seq", "rand", "rewrite"):
                    pageout_cells(rows, "w1-corpus-" + content, group, mode)
        for metric, unit in (("ack_conditional_p99_ns", "ns"),
                             ("alive_before_cleanup", "workers")):
            for group, _ in GROUPS:
                cell(rows, "w2", group, metric, unit)
        for metric, unit in (("eligible_count", "count"), ("refault_fraction", "fraction"),
                             ("refault_p50_ns", "ns"), ("refault_p99_ns", "ns")):
            for group, _ in GROUPS:
                cell(rows, "w3-anon-512m", group, metric, unit)
        for metric, unit in (("post_writeback_readback_p50_ns", "ns"), ("post_writeback_readback_p99_ns", "ns"),
                             ("write_volume_bytes", "bytes"), ("writeback_stability_wait_ns", "ns")):
            for group in ("memcomp-swap-zstd", "zram-writeback-zstd", "zswap-zstd"):
                cell(rows, "w4-832m", group, metric, unit)
        names = ("w1-random-refault", "w2-switch-and-survival", "w1-rewrite-refault", "w3-anonymous-refault", "w4-writeback-readback") + tuple("w1-pageout-" + mode for mode in ("seq", "rand", "rewrite"))
        paths = [args.output_dir / (name+".svg") for name in names]
        if args.preview_dir:
            paths += [args.preview_dir / (name+".png") for name in names]
        if any(path.exists() for path in paths):
            raise ValueError("output already exists; choose a fresh output directory")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "svg.fonttype": "none",
                         "svg.hashsalt": "memcomp-compare-v46"})
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.preview_dir:
        args.preview_dir.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(4, 2, figsize=(15, 19))
    fig.subplots_adjust(left=.18, right=.945, top=.90, bottom=.14,
                        wspace=.88, hspace=.45)
    fig.suptitle("W1: random-order eligible refaults", fontsize=18, weight="bold")
    for ax, content in zip(axes.flat, SOURCES):
        panel(ax, rows, "w1-corpus-"+content, "randrf_ns", "ns", 1000,
              content+" application", "Per-touch latency (us, log scale)", log=True)
    caption(fig, metadata)
    figures = [fig]

    fig, axes = plt.subplots(1, 2, figsize=(15, 6.5))
    fig.subplots_adjust(left=.18, right=.945, top=.84, bottom=.20, wspace=.88)
    fig.suptitle("W2: switching and worker survival", fontsize=18, weight="bold")
    panel(axes[0], rows, "w2", "ack_conditional_p99_ns", "ns", 1e6,
          "Verified completed switches only", "p99 switch latency (ms)")
    panel(axes[1], rows, "w2", "alive_before_cleanup", "workers", 1,
          "Workers alive before cleanup", "Workers")
    caption(fig, metadata)
    figures.append(fig)
    fig, axes = plt.subplots(4, 2, figsize=(15, 19))
    fig.subplots_adjust(left=.18, right=.90, top=.90, bottom=.14, wspace=.88, hspace=.45)
    fig.suptitle("W1 rewrite: random-to-target overwrite, separate population", fontsize=16, weight="bold")
    for ax, content in zip(axes.flat, SOURCES):
        panel(ax, rows, "w1-corpus-"+content, "rewriterf_ns", "ns", 1000,
              content+" rewritten application", "Per-touch latency (us, log scale)", log=True)
    caption(fig, metadata)
    figures.append(fig)

    fig, axes = plt.subplots(2, 2, figsize=(15, 10))
    fig.subplots_adjust(left=.18, right=.90, top=.90, bottom=.14, wspace=.88, hspace=.45)
    fig.suptitle("W3: C.3 eligible anonymous refaults", fontsize=18, weight="bold")
    for ax, metric, unit, scale, title, xlabel in zip(axes.flat,
            ("refault_p50_ns", "refault_p99_ns", "eligible_count", "refault_fraction"),
            ("ns", "ns", "count", "fraction"), (1000, 1000, 1, 1),
            ("Eligible refault p50", "Eligible refault p99", "Eligible refault count", "Eligible refault / all first touches"),
            ("Latency (us, log scale)", "Latency (us, log scale)", "Touches", "Fraction")):
        panel(ax, rows, "w3-anon-512m", metric, unit, scale, title, xlabel, log=unit == "ns")
    caption(fig, metadata)
    figures.append(fig)

    fig, axes = plt.subplots(2, 2, figsize=(15, 10))
    fig.subplots_adjust(left=.20, right=.87, top=.90, bottom=.14, wspace=1.25, hspace=.45)
    fig.suptitle("W4: whole-segment post-stable-writeback readback", fontsize=17, weight="bold")
    for ax, metric, unit, scale, title, xlabel in zip(axes.flat,
            ("post_writeback_readback_p50_ns", "post_writeback_readback_p99_ns", "write_volume_bytes", "writeback_stability_wait_ns"),
            ("ns", "ns", "bytes", "ns"), (1000, 1000, 2**20, 1e9),
            ("Full-segment readback p50", "Full-segment readback p99", "Common swap-device write volume", "Stability wait (policy; not ranked)"),
            ("Latency (us, log scale)", "Latency (us, log scale)", "MiB: pressure_begin to read_begin", "Seconds; timeout waits retained")):
        panel(ax, rows, "w4-832m", metric, unit, scale, title, xlabel,
              log=metric.startswith("post_writeback_readback"))
    caption(fig, metadata)
    figures.append(fig)

    # Each real application retains both PAGEOUT denominators and memory context.
    for mode in ("seq", "rand", "rewrite"):
        fig, axes = plt.subplots(8, 4, figsize=(27, 36))
        fig.subplots_adjust(left=.10, right=.965, top=.92, bottom=.19,
                            wspace=1.12, hspace=.62)
        fig.suptitle("W1 " + mode + ": both PAGEOUT denominators and memory context",
                     fontsize=18, weight="bold")
        for row_axes, content in zip(axes, SOURCES):
            for ax, (metric, unit, scale, title, xlabel) in zip(row_axes, pageout_columns(mode)):
                panel(ax, rows, "w1-corpus-" + content, metric, unit, scale,
                      content.capitalize() + ": " + title, xlabel)
        fig.text(.5, .112,
                 "Primary: memcomp-noswap vs zram uses requested-page time; "
                 "memcomp-swap vs zswap uses first-nonpresent time. Both get E.4 tests.\n"
                 "zswap writes incompressible pages in the timed call; memcomp-swap may write them in a later untimed retry.\n"
                 "No first-pass reclamation means undefined per-reclaimed time, not zero. "
                 "zram storing raw incompressible pages is not memory saving.\n"
                 "Memory is the shared seq fill-to-drained MemFree+PCP window, not a rand/rewrite-specific measurement.",
                 ha="center", fontsize=9, linespacing=1.5)
        caption(fig, metadata)
        figures.append(fig)

    for name, figure in zip(names, figures):
        figure.savefig(args.output_dir / (name+".svg"),
                       metadata={"Date": None, "Creator": "memcomp plot_compare.py"})
        if args.preview_dir:
            figure.savefig(args.preview_dir / (name+".png"), dpi=120)
        plt.close(figure)


if __name__ == "__main__":
    main()
