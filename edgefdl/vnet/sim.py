# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""解析式仿真后端：按 OSPF 最短路 + 逐包随机过程生成与真实测量同构的 RoundResult。

用途：毫秒级产出全量故障样本做预训练、CI 回归，以及无特权环境下的演示。
物理含义与 faults/injector 的 tc 语义保持一致（每个方向叠加 netem 参数）。
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..faults.model import Fault
from ..measure.model import DeepProbe, LsdbEntry, MeasurePlan, PingStat, RoundResult
from ..topology import Topology
from .base import Backend


class Transient:
    """瞬时噪声：持续一轮或数轮的时延尖峰 / 突发丢包（抗干扰测试的对象）。"""

    def __init__(self, link: str, kind: str, mag: float):
        self.link, self.kind, self.mag = link, kind, mag


class SimBackend(Backend):
    name = "sim"

    def __init__(self, topo: Topology, plan: Optional[MeasurePlan] = None, seed: int = 0,
                 proc_ms: float = 0.03, bw_noise: float = 0.08, noise_prob: float = 0.0,
                 hold_p: Sequence[float] = (1.0,), icmp_drop: float = 0.0):
        super().__init__(topo, plan)
        self.rng = np.random.default_rng(seed)
        self.targets = topo.targets()
        self.lids = topo.link_ids()
        self.lidx = {l: i for i, l in enumerate(self.lids)}
        self.proc_ms = proc_ms
        self.bw_noise = bw_noise
        self._route_cache: dict = {}
        self._matrix_cache: Dict[tuple, tuple] = {}
        self._bw_ptr = int(self.rng.integers(len(self.lids)))
        self.transient: List[Transient] = []
        self.noise_prob = noise_prob          # 在线演示时每轮出现瞬时噪声的概率
        self.hold_p = tuple(hold_p)           # 瞬时噪声持续 1、2、3… 轮的概率
        self._held: List[Tuple[Transient, int]] = []
        self.icmp_drop = icmp_drop            # 路由器对 ICMP 限速、逐包丢弃探测应答的概率
        self.router_tgt = np.array([topo.nodes[t.node].transit and t.kind in ("if", "lo") for t in self.targets])
        self.started = False
        self.t0 = time.time()

    # ------------------------------------------------------------ 生命周期
    def up(self):
        self.started = True

    def down(self):
        self.clear()
        self.started = False

    def _apply(self, fault: Fault):
        pass                                   # 状态由 self.active 表达，测量时实时折算

    def _revert(self, fault: Fault):
        pass

    # ------------------------------------------------------------ 当前网络状态
    def routing_state(self) -> Tuple[frozenset, Dict[str, float]]:
        down = frozenset(f.link for f in self.active.values() if f.type == "link_down")
        cost = {f.link: max(1, round(self.topo.link(f.link).cost * f.value))
                for f in self.active.values() if f.type == "route_drift"}
        return down, cost

    def link_arrays(self):
        """每条链路单向参数：基础时延、抖动均值/方差、丢包、有效带宽。"""
        L = len(self.lids)
        delay = np.array([self.topo.link(l).delay_ms for l in self.lids], dtype=float)
        jit_mu = np.zeros(L)
        jit_sd = np.array([self.topo.link(l).jitter_ms for l in self.lids], dtype=float)
        loss = np.array([self.topo.link(l).loss for l in self.lids], dtype=float)
        bw = np.array([self.topo.link(l).bw_mbps for l in self.lids], dtype=float)
        bw_eff = bw.copy()
        for f in self.active.values():
            i = self.lidx[f.link]
            if f.type == "delay":
                delay[i] += f.value
            elif f.type == "loss":
                loss[i] = 1 - (1 - loss[i]) * (1 - f.value)
            elif f.type == "jitter":
                jit_mu[i] += f.value          # netem: delay base+J, jitter J, normal
                jit_sd[i] = np.hypot(jit_sd[i], f.value)
            elif f.type == "bandwidth":
                bw_eff[i] = bw[i] * f.value
            elif f.type == "link_down":
                bw_eff[i] = 0.0
        return delay, jit_mu, jit_sd, loss, bw, bw_eff

    def traversal_matrix(self):
        """A[t, l] = 目标 t 的一次往返穿越链路 l 的次数；以及正向路径。按路由状态缓存。"""
        down, cost = self.routing_state()
        key = (down, tuple(sorted(cost.items())))
        if key in self._matrix_cache:
            return self._matrix_cache[key]
        T, L = len(self.targets), len(self.lids)
        A = np.zeros((T, L))
        reach = np.zeros(T, dtype=bool)
        paths: List[Tuple[str, ...]] = []
        for ti, t in enumerate(self.targets):
            fwd, rev = self.topo.route_to_target(self.topo.vantage, t, set(down), cost, self._route_cache)
            if fwd:
                reach[ti] = True
                for lid in self.topo.path_links(fwd) + self.topo.path_links(rev):
                    A[ti, self.lidx[lid]] += 1
            paths.append(tuple(fwd[1:]) if t.src is None else ())     # NQA 探测不做逐跳路径
        hops = np.array([max(1, len(p)) for p in paths], dtype=float)
        self._matrix_cache[key] = (A, reach, paths, hops)
        return self._matrix_cache[key]

    # ------------------------------------------------------------ 测量
    def measure_round(self, round_id: int, transient: Optional[List[Transient]] = None) -> RoundResult:
        p = self.plan
        rng = self.rng
        A, reach, paths, hops = self.traversal_matrix()
        delay, jit_mu, jit_sd, loss, bw, bw_eff = self.link_arrays()
        if transient is None:
            transient = self.random_transient(self.noise_prob) if self.noise_prob else self.transient
        for tr in transient:
            i = self.lidx[tr.link]
            if tr.kind == "spike":
                delay = delay.copy(); delay[i] += tr.mag
            elif tr.kind == "burst":
                loss = loss.copy(); loss[i] = 1 - (1 - loss[i]) * (1 - tr.mag)
        N, T, L = p.ping_count, len(self.targets), len(self.lids)
        # 逐包逐链路的单向时延样本（同一链路正反两次穿越近似共用一个样本）
        per = delay[None, :] + np.abs(jit_mu[None, :] + jit_sd[None, :] * rng.standard_normal((N, L)))
        rtt = per @ A.T + self.proc_ms * 2 * hops[None, :] + 0.05          # (N, T)
        surv = np.exp(A @ np.log(np.clip(1 - loss, 1e-9, 1)))               # (T,)
        got = (rng.random((N, T)) < surv[None, :]) & reach[None, :]
        if self.icmp_drop:
            got &= ~((rng.random((N, T)) < self.icmp_drop) & self.router_tgt[None, :])
        rr = RoundResult(ts=time.time(), round_id=round_id, vantage=self.topo.vantage)
        do_trace = p.traceroute_every > 0 and round_id % p.traceroute_every == 0
        for ti, t in enumerate(self.targets):
            g = got[:, ti]
            k = int(g.sum())
            if k:
                x = rtt[g, ti]
                ps = PingStat(t.id, N, k, float(x.min()), float(x.mean()), float(x.max()),
                              float(np.sqrt(max(0.0, (x ** 2).mean() - x.mean() ** 2))),
                              paths[ti] if do_trace else ())
            else:
                ps = PingStat(t.id, N, 0, path=())
            rr.pings[t.id] = ps
        # 轻量带宽估计（包列离散度法）：轮询若干链路，估计值含乘性噪声
        for _ in range(min(p.bw_links_per_round, L)):
            i = self._bw_ptr % L
            self._bw_ptr += 1
            est = bw_eff[i] * float(np.exp(rng.normal(0, self.bw_noise))) if bw_eff[i] > 0 else 0.0
            rr.bw[self.lids[i]] = min(est, bw[i] * 1.05)
        down, cost = self.routing_state()
        for l in self.topo.links:
            rr.lsdb[l.id] = LsdbEntry(adj_up=l.id not in down, cost=float(cost.get(l.id, l.cost)))
        rr.probe_bytes = p.light_bytes_per_round(T, float(hops.mean()))
        return rr

    def deep_probe(self, link: str) -> DeepProbe:
        p = self.plan
        i = self.lidx[link]
        delay, jit_mu, jit_sd, loss, bw, bw_eff = self.link_arrays()
        n = p.deep_ping_count
        nbytes = n * 98 * 2 + int(bw[i] * 1e6 / 8 * p.deep_iperf_s * p.deep_iperf_rate_ratio)
        if bw_eff[i] <= 0:
            return DeepProbe(link, n, 0, float("nan"), float("nan"), 0.0, nbytes)
        one = delay[i] + np.abs(jit_mu[i] + jit_sd[i] * self.rng.standard_normal((n, 2)))
        rtt = one.sum(1) + 2 * self.proc_ms
        ok = self.rng.random((n, 2)) < (1 - loss[i])
        ok = ok.all(1)
        l = self.topo.link(link)
        if self.icmp_drop and (self.topo.nodes[l.a].transit or self.topo.nodes[l.b].transit):
            ok &= self.rng.random(n) >= self.icmp_drop        # 精测 ping 的应答同样受设备 ICMP 限速影响
        k = int(ok.sum())
        est = min(bw_eff[i] * float(np.exp(self.rng.normal(0, 0.03))), bw[i])
        if k == 0:
            return DeepProbe(link, n, 0, float("nan"), float("nan"), est, nbytes)
        x = rtt[ok]
        return DeepProbe(link, n, k, float(x.mean()), float(x.std()), est, nbytes)

    # ------------------------------------------------------------ 噪声
    def random_transient(self, prob: float = 0.3) -> List[Transient]:
        """每轮以 prob 概率新出现一个干扰，按 hold_p 持续若干轮；返回本轮生效的全部干扰。"""
        new = self._new_transient(prob)
        if len(self.hold_p) == 1:
            return new
        self._held = [(t, n) for t, n in self._held if n > 0]
        for t in new:
            self._held.append((t, 1 + int(self.rng.choice(len(self.hold_p), p=self.hold_p))))
        cur = [t for t, _ in self._held]
        self._held = [(t, n - 1) for t, n in self._held]
        return cur

    def _new_transient(self, prob: float) -> List[Transient]:
        if self.rng.random() >= prob:
            return []
        lid = self.lids[int(self.rng.integers(len(self.lids)))]
        if self.rng.random() < 0.5:
            return [Transient(lid, "spike", float(self.rng.uniform(10, 150)))]
        return [Transient(lid, "burst", float(self.rng.uniform(0.2, 0.8)))]

    def clear(self, link: Optional[str] = None) -> None:
        super().clear(link)
        if link is None:
            self._held = []
