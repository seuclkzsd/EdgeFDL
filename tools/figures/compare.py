# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""Generate docs/images/compare.png and the tables of docs/comparison.md from results/compare.json.

    python tools/figures/compare.py                # writes docs/images/compare.png
    python tools/figures/compare.py --tables       # also prints the Markdown tables for docs/comparison.md

Three panels share one row per method (tuned parameters; "+ EMS rules" adds the LSDB and bandwidth rules; the regression
setting that validation cannot separate from the selected one; EdgeFDL with and without deep probes): link + type
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
CONDS = (("standard", "Standard"), ("icmp_limit_5pct_new", "ICMP 5%\nnew"), ("icmp_limit_5pct_known", "ICMP 5%\nknown"),
         ("transient_15s", "15 s\ntransients"), ("background_jitter_1ms", "Jitter\n1 ms"),
         ("busy_hour_drift_1ms", "Busy-hour\ndrift"))
LOW = tuple((k.replace(":tuned", ":lowfa"), n + ", low-FA point") for k, n in ROWS if k.endswith(":tuned"))
NO_DEEP = ("edgefdl_no_deep", "EdgeFDL without deep probes")
TIE_F1 = 0.001       # validation F1 gap below which two settings count as not separated


def lam_rows(r, tied_only=False):
    """Regression + EMS rules settings that differ from the selected one in lambda only."""
    top = r["tuning"]["netbouncer+ems"]["val_f1"]
    return tuple((k, "NetBouncer + EMS rules, λ = " + k.split(":lam")[1]) for k, v in sorted(r.get("sensitivity", {}).items())
                 if not tied_only or top - v["val_f1"] < TIE_F1)


def cell(ax, m, labels, fmt, norm, cmap, sep, sep2):
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
    ax.axhline(sep2, color=INK2, lw=1)
    for s in ax.spines.values():
        s.set_visible(False)


def figure(r, out):
    names = [(k, v) for k, v in ROWS[:-1] + lam_rows(r, True) + (ROWS[-1], NO_DEEP) if k in r["single"]]
    acc = [[(r["by_type"][k]["standard"].get(t) or {}).get("link_type_pct") for t, _ in TYPES] for k, _ in names]
    cnd = [[(r["single"][k].get(c) or {}).get("link_type_pct") for c, _ in CONDS] for k, _ in names]
    fa = [[(r["false_alarms"].get(k, {}).get(c) or {}).get("per_day") for c, _ in CONDS] for k, _ in names]
    sep = sum(":tuned" in k and "+ems" not in k or k.startswith("threshold") for k, _ in names)
    sep2 = [k for k, _ in names].index("edgefdl")
    fig, axes = plt.subplots(1, 3, figsize=(13.6, 0.39 * len(names)), gridspec_kw=dict(width_ratios=[6, 6, 6], wspace=.05))
    pc = lambda v: f"{v:.1f}" if v < 100 else "100"
    n01 = Normalize(0, 100)
    cell(axes[0], acc, [x for _, x in TYPES], pc, n01, BLUES, sep, sep2)
    cell(axes[1], cnd, [x for _, x in CONDS], pc, n01, BLUES, sep, sep2)
    hi = max(max(v for row in fa for v in row if v is not None), 10)
    fnorm = LogNorm(0.2, hi, clip=True)
    cell(axes[2], fa, [x for _, x in CONDS], lambda v: "0" if v == 0 else f"{v:.0f}" if v >= 10 else f"{v:.1f}",
         lambda v: 0.0 if v == 0 else fnorm(v), ORANGES, sep, sep2)
    axes[0].set_yticks(np.arange(len(names)) + .5)
    axes[0].set_yticklabels([x for _, x in names], fontsize=9, color=INK)
    axes[0].get_yticklabels()[sep2].set_fontweight("bold")
    for ax in axes[1:]:
        ax.set_yticks([])
    for ax, t in zip(axes, ("Link + type correct, % by fault type (standard)", "Link + type correct, % by condition",
                            "False alarms per simulated day")):
        ax.set_xlabel(t, fontsize=9.5, color=INK, labelpad=6)
    fig.savefig(out, dpi=170, bbox_inches="tight", facecolor=SURFACE)


def fmt(v, p=1):
    return "n/a" if v is None else f"{v:.{p}f}"


def rows_for(r, extra=(NO_DEEP,)):
    """Table rows: the low-FA point appears only where it differs from the F1-tuned setting."""
    low = []
    for k, n in LOW:
        key = k.replace(":lowfa", "")
        if key in r.get("tuning_low_fa", {}) and r["tuning_low_fa"][key]["params"] != r["tuning"][key]["params"]:
            low.append((k, n))
    return ROWS[:-1] + tuple(low) + lam_rows(r) + ROWS[-1:] + extra


def overall(r):
    """All faults over the six conditions, one line per method."""
    ov = r["overall"]
    print("| Method | Correct | Correct % | Time to first alarm, s | False alarms |")
    print("|---|---|---|---|---|")
    for k, n in rows_for(r):
        a = ov.get(k)
        if a and not k.startswith("threshold"):
            print(f"| {n} | {a['correct']:,} of {a['faults']:,} | {fmt(a['link_type_pct'])} | {fmt(a['time_to_alarm_mean_s'])} | "
                  f"{a['false_alarms']} |")


