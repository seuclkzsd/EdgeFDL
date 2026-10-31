# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""现网只读后端：在网管服务器上测量真实路由网络，不改任何设备的配置。

默认只从本机发 ping 和 traceroute，目标是拓扑中的全部地址；解析与每轮流程与 netns / LXD 后端共用
（measure.probes、vnet.stack.KernelStackBackend）。其余测量都是可选的，由运维方显式提供：
  · --lsdb-cmd   一条 shell 命令，输出 FRR `show ip ospf database router json` 的 JSON（如经只读 ssh 账号）；
  · --endpoints  一个 YAML 文件，列出能在其上跑探测的设备及进入方式（命令前缀），只有两端都列出的链路才做包列
                 带宽估计和精测（网管本机算一端，此时探测进程也在本机运行），列出的设备作为盲区链路 NQA 目标的发起端，
                 由它 ping 对端代替设备原生 NQA / IP SLA 探测。读取设备原生探测的结果尚未实现（计划 0.3）。
没有提供的测量按缺测处理：不写入 RoundResult、不进基线，引擎按 Backend.can_* 屏蔽相应动作，见 docs/deployment.md。
--infer-state 时由数据面推断链路中断与开销变化（StateInference），代替缺失的 LSDB 特征。

up() / down() 不创建也不改变任何东西；inject / clear 一律拒绝，LiveExec 只放行上述探测命令，所以即使上层误调
注入路径，命令也发不出去。
"""
from __future__ import annotations

import json
import math
import os
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

from ..faults.generator import drift_allowed
from ..faults.model import DRIFT_FACTORS, Fault
from ..measure import probes
from ..measure.model import NAN, LsdbEntry, MeasurePlan, PingStat, RoundResult
from ..topology import Target, Topology
from .stack import KernelStackBackend, NodeExec, parse_lsdb

LSDB_TIMEOUT_S = 4.0
READY_TIMEOUT_S = 3.0                           # 等端点上的探测接收端就绪的上限：端点不可达时不能卡住整轮测量
DEEP_CHECK_PINGS = 3                            # 上线前检查用几个包试精测的短间隔 ping
REFUSE = "live 后端只读：不向真实网络注入或清除故障，请在等价虚拟网络（sim / netns / lxd）上演练"


class ReadOnlyError(RuntimeError):
    """要求只读后端写入，或执行探测以外的命令。"""


def load_endpoints(path: Optional[str]) -> Dict[str, str]:
    """读探测端点 YAML：`endpoints: {设备名: 命令前缀}`。

    命令前缀之后接一个参数——一条 shell 命令行，例如 `ssh -o BatchMode=yes -o ConnectTimeout=3 probe@10.255.0.2`
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
    for k, v in eps.items():
        try:
            argv = shlex.split(v)
        except ValueError as e:
            raise ValueError(f"{path}: {k} 的命令前缀无法解析（{e}）：{v}") from None
        if not argv or not argv[0]:
            raise ValueError(f"{path}: {k} 的命令前缀为空")
    return dict(eps)


def bundle_plan(plan: MeasurePlan) -> MeasurePlan:
    """live 后端采用的测量计划。探测量（包数、包长、速率、时长）不由模型包决定，一律取缺省值，文档写明的探测上限才成立；
    模型包只决定研判窗口，以及比缺省值更慢的测量周期（更快的周期由运维方用 --interval 显式给出）。"""
    p = MeasurePlan()
    if isinstance(plan.interval_s, (int, float)) and math.isfinite(plan.interval_s):
        p.interval_s = max(p.interval_s, float(plan.interval_s))
    if isinstance(plan.window, int) and 1 <= plan.window <= 60:
        p.window = plan.window
    keys = [k for k in vars(p) if vars(p)[k] != vars(plan)[k]]
    if keys:
        print(f"模型包的测量计划里 {', '.join(keys)} 超出 live 后端允许的范围，已改用缺省值", file=sys.stderr, flush=True)
    return p


def limited(argv: Sequence[str], seconds: float) -> List[str]:
    """给命令加运行时限（coreutils timeout）。端点上的 iperf3 不能靠本机杀进程收尾：杀掉 ssh 不会结束对端进程。"""
    return ["timeout", f"{int(math.ceil(seconds))}"] + list(argv)


def _finish(p: subprocess.Popen, wait_s: float = 0.0) -> None:
    """收尾一个探测进程。先发 SIGTERM：本机的 timeout 收到后会转发给 iperf3，收到 SIGKILL 则会把 iperf3 留下。"""
    try:
        p.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        p.terminate()
        try:
            p.wait(timeout=1)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    p.stdout.close()


