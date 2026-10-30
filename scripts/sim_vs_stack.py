#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""仿真后端与真实协议栈的基线 RTT 对比（逐监测目标）。

用法（宿主机，需 Docker）：
  scripts/lab.sh run python3 scripts/sim_vs_stack.py --out results/sim_vs_stack.json
步骤：netns 后端按拓扑起整张等价虚拟网络（FRR OSPF + tc）→ 等 OSPF 收敛与稳态 → 对全部监测目标
测 N 轮轻量 ping（与在线系统相同的每轮包数与间隔）→ 同一拓扑、同一测量计划下仿真后端测 N 轮 →
两边各取逐目标"每轮平均 RTT 的中位数"（即 Baseline.fit 的基线 RTT 估计），输出逐目标数值、
绝对偏差的最大值/中位数，以及可达性是否一致。只依赖 numpy / pyyaml（实验镜像不装 torch）。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from edgefdl.measure.model import MeasurePlan  # noqa: E402
from edgefdl.topology import load  # noqa: E402
from edgefdl.vnet.netns import NetnsBackend  # noqa: E402
from edgefdl.vnet.sim import SimBackend  # noqa: E402


def per_target(rounds, tids):
    """逐目标：每轮平均 RTT 的中位数、有应答的轮数。"""
    out = {}
    for tid in tids:
        xs = [r.pings[tid].rtt_avg for r in rounds if tid in r.pings and r.pings[tid].recv > 0]
        out[tid] = (statistics.median(xs) if xs else None, len(xs))
    return out


def frr_version() -> str:
    try:
        out = subprocess.run(["/usr/lib/frr/zebra", "--version"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, universal_newlines=True, timeout=5).stdout
        return out.splitlines()[0].strip() if out else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def main():
    ap = argparse.ArgumentParser(description="仿真后端与真实协议栈（netns + FRR + tc）的逐目标基线 RTT 对比")
    ap.add_argument("--topo", default="configs/demo_topology.yaml")
    ap.add_argument("--rounds", type=int, default=30, help="两边各测几轮")
    ap.add_argument("--seed", type=int, default=1, help="仿真后端随机种子")
    ap.add_argument("--out", default="results/sim_vs_stack.json")
    a = ap.parse_args()

    topo = load(a.topo)
    # 与在线系统相同的 ping 参数；只比 RTT，不做逐跳路径与包列带宽测量（两者不影响 RTT 统计口径）
    plan = MeasurePlan(traceroute_every=0, bw_links_per_round=0)
    targets = topo.targets()
    tids = [t.id for t in targets]
    print(f"== 拓扑 {topo.name}: {len(topo.nodes)} 设备 / {len(topo.links)} 链路 / {len(targets)} 监测目标；"
          f"每目标每轮 {plan.ping_count} 个 ping（间隔 {plan.ping_interval_s:g}s），{a.rounds} 轮", flush=True)

    b = NetnsBackend(topo, plan)
    t0 = time.time()
    b.up()
    try:
        built = time.time() - t0
        conv = b.wait_converged(90)
        stable = b.wait_stable(90)
        print(f"== 真实协议栈：创建 {built:.1f}s，OSPF 收敛 {conv:.1f}s，稳态再等 {stable:.1f}s；"
              f"路由={'FRR-OSPF' if b.use_frr else '静态'}", flush=True)
        stack_rounds = []
        for i in range(a.rounds):
            stack_rounds.append(b.measure_round(i))
        stack = per_target(stack_rounds, tids)
        routing = "frr-ospf" if b.use_frr else "static"
    finally:
        b.down()

    s = SimBackend(topo, plan, seed=a.seed)
    s.up()
    sim = per_target([s.measure_round(i, transient=[]) for i in range(a.rounds)], tids)

    rows, diffs = [], []
    for t in targets:
        (rs, ks), (rm, km) = stack[t.id], sim[t.id]
        reach_s, reach_m = ks == a.rounds, km == a.rounds
        d = abs(rs - rm) if rs is not None and rm is not None else None
        if d is not None:
            diffs.append((d, t.id))
        rows.append(dict(id=t.id, ip=t.ip, node=t.node, kind=t.kind, link=t.link,
                         src=t.src or topo.vantage,
                         stack_rtt_ms=None if rs is None else round(rs, 3),
                         sim_rtt_ms=None if rm is None else round(rm, 3),
                         abs_diff_ms=None if d is None else round(d, 3),
                         stack_rounds_reachable=ks, sim_rounds_reachable=km,
                         reachability_agrees=reach_s == reach_m))
    ds = sorted(x for x, _ in diffs)
    worst = max(diffs) if diffs else (None, None)
    agree = sum(r["reachability_agrees"] for r in rows)
    summary = dict(targets=len(rows), compared=len(ds),
                   max_abs_diff_ms=round(ds[-1], 3) if ds else None,
                   median_abs_diff_ms=round(statistics.median(ds), 3) if ds else None,
                   mean_abs_diff_ms=round(sum(ds) / len(ds), 3) if ds else None,
                   p90_abs_diff_ms=round(ds[min(len(ds) - 1, int(0.9 * len(ds)))], 3) if ds else None,
                   worst_target=worst[1],
                   reachability_agree=agree, reachability_agree_all=agree == len(rows))
    res = dict(
        description="Per-target baseline RTT: real protocol stack (netns backend, FRR OSPF, tc netem/tbf) vs. "
                    "the analytic simulator (sim backend), same topology and measurement plan. Per target and "
                    "backend: median over rounds of the round's average RTT (the Baseline.fit estimator).",
        topology=topo.name, topology_file=a.topo, devices=len(topo.nodes), links=len(topo.links),
        rounds=a.rounds, ping_count=plan.ping_count, ping_interval_s=plan.ping_interval_s, sim_seed=a.seed,
        stack=dict(routing=routing, ospf_convergence_s=round(conv, 1), kernel=platform.release(),
                   frr=frr_version()),
        created=time.strftime("%Y-%m-%d %H:%M:%S"), summary=summary, per_target=rows)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"== 逐目标基线 RTT 绝对偏差：最大 {summary['max_abs_diff_ms']} ms（{summary['worst_target']}），"
          f"中位数 {summary['median_abs_diff_ms']} ms，P90 {summary['p90_abs_diff_ms']} ms；"
          f"可达性一致 {agree}/{len(rows)}", flush=True)
    for r in sorted(rows, key=lambda r: -(r["abs_diff_ms"] or 0))[:5]:
        print(f"   {r['id']:10s} 真实 {r['stack_rtt_ms']} ms  仿真 {r['sim_rtt_ms']} ms  |Δ| {r['abs_diff_ms']} ms")
    print(f"== 已写出 {a.out}")


if __name__ == "__main__":
    main()