def settings(r):
    """Selected settings and the keys that sit on an edge of their grid."""
    print("| Method and variant | Selected setting | Validation F1 | Validation false alarms per day | On a grid edge |")
    print("|---|---|---|---|---|")
    show = lambda p: ", ".join(f"{k} = {v}" for k, v in p.items())
    edges = lambda e: ", ".join(f"{k} ({v})" for k, v in e.items()) or "none"
    for k, t in r["tuning"].items():
        print(f"| {k} | {show(t['params'])} | {t['val_f1']:.5f} | {t['val_fa_per_day']:.2f} | {edges(t['grid_edges'])} |")
        lo = r["tuning_low_fa"].get(k)
        if lo and lo["params"] != t["params"]:
            print(f"| {k}, low-FA point | {show(lo['params'])} | {lo['val_f1']:.5f} | {lo['val_fa_per_day']:.2f} | "
                  f"{edges(lo['grid_edges'])} |")
    for k, t in sorted(r["sensitivity"].items()):
        print(f"| {k.split(':')[0]}, λ = {k.split(':lam')[1]} | {show(t['params'])} | {t['val_f1']:.5f} | "
              f"{t['val_fa_per_day']:.2f} | n/a |")


def paired(r):
    """Paired gap in link + type correct over all single faults, with the scenario-resampling interval."""
    names = dict(rows_for(r))
    print("| Compared | Only the first correct | Only the second correct | Gap, points | 95% interval, points |")
    print("|---|---|---|---|---|")
    for p in r["paired"]:
        print(f"| {names[p['a']]} vs {names[p['b']]} | {p['only_a']} | {p['only_b']} | {p['gap_points']:+.2f} | "
              f"{p['ci95'][0]:+.2f} to {p['ci95'][1]:+.2f} |")


def probes(r):
    """How often EdgeFDL uses deep probes, by condition."""
    d = r["edgefdl_deep_probes_per_fault"]
    print("| Condition | Deep probes per fault | Of those, alarm-clearing rechecks | Faults with a diagnosis probe % | "
          "Deep probes per normal day |")
    print("|---|---|---|---|---|")
    for c, n in CONDS:
        if c not in d["by_condition"]:
            continue
        a = d["by_condition"][c]
        print(f"| {n.replace(chr(10), ' ')} | {a['per_fault']:.2f} | {a['rechecks_per_fault']:.2f} | "
              f"{fmt(a['faults_with_diagnosis_probe_pct'])} | {d['normal_run_per_day'][c]:.1f} |")
    a = d["all_conditions"]
    print(f"| All six | {a['per_fault']:.2f} | {a['rechecks_per_fault']:.2f} | {fmt(a['faults_with_diagnosis_probe_pct'])} | "
          f"{sum(d['normal_run_per_day'].values()) / len(CONDS):.1f} |")


def tables(r):
    overall(r)
    print()
    settings(r)
    print()
    paired(r)
    print()
    probes(r)
    print()
    print("| Method | Detected % | Link % | Link + type % | Link in any alarm % | Alarms per fault, mean / max | "
          "Time to first alarm, s | False alarms per day |")
    print("|---|---|---|---|---|---|---|---|")
    for k, n in rows_for(r):
        a = r["single"].get(k, {}).get("standard")
        if not a:
            continue
        fa = r["false_alarms"].get(k, {}).get("standard", {}).get("per_day")
        print(f"| {n} | {fmt(a['detected_pct'])} | {fmt(a['link_pct'])} | {fmt(a['link_type_pct'])} | "
              f"{fmt(a['all_link_pct'])} | {a['alarms_mean']:.2f} / {a['alarms_max']} | "
              f"{fmt(a['time_to_alarm_mean_s'])} | {fmt(fa, 2)} |")
    print()
    print("| Method, default parameters | Link % | Link + type % | Alarms per fault, mean / max | Time to first alarm, s | "
          "False alarms per day | 15 s transients: link + type % / false alarms per day |")
    print("|---|---|---|---|---|---|---|")
    for k, n in ((k.replace(":tuned", ":default"), x) for k, x in ROWS if k.endswith(":tuned") and k != "threshold:tuned"):
        a = r["single"][k]["standard"]
        b = r["single"][k]["transient_15s"]
        fa = r["false_alarms"][k]
        print(f"| {n} | {fmt(a['link_pct'])} | {fmt(a['link_type_pct'])} | {a['alarms_mean']:.2f} / {a['alarms_max']} | "
              f"{fmt(a['time_to_alarm_mean_s'])} | {fmt(fa['standard']['per_day'])} | "
              f"{fmt(b['link_type_pct'])} / {fmt(fa['transient_15s']['per_day'])} |")
    print()
    print("| Method | " + " | ".join(x.replace("\n", " ") for _, x in TYPES) + " |")
    print("|---|" + "---|" * len(TYPES))
    for k, n in rows_for(r):
        if k not in r["by_type"] or k == "threshold:default":
            continue
        print(f"| {n} | " + " | ".join(fmt((r["by_type"][k]["standard"].get(t) or {}).get("time_to_alarm_mean_s"), 0)
                                       for t, _ in TYPES) + " |")
    print()
    print("| Method | " + " | ".join(x.replace("\n", " ") for _, x in TYPES) + " | L3 (all types) |")
    print("|---|" + "---|" * (len(TYPES) + 1))
    for k, n in rows_for(r):
        if k not in r["by_type"]:
            continue
        row = [fmt((r["by_type"][k]["standard"].get(t) or {}).get("link_type_pct")) for t, _ in TYPES]
        row.append(fmt((r["by_link"][k]["standard"].get("L3") or {}).get("link_type_pct")))
        print(f"| {n} | " + " | ".join(row) + " |")
    print()
    print("| Method | " + " | ".join(x.replace("\n", " ") for _, x in CONDS) + " |")
    print("|---|" + "---|" * len(CONDS))
    for k, n in rows_for(r):
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
    for k, n in rows_for(r):
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