def _wait_output(p: subprocess.Popen, mark: str, timeout: float) -> bool:
    """等进程的标准输出出现 mark。进程已退出或超时返回 False。直接读文件描述符：select 看不到文件对象内部缓冲的行。"""
    fd, end, seen = p.stdout.fileno(), time.monotonic() + timeout, ""
    while True:
        left = end - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            return False
        chunk = os.read(fd, 4096)
        if not chunk:
            return False
        seen += chunk.decode("utf-8", "replace")
        if mark in seen:
            return True


def _is_probe(argv: Sequence[str]) -> bool:
    """放行的探测命令：ping、traceroute、iperf3 和包列收发脚本（measure.probes）。"""
    if not argv:
        return False
    if argv[0] == "timeout":                    # 只放行 limited() 给 iperf3 加的时限
        return len(argv) > 2 and re.fullmatch(r"\d+", argv[1]) is not None and argv[2] == "iperf3"
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
        """运行运维方提供的只读命令（--lsdb-cmd），返回 (返回码, 标准输出, 标准错误)。
        命令单独成一个进程组，超时时整组杀掉：只杀 sh 的话，复合命令里的子进程每轮都会留下一个。"""
        try:
            p = subprocess.Popen(["sh", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 universal_newlines=True, start_new_session=True)
        except OSError as e:
            return 127, "", str(e)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
            p.stdout.close()
            p.stderr.close()
            return 124, "", f"超时（{timeout:g}s）"
        return p.returncode, out, err


class StateInference:
    """没有 LSDB 时由数据面推断控制面状态，结果按 LSDB 的形式交给特征层（--infer-state）。

    模型是带 LSDB 特征训练的：只把 LSDB 置空时，链路中断会被说成带宽或丢包，路由漂移没有告警（见
    results/live_missing.json）。这里是固定规则，只用实测的 ping 与 traceroute，连续 confirm 轮一致才算数：
      · 链路中断：链路两端的接口地址都不应答（网管上联只有对端地址），而至少一端设备还有别的地址应答——
        这排除了"一段网络整体不可达时，其中的链路全被判为中断"；
      · 开销变化：至少 2 个目标的实测路径换到了开销不同的路径（等价路径之间换不算）时，在路径离开或新进入的路由器
        互联链路上试 DRIFT_FACTORS 各倍率，取与全部实测路径最一致的（链路, 倍率）。没有候选能解释至少 min_match
        比例的改道目标时不推断。"""

    def __init__(self, topo: Topology, confirm: int = 2, min_match: float = 0.6):
        self.topo, self.confirm, self.min_match = topo, confirm, min_match
        ts = [t for t in topo.targets() if t.kind != "nqa"]
        self.targets = ts
        self.ends = {l.id: [t.id for t in ts if t.link == l.id] for l in topo.links}
        self.ref: Dict[str, Tuple[str, ...]] = {}
        self.cur: Dict[str, Tuple[str, ...]] = {}   # 各目标最近一次测到的路径（traceroute 按预算轮转，不是每轮都有）
        self.dead: Dict[str, int] = {l.id: 0 for l in topo.links}
        self.last_cost, self.cost_n = None, 0
        self.was_down: set = set()
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

    def _to_target(self, t: Target, path: Sequence[str]) -> float:
        """按拓扑开销算这条路径到目标的总开销。接口地址按其网段计：网段开销只算一次，从网段哪一端进入都一样，
        所以到同一网段的等价路径开销相等（这类目标的路径本来就可能在两端之间换）。"""
        ls = self._links_of(path)
        own = self.topo.link(t.link).cost if t.link else 0.0
        return sum(self.topo.link(l).cost for l in ls if l != t.link) + own

    def _route(self, t: Target, down: set, cost: Dict[str, float]) -> Tuple[str, ...]:
        return tuple(self.topo.route_to_target(self.topo.vantage, t, set(down), cost, self._cache)[0][1:])

    def _cost(self, rr: RoundResult, down: set) -> Optional[Tuple[str, float]]:
        for t in self.targets:
            ps = rr.pings.get(t.id)
            if ps is not None and ps.path and t.id in self.ref:
                self.cur[t.id] = ps.path
        cur = self.cur
        moved = [t for t in self.targets if t.id in cur and self._to_target(t, cur[t.id]) != self._to_target(t, self.ref[t.id])]
        if len(moved) < 2:                      # 等价路径之间换一条不算改道；单个目标改道也可能只是测量偶然
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
        if self.was_down - down:                # 链路恢复：中断期间测到的绕行路径已经过时
            self.cur.clear()
        self.was_down = down
        c = self._cost(rr, down) if self.ref else None
        self.cost_n = self.cost_n + 1 if c is not None and c == self.last_cost else (1 if c is not None else 0)
        self.last_cost = c
        cost = {c[0]: c[1]} if c is not None and self.cost_n >= self.confirm else {}
        return {l.id: LsdbEntry(adj_up=l.id not in down, cost=float(cost.get(l.id, l.cost))) for l in self.topo.links}


class LiveBackend(KernelStackBackend):
    name = "live"
    read_only = True
    LINUX_IFNAMES = False                       # 现网接口名（GigabitEthernet0/0/1）只用于告警展示
    READY_TIMEOUT_S = READY_TIMEOUT_S

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
    def _ping(self, node: str, ip: str, tid: str, count: int, interval: float) -> PingStat:
        """返回码 0 / 1 才是 ping 跑完了（1 = 没有收到应答）。其余（2 参数或权限，124 超时，127 无此命令，255 ssh 失败）
        以及没有统计输出的 1（前缀自身的失败）都按缺测处理（sent=0）："没跑成"不是"没应答"，后者是链路中断的证据。"""
        try:
            rc, out = self.ex.run(node, probes.ping_cmd(ip, count, interval, self.plan.ping_size), quiet=True,
                                  timeout=count * interval + 4)
        except OSError:                         # 前缀里的程序不存在
            return PingStat(target=tid, sent=0, recv=0)
        if rc not in (0, 1):
            return PingStat(target=tid, sent=0, recv=0)
        return probes.parse_ping(out, tid, 0 if rc else count)

    def _iperf_want(self, l, est: float) -> float:
        """精测在怀疑有故障时才跑，链路此时的容量可能远低于标称值：按本次包列估计与标称带宽中较小者的比例限速。
        包列没有给出可信估计时只能按标称带宽。"""
        return min(l.bw_mbps, est if est == est else l.bw_mbps) * self.plan.deep_iperf_rate_ratio

    def _iperf(self, src: str, dst: str, dst_ip: str, port: int, want: float) -> Tuple[float, int]:
        """与虚拟网络的差别都在失败路径上：服务端自带时限，确认在监听后才发流，起不来或运行失败时吞吐按缺测处理。"""
        s = self.plan.deep_iperf_s
        try:
            srv = self.ex.popen(dst, limited(probes.iperf3_server_cmd(port) + ["--forceflush"],
                                             s + 2 + self.READY_TIMEOUT_S + 4))
        except OSError:
            return NAN, 0
        wait = 0.0
        try:
            if not _wait_output(srv, "listening", self.READY_TIMEOUT_S):
                return NAN, 0
            wait = 2.0
            _, out = self.ex.run(src, limited(probes.iperf3_client_cmd(dst_ip, s, want, port, udp=True), s + 4),
                                 quiet=True, timeout=s + 6)
            return probes.parse_iperf3(out)
        except OSError:
            return NAN, 0
        finally:
            _finish(srv, wait)

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
        except (ValueError, TypeError, AttributeError) as e:     # 坏 JSON，或合法 JSON 但结构不对；本轮 LSDB 缺测
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


def _is_local(ip: str) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind((ip, 0))
        return True
    except OSError:
        return False


def preflight(b: LiveBackend, count: int = 3) -> dict:
    """只读检查一遍接入条件。ok 为 False 表示有地址不应答或工具不可用，其余差异只提示。"""
    topo = b.topo
    rep: dict = dict(topology=topo.name, vantage=topo.vantage, problems=[], notes=[])
    # 工具与权限
    miss = [c for c in ("ping", "traceroute") if not b.ex.have(c)]
    for c in miss:
        rep["problems"].append(f"本机没有 {c} 命令")
    links = [l.id for l in topo.links if b.can_bw(l.id)]
    if any(topo.vantage in (topo.link(lid).a, topo.link(lid).b) for lid in links):   # 网管本机也是探测的一端
        for c in ("python3", "iperf3", "timeout"):
            if not b.ex.have(c):
                rep["problems"].append(f"本机没有 {c} 命令：网管所在的链路要做包列带宽估计和精测")
    own = [l.ip_of(topo.vantage) for l in topo.links if topo.vantage in (l.a, l.b)]
    if own and not any(_is_local(ip) for ip in own):
        rep["notes"].append(f"本机没有拓扑给网管的地址（{', '.join(own)}）：确认在网管上运行，或按实际地址修正拓扑")
    # 地址应答
    ts = list(b._targets)
    pings = {}
    if "ping" not in miss:
        with ThreadPoolExecutor(b.workers) as pool:
            fs = {t.id: pool.submit(b._ping, t.src or topo.vantage, t.ip, t.id, count, 0.2) for t in ts}
            pings = {tid: f.result() for tid, f in fs.items()}
    silent = [t for t in ts if t.id in pings and pings[t.id].sent and pings[t.id].recv == 0]
    failed = [t for t in ts if t.id in pings and not pings[t.id].sent]      # ping 没跑成：不是地址不应答，--skip-silent 不适用
    rep["answered"], rep["total"] = sum(1 for p in pings.values() if p.recv > 0), len(ts)
    rep["silent"] = [dict(id=t.id, ip=t.ip, node=t.node, kind=t.kind, link=t.link) for t in silent]
    rep["failed"] = [dict(id=t.id, ip=t.ip, node=t.src or topo.vantage) for t in failed]
    if silent:
        rep["problems"].append(f"{len(silent)} 个地址不应答：" + ", ".join(f"{t.ip}（{t.id}）" for t in silent))
    if failed:
        rep["problems"].append(f"{len(failed)} 个目标的 ping 没能运行（命令失败或探测端点不可用）："
                               + ", ".join(f"{t.ip}（{t.id}）" for t in failed))
    nm = [t.id for t in topo.targets() if not b._measurable(t)]
    rep["not_measured"] = nm
    if nm:
        rep["notes"].append(f"{len(nm)} 个盲区链路目标不测（把发起端列入 --endpoints 即可测；读取设备原生 NQA / IP SLA "
                            "结果尚未实现，计划 0.3）：" + ", ".join(nm))
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
    rep["probe_links"] = links
    rep["endpoints"] = {}
    for lid in links:
        bw, _, clean = b._train_q(lid, b.plan.bw_train_pkts, b.plan.bw_pkt_size)
        rep["endpoints"][lid] = None if bw != bw else round(bw, 1)
        if bw != bw:
            rep["problems"].append(f"{lid} 的探测端点不可用（包列带宽估计失败）")
        l = topo.link(lid)                      # 精测的短间隔 ping：普通用户在老版本 iputils 上会被拒绝（返回码 2）
        src, dst = (l.a, l.b) if topo.nodes[l.a].agent else (l.b, l.a)
        i = b.plan.deep_ping_interval_s
        if not b._ping(src, l.ip_of(dst), lid, DEEP_CHECK_PINGS, i).sent:
            rep["problems"].append(f"{lid} 的精测不可用：{src} 上 {i:g} s 间隔的 ping 运行失败，"
                                   "见 docs/deployment.md 的 ICMP permissions")
    for n in sorted({x for lid in links for x in (topo.link(lid).a, topo.link(lid).b)}):
        try:
            rc = b.ex.run(n, limited(["iperf3", "--version"], 5), quiet=True, timeout=8)[0]
        except OSError:
            rc = 127
        if rc != 0:
            rep["problems"].append(f"{n} 上运行不了 iperf3 或 timeout（精测需要）")
    if not links:
        rep["notes"].append("未配置探测端点（--endpoints）：带宽估计与精测按缺测处理")
    rep["ok"] = not rep["problems"]
    return rep


def format_preflight(rep: dict) -> str:
    out = [f"拓扑 {rep['topology']}，网管 {rep['vantage']}：{rep['answered']}/{rep['total']} 个地址应答"]
    for s in rep["silent"]:
        where = f"链路 {s['link']}" if s["link"] else "环回"
        out.append(f"  不应答：{s['ip']}  {s['id']}（设备 {s['node']}，{where}）")
    for s in rep.get("failed", ()):
        out.append(f"  ping 没能运行：{s['ip']}  {s['id']}（发起设备 {s['node']}）")
    for m in rep["path_mismatch"][:8]:
        out.append(f"  路径不同：{m['id']} 实测 {'>'.join(m['measured'])}，拓扑 {'>'.join(m['expected'])}")
    out.append(f"LSDB：{rep['lsdb']}")
    if rep["probe_links"]:
        out.append("探测端点：" + "，".join(f"{k} {v} Mbps" for k, v in rep["endpoints"].items()))
    out += [f"提示：{x}" for x in rep["notes"]]
    out += [f"问题：{x}" for x in rep["problems"]]
    out.append("检查通过" if rep["ok"] else "检查未通过")
    return "\n".join(out)
