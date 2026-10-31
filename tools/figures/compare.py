# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""Generate docs/images/compare.png and the tables of docs/comparison.md from results/compare.json.

    python tools/figures/compare.py                # writes docs/images/compare.png
    python tools/figures/compare.py --tables       # also prints the Markdown tables for docs/comparison.md

Three panels share one row per method (tuned parameters; "+ EMS rules" adds the LSDB and bandwidth rules): link + type
accuracy on the 84 single-fault scenarios by fault type under standard conditions, the same accuracy by test
condition, and false alarms per simulated day by test condition. Needs matplotlib; does not import PyTorch.
"""
import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, LogNorm, Normalize  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
SURFACE = "#ffffff"
NA = "#f0efec"
BLUES = LinearSegmentedColormap.from_list("blues", ["#eef4fc", "#86b6ef", "#2a78d6", "#123f77"])
ORANGES = LinearSegmentedColormap.from_list("oranges", ["#fdeee8", "#f4a988", "#eb6834", "#8f3412"])

ROWS = (("threshold:default", "Threshold, Zabbix ICMP defaults"), ("threshold:tuned", "Threshold, tuned"),
        ("grouped:tuned", "Alarm grouping"), ("scfs:tuned", "Boolean tomography (SCFS)"),
        ("score:tuned", "Greedy set cover (SCORE)"), ("netbouncer:tuned", "NetBouncer-style regression"),
        ("vote007:tuned", "007-style voting"),
        ("grouped+ems:tuned", "Alarm grouping + EMS rules"), ("scfs+ems:tuned", "SCFS + EMS rules"),
        ("score+ems:tuned", "SCORE + EMS rules"), ("netbouncer+ems:tuned", "NetBouncer + EMS rules"),
        ("vote007+ems:tuned", "007 + EMS rules"), ("edgefdl", "EdgeFDL"))
TYPES = (("link_down", "Link\ndown"), ("delay", "Delay"), ("loss", "Loss"), ("jitter", "Jitter"), ("bandwidth", "Band-\nwidth"),
         ("route_drift", "Route\ndrift"))
CONDS = (("standard", "Standard"), ("icmp_limit_5pct_new", "ICMP limit\nnew"), ("icmp_limit_5pct_known", "ICMP limit\nknown"),
         ("transient_15s", "15 s\ntransients"), ("background_jitter_1ms", "Jitter\n1 ms"),
         ("busy_hour_drift_1ms", "Busy-hour\ndrift"))


def cell(ax, m, labels, fmt, norm, cmap, sep):
    """One heatmap panel: None cells stay neutral and read n/a, every cell carries its value."""
    for i, row in enumerate(m):
        for j, v in enumerate(row):
            if v is None:
                ax.add_patch(plt.Rectangle((j, i), 1, 1, fc=NA, ec=SURFACE, lw=1.5))
                ax.text(j + .5, i + .5, "n/a", ha="center", va="center", fontsize=7.5, color=MUTED)
                continue
            c = cmap(norm(v))
            ax.add_patch(plt.Rectangle((j, i), 1, 1, fc=c, ec=SURFACE, lw=1.5))
            lum = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
            ax.text(j + .5, i + .5, fmt(v), ha="center", va="center", fontsize=8,
                    color=SURFACE if lum < .5 else INK)
    ax.set_xlim(0, len(labels))
    ax.set_ylim(len(m), 0)
    ax.set_xticks(np.arange(len(labels)) + .5)
    ax.set_xticklabels(labels, fontsize=8, color=INK2)
    ax.xaxis.tick_top()
    ax.tick_params(length=0)
    ax.axhline(sep, color=INK2, lw=1)
    ax.axhline(len(m) - 1, color=INK2, lw=1)
    for s in ax.spines.values():
        s.set_visible(False)


def figure(r, out):
    names = [(k, v) for k, v in ROWS if k in r["single"]]
    acc = [[(r["by_type"][k]["standard"].get(t) or {}).get("link_type_pct") for t, _ in TYPES] for k, _ in names]
    cnd = [[(r["single"][k].get(c) or {}).get("link_type_pct") for c, _ in CONDS] for k, _ in names]
    fa = [[(r["false_alarms"].get(k, {}).get(c) or {}).get("per_day") for c, _ in CONDS] for k, _ in names]
    sep = sum(":tuned" in k and "+ems" not in k or k.startswith("threshold") for k, _ in names)
    fig, axes = plt.subplots(1, 3, figsize=(13.6, 5.1), gridspec_kw=dict(width_ratios=[6, 6, 6], wspace=.05))
    pc = lambda v: f"{v:.1f}" if v < 100 else "100"
    n01 = Normalize(0, 100)
    cell(axes[0], acc, [x for _, x in TYPES], pc, n01, BLUES, sep)
    cell(axes[1], cnd, [x for _, x in CONDS], pc, n01, BLUES, sep)
    hi = max(max(v for row in fa for v in row if v is not None), 10)
    fnorm = LogNorm(0.2, hi, clip=True)
    cell(axes[2], fa, [x for _, x in CONDS], lambda v: "0" if v == 0 else f"{v:.0f}" if v >= 10 else f"{v:.1f}",
         lambda v: 0.0 if v == 0 else fnorm(v), ORANGES, sep)
    axes[0].set_yticks(np.arange(len(names)) + .5)
    axes[0].set_yticklabels([x for _, x in names], fontsize=9, color=INK)
    axes[0].get_yticklabels()[-1].set_fontweight("bold")
    for ax in axes[1:]:
        ax.set_yticks([])
    for ax, t in zip(axes, ("Link + type correct, % of 84 faults (standard)", "Link + type correct, % of 84 faults",
                            "False alarms per simulated day")):
        ax.set_xlabel(t, fontsize=9.5, color=INK, labelpad=6)
    fig.savefig(out, dpi=170, bbox_inches="tight", facecolor=SURFACE)


def fmt(v, p=1):
    return "n/a" if v is None else f"{v:.{p}f}"


def tables(r):
    print("| Method | Detected % | Link % | Link + type % | Alarms per fault, mean / max | Time to first alarm, s | "
          "False alarms per day |")
    print("|---|---|---|---|---|---|---|")
    for k, n in ROWS + (("edgefdl_no_deep", "EdgeFDL without deep probes"),):
        a = r["single"].get(k, {}).get("standard")
        if not a:
            continue
        fa = r["false_alarms"].get(k, {}).get("standard", {}).get("per_day")
        print(f"| {n} | {fmt(a['detected_pct'])} | {fmt(a['link_pct'])} | {fmt(a['link_type_pct'])} | "
              f"{a['alarms_mean']:.2f} / {a['alarms_max']} | {fmt(a['time_to_alarm_mean_s'])} | {fmt(fa, 2)} |")
    print()
    print("| Method | " + " | ".join(x.replace("\n", " ") for _, x in TYPES) + " | L3 (all types) |")
    print("|---|" + "---|" * (len(TYPES) + 1))
    for k, n in ROWS:
        if k not in r["by_type"]:
            continue
        row = [fmt((r["by_type"][k]["standard"].get(t) or {}).get("link_type_pct")) for t, _ in TYPES]
        row.append(fmt((r["by_link"][k]["standard"].get("L3") or {}).get("link_type_pct")))
        print(f"| {n} | " + " | ".join(row) + " |")
    print()
    print("| Method | " + " | ".join(x.replace("\n", " ") for _, x in CONDS) + " |")
    print("|---|" + "---|" * len(CONDS))
    for k, n in ROWS:
        if k not in r["single"]:
            continue
        row = []
        for c, _ in CONDS:
            a = r["single"][k].get(c) or {}
            fa = r["false_alarms"].get(k, {}).get(c, {}).get("per_day")
            row.append(f"{fmt(a.get('link_type_pct'))} / {fmt(fa)}")
        print(f"| {n} | " + " | ".join(row) + " |")
    print()
    print("| Method | Dual: first correct % | Dual: both links % | Dual: both + types % | Dual: alarms mean / max | "
          "Dual: wrong alarms | Device: named % | Device: alarms mean / max |")
    print("|---|---|---|---|---|---|---|---|")
    for k, n in ROWS + (("edgefdl_no_deep", "EdgeFDL without deep probes"),):
        d, g = r["dual"].get(k) or {}, r["node"].get(k) or {}
        if not d:
            continue
        print(f"| {n} | {fmt(d.get('link_type_pct'))} | {fmt(d.get('all_link_pct'))} | {fmt(d.get('all_type_pct'))} | "
              f"{d['alarms_mean']:.2f} / {d['alarms_max']} | {d['wrong_alarms_mean']:.2f} | "
              f"{fmt(g.get('all_type_pct'))} | " + (f"{g['alarms_mean']:.2f} / {g['alarms_max']} |" if g else "n/a |"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default=str(ROOT / "results" / "compare.json"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "images" / "compare.png"))
    ap.add_argument("--tables", action="store_true")
    a = ap.parse_args()
    with open(a.data, encoding="utf-8") as f:
        r = json.load(f)
    figure(r, a.out)
    if a.tables:
        tables(r)


if __name__ == "__main__":
    main()
