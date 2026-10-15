#!/usr/bin/env python3
"""
TOCTOU detection rate against tau/SSP, fixed and exponential measurement delay.

Reads the CSVs of run_experiments_toctou.py (one row per tamper-run-restore
trial) and plots, for each SSP and mode, the share of detected attacks with its
95 % Wilson interval. Theory: 1 - exp(-tau/SSP) for the exponential delay, and
for the fixed delay, nothing before tau = SSP and everything after.
"""

import csv, math
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import plot_style

SCRIPT_DIR = Path(__file__).parent
DATA_DIR   = SCRIPT_DIR.parent / "data" / "toctou" / "results"
OUT_PDF    = SCRIPT_DIR / "lineplot_toctou_detection.pdf"

FONT_SCALE = 1.6
C_MODE  = {"fixed": "#C97A2B", "exp": "#2E72B8"}   # orange, blue
LABEL   = {"fixed": "fixed delay", "exp": "exponential delay"}
MARKERS = ["o", "s", "^", "D"]                     # one per SSP
DODGE   = 1.08                                     # x shift between SSPs, on the log axis

plot_style.apply(FONT_SCALE)


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def load():
    """{(ssp_ms, mode): {ratio: [detected, ...]}}, complete trials only."""
    out = defaultdict(lambda: defaultdict(list))
    files = sorted(DATA_DIR.glob("SSP*ms-*.csv"))
    if not files:
        raise SystemExit(f"no CSV in {DATA_DIR}")
    for f in files:
        with f.open() as fh:
            for row in csv.DictReader(fh):
                if row["complete"] == "1":
                    out[(int(row["ssp_ms"]), row["mode"])][float(row["ratio"])].append(int(row["detected"]))
    return out


def main():
    data = load()
    ssps = sorted({s for s, _ in data})
    fig, ax = plt.subplots(figsize=(8, 5))

    x = np.logspace(np.log10(0.03), np.log10(3), 200)
    ax.plot(x, 1 - np.exp(-x), color=C_MODE["exp"], lw=1.2, ls="--", label="theory, exponential")
    ax.plot([0.03, 1, 1, 3], [0, 0, 1, 1], color=C_MODE["fixed"], lw=1.2, ls="--", label="theory, fixed")

    for (ssp, mode), by_ratio in sorted(data.items()):
        r = sorted(by_ratio)
        n = [len(by_ratio[q]) for q in r]
        k = [sum(by_ratio[q]) for q in r]
        rate = [a / b for a, b in zip(k, n)]
        lo, hi = zip(*(wilson(a, b) for a, b in zip(k, n)))
        shift = DODGE ** (ssps.index(ssp) - (len(ssps) - 1) / 2)
        ax.errorbar([q * shift for q in r], rate, yerr=[np.subtract(rate, lo), np.subtract(hi, rate)], color=C_MODE[mode],
                    marker=MARKERS[ssps.index(ssp)], ms=7, lw=0, elinewidth=1.2, capsize=3,
                    label=f"{LABEL[mode]}, SSP {ssp / 1000:g} s (n = {min(n)} per point)")

    ax.set_xscale("log")
    ax.set_xlim(0.03, 3)
    ax.set_ylim(-0.03, 1.03)
    ax.set_xlabel(r"Attack duration $\tau$ / SSP")
    ax.set_ylabel("Detected attacks")
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.grid(axis="both", **plot_style.GRID_KW)
    ax.tick_params(axis="both", **plot_style.TICK_KW)
    ax.legend(fontsize="x-small", loc="upper left", frameon=False)
    fig.tight_layout()
    fig.savefig(OUT_PDF)
    print(f"Saved: {OUT_PDF}")


if __name__ == "__main__":
    main()
