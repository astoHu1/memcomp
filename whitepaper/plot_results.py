#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Draw the formal 965-job figures and tables from published summary files.

Inputs are the post-processing outputs of the formal collection:
E4-presentation-table.csv (paired decisions), paired-summary.csv (paired
medians) and w1-pageout.csv (per-boot W1 memory observations). Decisions are
taken verbatim from the presentation table; nothing is re-judged here. A
within-noise row whose pairs meet the consistency condition of the paired
rule is drawn in its direction's color, hollow; its decision is unchanged.
"""
import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.legend_handler import HandlerTuple
from matplotlib.lines import Line2D


PAIRS = (
    ("memcomp-noswap-lz4", "zram-lz4", "memcomp, no swap\nvs zram (lz4)"),
    ("memcomp-noswap-zstd", "zram-zstd", "memcomp, no swap\nvs zram (zstd)"),
    ("memcomp-noswap-zstd", "zswap-zstd", "memcomp, no swap\nvs zswap (zstd)"),
    ("memcomp-swap-lz4", "zram-lz4", "memcomp + swap\nvs zram (lz4)"),
    ("memcomp-swap-zstd", "zram-zstd", "memcomp + swap\nvs zram (zstd)"),
    ("memcomp-swap-zstd", "zswap-zstd", "memcomp + swap\nvs zswap (zstd)"),
)

W1 = "w1-corpus-geomean"
ROWS = (
    (W1, "seq.pageout_attempt_ns", "W1 sequential pageout, attempt"),
    (W1, "seq.pageout_ns", "W1 sequential pageout, first pass"),
    (W1, "rand.pageout_attempt_ns", "W1 random pageout, attempt"),
    (W1, "rand.pageout_ns", "W1 random pageout, first pass"),
    (W1, "rewrite.pageout_attempt_ns", "W1 rewrite pageout, attempt"),
    (W1, "rewrite.pageout_ns", "W1 rewrite pageout, first pass"),
    (W1, "single.spo_p50_ns", "W1 single-page pageout p50"),
    (W1, "single.spo_p99_ns", "W1 single-page pageout p99"),
    (W1, "seqrf_ns", "W1 sequential refault"),
    (W1, "randrf_ns", "W1 random refault"),
    (W1, "rewriterf_ns", "W1 rewrite refault"),
    (W1, "single.srf_p50_ns", "W1 single-page refault p50"),
    (W1, "single.srf_p99_ns", "W1 single-page refault p99"),
    ("w2", "ack_conditional_p50_ns", "W2 completed switch p50"),
    ("w2", "ack_conditional_p99_ns", "W2 completed switch p99"),
    ("w3-anon-512m", "refault_p50_ns", "W3 anonymous refault p50"),
    ("w3-anon-512m", "refault_p99_ns", "W3 anonymous refault p99"),
)

W4_REFS = (("zswap-zstd", "zswap"), ("zram-writeback-zstd", "zram writeback"))
W4_METRICS = ("post_writeback_readback_p50_ns", "post_writeback_readback_p99_ns",
              "write_volume_bytes")

APPS = ("chrome", "discord", "netease_music", "qq", "system_server",
        "systemui", "telegram", "wechat")
MEMORY_GROUPS = ("zram-lz4", "memcomp-noswap-lz4", "memcomp-swap-lz4",
                 "zram-zstd", "zswap-zstd", "memcomp-noswap-zstd",
                 "memcomp-swap-zstd", "zram-writeback-zstd")

# Reference palette: categorical slots 1-3 (validated all-pairs), chrome inks.
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"
DECISION_STYLE = {
    "lower": dict(marker="o", color=BLUE, mfc=BLUE),
    "higher": dict(marker="o", color=ORANGE, mfc=ORANGE),
    "within_noise": dict(marker="o", color=MUTED, mfc=SURFACE),
}


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def index(rows, what):
    out = {}
    for row in rows:
        if row["candidate"].startswith("zswap-crosscheck"):
            continue
        key = row["workload_id"], row["candidate"], row["reference"], row["metric"]
        if key in out and out[key] != row:
            raise ValueError(f"conflicting {what} rows for {key}")
        out[key] = row
    return out


def lookup(table, workload, candidate, reference, metric):
    try:
        return table[workload, candidate, reference, metric]
    except KeyError:
        raise ValueError(f"missing row {workload} {candidate} {reference} {metric}")


def same_direction(paired):
    """Whether every pair is valid and enough of them point the same way.

    This is the consistency condition of the paired rule alone.  The E.4
    decision also needs the spread condition, so a row that passes here can
    still be within noise; it only gets its direction's color in a figure.
    """
    n = int(paired["n_expected"])
    return (int(paired["n_ratio_valid"]) == n and
            int(paired["consistent_k"]) >= (8 if n == 10 else 5))


def direction(paired):
    return "lower" if float(paired["ratio_median"]) < 1 else "higher"


def verdict(decision, paired):
    if decision != "within_noise":
        return decision.replace("_", " ")
    if same_direction(paired):
        return (f"{direction(paired)} in {paired['consistency']}, "
                "below reference spread")
    return f"no consistent direction ({paired['consistency']})"


def table_direction(paired):
    return direction(paired) if same_direction(paired) else "mixed"


def mark_style(decision, paired):
    style = dict(DECISION_STYLE[decision])
    if decision == "within_noise" and same_direction(paired):
        style["color"] = DECISION_STYLE[direction(paired)]["color"]
    return style


def style_axes(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=INK2, length=0, labelsize=9)


def ratio_figure(decisions, medians, path):
    ticks = (0.5, 1, 2, 5)
    fig, axes = plt.subplots(1, len(PAIRS), figsize=(15, 7.9), sharey=True)
    fig.patch.set_facecolor(SURFACE)
    fig.subplots_adjust(left=.205, right=.99, top=.80, bottom=.16, wspace=.12)
    ys = range(len(ROWS))
    for ax, (cand, ref, title) in zip(axes, PAIRS):
        style_axes(ax)
        ax.set_xscale("log")
        ax.set_xlim(.42, 6.2)
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{t:g}x" for t in ticks])
        ax.minorticks_off()
        for t in ticks:
            ax.axvline(t, color=GRID, lw=.8, zorder=0)
        ax.axvline(1, color=AXIS, lw=1.2, zorder=1)
        for y in ys:
            ax.axhline(y, color=GRID, lw=.6, zorder=0)
        for y, (workload, metric, _) in zip(ys, ROWS):
            row = lookup(decisions, workload, cand, ref, metric)
            paired = lookup(medians, workload, cand, ref, metric)
            decision = row["presentation_decision"]
            if decision not in DECISION_STYLE:
                raise ValueError(f"unexpected decision {decision} for {metric}")
            ax.plot([float(row["ratio_median"])], [y], ms=8.5, mew=1.6,
                    zorder=3, linestyle="none", **mark_style(decision, paired))
        ax.set_title(title, size=10, color=INK, linespacing=1.3, pad=8)
    axes[0].set_yticks(list(ys))
    axes[0].set_yticklabels([label for _, _, label in ROWS], color=INK, size=9.5)
    axes[0].invert_yaxis()
    fig.text(.5, .955, "Paired median ratios, formal 965-job comparison",
             ha="center", size=15, weight="bold", color=INK)
    fig.text(.5, .918, "memcomp time / reference time; below 1x means memcomp "
             "was faster. Log scale. Decisions from the E.4 table.",
             ha="center", size=10, color=INK2)

    def mark(**style):
        return Line2D([], [], linestyle="none", ms=8.5, mew=1.6, **style)

    hollow = DECISION_STYLE["within_noise"]
    handles = [mark(**DECISION_STYLE["lower"]), mark(**DECISION_STYLE["higher"]),
               (mark(**{**hollow, "color": BLUE}),
                mark(**{**hollow, "color": ORANGE})),
               mark(**hollow)]
    labels = ["memcomp faster (E.4: lower)", "memcomp slower (E.4: higher)",
              "faster / slower in 8+ of 10 rounds (W3: 5 of 5), "
              "below reference spread",
              "no consistent direction"]
    fig.legend(handles, labels, loc="lower center", ncol=2, frameon=False,
               fontsize=10, labelcolor=INK, bbox_to_anchor=(.66, .005),
               handler_map={tuple: HandlerTuple(ndivide=None, pad=.6)})
    fig.text(.01, .025, "W1: geometric mean over eight applications, 10 rounds. "
             "W2: 10 rounds. W3: 5 rounds.\nNested virtualization (WSL2 + KVM); "
             "relative comparison only.", size=8.5, color=INK2)
    save(fig, path)


def save(fig, path):
    fig.savefig(path, format="svg", metadata={"Date": None},
                facecolor=SURFACE)
    if PREVIEW_DIR is not None:
        fig.savefig(PREVIEW_DIR / (path.stem + ".png"), dpi=110,
                    facecolor=SURFACE)
    plt.close(fig)


PREVIEW_DIR = None


def w4_values(medians, decisions):
    values = {}
    for ref, _ in W4_REFS:
        for metric in W4_METRICS:
            med = lookup(medians, "w4-832m", "memcomp-swap-zstd", ref, metric)
            dec = lookup(decisions, "w4-832m", "memcomp-swap-zstd", ref, metric)
            values.setdefault(("memcomp-swap-zstd", metric), set()).add(
                float(med["candidate_median"]))
            values[(ref, metric)] = {float(med["reference_median"])}
            values[("decision", ref, metric)] = dec
            values[("paired", ref, metric)] = med
    for key, value in list(values.items()):
        if isinstance(value, set):
            if len(value) != 1:
                raise ValueError(f"inconsistent candidate median for {key}")
            values[key] = value.pop()
    return values


def w4_figure(values, path):
    backends = (("memcomp-swap-zstd", "memcomp + swap", BLUE),
                ("zswap-zstd", "zswap", ORANGE),
                ("zram-writeback-zstd", "zram writeback", AQUA))
    panels = (("post_writeback_readback_p50_ns", "Readback p50 (µs)", 1e3),
              ("post_writeback_readback_p99_ns", "Readback p99 (µs)", 1e3),
              ("write_volume_bytes", "Written before readback (MiB)", 2**20))
    fig, axes = plt.subplots(1, 3, figsize=(12, 4.8))
    fig.patch.set_facecolor(SURFACE)
    fig.subplots_adjust(left=.04, right=.99, top=.76, bottom=.24, wspace=.25)
    for ax, (metric, title, scale) in zip(axes, panels):
        style_axes(ax)
        ax.tick_params(axis="y", labelleft=False)
        heights = [values[(b, metric)] / scale for b, _, _ in backends]
        bars = ax.bar(range(3), heights, width=.55,
                      color=[c for _, _, c in backends], zorder=2)
        top = max(heights)
        ax.set_ylim(0, top * 1.18)
        for bar, h in zip(bars, heights):
            ax.text(bar.get_x() + bar.get_width() / 2, h + top * .025,
                    f"{h:.1f}", ha="center", va="bottom", size=10, color=INK)
        ax.set_xticks(range(3))
        ax.set_xticklabels([label for _, label, _ in backends], size=9.5,
                           color=INK)
        ax.set_title(title, size=11, color=INK, pad=10)
        verdicts = []
        for ref, short in W4_REFS:
            dec = values[("decision", ref, metric)]["presentation_decision"]
            paired = values[("paired", ref, metric)]
            verdicts.append(f"vs {short}: {verdict(dec, paired)}")
        ax.text(.5, -.17, "\n".join(verdicts), transform=ax.transAxes,
                ha="center", va="top", size=8.5, color=INK2, linespacing=1.4)
    fig.text(.5, .94, "W4: readback after writeback, 832 MiB target, zstd",
             ha="center", size=14, weight="bold", color=INK)
    fig.text(.5, .87, "Medians of five paired rounds; decisions from the E.4 "
             "table. Readback timing covers the full target, not only "
             "disk-backed pages.", ha="center", size=9.5, color=INK2)
    save(fig, path)


def fmt_time(ns):
    us = float(ns) / 1e3
    return f"{us:,.0f}" if us >= 1000 else f"{us:.2f}" if us < 100 else f"{us:.1f}"


def timing_table(medians, decisions):
    lines = ["| Measurement | Pair | memcomp (µs) | reference (µs) | ratio | "
             "consistent | direction | E.4 decision |",
             "| --- | --- | ---: | ---: | ---: | ---: | --- | --- |"]
    for workload, metric, label in ROWS:
        for cand, ref, title in PAIRS:
            med = lookup(medians, workload, cand, ref, metric)
            dec = lookup(decisions, workload, cand, ref, metric)
            lines.append(
                f"| {label} | {title.replace(chr(10), ' ')} | "
                f"{fmt_time(med['candidate_median'])} | "
                f"{fmt_time(med['reference_median'])} | "
                f"{float(dec['ratio_median']):.3f} | {dec['consistency']} | "
                f"{table_direction(med)} | {dec['presentation_decision']} |")
    return lines


def w4_table(values):
    lines = ["| Measurement | memcomp + swap | zswap | zram writeback | "
             "vs zswap | vs zram writeback |",
             "| --- | ---: | ---: | ---: | --- | --- |"]
    for metric, label, scale, unit in (
            ("post_writeback_readback_p50_ns", "Readback p50", 1e3, "µs"),
            ("post_writeback_readback_p99_ns", "Readback p99", 1e3, "µs"),
            ("write_volume_bytes", "Written before readback", 2**20, "MiB")):
        cells = [f"{values[(b, metric)] / scale:.1f} {unit}" for b in
                 ("memcomp-swap-zstd", "zswap-zstd", "zram-writeback-zstd")]
        decs = []
        for ref in ("zswap-zstd", "zram-writeback-zstd"):
            dec = values[("decision", ref, metric)]
            decs.append(f"{dec['presentation_decision']} "
                        f"({float(dec['effect_percent']):+.1f}%, "
                        f"{dec['consistency']})")
        lines.append(f"| {label} | " + " | ".join(cells + decs) + " |")
    return lines


def memory_rows(path):
    per_boot = defaultdict(set)
    for row in read_csv(path):
        if (row["run_valid"] not in ("1", "true", "True") or
                row["scope"] != "per-guest-median" or row["mode"] != "seq"):
            continue
        app = row["workload_id"].removeprefix("w1-corpus-")
        if app not in APPS or row["physical_free_consumed_kib"] in ("", None):
            continue
        key = app, row["group_id"], row["host_boot_round"]
        per_boot[key].add(float(row["physical_free_consumed_kib"]))
    medians = defaultdict(dict)
    rounds = defaultdict(list)
    for (app, group, rnd), vals in per_boot.items():
        if len(vals) != 1:
            raise ValueError(f"memory value differs within boot {app} {group} {rnd}")
        rounds[app, group].append(vals.pop())
    for (app, group), vals in rounds.items():
        if len(vals) != 10:
            raise ValueError(f"expected 10 boots for {app} {group}")
        medians[group][app] = statistics.median(vals)
    return medians


def memory_table(medians):
    lines = ["| Application | " + " | ".join(MEMORY_GROUPS) + " |",
             "| --- |" + " ---: |" * len(MEMORY_GROUPS)]
    for app in APPS:
        lines.append(f"| {app} | " + " | ".join(
            f"{medians[g][app] / 1024:.1f}" for g in MEMORY_GROUPS) + " |")
    lines.append("")
    lines.append("| memcomp / reference | min | median | max |")
    lines.append("| --- | ---: | ---: | ---: |")
    for cand, ref in (("memcomp-noswap-lz4", "zram-lz4"),
                      ("memcomp-noswap-zstd", "zram-zstd"),
                      ("memcomp-noswap-zstd", "zswap-zstd"),
                      ("memcomp-swap-zstd", "zswap-zstd")):
        ratios = [medians[cand][a] / medians[ref][a] for a in APPS]
        lines.append(f"| {cand} / {ref} | {min(ratios):.3f} | "
                     f"{statistics.median(ratios):.3f} | {max(ratios):.3f} |")
    return lines


def unfavorable_table(decisions):
    lines = ["| Workload | Metric | Candidate | Reference | ratio | effect | "
             "consistent |", "| --- | --- | --- | --- | ---: | ---: | ---: |"]
    rows = [r for r in decisions.values() if r["presentation_decision"] == "higher"]
    rows.sort(key=lambda r: (r["workload_id"], r["metric"], r["candidate"],
                             r["reference"]))
    for r in rows:
        lines.append(f"| {r['workload_id']} | {r['metric']} | {r['candidate']} | "
                     f"{r['reference']} | {float(r['ratio_median']):.3f} | "
                     f"{float(r['effect_percent']):+.1f}% | {r['consistency']} |")
    return lines, len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True,
                        help="directory with the formal post-processing outputs")
    parser.add_argument("--output-dir", type=Path,
                        default=Path(__file__).parent)
    parser.add_argument("--preview-dir", type=Path,
                        help="optional PNGs for visual review, not publication data")
    args = parser.parse_args()
    global PREVIEW_DIR
    PREVIEW_DIR = args.preview_dir
    plt.rcParams.update({"font.family": "DejaVu Sans", "svg.fonttype": "none",
                         "svg.hashsalt": "memcomp-formal-965-v1"})
    decisions = index(read_csv(args.summary / "E4-presentation-table.csv"),
                      "decision")
    medians = index(read_csv(args.summary / "paired-summary.csv"), "median")
    figures = args.output_dir / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    ratio_figure(decisions, medians, figures / "timing-ratios.svg")
    w4 = w4_values(medians, decisions)
    w4_figure(w4, figures / "w4-writeback-readback.svg")
    unfavorable, count = unfavorable_table(decisions)
    memory = memory_rows(args.summary / "w1-pageout.csv")
    text = ["# Formal comparison tables", "",
            "Generated by whitepaper/plot_results.py from the formal "
            "post-processing outputs. Decisions are copied from "
            "E4-presentation-table.csv; medians are paired medians from "
            "paired-summary.csv.", "",
            "## Timing decisions", "",
            "direction: the way all valid pairs point in at least 8 of 10 "
            "rounds (5 of 5 for W3), or mixed. A row with a direction but "
            "an E.4 decision of within_noise differs by less than the "
            "reference spread.", "",
            *timing_table(medians, decisions), "",
            "## W4 readback after writeback", "", *w4_table(w4), "",
            "## W1 physical memory consumed (MiB, descriptive)", "",
            "Fresh sequential fill-before to post-drain window: MemFree plus "
            "PCP free-page consumption. Median of ten boots per application; "
            "each application loads 64 MiB. Not part of E.4.", "",
            *memory_table(memory), "",
            f"## Every E.4 row where memcomp is higher ({count} rows)", "",
            "Includes counter rows. Lower is better for every listed metric.",
            "", *unfavorable, ""]
    (args.output_dir / "evaluation-tables.md").write_text("\n".join(text),
                                                         encoding="utf-8")


if __name__ == "__main__":
    main()
