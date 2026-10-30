# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""故障注入：把 Fault 翻译成对等价虚拟网络执行的命令序列（tc netem / ip link / FRR）。

语义与仿真后端严格一致：
  · 每条链路两端出口各挂两级队列：根 netem（时延/抖动/丢包，不限速）+ 子队列 tbf（限速），
    基线 = `netem delay {delay_ms}ms` + `tbf rate {bw_mbps}mbit`，等价虚拟网络的链路时延与带宽由它复现。
    限速不放在 netem 里：netem 带 rate 时强制保序，抖动会退化成"向近期最大时延靠拢"；
    拆成两级后抖动逐包独立（允许乱序），与仿真的逐包 |N(J,J)| 语义一致；
  · 故障在基线上叠加，作用于两个方向（两端出口各一份）：
      delay v      → delay (base+v)ms
      loss p       → loss p
      jitter J     → delay (base+J)ms Jms distribution normal（显式正态，netem 缺省为均匀分布）
      bandwidth r  → tbf rate r×bw
      link_down    → 两端 ip link set down 并摘除接口地址（网段撤销、地址不可达）
      route_drift f→ 两端路由器 OSPF 接口开销改为 max(1, round(cost×f))
  · 恢复即回到基线。
这里只生成命令、不执行，便于 dry-run 审阅、单元测试和写入操作审计日志。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..topology import Link, Topology
from .model import Fault


@dataclass
class Cmd:
    node: str
    argv: List[str] = field(default_factory=list)
    kind: str = "exec"      # exec: 在设备上执行；vtysh: 交给该设备的 FRR 配置终端（argv 为配置行）

    def __str__(self) -> str:
        if self.kind == "vtysh":
            return f"[{self.node}] vtysh " + " ".join(f"-c '{c}'" for c in self.argv)
        return f"[{self.node}] " + " ".join(self.argv)


def _ms(x: float) -> str:
    return f"{max(0.0, x):.3f}".rstrip("0").rstrip(".") + "ms"


# tbf 令牌桶深度取 2 个最大帧：1 个帧时千兆口会因定时器松弛丢令牌、实际速率偏低 3~5%；
# 更深的桶会让包列前几个包"借存量"线速通过，测量侧丢弃包列前 2 个包即可抵消（见 probes.train_capacity）
TBF_BURST = 3028
TBF_LATENCY = "100ms"


def netem_args(link: Link, fault: Optional[Fault] = None) -> List[str]:
    """一端出口根 netem 的参数（不含 `tc qdisc replace dev X root handle 1: netem` 前缀），不含限速。"""
    ftype = fault.type if fault is not None else ""
    delay = link.delay_ms
    if ftype in ("delay", "jitter"):
        delay += fault.value
    args = ["delay", _ms(delay)]
    if ftype == "jitter":
        args += [_ms(fault.value), "distribution", "normal"]
    # limit 放大到 1 万包：1Gbps×100ms 的在途包约 8 千，默认 1000 会造成非故障性丢包
    args += ["limit", "10000"]
    if ftype == "loss":
        args += ["loss", f"{fault.value * 100:g}%"]
    return args


def tbf_args(link: Link, fault: Optional[Fault] = None) -> List[str]:
    """子队列 tbf 的参数：链路带宽（带宽劣化时为 r×bw）。"""
    bw = link.bw_mbps
    if fault is not None and fault.type == "bandwidth":
        bw = max(0.1, bw * fault.value)
    return ["rate", f"{bw:g}mbit", "burst", str(TBF_BURST), "latency", TBF_LATENCY]


def qdisc_cmds(topo: Topology, link_id: str, fault: Optional[Fault] = None) -> List[Cmd]:
    l = topo.link(link_id)
    out: List[Cmd] = []
    for n, ifn in ((l.a, l.a_if), (l.b, l.b_if)):
        out.append(Cmd(n, ["tc", "qdisc", "replace", "dev", ifn, "root", "handle", "1:", "netem"]
                       + netem_args(l, fault)))
        out.append(Cmd(n, ["tc", "qdisc", "replace", "dev", ifn, "parent", "1:1", "handle", "10:", "tbf"]
                       + tbf_args(l, fault)))
    return out


def drift_cost(link: Link, factor: float) -> int:
    return max(1, int(round(link.cost * factor)))


def ospf_cost_cmds(topo: Topology, link_id: str, cost: int) -> List[Cmd]:
    l = topo.link(link_id)
    return [Cmd(n, ["configure terminal", f"interface {ifn}", f"ip ospf cost {cost}"], "vtysh")
            for n, ifn in ((l.a, l.a_if), (l.b, l.b_if)) if topo.nodes[n].transit]


def apply_cmds(topo: Topology, fault: Fault) -> List[Cmd]:
    l = topo.link(fault.link)
    if fault.type == "link_down":
        # 同时摘掉接口地址：Linux 是弱主机模型，down 口上的地址仍可经其他接口应答；
        # 真实路由器接口 down 后该地址即不可达，摘地址才与物理设备、仿真语义一致
        out: List[Cmd] = []
        for n, ifn in ((l.a, l.a_if), (l.b, l.b_if)):
            out += [Cmd(n, ["ip", "link", "set", ifn, "down"]), Cmd(n, ["ip", "addr", "flush", "dev", ifn])]
        return out
    if fault.type == "route_drift":
        return ospf_cost_cmds(topo, l.id, drift_cost(l, fault.value))
    return qdisc_cmds(topo, l.id, fault)


def revert_cmds(topo: Topology, fault: Fault) -> List[Cmd]:
    l = topo.link(fault.link)
    if fault.type == "link_down":
        out = []
        for n, ifn, ip in ((l.a, l.a_if, l.a_ip), (l.b, l.b_if, l.b_ip)):
            out += [Cmd(n, ["ip", "addr", "replace", ip, "dev", ifn]), Cmd(n, ["ip", "link", "set", ifn, "up"])]
        return out
    if fault.type == "route_drift":
        return ospf_cost_cmds(topo, l.id, l.cost)
    return qdisc_cmds(topo, l.id, None)


def baseline_cmds(topo: Topology) -> List[Cmd]:
    out: List[Cmd] = []
    for l in topo.links:
        out += qdisc_cmds(topo, l.id, None)
    return out
