# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""现网只读后端：在网管服务器上测量真实路由网络，对设备不写入任何东西。

默认只从本机发 ping 和 traceroute，目标是拓扑中的全部地址；解析与每轮流程与 netns / LXD 后端共用
（measure.probes、vnet.stack.KernelStackBackend）。其余测量都是可选的，由运维方显式提供：
  · --lsdb-cmd   一条 shell 命令，输出 FRR `show ip ospf database router json` 的 JSON（如经只读 ssh 账号）；
  · --endpoints  一个 YAML 文件，列出能在其上跑探测的设备及进入方式（命令前缀），只有两端都列出的链路才做包列
                 带宽估计和精测，列出的设备作为盲区链路 NQA 目标的发起端。
没有提供的测量按缺测处理：不写入 RoundResult、不进基线，引擎按 Backend.can_* 屏蔽相应动作，见 docs/deployment.md。
--infer-state 时由数据面推断链路中断与开销变化（StateInference），代替缺失的 LSDB 特征。

up() / down() 不创建也不改变任何东西；inject / clear 一律拒绝，LiveExec 只放行上述探测命令，所以即使上层误调
注入路径，命令也发不出去。
"""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

from ..faults.generator import drift_allowed
from ..faults.model import DRIFT_FACTORS, Fault
from ..measure import probes
from ..measure.model import NAN, LsdbEntry, MeasurePlan, RoundResult
from ..topology import Target, Topology
from .stack import KernelStackBackend, NodeExec, parse_lsdb

LSDB_TIMEOUT_S = 4.0
REFUSE = "live 后端只读：不向真实网络注入或清除故障，请在等价虚拟网络（sim / netns / lxd）上演练"


class ReadOnlyError(RuntimeError):
    """要求只读后端写入，或执行探测以外的命令。"""


def load_endpoints(path: Optional[str]) -> Dict[str, str]:
    """读探测端点 YAML：`endpoints: {设备名: 命令前缀}`。

    命令前缀之后接一个参数——一条 shell 命令行，例如 `ssh -o BatchMode=yes probe@10.255.0.2`
    或 `ip netns exec efl-agg1 sh -c`。"""
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        try:
            d = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ValueError(f"{path}: {e}") from None
    eps = d.get("endpoints") if isinstance(d, dict) else None
    if not isinstance(eps, dict) or not all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                            for k, v in eps.items()):
        raise ValueError(f"{path}: 须为 endpoints: {{设备名: 命令前缀}} 的 YAML 映射")
    return dict(eps)


def _is_probe(argv: Sequence[str]) -> bool:
    """放行的探测命令：ping、traceroute、iperf3 和包列收发脚本（measure.probes）。"""
    if not argv:
        return False
    if argv[0] in ("ping", "traceroute", "iperf3"):
        return True
    return argv[0] == "python3" and len(argv) > 2 and argv[1] == "-c" and argv[2] in (probes.TRAIN_RECV, probes.TRAIN_SEND)


class LiveExec(NodeExec):
    """本机直接执行；其他设备经运维方给定的命令前缀执行。探测以外的命令一律拒绝。"""

    def __init__(self, vantage: str, endpoints: Optional[Dict[str, str]] = None):
        super().__init__(False)
        self.vantage = vantage
        self.endpoints = {n: shlex.split(p) for n, p in (endpoints or {}).items()}

    def wrap(self, node: str, argv: Sequence[str]) -> List[str]:
        if not _is_probe(argv):
            raise ReadOnlyError(f"live 后端只执行探测命令，拒绝：{' '.join(argv)[:80]}")
        if node == self.vantage:
            return list(argv)
        if node not in self.endpoints:
            raise ReadOnlyError(f"设备 {node} 未列入探测端点（--endpoints）")
        return self.endpoints[node] + [" ".join(shlex.quote(a) for a in argv)]

    def vtysh_argv(self, node: str, lines: Sequence[str]) -> List[str]:
        raise ReadOnlyError("live 后端不向设备下发配置")

    def have(self, cmd: str) -> bool:
        return shutil.which(cmd) is not None

    def shell(self, cmd: str, timeout: float) -> Tuple[int, str, str]:
        """运行运维方提供的只读命令（--lsdb-cmd），返回 (返回码, 标准输出, 标准错误)。"""
        try:
            p = subprocess.run(["sh", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "", f"超时（{timeout:g}s）"
        except OSError as e:
            return 127, "", str(e)
        return p.returncode, p.stdout, p.stderr


class StateInference:
    """没有 LSDB 时由数据面推断控制面状态，结果按 LSDB 的形式交给特征层（--infer-state）。

    模型是带 LSDB 特征训练的：只把 LSDB 置空时，链路中断会被说成带宽或丢包，路由漂移没有告警（见
    results/live_missing.json）。这里只用本轮实测，连续 confirm 轮一致才算数，没有可调的学习参数：
      · 链路中断：链路两端的接口地址都不应答（网管上联只有对端地址），而至少一端设备还有别的地址应答——
        这排除了"整段网络不可达时下游链路全被判断"；
      · 开销变化：实测路径偏离参照路径时，在路径离开或新进入的路由器互联链路上试 DRIFT_FACTORS 各倍率，取与全部
        实测路径最一致的（链路, 倍率）。没有候选能解释至少 min_match 比例的改道目标时不推断。"""

    def __init__(self, topo: Topology, confirm: int = 2, min_match: float = 0.6):
        self.topo, self.confirm, self.min_match = topo, confirm, min_match
        ts = [t for t in topo.targets() if t.kind != "nqa"]
        self.targets = ts
        self.ends = {l.id: [t.id for t in ts if t.link == l.id] for l in topo.links}
        self.ref: Dict[str, Tuple[str, ...]] = {}
        self.cur: Dict[str, Tuple[str, ...]] = {}   # 各目标最近一次测到的路径（traceroute 按预算轮转，不是每轮都有）
        self.dead: Dict[str, int] = {l.id: 0 for l in topo.links}
        self.last_cost, self.cost_n = None, 0
        self._cache: dict = {}

    def set_reference(self, paths: Dict[str, Tuple[str, ...]]) -> None:
        """参照路径：现场基线学到的各目标路径。未给定时不推断开销。"""
        self.ref = {k: tuple(v) for k, v in paths.items() if v}

    def _links_of(self, path: Sequence[str]) -> set:
        out = set()
        for a, b in zip((self.topo.vantage,) + tuple(path), path):
            try:
                out.add(self.topo.link_between(a, b).id)
            except KeyError:                    # 路径上有拓扑里不相邻的设备（中间有未纳管设备）
                pass
        return out

    def _down(self, rr: RoundResult) -> set:
        p = rr.pings
        alive = {self.topo.vantage} | {t.node for t in self.targets if t.id in p and p[t.id].recv > 0}
        down = set()
        for l in self.topo.links:
            ids = [i for i in self.ends[l.id] if i in p and p[i].sent]
            hit = bool(ids) and all(p[i].recv == 0 for i in ids) and (l.a in alive or l.b in alive)
            self.dead[l.id] = self.dead[l.id] + 1 if hit else 0
            if self.dead[l.id] >= self.confirm:
                down.add(l.id)
        return down

    def _route(self, t: Target, down: set, cost: Dict[str, float]) -> Tuple[str, ...]:
        return tuple(self.topo.route_to_target(self.topo.vantage, t, set(down), cost, self._cache)[0][1:])

    def _cost(self, rr: RoundResult, down: set) -> Optional[Tuple[str, float]]:
        for t in self.targets:
            ps = rr.pings.get(t.id)
            if ps is not None and ps.path and t.id in self.ref:
                self.cur[t.id] = ps.path
        cur = self.cur
        moved = [t for t in self.targets if t.id in cur and cur[t.id] != self.ref[t.id]]
        if len(moved) < 2:                      # 单个目标改道可能只是等价多路径换了一条
            return None
        explained = sum(cur[t.id] == self._route(t, down, {}) for t in moved)
        if explained >= self.min_match * len(moved):        # 已推断的中断足以解释这些改道
            return None
        left, gained = set(), set()
        for t in moved:
            a, b = self._links_of(self.ref[t.id]), self._links_of(cur[t.id])
            left |= a - b
            gained |= b - a
        ok = lambda ls: sorted(l for l in ls if l not in down and drift_allowed(self.topo, l))
        # 路径离开的链路开销变大、路径新进入的链路开销变小；同分时取前者（路径离开是该链路的直接证据），再取变化小的
        cand = [(l, f) for f in sorted(x for x in DRIFT_FACTORS if x > 1) for l in ok(left)] + \
               [(l, f) for f in sorted((x for x in DRIFT_FACTORS if x < 1), reverse=True) for l in ok(gained)]
        best, score = None, 0
        all_t = [t for t in self.targets if t.id in cur]
        for lid, f in cand:
            c = {lid: max(1, round(self.topo.link(lid).cost * f))}
            n = sum(cur[t.id] == self._route(t, down, c) for t in moved)
            if n >= self.min_match * len(moved):
                n += sum(cur[t.id] == self._route(t, down, c) for t in all_t)      # 其余目标的路径也要说得通
                if n > score:
                    best, score = (lid, float(c[lid])), n
        return best

    def update(self, rr: RoundResult) -> Dict[str, LsdbEntry]:
        down = self._down(rr)
        c = self._cost(rr, down) if self.ref else None
        self.cost_n = self.cost_n + 1 if c is not None and c == self.last_cost else (1 if c is not None else 0)
        self.last_cost = c
        cost = {c[0]: c[1]} if c is not None and self.cost_n >= self.confirm else {}
        return {l.id: LsdbEntry(adj_up=l.id not in down, cost=float(cost.get(l.id, l.cost))) for l in self.topo.links}


class LiveBackend(KernelStackBackend):
    name = "live"
    read_only = True
    LINUX_IFNAMES = False                       # 现网接口名（GigabitEthernet0/0/1）只用于告警展示

    def __init__(self, topo: Topology, plan: Optional[MeasurePlan] = None, lsdb_cmd: Optional[str] = None,
                 endpoints: Optional[Dict[str, str]] = None, skip: Sequence[str] = (),
                 ex: Optional[LiveExec] = None, lsdb_timeout: float = LSDB_TIMEOUT_S,
                 infer_state: bool = False, trace_per_round: Optional[int] = None, **kw):
        """trace_per_round：每轮 traceroute 几个目标（轮转）。路由器的 TTL 超时报文普遍限速（Linux 缺省每秒 1 个，
        突发 6 个），每条路径都经过的上联路由器一轮只回得了这么多，全量 traceroute 大部分会缺跳，轮次还因等超时而
        拉长。缺省按每秒 1 个；0 表示每轮全部目标（确认设备不限速时）。"""
        super().__init__(topo, plan, use_frr=False, **kw)
        self.trace_per_round = max(1, int(self.plan.interval_s)) if trace_per_round is None else trace_per_round
        self._trace_cursor = 0
        self.ex = ex if ex is not None else LiveExec(topo.vantage, endpoints)
        unknown = sorted(set(self.ex.endpoints) - set(topo.nodes))
        if unknown:
            raise ValueError(f"探测端点中有拓扑外的设备：{', '.join(unknown)}")
        self.lsdb_cmd = lsdb_cmd
        self.lsdb_timeout = lsdb_timeout
        self.lsdb_reads = self.lsdb_fails = 0
        self.lsdb_error = ""
        self.infer = StateInference(topo) if infer_state and not lsdb_cmd else None
        self.skip_targets(skip)

    # ------------------------------------------------------------ 能力
    def _has_end(self, node: str) -> bool:
        return node == self.topo.vantage or node in self.ex.endpoints

    def can_probe(self, link: str) -> bool:
        l = self.topo.link(link)
        return self._has_end(l.a) and self._has_end(l.b)

    can_bw = can_probe

    def can_lsdb(self) -> bool:
        return bool(self.lsdb_cmd)

    def _measurable(self, t: Target) -> bool:
        return t.src is None or self._has_end(t.src)

    def skip_targets(self, ids: Sequence[str]) -> None:
        """这些目标不再探测（如设备不回 ICMP）：按缺测处理，不算不可达。"""
        self.skipped = sorted(set(ids))
        self._targets = [t for t in self.topo.targets() if self._measurable(t) and t.id not in self.skipped]

    @property
    def pace_s(self) -> float:
        return self.plan.interval_s

    # ------------------------------------------------------------ 生命周期：不创建、不改变任何东西
    def up(self) -> None:
        pass

    def down(self) -> None:
        pass

    def inject(self, fault: Fault) -> None:
        raise ReadOnlyError(REFUSE)

    def clear(self, link: Optional[str] = None) -> None:
        raise ReadOnlyError(REFUSE)

    def _apply(self, fault: Fault) -> None:
        raise ReadOnlyError(REFUSE)

    def _revert(self, fault: Fault) -> None:
        raise ReadOnlyError(REFUSE)

    def _run_cmds(self, cmds) -> None:
        raise ReadOnlyError(REFUSE)

    # ------------------------------------------------------------ 测量
    def _bw_cand(self) -> List[str]:
        return [l.id for l in self.topo.links if self.can_bw(l.id)]

    def _trace_list(self) -> List[Target]:
        ts = super()._trace_list()
        k = self.trace_per_round
        if not k or k >= len(ts):
            return ts
        out = [ts[(self._trace_cursor + i) % len(ts)] for i in range(k)]
        self._trace_cursor = (self._trace_cursor + k) % len(ts)
        return out

    def _trace(self, ip: str, retries: Optional[int] = None) -> Tuple[Tuple[str, ...], int]:
        # 按预算轮转时不重测：重测只会多耗设备的限速额度，下一圈再测到
        return super()._trace(ip, retries=(0 if self.trace_per_round else 1) if retries is None else retries)

    def _train_q(self, link_id: str, n: int, size: int, port_off: int = 0, clip: float = 1.05):
        try:
            return super()._train_q(link_id, n, size, port_off, clip)
        except OSError:                         # 端点命令本身起不来（前缀里的程序不存在）：本次估计缺测
            return NAN, 0, False

    def _parse_lsdb(self, text: str) -> Dict[str, LsdbEntry]:
        """FRR JSON → 各链路状态。没有任何已知路由器的 router-LSA 时抛 ValueError：空结果不能读成"全部邻接断开"。"""
        js = json.loads(text)
        areas = ((js.get("routerLinkStates") or {}).get("areas") or {}) if isinstance(js, dict) else {}
        rids = {n.loopback.split("/")[0] for n in self.topo.nodes.values() if n.loopback}
        lsas = [x for a in areas.values() for x in a]
        if not any(x.get("advertisingRouter") in rids and x.get("routerLinks") for x in lsas):
            raise ValueError("输出中没有拓扑内路由器的 router-LSA 链路信息"
                             "（应为 show ip ospf database router json，router-id 应等于环回地址）")
        return parse_lsdb(js, self.topo, self._lsdb_cost)

    def measure_round(self, round_id: int) -> RoundResult:
        res = super().measure_round(round_id)
        if self.infer is not None:
            res.lsdb = self.infer.update(res)
        return res

    def set_reference(self, paths: Sequence[Tuple[str, ...]]) -> None:
        """基线就绪后给状态推断参照路径（与 topo.targets() 同序）。"""
        if self.infer is not None:
            self.infer.set_reference({t.id: p for t, p in zip(self.topo.targets(), paths)})

    def _read_lsdb(self) -> Dict[str, LsdbEntry]:
        if not self.lsdb_cmd:
            return {}
        self.lsdb_reads += 1
        rc, out, err = self.ex.shell(self.lsdb_cmd, self.lsdb_timeout)
        try:
            if rc != 0:
                raise ValueError(f"返回码 {rc}：{err.strip()[:200]}")
            res = self._parse_lsdb(out)
        except ValueError as e:                 # json.JSONDecodeError 也是 ValueError；本轮 LSDB 缺测
            self.lsdb_fails += 1
            if not self.lsdb_error:             # 只在由好转坏时报一次
                print(f"--lsdb-cmd 读取失败，LSDB 按缺测处理：{e}", file=sys.stderr, flush=True)
            self.lsdb_error = str(e)
            return {}
        self.lsdb_error = ""
        return res

    def health(self) -> dict:
        return dict(read_only=True, lsdb=self.can_lsdb(), infer_state=self.infer is not None,
                    trace_per_round=self.trace_per_round, lsdb_reads=self.lsdb_reads, lsdb_fails=self.lsdb_fails,
                    lsdb_error=self.lsdb_error, endpoints=sorted(self.ex.endpoints), targets=len(self._targets),
                    skipped=self.skipped, not_measured=[t.id for t in self.topo.targets() if not self._measurable(t)])

    def status(self) -> dict:
        d = super().status()
        d.update(self.health(), routing="live")
        return d


# ---------------------------------------------------------------------- 上线前检查
def _expected_path(topo: Topology, t: Target) -> Tuple[str, ...]:
    return tuple(topo.route_to_target(topo.vantage, t)[0][1:])


def preflight(b: LiveBackend, count: int = 3) -> dict:
    """只读检查一遍接入条件。ok 为 False 表示有地址不应答或工具不可用，其余差异只提示。"""
    topo = b.topo
    rep: dict = dict(topology=topo.name, vantage=topo.vantage, problems=[], notes=[])
    # 工具与权限
    miss = [c for c in ("ping", "traceroute") if not b.ex.have(c)]
    for c in miss:
        rep["problems"].append(f"本机没有 {c} 命令")
    # 地址应答
    ts = list(b._targets)
    pings = {}
    if "ping" not in miss:
        with ThreadPoolExecutor(b.workers) as pool:
            fs = {t.id: pool.submit(b._ping, t.src or topo.vantage, t.ip, t.id, count, 0.2) for t in ts}
            pings = {tid: f.result() for tid, f in fs.items()}
    silent = [t for t in ts if t.id in pings and pings[t.id].recv == 0]
    rep["answered"], rep["total"] = len(ts) - len(silent), len(ts)
    rep["silent"] = [dict(id=t.id, ip=t.ip, node=t.node, kind=t.kind, link=t.link) for t in silent]
    if silent:
        rep["problems"].append(f"{len(silent)} 个地址不应答：" + ", ".join(f"{t.ip}（{t.id}）" for t in silent))
    nm = [t.id for t in topo.targets() if not b._measurable(t)]
    rep["not_measured"] = nm
    if nm:
        rep["notes"].append(f"{len(nm)} 个盲区链路目标不测（需要设备原生 NQA / IP SLA，或把发起端列入 --endpoints）："
                            + ", ".join(nm))
    # traceroute：权限与路径是否与拓扑一致；按预算定速，不触发设备的 ICMP 限速
    alive = [t for t in ts if t.id in pings and pings[t.id].recv > 0 and t.src is None]
    gap = b.plan.interval_s / b.trace_per_round if b.trace_per_round else 0.0
    mism, unknown = [], []
    if alive and "traceroute" not in miss:
        rc, out = b.ex.run(topo.vantage, probes.traceroute_cmd(alive[0].ip), quiet=True, timeout=8)
        if rc != 0 and not probes.parse_traceroute(out):
            rep["problems"].append("traceroute 运行失败：ICMP 方式（-I）需要 root 或 CAP_NET_RAW，"
                                   "见 docs/deployment.md 的 ICMP permissions")
        else:
            for t in alive:
                time.sleep(gap)
                got = b._trace(t.ip)[0]
                want = _expected_path(topo, t)
                if not got:
                    unknown.append(t.id)
                elif got != want:
                    mism.append(dict(id=t.id, ip=t.ip, measured=list(got), expected=list(want)))
    rep["path_unknown"], rep["path_mismatch"] = unknown, mism
    if unknown:
        rep["notes"].append(f"{len(unknown)} 个目标的逐跳路径不完整（设备不回 TTL 超时或限速）：" + ", ".join(unknown[:8]))
    if mism:
        rep["notes"].append(f"{len(mism)} 个目标的实测路径与拓扑计算不同（基线学习以实测为准；若非等价多路径请核对拓扑）")
    # LSDB
    rep["lsdb"] = "已由 --infer-state 从 ping 与 traceroute 推断" if b.infer is not None else \
        "未配置（--lsdb-cmd）：LSDB 特征按缺测处理，链路中断的类型和路由漂移缺证据，建议加 --infer-state"
    if b.lsdb_cmd:
        ls = b._read_lsdb()
        if not ls:
            rep["lsdb"] = "读取失败"
            rep["problems"].append(f"--lsdb-cmd 不可用：{b.lsdb_error}")
        else:
            down = sorted(l for l, e in ls.items() if not e.adj_up)
            cost = sorted(l.id for l in topo.links if l.id in ls and abs(ls[l.id].cost - l.cost) > 0.5)
            rep["lsdb"] = dict(links=len(ls), down=down, cost_differs=cost)
            if down:
                rep["notes"].append("LSDB 中邻接未建立的链路：" + ", ".join(down))
            if cost:
                rep["notes"].append("LSDB 开销与拓扑不同的链路（基线学习以实测为准）：" + ", ".join(cost))
    # 探测端点
    links = [l.id for l in topo.links if b.can_bw(l.id)]
    rep["probe_links"] = links
    rep["endpoints"] = {}
    for lid in links:
        bw, _, clean = b._train_q(lid, b.plan.bw_train_pkts, b.plan.bw_pkt_size)
        rep["endpoints"][lid] = None if bw != bw else round(bw, 1)
        if bw != bw:
            rep["problems"].append(f"{lid} 的探测端点不可用（包列带宽估计失败）")
    if not links:
        rep["notes"].append("未配置探测端点（--endpoints）：带宽估计与精测按缺测处理")
    rep["ok"] = not rep["problems"]
    return rep


def format_preflight(rep: dict) -> str:
    out = [f"拓扑 {rep['topology']}，网管 {rep['vantage']}：{rep['answered']}/{rep['total']} 个地址应答"]
    for s in rep["silent"]:
        where = f"链路 {s['link']}" if s["link"] else "环回"
        out.append(f"  不应答：{s['ip']}  {s['id']}（设备 {s['node']}，{where}）")
    for m in rep["path_mismatch"][:8]:
        out.append(f"  路径不同：{m['id']} 实测 {'>'.join(m['measured'])}，拓扑 {'>'.join(m['expected'])}")
    out.append(f"LSDB：{rep['lsdb']}")
    if rep["probe_links"]:
        out.append("探测端点：" + "，".join(f"{k} {v} Mbps" for k, v in rep["endpoints"].items()))
    out += [f"提示：{x}" for x in rep["notes"]]
    out += [f"问题：{x}" for x in rep["problems"]]
    out.append("检查通过" if rep["ok"] else "检查未通过")
    return "\n".join(out)
