#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""netns 后端冒烟测试：在等价虚拟网络里逐一注入 6 类故障，验证测量现象与故障语义一致。

用法（宿主机）：scripts/lab.sh smoke [--link L4] [--static]
步骤：up → 等 OSPF 收敛 → 基线测量 → 对一条有冗余的链路依次注入
      链路中断/时延/丢包/抖动/带宽/路由漂移，各测一轮 + 精测 → 恢复 → down。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from edgefdl.faults.model import Fault  # noqa: E402
from edgefdl.measure.model import MeasurePlan  # noqa: E402
from edgefdl.topology import load  # noqa: E402
from edgefdl.vnet.netns import NetnsBackend  # noqa: E402

RESULTS = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok))
    print(f"   [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def fmt(x: float) -> str:
    return "nan" if x != x else f"{x:.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topo", default="configs/demo_topology.yaml")
    ap.add_argument("--link", default="L4", help="被测链路（需有冗余路径）")
    ap.add_argument("--probe", default="lo.acc1", help="其路径经过被测链路的监测目标")
    ap.add_argument("--static", action="store_true", help="不用 FRR，走静态路由回退")
    ap.add_argument("--loss", type=float, default=0.1,
                    help="丢包步取值。hello 1/dead 4 下 ≥20%% 时一步之内就可能出现真实的邻接抖动，"
                         "冒烟默认取 10%%（见报告的抖动频率实测）")
    ap.add_argument("--ospf", default="1,4", help="OSPF hello,dead 秒")
    a = ap.parse_args()

    topo = load(a.topo)
    plan = MeasurePlan(ping_count=10, ping_interval_s=0.1)
    hello, dead = (int(x) for x in a.ospf.split(","))
    b = NetnsBackend(topo, plan, use_frr=False if a.static else None, ospf_timers=(hello, dead))
    probe_ip = next(t.ip for t in topo.targets() if t.id == a.probe)
    L = topo.link(a.link)
    if_a, if_b = f"{L.id}.{L.a}", f"{L.id}.{L.b}"
    print(f"== 拓扑 {topo.name}: {len(topo.nodes)} 设备 / {len(topo.links)} 链路 / "
          f"{len(topo.targets())} 监测目标；路由={'FRR-OSPF' if b.use_frr else '静态'}")
    t0 = time.time()
    b.up()
    print(f"== 虚拟网络创建 {time.time() - t0:.1f}s，", end="", flush=True)
    conv = b.wait_converged(90)
    print(f"收敛 {conv:.1f}s")

    try:
        base = b.measure_round(0)
        bp = base.pings[a.probe]
        reach = sum(1 for s in base.pings.values() if s.reachable)
        unknown = [k for k, s in base.pings.items()
                   if s.reachable and not s.path and not k.startswith("nqa.")]
        print(f"== 基线：可达 {reach}/{len(base.pings)}，{a.probe} RTT={fmt(bp.rtt_avg)}ms "
              f"mdev={fmt(bp.rtt_mdev)} 路径={'>'.join(bp.path)}；本轮测量字节 {base.probe_bytes}"
              f"（{base.probe_bytes * 8 / plan.interval_s / 1e3:.0f} kbps @ {plan.interval_s:g}s 周期）")
        print(f"   轻量带宽估计 {dict((k, round(v)) for k, v in base.bw.items())}；"
              f"LSDB[{a.link}]={base.lsdb.get(a.link)}")
        check("基线全部可达", reach == len(base.pings))
        check("逐跳路径完整（ICMP 限速丢跳已重测）", len(unknown) <= 1, f"路径未知 {unknown}")
        check("基线路径经过被测链路", L.a in bp.path and L.b in bp.path, str(bp.path))
        bdp = b.deep_probe(a.link)
        tr, _ = b._train(a.link, 16, 1400)
        print(f"   精测 {a.link}: RTT={fmt(bdp.rtt_avg)} mdev={fmt(bdp.rtt_mdev)} loss={fmt(bdp.loss)} "
              f"bw={fmt(bdp.bw_mbps)}Mbps（标称 {L.bw_mbps:g}）；单次包列估计 {fmt(tr)}Mbps")
        check("精测带宽接近标称", abs(bdp.bw_mbps - L.bw_mbps) / L.bw_mbps < 0.3)

        bfib = b.fib_path(probe_ip)
        check("转发表路径与 traceroute 一致", bfib == bp.path, f"fib={bfib}")

        def avoids(path):
            return bool(path) and not any({x, y} == {L.a, L.b} for x, y in zip((topo.vantage,) + path, path))

        faults = [Fault("link_down", a.link), Fault("delay", a.link, 20.0), Fault("loss", a.link, a.loss),
                  Fault("jitter", a.link, 10.0), Fault("bandwidth", a.link, 0.2),
                  Fault("route_drift", a.link, 10.0)]
        for i, f in enumerate(faults, 1):
            pre = b.wait_stable(90)                  # 注入前：邻接全 Full + 全部可达 + 路由稳定
            print(f"== 注入 {f.label}（注入前稳态等待 {pre:.1f}s）")
            b.inject(f)
            t_inj = time.time()
            if f.type == "link_down":
                b.wait_stable(30, expect_down=[a.link])
            elif f.type == "route_drift":
                b.wait_stable(30, expect_cost={a.link: float(max(1, round(L.cost * f.value)))})
            else:
                time.sleep(0.5)
            r = b.measure_round(i)
            p = r.pings[a.probe]
            unreach = sorted(k for k, s in r.pings.items() if not s.reachable)
            dp = b.deep_probe(a.link)
            fib = b.fib_path(probe_ip)
            up, el = b.adj_uptime(a.link), time.time() - t_inj
            print(f"   不可达={unreach}；{a.probe} RTT={fmt(p.rtt_avg)}(基线 {fmt(bp.rtt_avg)}) "
                  f"mdev={fmt(p.rtt_mdev)} loss={fmt(p.loss)} 路径={'>'.join(p.path) or '未知'} "
                  f"转发表路径={'>'.join(fib)}")
            print(f"   LSDB[{a.link}]={r.lsdb.get(a.link)}；邻接 Full 持续 {fmt(up) if up is not None else '-'}s"
                  f"（注入后 {el:.1f}s）；精测 RTT={fmt(dp.rtt_avg)} mdev={fmt(dp.rtt_mdev)} "
                  f"loss={fmt(dp.loss)} bw={fmt(dp.bw_mbps)}")
            if f.type in ("delay", "loss", "jitter", "bandwidth"):
                # 性能劣化类故障不应改变控制面：邻接全程 Full、被测路径不变
                check("邻接全程保持 Full", not b.use_frr or (up is not None and up >= el - 0.5
                                                       and r.lsdb.get(a.link) is not None
                                                       and r.lsdb[a.link].adj_up),
                      f"Full 持续 {up}s / 注入后 {el:.1f}s")
                check("被测路径不变", fib == bfib and (not p.path or p.path == bp.path),
                      f"fib={fib} trace={p.path}")
            if f.type == "link_down":
                check("两端接口不可达", set(unreach) == {if_a, if_b}, str(unreach))
                check("目标经冗余路径仍可达且绕开该链路", p.reachable and avoids(fib) and
                      (not p.path or avoids(p.path)), f"fib={fib}")
                check("LSDB 邻接断开", r.lsdb.get(a.link) is not None and not r.lsdb[a.link].adj_up)
            elif f.type == "delay":
                d = p.rtt_avg - bp.rtt_avg
                check("RTT 上升约 2v", abs(d - 2 * f.value) < 5, f"Δ={d:.1f}ms 期望≈{2 * f.value:g}")
            elif f.type == "loss":
                exp = 1 - (1 - f.value) ** 2
                check("精测丢包≈1-(1-p)^2", abs(dp.loss - exp) < 0.15, f"{dp.loss:.2f} 期望≈{exp:.2f}")
            elif f.type == "jitter":
                check("精测抖动显著上升", dp.rtt_mdev > 4 * max(0.05, bdp.rtt_mdev) and dp.rtt_mdev > 5,
                      f"mdev={dp.rtt_mdev:.1f}ms")
                check("抖动不被误测成带宽下降", abs(dp.bw_mbps - L.bw_mbps) / L.bw_mbps < 0.3,
                      f"bw={dp.bw_mbps:.0f} 来源={b.deep_extra[a.link].get('bw_source')}")
                print(f"   轻量探测 RTT 增量 {p.rtt_avg - bp.rtt_avg:.1f}ms（2J={2 * f.value:g}），"
                      f"mdev {p.rtt_mdev:.1f}ms")
            elif f.type == "bandwidth":
                exp = L.bw_mbps * f.value
                check("精测带宽≈r×bw", abs(dp.bw_mbps - exp) / exp < 0.3, f"{dp.bw_mbps:.0f} 期望≈{exp:.0f}")
            elif f.type == "route_drift":
                check("全部可达", not unreach)
                check("路径改变且绕开该链路", fib != bfib and avoids(fib) and (not p.path or avoids(p.path)),
                      f"fib={fib}")
                check("LSDB 开销变化", r.lsdb.get(a.link) is not None and
                      r.lsdb[a.link].cost == max(1, round(L.cost * f.value)), str(r.lsdb.get(a.link)))
            b.clear(f.link)
            post = b.wait_stable(90)                 # 清除后：等邻接重建完成、路由回到基线
            print(f"   清除后稳态等待 {post:.1f}s")

        r = b.measure_round(99)
        p = r.pings[a.probe]
        check("恢复后回到基线", all(s.reachable for s in r.pings.values()) and b.fib_path(probe_ip) == bfib
              and (not p.path or p.path == bp.path) and abs(p.rtt_avg - bp.rtt_avg) < 1.0,
              f"RTT={fmt(p.rtt_avg)} 路径={'>'.join(p.path)}")
    finally:
        b.down()
    left = subprocess.run(["ip", "netns", "list"], stdout=subprocess.PIPE, universal_newlines=True).stdout
    check("down 后无残留命名空间", not any(ln.startswith(b.prefix) for ln in left.splitlines()))
    n_ok = sum(ok for _, ok in RESULTS)
    print(f"== 冒烟结果 {n_ok}/{len(RESULTS)} 通过")
    sys.exit(0 if n_ok == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
