# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""由 Topology 生成 FRR 配置：OSPF 单区域、点到点、快速收敛（hello 1s / dead 4s）。

路由器：互联口参与 OSPF；连接主机/网管的接口与 loopback 作为被动接口宣告进 OSPF。
主机与网管不跑 FRR，只配默认路由指向网关（见 gateway_of）。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from ..topology import Topology

DAEMONS = """# 由 EdgeFDL 生成：仅启用 zebra + ospfd
zebra=yes
bgpd=no
ospfd=yes
ospf6d=no
ripd=no
ripngd=no
isisd=no
pimd=no
ldpd=no
nhrpd=no
eigrpd=no
babeld=no
sharpd=no
pbrd=no
bfdd=no
fabricd=no
vrrpd=no
pathd=no
vtysh_enable=yes
zebra_options="  -A 127.0.0.1 -s 90000000"
ospfd_options="  -A 127.0.0.1"
"""

VTYSH_CONF = "service integrated-vtysh-config\n"


def router_id(topo: Topology, node: str) -> str:
    lo = topo.nodes[node].loopback
    if not lo:
        raise ValueError(f"路由器 {node} 缺少 loopback，无法确定 router-id")
    return lo.split("/")[0]


def router_conf(topo: Topology, node: str, hello_s: int = 1, dead_s: int = 4) -> str:
    n = topo.nodes[node]
    if not n.transit:
        raise ValueError(f"{node} 不是路由设备")
    lines = ["frr defaults traditional", f"hostname {node}", "service integrated-vtysh-config",
             "log syslog warnings", "!",
             "interface lo", " ip ospf area 0", " ip ospf passive", "!"]
    for l in topo.links:
        if node not in (l.a, l.b):
            continue
        ifn = l.a_if if node == l.a else l.b_if
        peer = topo.nodes[l.other(node)]
        lines += [f"interface {ifn}", f" description {l.id} to {peer.name}", " ip ospf area 0"]
        if peer.transit:
            lines += [" ip ospf network point-to-point",
                      f" ip ospf hello-interval {hello_s}",
                      f" ip ospf dead-interval {dead_s}"]
        else:
            lines.append(" ip ospf passive")
        lines += [f" ip ospf cost {l.cost}", "!"]
    lines += ["router ospf", f" ospf router-id {router_id(topo, node)}",
              # 虚拟网络里把 LSA 生成/接收节流压到 100ms 内（默认 5s/1s），故障样本生成时秒级收敛
              " timers throttle spf 0 50 500", " timers throttle lsa all 100",
              " timers lsa min-arrival 50", "!", ""]
    return "\n".join(lines)


def gateway_of(topo: Topology, node: str) -> Optional[Tuple[str, str, str]]:
    """主机/网管的 (本端接口, 网关 IP, 网关设备)。多上联时取开销最小的一条。"""
    best = None
    for l in topo.links:
        if node not in (l.a, l.b):
            continue
        peer = l.other(node)
        if not topo.nodes[peer].transit:
            continue
        cand = (l.cost, l.a_if if node == l.a else l.b_if, l.ip_of(peer), peer)
        if best is None or cand < best:
            best = cand
    return None if best is None else best[1:]


def all_router_confs(topo: Topology) -> List[Tuple[str, str]]:
    return [(n, router_conf(topo, n)) for n in topo.nodes if topo.nodes[n].transit]
