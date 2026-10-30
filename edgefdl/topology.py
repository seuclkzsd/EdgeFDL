# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""边缘网络拓扑数据模型：设备、链路、IP 规划、OSPF 最短路由与监测目标。

整个系统（虚拟网络构建、故障注入、测量、智能体特征）都以这里的 Topology 为唯一事实来源，
保证"等价虚拟网络"与物理网络在设备、拓扑、IP、带宽、性能五个维度上一致。
"""
from __future__ import annotations

import heapq
import ipaddress
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import yaml

ROLES = ("router", "switch", "host", "ems")


@dataclass
class Node:
    name: str
    role: str = "router"          # router | switch | host | ems(边缘网管服务器)
    vendor: str = "generic"       # huawei / h3c / ruijie / cisco / linux ...
    model: str = ""
    loopback: str = ""            # 路由器 loopback，形如 10.255.0.1/32
    agent: bool = True            # 能否部署轻量探针（iperf3 服务端 / 精测）
    site: str = ""                # 物理位置，便于告警定位到机房

    @property
    def transit(self) -> bool:
        return self.role in ("router", "switch")


@dataclass
class Link:
    id: str
    a: str
    b: str
    a_if: str = ""
    b_if: str = ""
    a_ip: str = ""                # 带掩码，如 10.10.0.1/30
    b_ip: str = ""
    bw_mbps: float = 1000.0
    delay_ms: float = 0.5         # 单向基线时延
    jitter_ms: float = 0.05
    loss: float = 0.0             # 基线丢包率（0~1）
    cost: int = 10                # OSPF 接口开销（两端相同）

    def other(self, n: str) -> str:
        return self.b if n == self.a else self.a

    def ip_of(self, n: str) -> str:
        return (self.a_ip if n == self.a else self.b_ip).split("/")[0]


@dataclass
class Target:
    """一个被周期探测的 IP。kind=if 为链路接口地址（链路级连通性的直接证据），lo 为环回，host 为主机，
    nqa 为盲区链路上由端点设备发起的探测（华为/华三 NQA、思科 IP SLA，网管经 SNMP 读取结果）。"""
    id: str
    ip: str
    node: str
    kind: str
    link: Optional[str] = None
    src: Optional[str] = None      # 探测发起设备；None 表示网管服务器


@dataclass
class Topology:
    name: str
    nodes: Dict[str, Node]
    links: List[Link]
    vantage: str = "ems"
    description: str = ""
    _adj: Dict[str, List[Tuple[str, Link]]] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._rebuild()

    def _rebuild(self):
        self._targets = None
        self._adj = {n: [] for n in self.nodes}
        self._by_pair: Dict[Tuple[str, str], Link] = {}
        self._by_id: Dict[str, Link] = {}
        for l in self.links:
            if (l.a, l.b) in self._by_pair:
                raise ValueError(f"暂不支持并行链路: {l.a}-{l.b}")
            self._adj[l.a].append((l.b, l))
            self._adj[l.b].append((l.a, l))
            self._by_pair[(l.a, l.b)] = self._by_pair[(l.b, l.a)] = l
            self._by_id[l.id] = l

    # ------------------------------------------------------------------ 查询
    def link(self, lid: str) -> Link:
        return self._by_id[lid]

    def link_between(self, a: str, b: str) -> Link:
        return self._by_pair[(a, b)]

    def link_ids(self) -> List[str]:
        return [l.id for l in self.links]

    def ip_owner(self) -> Dict[str, str]:
        """IP -> 节点名，用于把 traceroute 的逐跳 IP 还原成设备。"""
        m = {}
        for l in self.links:
            m[l.a_ip.split("/")[0]] = l.a
            m[l.b_ip.split("/")[0]] = l.b
        for n in self.nodes.values():
            if n.loopback:
                m[n.loopback.split("/")[0]] = n.name
        return m

    def targets(self) -> List[Target]:
        """测量计划：所有链路两端接口地址 + 路由器环回 + 盲区链路的 NQA 探测。"""
        if getattr(self, "_targets", None) is None:
            base = self._base_targets()
            self._targets = base + [Target(f"nqa.{lid}", self.link(lid).b_ip.split("/")[0], self.link(lid).b,
                                           "nqa", lid, src=self.link(lid).a)
                                    for lid in self.uncovered_links(base)]
        return list(self._targets)

    def uncovered_links(self, targets: Optional[List[Target]] = None) -> List[str]:
        """覆盖规划：基线路由下没有任何探测往返经过的链路（劣化无法被网管直接观测）。"""
        covered: Set[str] = set()
        for t in targets if targets is not None else self._base_targets():
            fwd, rev = self.route_to_target(self.vantage, t)
            covered.update(self.path_links(fwd) + self.path_links(rev))
        return [l.id for l in self.links if l.id not in covered]

    def _base_targets(self) -> List[Target]:
        out = []
        for l in self.links:
            for n, ip in ((l.a, l.a_ip), (l.b, l.b_ip)):
                if n == self.vantage:
                    continue
                kind = "host" if self.nodes[n].role == "host" else "if"
                out.append(Target(f"{l.id}.{n}", ip.split("/")[0], n, kind, l.id))
        for n in self.nodes.values():
            if n.loopback and n.name != self.vantage:
                out.append(Target(f"lo.{n.name}", n.loopback.split("/")[0], n.name, "lo"))
        return out

    # ------------------------------------------------------------------ 路由
    def spf(self, src: str, down: Set[str] = frozenset(),
            cost: Optional[Dict[str, float]] = None) -> Tuple[Dict[str, float], Dict[str, str]]:
        """Dijkstra。主机/网管不转发（仅可作为起点）。并列时按节点名取最小，保证确定性。"""
        dist = {src: 0.0}
        prev: Dict[str, str] = {}
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, float("inf")):
                continue
            if u != src and not self.nodes[u].transit:
                continue
            for v, l in self._adj[u]:
                if l.id in down:
                    continue
                c = (cost or {}).get(l.id, l.cost)
                nd = d + c
                if nd < dist.get(v, float("inf")) - 1e-9 or (
                        abs(nd - dist.get(v, float("inf"))) < 1e-9 and u < prev.get(v, "~")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        return dist, prev

    @staticmethod
    def _walk(prev: Dict[str, str], src: str, dst: str) -> List[str]:
        path = [dst]
        while path[-1] != src:
            path.append(prev[path[-1]])
        return path[::-1]

    def route_to_target(self, src: str, t: Target, down: Set[str] = frozenset(),
                        cost: Optional[Dict[str, float]] = None,
                        _cache: Optional[dict] = None) -> Tuple[List[str], List[str]]:
        """返回 (正向节点路径, 回程节点路径)；不可达返回 ([], [])。

        接口地址的可达性遵循真实设备行为：链路断开时该网段被撤销 → 两端接口地址均不可达；
        否则流量先到达离源更近的一端，若不是地址属主，则经该链路的直连路由送达属主。
        """
        if t.src is not None and t.src != self.vantage:
            # 端点设备直接探测对端接口：往返各穿越一次该链路
            if t.link in down:
                return [], []
            return [t.src, t.node], [t.node, t.src]
        key = (src, frozenset(down), tuple(sorted((cost or {}).items())))
        if _cache is not None and key in _cache:
            dist, prev = _cache[key]
        else:
            dist, prev = self.spf(src, down, cost)
            if _cache is not None:
                _cache[key] = (dist, prev)
        if t.link is not None:
            if t.link in down:
                return [], []
            l = self.link(t.link)
            owner, peer = t.node, l.other(t.node)
            c = (cost or {}).get(l.id, l.cost)
            d_owner = dist.get(owner, float("inf")) + c
            d_peer = dist.get(peer, float("inf")) + c
            if owner == src or peer == src:
                fwd = [src] if owner == src else [src, owner]
            elif d_owner == float("inf") and d_peer == float("inf"):
                return [], []
            elif d_owner <= d_peer:
                fwd = self._walk(prev, src, owner)
            else:
                if not self.nodes[peer].transit:
                    return [], []
                fwd = self._walk(prev, src, peer) + [owner]
        else:
            if t.node not in dist:
                return [], []
            fwd = self._walk(prev, src, t.node)
        # 回程：属主按自己的最短路回到源（源是主机，路由器到主机的网段即主机所连链路）
        rkey = (t.node, frozenset(down), tuple(sorted((cost or {}).items())))
        if _cache is not None and rkey in _cache:
            rdist, rprev = _cache[rkey]
        else:
            rdist, rprev = self.spf(t.node, down, cost)
            if _cache is not None:
                _cache[rkey] = (rdist, rprev)
        if src not in rdist:
            return [], []
        rev = self._walk(rprev, t.node, src)
        return fwd, rev

    def path_links(self, path: List[str]) -> List[str]:
        return [self.link_between(a, b).id for a, b in zip(path, path[1:])]

    # ------------------------------------------------------------------ 序列化
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "vantage": self.vantage,
            "nodes": [asdict(n) for n in self.nodes.values()],
            "links": [asdict(l) for l in self.links],
        }

    def save(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.to_dict(), f, allow_unicode=True, sort_keys=False)


def _alloc_p2p(pool: ipaddress.IPv4Network, used: Set[ipaddress.IPv4Network]):
    for sub in pool.subnets(new_prefix=30):
        if not any(sub.overlaps(u) for u in used):
            used.add(sub)
            h = list(sub.hosts())
            return f"{h[0]}/30", f"{h[1]}/30"
    raise ValueError("互联地址池耗尽")


def from_dict(d: dict) -> Topology:
    """从需求采集表（YAML/JSON）构建拓扑；缺省的接口名、IP、环回自动规划。"""
    plan = d.get("ip_plan", {})
    p2p_pool = ipaddress.ip_network(plan.get("p2p_pool", "10.10.0.0/16"))
    lo_pool = list(ipaddress.ip_network(plan.get("loopback_pool", "10.255.0.0/24")).hosts())
    nodes: Dict[str, Node] = {}
    for i, nd in enumerate(d["nodes"]):
        n = Node(**{k: v for k, v in nd.items() if k in Node.__dataclass_fields__})
        if n.role not in ROLES:
            raise ValueError(f"未知设备角色 {n.role}")
        if n.transit and not n.loopback:
            n.loopback = f"{lo_pool[len([x for x in nodes.values() if x.transit])]}/32"
        if n.role in ("host", "ems") and "agent" not in nd:
            n.agent = True
        nodes[n.name] = n
    used: Set[ipaddress.IPv4Network] = set()
    for ld in d["links"]:
        for k in ("a_ip", "b_ip"):
            if ld.get(k):
                used.add(ipaddress.ip_interface(ld[k]).network)
    ifcount: Dict[str, int] = {n: 0 for n in nodes}
    links: List[Link] = []
    alias = {"bw": "bw_mbps", "delay": "delay_ms", "jitter": "jitter_ms"}
    for i, ld in enumerate(d["links"]):
        ld = {alias.get(k, k): v for k, v in ld.items()}
        l = Link(id=ld.get("id", f"L{i + 1}"),
                 **{k: v for k, v in ld.items() if k in Link.__dataclass_fields__ and k != "id"})
        for end in ("a", "b"):
            n = getattr(l, end)
            if n not in nodes:
                raise ValueError(f"链路 {l.id} 引用了不存在的设备 {n}")
            ifcount[n] += 1
            if not getattr(l, f"{end}_if"):
                setattr(l, f"{end}_if", f"eth{ifcount[n]}")
        if not l.a_ip or not l.b_ip:
            l.a_ip, l.b_ip = _alloc_p2p(p2p_pool, used)
        links.append(l)
    return Topology(name=d.get("name", "edge"), nodes=nodes, links=links,
                    vantage=d.get("vantage", "ems"), description=d.get("description", ""))


def load(path: str) -> Topology:
    with open(path, encoding="utf-8") as f:
        return from_dict(yaml.safe_load(f))


def describe(topo: Topology) -> str:
    lines = [f"拓扑 {topo.name}: {len(topo.nodes)} 台设备, {len(topo.links)} 条链路, 网管={topo.vantage}"]
    for l in topo.links:
        lines.append(f"  {l.id:4s} {l.a}({l.a_ip}) <-> {l.b}({l.b_ip})  {l.bw_mbps:g}Mbps "
                     f"{l.delay_ms:g}ms cost={l.cost}")
    return "\n".join(lines)

