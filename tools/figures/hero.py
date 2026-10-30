# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""Generate the README hero figure docs/images/hero.png from the topology and the released results.

    python tools/figures/hero.py                 # writes docs/images/hero.png
    python tools/figures/hero.py --out x.png

Panel (a) draws the demo topology (configs/demo_topology.yaml) with link L1 degraded. The monitored targets whose
baseline round trip from the EMS crosses L1 are computed from the topology's OSPF routes; each of them would cross
a per-target threshold, while EdgeFDL raises one alarm that names L1. Panel (b) plots the type + location accuracy
of the independent test set (results/eval_test.json) against the design targets, plus three figures from the
self-test (results/bench.json). Needs matplotlib and pyyaml; does not import PyTorch.
"""
import argparse
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from edgefdl import topology as T  # noqa: E402

FAULT_LINK = "L1"

# Palette: white surface, reference categorical / status steps validated for a light surface.
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS = "#e1e0d9", "#c3c2b7"
BLUE, BLUE_LIGHT = "#2a78d6", "#86b6ef"
ORANGE = "#eb6834"
CRIT = "#d03b3b"
VIOLET = "#6250d6"
HOST = "#898781"

# Design targets (category -> minimum type + location accuracy).
REQ = {"链路中断": 0.98, "性能劣化": 0.95, "路由漂移": 0.95}


def hop_layout(topo):
    """Same layering as the dashboard (edgefdl/core/api.py): rows by hop count from the EMS, names sorted."""
    level = {topo.vantage: 0}
    frontier = [topo.vantage]
    while frontier:
        nxt = []
        for u in frontier:
            for v, _ in topo._adj[u]:
                if v not in level:
                    level[v] = level[u] + 1
                    nxt.append(v)
        frontier = nxt
    rows = {}
    for n in topo.nodes:
        rows.setdefault(level[n], []).append(n)
    pos = {}
    for lv, ns in rows.items():
        ns.sort()
        for i, n in enumerate(ns):
            pos[n] = ((i + 1) / (len(ns) + 1), lv)
    return pos


def affected_targets(topo, lid):
    """EMS-side targets (no NQA) whose baseline forward or return path crosses link lid."""
    base = topo._base_targets()
    hit = []
    for t in base:
        fwd, rev = topo.route_to_target(topo.vantage, t)
        if lid in topo.path_links(fwd) + topo.path_links(rev):
            hit.append(t.id)
    return base, hit


def draw_topology(ax, topo):
    pos0 = hop_layout(topo)
    W, H = 10.0, 1.55                      # x scale, vertical distance between hop levels
    pos = {n: (x * W, -lv * H) for n, (x, lv) in pos0.items()}
    base, hit = affected_targets(topo, FAULT_LINK)
    hit = set(hit)
    hw, hh = 0.67, 0.24                     # node box half width / half height

    # links
    for l in topo.links:
        (x1, y1), (x2, y2) = pos[l.a], pos[l.b]
        if l.id == FAULT_LINK:
            ax.plot([x1, x2], [y1, y2], color=CRIT, lw=5.0, solid_capstyle="round", zorder=2)
            continue
        ax.plot([x1, x2], [y1, y2], color=AXIS, lw=2.0, zorder=1)
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        if abs(y1 - y2) < 1e-6:
            dx, dy = 0.0, 0.22
        elif abs(x1 - x2) < 1e-6:
            dx, dy = 0.34, 0.0
        else:                               # diagonal: label on the outer side of the tree
            dx, dy = (0.34 if mx > 5.0 else -0.34), 0.0
        ax.text(mx + dx, my + dy, l.id, fontsize=10, color=MUTED, ha="center", va="center", zorder=3)

    # the faulty link label
    l1 = topo.link(FAULT_LINK)
    (x1, y1), (x2, y2) = pos[l1.a], pos[l1.b]
    mx1, my1 = (x1 + x2) / 2, (y1 + y2) / 2
    ax.text(mx1 - 0.40, my1 - 0.05, "L1\ndelay +20 ms", fontsize=11, color=CRIT, fontweight="bold",
            ha="right", va="center", zorder=6, linespacing=1.1)

    def marker(x, y, on):
        if on:
            ax.scatter([x], [y], s=95, c=ORANGE, edgecolors="white", linewidths=1.4, zorder=7)
        else:
            ax.scatter([x], [y], s=60, c="white", edgecolors=MUTED, linewidths=1.4, zorder=7)

    # nodes; a router's loopback target is drawn inside its box
    lo_hit = {t.node: (t.id in hit) for t in base if t.kind == "lo"}
    for n, (x, y) in pos.items():
        role = topo.nodes[n].role
        fc = VIOLET if role == "ems" else (HOST if role == "host" else BLUE)
        ax.add_patch(FancyBboxPatch((x - hw, y - hh), 2 * hw, 2 * hh, boxstyle="round,pad=0.02,rounding_size=0.12",
                                    fc=fc, ec="white", lw=1.5, zorder=4))
        if n in lo_hit:
            marker(x - hw + 0.21, y, lo_hit[n])
            ax.text(x + 0.17, y, n, fontsize=12, color="white", ha="center", va="center", zorder=5,
                    fontweight="bold")
        else:
            ax.text(x, y, n, fontsize=12, color="white", ha="center", va="center", zorder=5, fontweight="bold")

    # interface (and host) addresses: on the link, just outside the owner's box
    for t in base:
        if t.kind == "lo":
            continue
        l = topo.link(t.link)
        (xo, yo), (xp, yp) = pos[t.node], pos[l.other(t.node)]
        dx, dy = xp - xo, yp - yo
        d = (dx * dx + dy * dy) ** 0.5
        ux, uy = dx / d, dy / d
        exit_d = min(hw / abs(ux) if abs(ux) > 1e-9 else 1e9, hh / abs(uy) if abs(uy) > 1e-9 else 1e9)
        off = exit_d + 0.15
        marker(xo + ux * off, yo + uy * off, t.id in hit)

    ax.set_xlim(-0.65, 10.65)
    ax.set_ylim(-4 * H - 1.05, 0.62)
    ax.set_aspect("equal")
    ax.axis("off")

    # call-outs
    n_all, n_hit = len(base), len(hit)
    def callout(x0, x1, title, body, color, tint):
        y0, y1 = 0.55, -1.12
        ax.add_patch(FancyBboxPatch((x0, y1), x1 - x0, y0 - y1, boxstyle="round,pad=0,rounding_size=0.14",
                                    fc=tint, ec=color, lw=1.4, zorder=8))
        ax.text(x0 + 0.2, y0 - 0.17, title, fontsize=12.5, color=color, fontweight="bold", ha="left", va="top",
                zorder=9)
        ax.text(x0 + 0.2, y0 - 0.62, body, fontsize=10.5, color=INK2, ha="left", va="top", zorder=9,
                linespacing=1.3)

    callout(-0.55, 4.05, "EdgeFDL: 1 alarm",
            "link L1 (core\u2013agg1), type: delay,\nwith evidence, vendor commands\nand rehearsed remediation",
            BLUE, "#eef4fc")
    ax.annotate("", xy=(mx1 + 0.06, my1 + 0.08), xytext=(3.35, -1.14),
                arrowprops=dict(arrowstyle="-|>", color=BLUE, lw=1.8, mutation_scale=16), zorder=9)
    callout(5.95, 10.55, "Thresholds: %d alarms" % n_hit,
            "%d of the %d monitored targets\ncross per-target thresholds at\nonce; none of them names L1"
            % (n_hit, n_all), ORANGE, "#fdf1eb")

    # legend
    yl = -4 * H - 0.78
    lx = -0.35
    ax.scatter([lx], [yl], s=95, c=ORANGE, edgecolors="white", linewidths=1.4)
    ax.text(lx + 0.22, yl, "monitored target whose round trip crosses L1 (%d)" % n_hit, fontsize=10.5,
            color=INK2, va="center")
    lx2 = lx + 6.95
    ax.scatter([lx2], [yl], s=60, c="white", edgecolors=MUTED, linewidths=1.4)
    ax.text(lx2 + 0.22, yl, "other monitored target (%d)" % (n_all - n_hit), fontsize=10.5, color=INK2, va="center")
    return n_all, n_hit


def draw_results(ax, ax_kpi, ev, bench):
    cat, typ = ev["per_category"], ev["per_type"]
    rows = [  # label, value, target or None, is_category
        ("Link down", cat["链路中断"]["joint_acc"], REQ["链路中断"], True),
        ("Performance\ndegradation", cat["性能劣化"]["joint_acc"], REQ["性能劣化"], True),
        ("delay", typ["delay"]["joint_acc"], None, False),
        ("loss", typ["loss"]["joint_acc"], None, False),
        ("jitter", typ["jitter"]["joint_acc"], None, False),
        ("bandwidth", typ["bandwidth"]["joint_acc"], None, False),
        ("Route drift", cat["路由漂移"]["joint_acc"], REQ["路由漂移"], True),
    ]
    lo, hi = 90.0, 100.0
    n = len(rows)
    for i, (label, v, req, is_cat) in enumerate(rows):
        y = n - 1 - i
        ax.plot([lo, hi], [y, y], color=GRID, lw=1.0, zorder=1)
        if req is not None:
            ax.plot([req * 100, req * 100], [y - 0.32, y + 0.32], color=INK, lw=2.2, zorder=3,
                    solid_capstyle="butt")
            ax.text(req * 100, y - 0.38, "≥%g%%" % (req * 100), fontsize=9.5, color=INK2, ha="center",
                    va="top", zorder=3)
        ax.plot([lo, v * 100], [y, y], color=BLUE if is_cat else BLUE_LIGHT, lw=2.0, zorder=2)
        ax.scatter([v * 100], [y], s=110 if is_cat else 70, c=BLUE if is_cat else BLUE_LIGHT,
                   edgecolors="white", linewidths=1.5, zorder=4, clip_on=False)
        ax.text(hi + 0.55, y, "%.2f%%" % (v * 100), fontsize=11.5 if is_cat else 10.5,
                fontweight="bold" if is_cat else "normal", color=INK if is_cat else INK2,
                ha="left", va="center")
        ax.text(lo - 0.3, y, label if is_cat else "   " + label, fontsize=11.5 if is_cat else 10.5,
                fontweight="bold" if is_cat else "normal", color=INK if is_cat else INK2,
                ha="right", va="center", linespacing=1.05)
    ax.set_xlim(lo, hi)
    ax.set_ylim(-0.75, n - 0.4)
    ax.set_yticks([])
    ax.set_xticks([90, 92, 94, 96, 98, 100])
    ax.set_xticklabels(["90%", "92%", "94%", "96%", "98%", "100%"], fontsize=10, color=MUTED)
    ax.tick_params(axis="x", length=0, pad=4)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.set_xlabel("type + location accuracy;  │ = target", fontsize=10, color=INK2)

    noise = bench["noise"]
    kpis = [
        ("%d" % noise["false_alarms"], "false alarms in %.0f h of\nsimulated operation,\n%.0f%% transient noise"
         % (noise["sim_hours"], noise["noise_prob"] * 100)),
        ("%.3f ms" % bench["latency"]["torch"]["forward_p99_ms"], "p99 inference,\n1 CPU thread\n(target ≤100 ms)"),
        ("%.3f%%" % noise["overhead"]["pct_of_min_link"], "probe traffic on the\nnarrowest 100 Mbps\nlink (target <5%)"),
    ]
    ax_kpi.axis("off")
    ax_kpi.set_xlim(0, 3)
    ax_kpi.set_ylim(0, 1)
    for i, (big, small) in enumerate(kpis):
        x0 = i + 0.04
        ax_kpi.add_patch(FancyBboxPatch((x0, 0.04), 0.92, 0.92, boxstyle="round,pad=0,rounding_size=0.06",
                                        fc="#f6f6f4", ec=GRID, lw=1.0, transform=ax_kpi.transData))
        ax_kpi.text(x0 + 0.46, 0.73, big, fontsize=15, fontweight="bold", color=INK, ha="center", va="center")
        ax_kpi.text(x0 + 0.46, 0.33, small, fontsize=9.5, color=INK2, ha="center", va="center", linespacing=1.2)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--topo", default=str(ROOT / "configs" / "demo_topology.yaml"))
    ap.add_argument("--results", default=str(ROOT / "results"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "images" / "hero.png"))
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args()

    topo = T.load(args.topo)
    res = Path(args.results)
    ev = json.loads((res / "eval_test.json").read_text(encoding="utf-8"))
    bench = json.loads((res / "bench.json").read_text(encoding="utf-8"))

    plt.rcParams.update({"font.family": "DejaVu Sans", "axes.facecolor": "white", "savefig.facecolor": "white"})
    fig = plt.figure(figsize=(12.0, 5.6), facecolor="white")
    ax_t = fig.add_axes([0.005, 0.07, 0.565, 0.84])
    ax_r = fig.add_axes([0.715, 0.415, 0.205, 0.47])
    ax_k = fig.add_axes([0.600, 0.075, 0.385, 0.21])

    n_all, n_hit = draw_topology(ax_t, topo)
    draw_results(ax_r, ax_k, ev, bench)

    fig.text(0.012, 0.955, "(a) Link L1 degrades: what the operator receives", fontsize=13.5, fontweight="bold",
             color=INK, ha="left", va="center")
    fig.text(0.600, 0.955, "(b) Accuracy on the independent test set", fontsize=13.5, fontweight="bold",
             color=INK, ha="left", va="center")
    fig.text(0.600, 0.915, "%s samples, end to end (a missed trigger counts as an error)"
             % format(ev["overall"]["n"], ","), fontsize=10, color=INK2, ha="left", va="center")
    fig.text(0.600, 0.305, "Self-test of the same model:", fontsize=10, color=INK2, ha="left", va="center")
    fig.text(0.012, 0.022, "Simulation backend, 12-device demo topology, model results/model.pt. "
             "Data: results/eval_test.json, results/bench.json. Generated by tools/figures/hero.py.",
             fontsize=9, color=MUTED, ha="left", va="center")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=args.dpi, facecolor="white")
    print("wrote %s (%d of %d monitored targets cross %s)" % (out, n_hit, n_all, FAULT_LINK))


if __name__ == "__main__":
    main()
