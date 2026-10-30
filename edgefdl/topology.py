# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""边缘网络拓扑数据模型：设备、链路、IP 规划、OSPF 最短路由与监测目标。

整个系统（虚拟网络构建、故障注入、测量、智能体特征）都以这里的 Topology 为唯一事实来源，
保证"等价虚拟网络"与物理网络在设备、拓扑、IP、带宽、性能五个维度上一致。
"""
from __future__ import annotations

import heapq
import ipaddress
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import yaml

ROLES = ("router", "switch", "host", "ems")
# 标识符会写进 netns/容器名、FRR 配置、文件路径与设备命令，只允许安全字符（不含空白、换行、路径分隔符）
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")          # 设备名、链路 id
IFNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,63}")      # 接口名（允许厂商名，如 GigabitEthernet0/0/1）
LINUX_IFNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}")  # 真实协议栈后端：Linux 接口名 ≤15 字符、无 / :


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
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.to_dict(), f, allow_unicode=True, sort_keys=False)


def _alloc_p2p(pool: ipaddress.IPv4Network, used: Set[ipaddress.IPv4Network]):
    for sub in pool.subnets(new_prefix=30):
        if not any(sub.overlaps(u) for u in used):
            used.add(sub)
            h = list(sub.hosts())
            return f"{h[0]}/30", f"{h[1]}/30"
    raise ValueError("互联地址池耗尽")


def _check_name(v, what: str, pat=NAME_RE, chars: str = "_ . -", maxlen: int = 32) -> str:
    if v == "lo":
        raise ValueError(f"{what} 'lo' 是保留名（环回接口）")
    if not isinstance(v, str) or not pat.fullmatch(v):
        raise ValueError(f"{what} {v!r} 不合法：须以字母或数字开头，只含字母、数字和 {chars}，"
                         f"不超过 {maxlen} 个字符")
    return v


def _number(v, what: str, lo: float, hi: float, integer: bool = False):
    """数值字段：数字或数字字符串（大模型提取结果常为字符串）；越界、非数报错，合法数字原样保留。"""
    try:
        x = None if isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        x = None
    if x is None or not lo <= x <= hi or (integer and x != int(x)):
        raise ValueError(f"{what} 取值无效: {v!r}（应在 {lo:g}~{hi:g}{'，整数' if integer else ''}）")
    if integer:
        return int(x)
    return v if isinstance(v, (int, float)) else x


def _check_addr(v, what: str) -> str:
    try:
        if not isinstance(v, str):
            raise ValueError
        ipaddress.ip_interface(v)
    except ValueError:
        raise ValueError(f"{what} 不是合法的 IP 地址: {v!r}") from None
    return v


def from_dict(d: dict) -> Topology:
    """从需求采集表（YAML/JSON）构建拓扑；缺省的链路 id、接口名、IP、环回自动规划。

    自动规划跳过表中已显式使用的取值；设备名 / 链路 id / 接口名不合法、重复，或地址重复时报错，
    保证构建出的拓扑与采集表一致（不会静默合并或覆盖）。"""
    plan = d.get("ip_plan", {})
    p2p_pool = ipaddress.ip_network(plan.get("p2p_pool", "10.10.0.0/16"))
    lo_pool = list(ipaddress.ip_network(plan.get("loopback_pool", "10.255.0.0/24")).hosts())
    sv = lambda v: str(v) if isinstance(v, int) and not isinstance(v, bool) else v   # noqa: E731  YAML 中的纯数字名
    nodes: Dict[str, Node] = {}
    for nd in d["nodes"]:
        n = Node(**{k: v for k, v in nd.items() if k in Node.__dataclass_fields__})
        n.name = sv(n.name)
        _check_name(n.name, "设备名")
        if n.name in nodes:
            raise ValueError(f"设备名重复: {n.name}")
        if n.role not in ROLES:
            raise ValueError(f"未知设备角色 {n.role}")
        if n.loopback:
            _check_addr(n.loopback, f"设备 {n.name} 的 loopback")
        if n.role in ("host", "ems") and "agent" not in nd:
            n.agent = True
        nodes[n.name] = n
    vantage = sv(d.get("vantage", "ems"))
    if vantage not in nodes:
        raise ValueError(f"网管设备 {vantage} 不在设备清单中")
    # 环回：未填的按地址池顺序分配（第 k 台路由设备取第 k 个），跳过已被显式占用的地址（router-id 不得重复）
    used_lo = {n.loopback.split("/")[0] for n in nodes.values() if n.loopback}
    for k, n in enumerate([x for x in nodes.values() if x.transit]):
        if n.loopback:
            continue
        while k < len(lo_pool) and str(lo_pool[k]) in used_lo:
            k += 1
        if k >= len(lo_pool):
            raise ValueError("环回地址池耗尽")
        n.loopback = f"{lo_pool[k]}/32"
        used_lo.add(str(lo_pool[k]))
    used: Set[ipaddress.IPv4Network] = set()
    for ld in d["links"]:
        for key in ("a_ip", "b_ip"):
            if ld.get(key):
                used.add(ipaddress.ip_interface(_check_addr(ld[key], f"链路 {ld.get('id', '')} 的 {key}")).network)
    alias = {"bw": "bw_mbps", "delay": "delay_ms", "jitter": "jitter_ms"}
    lds = [{alias.get(k, k): (sv(v) if k in ("id", "a", "b", "a_if", "b_if") else v) for k, v in ld.items()}
           for ld in d["links"]]
    # 显式给出的链路 id、各设备接口名先登记，自动规划时跳过
    ids = [ld["id"] for ld in lds if ld.get("id")]
    for lid in ids:
        _check_name(lid, "链路 id")
    dup = sorted({x for x in ids if ids.count(x) > 1})
    if dup:
        raise ValueError(f"链路 id 重复: {', '.join(dup)}")
    taken_if: Dict[str, Set[str]] = {n: set() for n in nodes}
    for ld in lds:
        for end in ("a", "b"):
            n, ifn = ld.get(end), ld.get(f"{end}_if")
            if n not in nodes:
                raise ValueError(f"链路 {ld.get('id', '')} 引用了不存在的设备 {n}")
            if ifn:
                _check_name(ifn, f"设备 {n} 的接口名", IFNAME_RE, "_ . / : -", 64)
                if ifn in taken_if[n]:
                    raise ValueError(f"设备 {n} 的接口名重复: {ifn}")
                taken_if[n].add(ifn)
    ifcount: Dict[str, int] = {n: 0 for n in nodes}
    links: List[Link] = []
    used_ids = set(ids)
    for i, ld in enumerate(lds):
        lid = ld.get("id")
        if not lid:
            j = i + 1
            while f"L{j}" in used_ids:
                j += 1
            lid = f"L{j}"
            used_ids.add(lid)
        l = Link(id=lid, **{k: v for k, v in ld.items() if k in Link.__dataclass_fields__ and k != "id"})
        if l.a == l.b:
            raise ValueError(f"链路 {l.id} 两端是同一台设备 {l.a}")
        l.bw_mbps = _number(l.bw_mbps, f"链路 {l.id} 带宽", 1e-3, 1e7)
        l.delay_ms = _number(l.delay_ms, f"链路 {l.id} 时延", 0, 1e4)
        l.jitter_ms = _number(l.jitter_ms, f"链路 {l.id} 抖动", 0, 1e4)
        l.loss = _number(l.loss, f"链路 {l.id} 丢包率", 0, 0.99)
        l.cost = _number(l.cost, f"链路 {l.id} OSPF 开销", 1, 65535, integer=True)
        for end in ("a", "b"):
            n = getattr(l, end)
            ifcount[n] += 1
            if not getattr(l, f"{end}_if"):
                while f"eth{ifcount[n]}" in taken_if[n]:
                    ifcount[n] += 1
                setattr(l, f"{end}_if", f"eth{ifcount[n]}")
                taken_if[n].add(f"eth{ifcount[n]}")
        if not l.a_ip or not l.b_ip:
            l.a_ip, l.b_ip = _alloc_p2p(p2p_pool, used)
        links.append(l)
    seen: Dict[str, str] = {}
    for what, ip in ([(f"设备 {n.name} 的 loopback", n.loopback) for n in nodes.values() if n.loopback]
                     + [(f"链路 {l.id} 的 {e}_ip", getattr(l, f"{e}_ip")) for l in links for e in "ab"]):
        a = ip.split("/")[0]
        if a in seen:
            raise ValueError(f"地址重复: {a}（{seen[a]} 与 {what}）")
        seen[a] = what
    return Topology(name=d.get("name", "edge"), nodes=nodes, links=links,
                    vantage=vantage, description=d.get("description", ""))


def check_linux_ifnames(topo: Topology) -> None:
    """真实协议栈后端（netns / LXD）直接用接口名创建 Linux 接口：须 ≤15 字符、不含 / 与 :。
    由设备配置导入的厂商接口名（如 GigabitEthernet0/0/1）需在拓扑文件中改为 eth1 这类名字。"""
    bad = [f"{n}:{ifn}" for l in topo.links for n, ifn in ((l.a, l.a_if), (l.b, l.b_if))
           if not LINUX_IFNAME_RE.fullmatch(ifn)]
    if bad:
        raise ValueError("以下接口名不是合法的 Linux 接口名（≤15 字符，只含字母、数字和 _ . -），"
                         "真实协议栈后端无法创建，请在拓扑中改名（如 eth1）: " + ", ".join(bad))


def load(path: str) -> Topology:
    with open(path, encoding="utf-8") as f:
        return from_dict(yaml.safe_load(f))


def describe(topo: Topology) -> str:
    lines = [f"拓扑 {topo.name}: {len(topo.nodes)} 台设备, {len(topo.links)} 条链路, 网管={topo.vantage}"]
    for l in topo.links:
        lines.append(f"  {l.id:4s} {l.a}({l.a_ip}) <-> {l.b}({l.b_ip})  {l.bw_mbps:g}Mbps "
                     f"{l.delay_ms:g}ms cost={l.cost}")
    return "\n".join(lines)

