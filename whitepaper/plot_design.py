#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Draw where memcomp stores a reclaimed page and how a fault restores it."""
import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

INK, INK2, SURFACE = "#0b0b0b", "#52514e", "#fcfcfb"
EDGE, ARROW = "#898781", "#52514e"
PAGE, RAM, SWAP = "#e6eef9", "#e2f3ec", "#fcebe2"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).parent / "figures/memcomp-paths.svg")
    parser.add_argument("--preview", type=Path, help="optional review-only PNG")
    args = parser.parse_args()
    plt.rcParams.update({"font.family": "DejaVu Sans", "svg.fonttype": "none",
                         "svg.hashsalt": "memcomp-backing-paths-v1"})
    fig, ax = plt.subplots(figsize=(12, 7))
    fig.patch.set_facecolor(SURFACE)
    fig.subplots_adjust(left=.02, right=.98, top=.97, bottom=.03)
    ax.set(xlim=(0, 1), ylim=(0, 1))
    ax.axis("off")
    ax.text(.5, .985, "memcomp: where a reclaimed page goes", ha="center",
            va="top", size=18, weight="bold", color=INK)

    def box(x, y, width, height, title, detail, color):
        ax.add_patch(FancyBboxPatch((x - width / 2, y - height / 2), width, height,
                                   boxstyle="round,pad=0.012,rounding_size=0.018",
                                   linewidth=1.2, edgecolor=EDGE, facecolor=color))
        ax.text(x, y + .022, title, ha="center", va="center", size=12,
                weight="bold", color=INK)
        ax.text(x, y - .026, detail, ha="center", va="center", size=10.2,
                color=INK2, linespacing=1.3)

    def arrow(start, end, label=None, label_xy=None, curve=0):
        ax.add_patch(FancyArrowPatch(start, end, arrowstyle="-|>", mutation_scale=14,
                                    linewidth=1.4, color=ARROW,
                                    connectionstyle=f"arc3,rad={curve}"))
        if label:
            ax.text(*label_xy, label, ha="center", va="center", size=10,
                    color=INK2, linespacing=1.3,
                    bbox=dict(facecolor=SURFACE, edgecolor="none", pad=2.0))

    box(.13, .66, .22, .13, "Anonymous or shmem page", "mapped, in RAM", PAGE)
    box(.52, .66, .28, .13, "memcomp token",
        "in the PTE or shmem page cache;\nnames a 16-byte slot", PAGE)
    box(.87, .66, .22, .13, "Swap device", "optional; raw or packed slots", SWAP)
    arrow((.245, .685), (.375, .685), "reclaim", (.31, .715))
    arrow((.375, .635), (.245, .635), "store fails:\nmapping restored",
          (.31, .575))
    arrow((.13, .73), (.87, .73),
          "incompressible page is marked; the next reclaim sends it to "
          "ordinary swap, if configured", (.50, .835), curve=-.28)

    box(.17, .40, .26, .13, "Same-filled page", "repeated word in the slot", RAM)
    box(.52, .40, .26, .13, "Compressed in RAM", "stock zsmalloc", RAM)
    arrow((.48, .595), (.22, .47), "same-filled", (.36, .50), curve=.06)
    arrow((.56, .595), (.56, .47), "compress", (.56, .535))
    arrow((.655, .42), (.83, .595), "writeback", (.74, .49))

    box(.52, .12, .34, .13, "Page faulted back in", "same mapping, same contents",
        PAGE)
    arrow((.17, .335), (.35, .13), "fill", (.21, .21), curve=.12)
    arrow((.52, .335), (.52, .185), "decompress", (.52, .26))
    arrow((.87, .595), (.69, .13), "read; decompress\nif packed", (.85, .30),
          curve=-.08)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, format="svg", facecolor=SURFACE,
                metadata={"Date": None, "Creator": "memcomp design figure"})
    # Matplotlib path attributes contain insignificant trailing spaces.
    args.output.write_text("\n".join(line.rstrip() for line in
                           args.output.read_text().splitlines()) + "\n")
    if args.preview:
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(args.preview, dpi=110, facecolor=SURFACE,
                    metadata={"Software": "memcomp design figure"})
    plt.close(fig)


if __name__ == "__main__":
    main()
