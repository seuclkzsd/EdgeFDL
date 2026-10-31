# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""同台对比：未改动的 EdgeFDL 在线引擎 vs 现有工具与研究背后的方法（按公开描述重新实现）。

所有方法吃同一条 RoundResult 流：引擎 step() 时在后端 measure_round 上挂一个只读旁路（Recorder），
记下引擎看到的每一轮轻量测量；基线方法随后在记录上逐轮、不看未来地回放，同种子、同瞬时干扰、同故障。
EdgeFDL 还可以调用精测，基线方法不回放精测；对照行 edgefdl_no_deep 关掉全部精测，输入才与基线方法相同。

基线方法（均只读测量，不调用精测）：
  threshold   逐目标阈值告警（Prometheus for: / Zabbix 触发器）：每目标一条告警，不指名链路
  grouped     Alertmanager group_by / OpenNMS ALEC 式：时间窗 + 拓扑距离聚成一个情景，候选根因取告警路径共有的最深链路
  scfs        布尔层析：最小一致故障集（Duffield 2006），好路径排除链路
  score       风险模型贪心集合覆盖（Kompella 2005 SCORE，2007 用于黑洞定位）：错误阈值过滤命中率，选覆盖最大者
  netbouncer  正则化最小二乘（Tan 2019）：min Σ(y_j-Πx_i)²+λΣx_i(1-x_i) 坐标下降；另对 RTT 超出量做同形式的非负回归
  vote007     路径投票（Arzani 2018）：坏流给路径上每条链路投 1/h，算法 1 迭代选最高票并扣除其解释的流

每种基线都拿到 EMS 能有的最好信息：每轮由 LSDB 重算的路由矩阵（含回程）、每条链路两端接口与 NQA 目标的 ping、
改道后按路径代数折算的期望 RTT、与引擎同样的 RTT 基线跟踪（η=1/360）和背景丢包跟踪。"+ems" 变体再加三条规则：
OSPF 邻接断开、开销变化（连续 2 轮）、带宽估计连续 2 次低于基线 70%，门限与 EdgeFDL 判决器相同。
默认参数取典型配置或论文值；tuned 在不同种子的验证集上网格搜索取 F1 最优，lowfa 取验证集误报 ≤1 条/天里 F1 最优的
一组（运维实际会用的工作点），再到测试集评分。NetBouncer 另报告只改 λ 的各点：验证集分不开的设置结果并不相同。
EdgeFDL 在每个条件、每个种子上另跑一遍不用精测的版本（edgefdl_no_deep，含正常运行的误报统计）。
口径见 docs/comparison.md。
"""
from __future__ import annotations

import itertools
import json
import multiprocessing as mp
import os
import pickle
import shutil
import time
from collections import deque
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .agent.features import Baseline, FeatureEncoder, min_lost
from .faults.model import FAULT_TYPES
from .robust import C, HarshSim, _site, dual_cases, node_cases, single_cases
from .topology import from_dict

TEST_SEEDS = (1001, 1002, 1003, 1004, 1005)        # 运行前固定，不按结果挑选
VAL_SEED = 2001
AFTER = 72                  # 故障持续轮数（6 min），长于 5 min 窗口的默认规则，避免基线因窗口过短而漏报
TAIL = 12                   # 故障消除后继续观察的轮数
HOLD = 12                   # 基线告警的保持轮数：实体连续 HOLD 轮未被判出才算恢复（类似 keep_firing_for: 1m）
BW_DROP, CP_K, BW_K = 0.7, 2, 2     # "+ems" 规则门限，取 EdgeFDL 判决器（core/denoise.py）的同名参数
ETA = 1 / 360               # RTT 与背景丢包的跟踪速率，同 FeatureEncoder.track
LOW_FA = 1.0                # 低误报工作点：验证集正常运行误报上限（条/天）
NODE_K = 3                  # 设备级规则：全部地址连续不通的轮数
JITTER_RATIO = 0.2          # 类型规则：RTT 抖动 ≥ 0.2 × RTT 超出量判为抖动（netem 式抖动约 0.6，固定时延约 0，取几何中点）
RTT_FLOOR_MS = 2.0          # 类型规则：路径 RTT 超出量低于此值时不算时延类证据，判为丢包


# ---------------------------------------------------------------------- 测试条件
def conditions(rounds: int) -> List[Dict]:
    """与 robust.online_matrix 同义的苛刻条件；rounds 是该条件下一段连续运行的总轮数（忙时抬升的周期）。"""
    return [C("standard"), C("icmp_limit_5pct_new", dict(icmp_drop=0.05)),
            C("icmp_limit_5pct_known", dict(icmp_drop=0.05), relearn=True),
            C("transient_15s", dict(hold=3)), C("background_jitter_1ms", sigma=1.0),
            C("busy_hour_drift_1ms", dict(swing_ms=1.0, period=rounds))]


# ---------------------------------------------------------------------- 只读旁路
class Recorder:
    """把每轮 RoundResult 压成数组。用自己的编码器（基线副本），只读 rr，不碰引擎状态。"""

    def __init__(self, enc: FeatureEncoder):
        self.enc = enc
        self.sent, self.recv, self.rtt, self.mdev = [], [], [], []
        self.adj, self.cost, self.bw, self.rid = [], [], [], []
        self.routes: List[np.ndarray] = []
        self._ids: Dict[int, int] = {}
        self._lsdb = {}

    @property
    def n(self) -> int:
        return len(self.sent)

    def add(self, rr):
        e = self.enc
        s, r, t, m = e.window_arrays([rr])
        self.sent.append(s[0].astype(np.int8))
        self.recv.append(r[0].astype(np.int8))
        self.rtt.append(t[0].astype(np.float32))
        self.mdev.append(m[0].astype(np.float32))
        self._lsdb = rr.lsdb or self._lsdb            # 整轮缺测时沿用上一轮
        A = e.traversal(self._lsdb)
        rid = self._ids.get(id(A))
        if rid is None:
            rid = self._ids[id(A)] = len(self.routes)
            self.routes.append(A)
        self.rid.append(rid)
        self.adj.append([self._lsdb[l].adj_up if l in self._lsdb else True for l in e.lids])
        self.cost.append([self._lsdb[l].cost if l in self._lsdb else e.base.cost[i] for i, l in enumerate(e.lids)])
        self.bw.append([rr.bw.get(l, np.nan) for l in e.lids])

    def tap(self, backend):
        """在后端 measure_round 上挂旁路；引擎代码不变，返回值原样传回。"""
        orig = backend.measure_round

        def measure_round(round_id, transient=None):
            rr = orig(round_id, transient)
            self.add(rr)
            return rr
        backend.measure_round = measure_round

    def arrays(self) -> Dict:
        st = lambda x, t: np.array(x, dtype=t)
        return dict(sent=st(self.sent, np.int8), recv=st(self.recv, np.int8), rtt=st(self.rtt, np.float32),
                    mdev=st(self.mdev, np.float32), adj=st(self.adj, bool), cost=st(self.cost, np.float32),
                    bw=st(self.bw, np.float32), rid=st(self.rid, np.int32), routes=self.routes)


class Evidence:
    """基线方法共用的证据：逐轮超出量、动态背景丢包门限、路由矩阵与规则事件。所有量只用当前及以前的轮次。"""

    def __init__(self, enc: FeatureEncoder, d: Dict):
        self.enc, self.topo = enc, enc.topo
        self.__dict__.update(d)
        self.lids = enc.lids
        self.T, self.L, self.R = enc.T, enc.L, len(self.sent)
        A0 = enc.A0
        # 每穿越一次链路的基线时延：基线 RTT 对路由矩阵的最小二乘（环上链路的两端接口不在同一条路径上，不能用远近端差）
        rb = np.nan_to_num(enc.base.rtt, nan=0.0)
        d_link = np.linalg.lstsq(A0, rb, rcond=None)[0]
        shift = np.stack([(A - A0) @ d_link for A in self.routes])    # 改道后的期望 RTT 偏移
        self.dead = (self.sent > 0) & (self.recv == 0)
        self.lost = (self.sent.astype(int) - self.recv).astype(np.int8)
        self.e = np.full((self.R, self.T), np.nan, np.float32)        # 相对改道后期望 RTT 的超出量
        self.e0 = self.e.copy()                                       # 相对固定路由基线的超出量（逐目标规则用）
        self.mlost = np.zeros((self.R, self.T), np.int8)
        self.bgl = np.zeros((self.R, self.T), np.float32)             # 跟踪到的背景丢包率
        b, bg = enc.base.rtt.astype(float).copy(), enc.base.loss.astype(float).copy()
        n = int(self.sent.max()) if self.R else 5
        ml = min_lost(n, bg)
        thr = enc.thr_rtt
        for r in range(self.R):
            x = self.rtt[r] - b
            self.e0[r] = x
            d = x - shift[self.rid[r]]
            self.e[r] = d
            ok = np.abs(np.nan_to_num(d, nan=1e9)) < thr
            b[ok] += ETA * d[ok]
            live = (self.sent[r] > 0) & (self.lost[r] <= 1)           # 单轮丢 2 个及以上视为突发，不计入背景
            bg[live] += ETA * (self.lost[r][live] / n - bg[live])
            if r % 60 == 0:
                ml = min_lost(n, np.minimum(bg, 0.05))
            self.mlost[r] = ml
            self.bgl[r] = bg
        self.md = self.mdev - np.nan_to_num(enc.base.mdev, nan=0.1)[None, :]
        self.Inc = [self._incidence(A) for A in self.routes]
        self.depth = self._depth()
        self.ends = [np.array([i for i, t in enumerate(enc.targets) if t.link == l], dtype=int) for l in self.lids]
        self.dist = self._hops()
        self.node_targets = {n: [i for i, t in enumerate(enc.targets) if t.node == n and t.kind != "nqa"]
                             for n, v in self.topo.nodes.items() if v.transit}
        self.node_links = {n: [self.lids.index(l.id) for l in self.topo.links if n in (l.a, l.b)]
                           for n in self.node_targets}
        self.node_adj = {n: [self.lids.index(l.id) for l in self.topo.links if n in (l.a, l.b)
                             and self.topo.nodes[l.other(n)].transit] for n in self.node_targets}
        self.cp_down, self.cp_cost, self.bw_low = self._rule_events()
        self.base_bw = enc.base.bw
        self._flag_cache: Dict = {}

    def _incidence(self, A: np.ndarray) -> np.ndarray:
        """目标对链路的依赖：可达目标取当前路由；不可达目标取基线路由加其地址所在链路（链路断开时网段撤销）。"""
        e, Inc = self.enc, A > 0
        for ti in np.flatnonzero(~Inc.any(1)):
            Inc[ti] = e.A0[ti] > 0
            t = e.targets[ti]
            if t.link is not None:
                Inc[ti, e.lidx[t.link]] = True
        return Inc

    def _depth(self) -> np.ndarray:
        dist, _ = self.topo.spf(self.topo.vantage)
        hops = {self.topo.vantage: 0}
        frontier = [self.topo.vantage]
        while frontier:
            nxt = []
            for u in frontier:
                if u != self.topo.vantage and not self.topo.nodes[u].transit:
                    continue
                for v, _l in self.topo._adj[u]:
                    if v not in hops:
                        hops[v] = hops[u] + 1
                        nxt.append(v)
            frontier = nxt
        return np.array([min(hops[self.topo.link(l).a], hops[self.topo.link(l).b]) for l in self.lids], dtype=float)

    def _hops(self) -> np.ndarray:
        names = list(self.topo.nodes)
        idx = {n: i for i, n in enumerate(names)}
        D = np.full((len(names), len(names)), 99.0)
        np.fill_diagonal(D, 0.0)
        for l in self.topo.links:
            D[idx[l.a], idx[l.b]] = D[idx[l.b], idx[l.a]] = 1.0
        for k in range(len(names)):
            D = np.minimum(D, D[:, [k]] + D[[k], :])
        ti = [idx[t.node] for t in self.enc.targets]
        return D[np.ix_(ti, ti)]

    def _rule_events(self):
        """规则事件（"+ems"）：邻接断开、开销变化连续 CP_K 轮，带宽估计连续 BW_K 次低于基线 BW_DROP。"""
        down = ~self.adj
        drift = np.abs(self.cost - self.enc.base.cost[None, :]) > 0.5
        run2 = lambda m: np.vstack([np.zeros((CP_K - 1, self.L), bool), np.logical_and.reduce(
            [m[i:self.R - CP_K + 1 + i] for i in range(CP_K)])]) if self.R >= CP_K else np.zeros_like(m)
        low = np.zeros((self.R, self.L), bool)
        for li in range(self.L):
            hist: deque = deque(maxlen=BW_K)
            cur = False
            for r in np.flatnonzero(np.isfinite(self.bw[:, li])):
                hist.append(0 < self.bw[r, li] < BW_DROP * self.enc.base.bw[li])     # 估计为 0 是链路中断，归邻接规则
                nxt = len(hist) == BW_K and all(hist)
                if nxt != cur:
                    low[r:, li] = nxt
                    cur = nxt
        return run2(down), run2(drift), low

    # ------------------------------------------------------------------ 路径状态
    def flags(self, rtt, lost, static: bool = False) -> np.ndarray:
        """逐轮逐目标异常。rtt/lost 为 "auto" 时用 EdgeFDL 判决器自身的门限（thr_rtt、thr_mdev、二项检验）。"""
        key = (rtt, lost, static)
        f = self._flag_cache.get(key)
        if f is None:
            x = self.e0 if static else self.e
            mr, mm = (self.enc.thr_rtt, self.enc.thr_mdev) if rtt == "auto" else (float(rtt), float(rtt))
            ml = self.mlost if lost == "auto" else int(lost)
            with np.errstate(invalid="ignore"):
                f = self.dead | (self.lost >= ml) | (np.nan_to_num(x) > mr) | (np.nan_to_num(self.md) > mm)
            f &= self.sent > 0
            self._flag_cache[key] = f
        return f

    def win_loss(self, s: int, e: int, w: int) -> np.ndarray:
        """本段内最近 w 轮聚合丢包率减背景丢包率（Prometheus avg_over_time 式）：5 个包一轮分辨率太粗，窗口聚合才分得开
        15% 的链路丢包和 5% 的 ICMP 限速。"""
        lost, sent = (np.cumsum(x[s:e].astype(np.int32), 0) for x in (self.lost, self.sent))
        lw, sw = lost.copy(), sent.copy()
        if len(lost) > w:
            lw[w:], sw[w:] = lost[w:] - lost[:-w], sent[w:] - sent[:-w]
        return lw / np.maximum(sw, 1) - self.bgl[s:e]

    def persist(self, fl: np.ndarray, k: int, n: int, g: int = 1) -> Tuple[np.ndarray, np.ndarray]:
        """本段内 k-of-n 持续异常（首轮起算，不看未来）。返回 (bad, good)：good = 窗口内异常不超过 g 轮
        （默认 1，同判决器 k_off：单轮瞬时干扰不取消一条路径的"好"）。"""
        c = np.cumsum(fl.astype(np.int32), 0)
        cnt = c.copy()
        if len(fl) > n:
            cnt[n:] = c[n:] - c[:-n]
        return cnt >= k, cnt <= g


def path_state(ev: Evidence, s: int, e: int, p: Dict, static: bool = False) -> Tuple[np.ndarray, np.ndarray]:
    """路径状态 (bad, good)：k-of-n 持续异常；给了 lossw 时另加窗口聚合丢包判据（窗口不短于 6 轮）。"""
    bad, good = ev.persist(ev.flags(p["rtt"], p["lost"], static=static)[s:e], p["k"], p["n"], p.get("g", 1))
    x = p.get("lossw")
    if x:
        lw = ev.win_loss(s, e, max(p["n"], 6))
        bad = bad | (lw >= x)
        good = good & (lw < x / 2)
    return bad, good


# ---------------------------------------------------------------------- 定位算法（单次推断）
def scfs(Inc: np.ndarray, bad: np.ndarray, good: np.ndarray, depth: np.ndarray, kmax: int = 5,
         tie: str = "near") -> List[int]:
    """最小一致故障集：好路径上的链路排除，其余链路中取能解释全部（可解释的）坏路径的最小集合；
    并列时默认取最靠近网管的（Duffield 2006 在树上的取法），tie="deep" 取最深的。无法解释的坏路径（噪声）忽略。"""
    cand = np.flatnonzero(~Inc[good].any(0))
    rows = Inc[bad][:, cand]
    rows = rows[rows.any(1)]
    if not len(rows):
        return []
    masks = [int("".join("1" if v else "0" for v in rows[:, j]), 2) for j in range(len(cand))]
    full = (1 << len(rows)) - 1
    order = sorted(range(len(cand)), key=lambda j: (depth[cand[j]], cand[j]))
    for k in range(1, min(kmax, len(cand)) + 1):
        best = None
        for co in itertools.combinations(order, k):
            m = 0
            for j in co:
                m |= masks[j]
            if m == full:
                key = ((1 if tie == "near" else -1) * sum(depth[cand[j]] for j in co), co)
                if best is None or key < best:
                    best = key
        if best is not None:
            return [int(cand[j]) for j in best[1]]
    return score(Inc, bad, good, 0.0, depth)


def score(Inc: np.ndarray, bad: np.ndarray, good: np.ndarray, thr: float, depth: np.ndarray) -> List[int]:
    """SCORE 贪心：命中率 |G∩O|/|G| ≥ 错误阈值的风险组里选覆盖最大者，直到观测全被解释或无组可选。
    分母只算状态明确的路径（坏或好），对基线有利。并列时取更深的链路。"""
    known = Inc[bad | good].sum(0)
    hit = Inc[bad].sum(0) / np.maximum(known, 1)
    o = bad.copy()
    out: List[int] = []
    while o.any():
        cov = Inc[o].sum(0).astype(float)
        cov[hit < thr] = 0
        cov[out] = 0
        if cov.max() <= 0:
            break
        top = np.flatnonzero(cov == cov.max())
        li = int(max(top, key=lambda i: (hit[i], depth[i], -i)))
        out.append(li)
        o &= ~Inc[:, li]
    return out


def vote007(Inc: np.ndarray, bad: np.ndarray, tau: float, depth: np.ndarray) -> List[int]:
    """007 算法 1：坏流给路径上每条链路投 1/h；票数 ≥ tau×总票数时选最高票，扣除经过它的坏流（假定都因它而丢）。
    单一网管下上游链路与故障链路同票，按 ECMP 的假设分不开；并列取最深的链路，这是对该方法最有利的取法。"""
    flows = np.flatnonzero(bad & Inc.any(1))
    if not len(flows):
        return []
    v = Inc[flows] / Inc[flows].sum(1, keepdims=True)
    total = v.sum()
    out: List[int] = []
    alive = np.ones(len(flows), bool)
    while alive.any():
        votes = v[alive].sum(0)
        if votes.max() < tau * total - 1e-12:
            break
        top = np.flatnonzero(votes >= votes.max() - 1e-9)
        li = int(max(top, key=lambda i: (depth[i], -i)))
        out.append(li)
        alive &= ~Inc[flows, li]
    return out


NB_GRID = 1 - np.array([0, .002, .005, .01, .02, .03, .05, .07, .1, .13, .16, .2, .25, .3, .35, .4, .5, .6, .7, .8, .9, 1])


def nb_loss(A: np.ndarray, y: np.ndarray, lam: float, eps: float = 1e-4, maxit: int = 30) -> np.ndarray:
    """NetBouncer 图 5 的坐标下降：min Σ_j (y_j-Π_i x_i^{A_ji})² + λ Σ_i x_i(1-x_i)，x∈[0,1]，初值取经过该链路路径的平均。
    往返穿越同一链路两次，指数不为 1，没有闭式解，逐坐标在 22 点网格上取最优。"""
    has = A.sum(0) > 0
    x = np.ones(A.shape[1])
    for i in np.flatnonzero(has):
        x[i] = y[A[:, i] > 0].mean()
    x = np.clip(x, 1e-6, 1)
    logx = np.log(np.maximum(x, 1e-12))
    pred = np.exp(A @ logx)
    f = ((y - pred) ** 2).sum() + lam * (x * (1 - x)).sum()
    g = NB_GRID
    lg = np.log(np.maximum(g, 1e-12))
    for _ in range(maxit):
        for i in np.flatnonzero(has):
            js = np.flatnonzero(A[:, i] > 0)
            rest = A[js] @ logx - A[js, i] * logx[i]
            val = np.exp(rest[None, :] + np.outer(lg, A[js, i]))
            c = ((y[js][None, :] - val) ** 2).sum(1) + lam * g * (1 - g)
            b = int(c.argmin())
            x[i], logx[i] = g[b], lg[b]
        pred = np.exp(A @ logx)
        nf = ((y - pred) ** 2).sum() + lam * (x * (1 - x)).sum()
        if f - nf < eps:
            break
        f = nf
    return x


def nb_delay(A: np.ndarray, e: np.ndarray, lam: float, maxit: int = 40) -> np.ndarray:
    """同形式的非负回归（原文只建模丢包；这一路让它也能看到时延与抖动）：min Σ(e_j-Σ A_ji d_i)² + λ Σ d_i，d≥0。"""
    d = np.zeros(A.shape[1])
    res = e.copy()
    nrm = (A ** 2).sum(0)
    for _ in range(maxit):
        ch = 0.0
        for i in np.flatnonzero(nrm):
            rho = A[:, i] @ res + nrm[i] * d[i]
            new = max(0.0, (rho - lam / 2) / nrm[i])
            if new != d[i]:
                res += A[:, i] * (d[i] - new)
                ch = max(ch, abs(new - d[i]))
                d[i] = new
        if ch < 1e-3:
            break
    return d


# ---------------------------------------------------------------------- 类型规则与告警生命周期
def classify(ev: Evidence, r: int, li: int, Inc: np.ndarray, bad: np.ndarray, s: int) -> str:
    """基线没有分类器，给一条简单、写明的规则：两端地址全不通 → 链路中断；经过该链路的坏路径 RTT 超出量不足
    → 丢包；RTT 抖动与超出量同量级 → 抖动；否则时延。不会给出带宽与路由漂移（它们只能由 "+ems" 规则给出）。"""
    lo = max(s, r - 2)
    ends = ev.ends[li]
    if len(ends) and ev.dead[max(s, r - 1):r + 1][:, ends].all():
        return "link_down"
    p = bad & Inc[:, li]
    if not p.any():
        p = Inc[:, li] & (ev.sent[r] > 0)
    if not p.any():
        return "loss"
    x = ev.e[lo:r + 1][:, p]
    m = ev.md[lo:r + 1][:, p]
    if not np.isfinite(x).any():
        return "loss"
    med = float(np.nanmedian(x))
    if med < RTT_FLOOR_MS:
        return "loss"
    return "jitter" if float(np.nanmedian(m)) >= JITTER_RATIO * med else "delay"


def node_dead(ev: Evidence, r: int, s: int, rules: bool = False) -> List[str]:
    """设备级规则：一台路由设备的全部地址连续 NODE_K 轮不通（且不是全网失联）即判设备宕机；rules=True 时改看 LSDB：
    地址连续 2 轮不通且与相邻路由设备的邻接全部断开（瞬时突发丢包不会断邻接）。"""
    k = 2 if rules else NODE_K
    if r - k + 1 < s:
        return []
    d = np.logical_and.reduce(ev.dead[r - k + 1:r + 1])
    out = [n for n, ts in ev.node_targets.items() if ts and d[ts].all()
           and (not rules or (ev.node_adj[n] and not ev.adj[r, ev.node_adj[n]].any()))]
    return out if len(out) * 2 < len(ev.node_targets) else []


class Raise:
    """告警生命周期：实体被判出且距上次判出超过 HOLD 轮即发一条新告警；首次发出时定类型。"""

    def __init__(self, hold: int = HOLD):
        self.hold = hold
        self.last: Dict = {}
        self.out: List[Tuple[int, str, str, Optional[str]]] = []

    def see(self, r: int, kind: str, ent: str, ftype) -> None:
        p = self.last.get((kind, ent))
        if p is None or r - p > self.hold:
            self.out.append((r, kind, ent, ftype() if callable(ftype) else ftype))
        self.last[(kind, ent)] = r


def link_alarms(ev: Evidence, s: int, e: int, inferred: Dict[int, Tuple[List[int], np.ndarray, np.ndarray]],
                rules: bool) -> List[Tuple]:
    """把每轮推断的链路集合变成告警：优先合并为设备级，rules=True 时并入 "+ems" 规则事件。"""
    rz = Raise()
    rounds = set(inferred)
    if rules:
        ev_rounds = np.flatnonzero((ev.cp_down[s:e] | ev.cp_cost[s:e] | ev.bw_low[s:e]).any(1)) + s
        rounds |= set(int(x) for x in ev_rounds)
    dead_rounds = np.flatnonzero(ev.dead[s:e].any(1)) + s
    rounds |= set(int(x) for x in dead_rounds)
    for r in sorted(rounds):
        ls, bad, Inc = inferred.get(r, ([], None, ev.Inc[ev.rid[r]]))
        ls = list(ls)
        nd = node_dead(ev, r, s, rules)
        for n in nd:
            rz.see(r, "node", n, "node_down")
        gone = {x for n in nd for x in ev.node_links[n]}
        if rules:
            for li in np.flatnonzero(ev.cp_down[r] | ev.cp_cost[r] | ev.bw_low[r]):
                li = int(li)
                if li in gone:
                    continue
                ft = "link_down" if ev.cp_down[r, li] else "route_drift" if ev.cp_cost[r, li] else "bandwidth"
                rz.see(r, "link", ev.lids[li], ft)
                if li in ls:
                    ls.remove(li)
        for li in ls:
            if li not in gone:
                b = bad if bad is not None else np.zeros(ev.T, bool)
                rz.see(r, "link", ev.lids[li], lambda li=li, b=b: classify(ev, r, li, Inc, b, s))
    return rz.out


# ---------------------------------------------------------------------- 各方法：逐轮回放
def run_threshold(ev: Evidence, s: int, e: int, p: Dict) -> List[Tuple]:
    if p.get("mode") == "zabbix":
        return _zabbix(ev, s, e, p)
    return _target_alarms(ev, s, path_state(ev, s, e, p, static=True)[0])


def _zabbix(ev: Evidence, s: int, e: int, p: Dict) -> List[Tuple]:
    """Zabbix "ICMP Ping" 模板：响应时间 5 分钟均值 > 150 ms，丢包率 5 分钟 > 20%，连续 3 次不通。
    模板的丢包触发器取 5 个 1 分钟检查的最小值；这里 5 s 一轮，取 60 轮均值，比最小值灵敏，对基线有利。
    窗口未满时按此前正常补齐（场景前网络平稳）。"""
    w = p["win"]
    rtt = np.nan_to_num(ev.rtt[s:e], nan=0.0).astype(float)
    loss = ev.lost[s:e] / np.maximum(ev.sent[s:e], 1)
    base = np.nan_to_num(ev.enc.base.rtt, nan=0.0)
    rtt = np.where(ev.recv[s:e] > 0, rtt, base)
    pad = np.tile(base, (w - 1, 1))
    roll = lambda x: (lambda c: (c[w:] - c[:-w]) / w)(np.cumsum(np.vstack([np.zeros((1, ev.T)), x]), 0))
    high = roll(np.vstack([pad, rtt])) > p["rtt_ms"]
    lossy = roll(np.vstack([np.zeros((w - 1, ev.T)), loss])) > p["loss"]
    dk = p["dead_k"]
    dead, _ = ev.persist(ev.dead[s:e], dk, dk)
    return _target_alarms(ev, s, high | lossy | dead)


def _target_alarms(ev: Evidence, s: int, bad: np.ndarray) -> List[Tuple]:
    out = []
    for ti in np.flatnonzero(bad.any(0)):
        rs = np.flatnonzero(bad[:, ti])
        for r in rs[np.r_[True, np.diff(rs) > HOLD]]:
            out.append((int(r) + s, "target", ev.enc.targets[ti].id, None))
    return sorted(out, key=lambda a: a[0])


def infer_tomo(ev: Evidence, s: int, e: int, p: Dict, fn) -> Dict:
    """逐轮把持续异常的路径交给 fn(Inc, bad, good)，同一路由 + 同一路径状态只算一次。"""
    bad, good = path_state(ev, s, e, p)
    good = good & (ev.sent[s:e] > 0)
    out: Dict = {}
    memo: Dict = {}
    for i in np.flatnonzero(bad.any(1)):
        r = int(i) + s
        rid = int(ev.rid[r])
        key = (rid, bad[i].tobytes(), good[i].tobytes())
        ls = memo.get(key)
        if ls is None:
            ls = memo[key] = fn(ev.Inc[rid], bad[i], good[i])
        out[r] = (ls, bad[i], ev.Inc[rid])
    return out


def infer_vote(ev: Evidence, s: int, e: int, p: Dict) -> Dict:
    """投票按 epoch 结算（论文 30 s = 6 轮）：epoch 内至少 m 轮异常的目标算一条坏流，结果在下一 epoch 内保持。"""
    fl = ev.flags(p["rtt"], p["lost"])[s:e]
    ep, m, x = p["epoch"], p["m"], p.get("lossw")
    lw = ev.win_loss(s, e, ep) if x else None
    out: Dict = {}
    for i in range(ep - 1, len(fl), ep):
        bad = fl[i - ep + 1:i + 1].sum(0) >= m
        if x:
            bad = bad | (lw[i] >= x)
        if not bad.any():
            continue
        r = i + s
        Inc = ev.Inc[int(ev.rid[r])]
        ls = vote007(Inc, bad, p["tau"], ev.depth)
        if ls:
            for j in range(ep):
                if r + j < e:
                    out[r + j] = (ls, bad, ev.Inc[int(ev.rid[r + j])])
    return out


def infer_nb(ev: Evidence, s: int, e: int, p: Dict) -> Dict:
    """每 step 轮用最近 n 轮聚合一次：路径成功率 y_j = 收/发，RTT 超出量取窗口中位数（滤掉单轮尖峰）。"""
    n, step, lam, thr, dthr = p["n"], p["step"], p["lam"], p["bad"], p["d_ms"]
    out: Dict = {}
    for i in range(n - 1 if s + n <= e else 0, e - s, step):
        r = i + s
        lo = max(s, r - n + 1)
        rid = int(ev.rid[r])
        A = ev.routes[rid].copy()
        Inc = ev.Inc[rid]
        dead_row = ~A.any(1)
        A[dead_row] = Inc[dead_row].astype(float)
        sent = ev.sent[lo:r + 1].sum(0).astype(float)
        recv = ev.recv[lo:r + 1].sum(0).astype(float)
        use = sent > 0
        y = recv[use] / np.maximum(sent[use], 1)
        bl = np.zeros(ev.L, bool)
        if len(y) and y.min() < 1:                       # 全部无丢包时 x=1 代价为 0，不必迭代；别的门限都会改变结果：
            x = nb_loss(A[use], y, lam)                  # 只穿越一次的链路 y≈x，λ 小时网格还会把略高于 thr 的 y 取整到 thr
            bl = x <= thr
        ew = ev.e[lo:r + 1]
        ok = np.isfinite(ew).any(0) & A.any(1)
        bd = np.zeros(ev.L, bool)
        if ok.any():
            with np.errstate(all="ignore"):
                med = np.nanmedian(ew[:, ok], 0)
            if med.max() >= dthr:
                bd = nb_delay(A[ok], np.maximum(med.astype(float), 0.0), lam) >= dthr
        ls = [int(i) for i in np.flatnonzero(bl | bd)]
        if ls:
            bad = (recv < sent) | (np.nan_to_num(ew[-1]) >= dthr)
            for j in range(step):
                if r + j < e:
                    out[r + j] = (ls, bad, ev.Inc[int(ev.rid[r + j])])
    return out


def infer_grouped(ev: Evidence, s: int, e: int, p: Dict) -> Dict:
    """逐目标告警（与 threshold 同样的持续判据，用改道折算后的超出量）按时间与拓扑距离聚成情景：与情景内某成员相距
    ≤ hops 跳、且距情景最近成员的时间 ≤ group_interval 的告警并入。group_wait 后发出一次通知，候选根因取情景内全部
    告警路径共有的最深链路；没有共有链路时不指名链路。情景只有在成员全部恢复 group_interval 后才关闭。"""
    bad = path_state(ev, s, e, p)[0]
    wait, itv, hops = p["wait"], p["interval"], p["hops"]
    sits: List[Dict] = []
    out: Dict = {}
    for i in np.flatnonzero(bad.any(1)):
        r = int(i) + s
        Inc = ev.Inc[int(ev.rid[r])]
        for ti in np.flatnonzero(bad[i]):
            hit = [x for x in sits if r - x["last"] <= itv and (ev.dist[ti, list(x["m"])] <= hops).any()]
            if not hit:
                x = dict(first=r, last=r, m=set(), link=None, done=False)
                sits.append(x)
                hit = [x]
            x = hit[0]
            for y in hit[1:]:                    # 新告警把两个情景连起来：合并
                x["m"] |= y["m"]
                x["first"] = min(x["first"], y["first"])
                x["done"] = x["done"] or y["done"]
                x["link"] = x["link"] if x["link"] is not None else y["link"]
                sits.remove(y)
            x["m"].add(int(ti))
            x["last"] = r
        for x in sits:
            if not x["done"] and r - x["first"] >= wait:
                x["done"] = True
                common = Inc[sorted(x["m"])].all(0)
                if common.any():
                    x["link"] = int(max(np.flatnonzero(common), key=lambda l: (ev.depth[l], -l)))
                    out.setdefault(r, ([], bad[i], Inc))
                    if x["link"] not in out[r][0]:
                        out[r][0].append(x["link"])
        for x in sits:                           # 情景存续期间候选根因保持在告警状态
            if x["done"] and x["link"] is not None and r - x["last"] <= HOLD:
                out.setdefault(r, ([], bad[i], Inc))
                if x["link"] not in out[r][0]:
                    out[r][0].append(x["link"])
    return out


def infer(method: str, ev: Evidence, s: int, e: int, p: Dict) -> Dict:
    if method == "scfs":
        return infer_tomo(ev, s, e, p, lambda I, b, g: scfs(I, b, g, ev.depth, tie=p.get("tie", "near")))
    if method == "score":
        return infer_tomo(ev, s, e, p, lambda I, b, g: score(I, b, g, p["hit"], ev.depth))
    if method == "vote007":
        return infer_vote(ev, s, e, p)
    if method == "netbouncer":
        return infer_nb(ev, s, e, p)
    if method == "grouped":
        return infer_grouped(ev, s, e, p)
    raise ValueError(method)


def run_method(method: str, ev: Evidence, s: int, e: int, p: Dict) -> Dict[str, List[Tuple]]:
    """返回 {变体: 告警}；告警 (轮, 实体种类 link/node/target, 实体, 类型)。"""
    if method == "threshold":
        return {"": run_threshold(ev, s, e, p)}
    inf = infer(method, ev, s, e, p)
    return {"": link_alarms(ev, s, e, inf, False), "+ems": link_alarms(ev, s, e, inf, True)}


# ---------------------------------------------------------------------- 参数
DET = dict(rtt="auto", lost="auto", k=3, n=6)      # EdgeFDL 判决器自身的门限与 3-of-6 持续判据
NO_ROUND_LOSS = 9                                  # 单轮丢包不算异常（5 个包不可能丢 9 个），只看窗口聚合丢包
DEFAULTS = {
    # Zabbix "ICMP Ping" 模板：{$ICMP_RESPONSE_TIME_WARN}=0.15 s，{$ICMP_LOSS_WARN}=20，max(icmpping,#3)=0
    "threshold": dict(mode="zabbix", rtt_ms=150.0, loss=0.20, win=60, dead_k=3),
    # Alertmanager 默认 group_wait=30s、group_interval=5m；路径状态取 EdgeFDL 判决器
    "grouped": dict(DET, wait=6, interval=60, hops=2),
    "scfs": dict(DET),
    "score": dict(DET, hit=0.6),                   # SCORE 仿真所用错误阈值 0.6
    # 007：30 s epoch，1% 总票门限，流有丢包即投票
    "vote007": dict(rtt="auto", lost="auto", epoch=6, m=1, tau=0.01),
    # NetBouncer：λ=1，1% 丢包判据，5 min 跳动窗口
    "netbouncer": dict(n=60, step=60, lam=1.0, bad=0.99, d_ms=1.0),
}


def grids() -> Dict[str, List[Dict]]:
    """各方法的网格。边缘不全是余量：选中点可能落在边缘，grid_edges() 把这种情况记进 compare.json，docs/comparison.md
    逐条写明。有的边缘不能再外推（hit=1.0、k=n=24 的 2 分钟持续判据、论文取值 tau=0.01），有的只是搜索范围到此为止。"""
    kn = ((1, 1), (2, 3), (3, 3), (3, 6), (4, 6), (6, 6), (9, 12), (12, 12), (18, 24), (24, 24))
    kn2 = kn[2:]
    lo = [dict(lost=x) for x in ("auto", 2, 3, 4)] + [dict(lost=NO_ROUND_LOSS, lossw=x) for x in (0.08, 0.12, 0.2)]
    lo2 = lo[:1] + lo[2:]
    base = [dict(rtt=r, k=k, n=n, **x) for r in ("auto", 5, 10, 20) for x in lo for k, n in kn]
    small = [dict(rtt=r, k=k, n=n, **x) for r in ("auto", 10, 20) for x in lo2 for k, n in kn2]
    return {
        "threshold": base,
        "scfs": [dict(b, g=g, tie=t) for b in small for g in (0, 1) for t in ("near", "deep")],
        "score": [dict(b, hit=h) for b in small for h in (0.6, 0.8, 1.0)],
        "grouped": [dict(b, wait=w, interval=60, hops=h) for b in small for w in (1, 6) for h in (1, 2, 3)],
        "vote007": [dict(rtt=r, epoch=ep, m=m, tau=t, **x) for r in ("auto", 10, 20)
                    for x in (dict(lost="auto"), dict(lost=3), dict(lost=NO_ROUND_LOSS, lossw=0.12))
                    for ep, ms in ((3, (1, 2, 3)), (6, (2, 3, 6)), (12, (3, 6, 12)), (24, (6, 12, 24)),
                                   (36, (9, 18, 36)), (48, (12, 24, 48)))
                    for m in ms for t in (0.01, 0.1, 0.25, 0.5)],
        "netbouncer": [dict(n=n, step=max(1, n // 2), lam=l, bad=b, d_ms=d) for n in (12, 24, 60)
                       for l in (0.01, 0.1, 1.0) for b in (0.8, 0.9, 0.97, 0.99) for d in (1.0, 3.0, 5.0, 8.0)],
    }


def grid_edges(p: Dict, grid: Sequence[Dict]) -> Dict[str, str]:
    """选中参数里落在网格边缘的数值键：{键: "min" 或 "max"}。字符串取值（"auto"、near/deep）与只有一个取值的键不算。"""
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    out: Dict[str, str] = {}
    for k, v in p.items():
        vals = {g[k] for g in grid if k in g and num(g[k])}
        if num(v) and len(vals) > 1 and v in (min(vals), max(vals)):
            out[k] = "min" if v == min(vals) else "max"
    return out


# ---------------------------------------------------------------------- 评分
def score_case(alarms: Sequence[Tuple], inj: int, case: Dict, after: int, interval: float) -> Dict:
    """告警按发出轮次相对注入时刻换算成 k（注入后第 1 轮 k=1）。与 bench 同口径：首条告警是否指向真值；
    另记全部告警。truth 里的实体是链路或设备。"""
    truth = case["truth"]
    ents = {x[1] for x in truth}
    al = [(r - inj + 1, kind, ent, ft) for r, kind, ent, ft in alarms]
    pre = sum(a[0] <= 0 for a in al)
    al = [a for a in al if a[0] > 0]
    inwin = [a for a in al if a[0] <= after]
    named = {a[2] for a in al if a[1] != "target"}
    pairs = {(a[3], a[2]) for a in al}
    first = inwin[0] if inwin else None
    right = next((a[0] for a in inwin if a[1] != "target" and a[2] in ents | set(case["links"])), None)
    wrong = sum(a[1] != "target" and a[2] not in ents and not (a[1] == "link" and a[2] in case["links"]) for a in al)
    return dict(n=len(al), pre=pre, det=first is not None, t=None if first is None else first[0] * interval,
                t_right=None if right is None else right * interval,
                first_link=bool(first and first[1] != "target" and first[2] in ents | set(case["links"])),
                first_joint=bool(first and (first[3], first[2]) in truth),
                all_link=ents <= named, all_joint=truth <= pairs, wrong=wrong,
                exact=bool(al) and wrong == 0 and ents <= named and len(al) == len(ents))


def f1_counts(rows: Sequence[Dict], fa: float, names_link: bool) -> Tuple[float, float, float]:
    """验证集 F1。TP/FN 按真值实体（逐目标规则按场景是否检出）；FP = 指错实体的告警 + 正常运行中的告警（按 24 h 折算）。"""
    tp = fn = 0.0
    fp = fa
    for r in rows:
        m = r["m"]
        if names_link:
            tp += r["hit"]
            fn += m - r["hit"]
            fp += r["wrong"]
        else:
            tp += float(r["det"])
            fn += float(not r["det"])
    return tp, fp, fn


def row_for(alarms, inj, case, after, interval) -> Dict:
    x = score_case(alarms, inj, case, after, interval)
    ents = {t[1] for t in case["truth"]}
    named = {a[2] for a in alarms if a[1] != "target" and a[0] >= inj}
    x["m"] = len(ents)
    x["hit"] = len(ents & named)
    return x


# ---------------------------------------------------------------------- 驱动：验证集（只跑仿真）
def sim_stream(topo_d, base_d, plan, c, seed, cases_fn, long_rounds, after, tail):
    topo = from_dict(topo_d)
    base = Baseline(**base_d)
    enc = FeatureEncoder(topo, base)
    rec = Recorder(enc)
    b = HarshSim(topo, plan, seed=seed, **c.get("bk", {}))
    b.up()
    rid = 0

    def step():
        nonlocal rid
        rec.add(b.measure_round(rid))
        rid += 1
    segs = []
    for suite, cases in cases_fn(topo).items():
        for cs in cases:
            b.clear()
            s = rec.n
            for _ in range(plan.window):
                step()
            for f in cs["faults"]:
                b.inject(f)
            inj = rec.n
            for k in range(after + tail):
                if k == after:
                    b.clear()
                step()
            segs.append(dict(suite=suite, s=s, inj=inj, e=rec.n, case=cs))
    b.clear()
    s = rec.n
    for _ in range(long_rounds):
        step()
    return enc, rec.arrays(), segs, (s, rec.n)


def _val_trace(job):
    topo_d, base_d, plan, c, seed, out, long_rounds, after, tail, dual_n, node_reps, idx = job
    site_t, site_b = _site(from_dict(topo_d), Baseline(**base_d), plan, c, seed)

    def cases(t):
        return dict(single=single_cases(t), dual=dual_cases(t, dual_n, seed), node=node_cases(t, node_reps))
    enc, arr, segs, lg = sim_stream(site_t, site_b, plan, c, seed, cases, long_rounds, after, tail)
    p = os.path.join(out, f"val_{idx}.pkl")
    for sg in segs:
        sg["case"] = {k: v for k, v in sg["case"].items() if k in ("truth", "links")}
    with open(p, "wb") as f:
        pickle.dump(dict(topo=site_t, base=site_b, arr=arr, segs=segs, long=lg, name=c["name"]), f, protocol=4)
    return p


_VAL: Dict[str, Tuple] = {}


def _load_val(p):
    if p not in _VAL:
        with open(p, "rb") as f:
            d = pickle.load(f)
        enc = FeatureEncoder(from_dict(d["topo"]), Baseline(**d["base"]))
        _VAL[p] = (Evidence(enc, d["arr"]), d["segs"], d["long"])
    return _VAL[p]


def _grid_job(job):
    """一组参数在全部验证条件上的 F1（路径版与 +ems 版一次算出）。"""
    method, p, paths, after, interval, long_rounds = job
    acc: Dict[str, List[float]] = {}
    for path in paths:
        ev, segs, lg = _load_val(path)
        rows: Dict[str, List[Dict]] = {}
        for sg in segs:
            for v, al in run_method(method, ev, sg["s"], sg["e"], p).items():
                rows.setdefault(v, []).append(row_for(al, sg["inj"], sg["case"], after, interval))
        norm = run_method(method, ev, lg[0], lg[1], p)
        for v, rs in rows.items():
            fa = len(norm[v]) * 17280 / max(1, long_rounds)
            tp, fp, fn = f1_counts(rs, fa, method != "threshold")
            a = acc.setdefault(v, [0.0, 0.0, 0.0, 0.0])
            a[0] += tp
            a[1] += fp
            a[2] += fn
            a[3] += fa
    return method, p, {v: dict(f1=round(2 * a[0] / max(1e-9, 2 * a[0] + a[1] + a[2]), 5), tp=a[0], fp=a[1], fn=a[2],
                               fa_per_day=round(a[3] / len(paths), 3)) for v, a in acc.items()}


def pick_low_fa(rows: Sequence[Dict], limit: float = LOW_FA) -> Dict:
    """第二个工作点：验证集正常运行误报 ≤ limit 条/天的参数里取 F1 最优；都超限时取误报最少的。
    运维不会接受每天几十条无效告警，F1 最优点不一定是实际会用的配置，这里两个点都报告。"""
    key = lambda r: (-r["f1"], str(r["params"]))
    ok = [r for r in rows if r["fa_per_day"] <= limit]
    return min(ok, key=key) if ok else min(rows, key=lambda r: (r["fa_per_day"], key(r)))


# ---------------------------------------------------------------------- 驱动：测试集（引擎 + 旁路）
def light_only(eng) -> None:
    """关掉全部精测：走引擎对"该链路不能精测"的既有路径（研判屏蔽精测动作、恢复复核跳过、留给全网恢复），
    引擎只用基线方法回放的轻量测量。"""
    eng.noprobe = list(eng.enc.lids)
    eng.policy.set_capabilities(eng.noprobe, [l for l in eng.enc.lids if not eng.backend.can_bw(l)])


def _engine_alarm(k, a) -> Tuple:
    if a["fault_type"] == "node_down":
        return (k, "node", a["node"], "node_down")
    return (k, "link", a["link_id"], a["fault_type"])


def drive_engine(eng, rec, cases, suite, after, tail) -> List[Dict]:
    """每例：复位 → 平稳 W 轮 → 注入 → after 轮 → 消除 → tail 轮，全程不因告警提前结束，记下每条告警。"""
    from .bench import _reset
    from .robust import _new_alarms
    segs = []
    for cs in cases:
        _reset(eng)
        s = rec.n
        seq = eng.alarm_seq
        al: List[Tuple] = []

        def step():
            nonlocal seq
            eng.step()
            al.extend(_engine_alarm(rec.n - 1, a) for a in _new_alarms(eng, seq))
            seq = eng.alarm_seq
        for _ in range(eng.plan.window):
            step()
        for f in cs["faults"]:
            eng.backend.inject(f)
        inj, p0, x0 = rec.n, eng.deep_probes, eng.extra_probes
        for k in range(after + tail):
            if k == after:
                eng.backend.clear()
            step()
        segs.append(dict(suite=suite, s=s, inj=inj, e=rec.n, case=cs, alarms=al, probes=eng.deep_probes - p0,
                         recheck_probes=eng.extra_probes - x0))
    return segs


def _test_job(job):
    """一个条件 + 一个种子：EdgeFDL 在线运行并记录测量流，全部基线在同一条流上回放。"""
    import torch
    from .bench import bench_noise
    from .robust import _engine
    model, c, seed, cfg, rounds, after, tail, dual_n, node_reps, out = job
    torch.set_num_threads(1)
    t0 = time.time()
    log_dir = os.path.join(out, "logs", f"{c['name']}_{seed}")
    eng, topo = _engine(model, c, seed, log_dir)
    enc0 = FeatureEncoder(eng.topo, Baseline(**eng.enc.base.to_dict()))     # 基线方法的起点：现场基线的副本
    rec = Recorder(enc0)
    rec.tap(eng.backend)
    segs = drive_engine(eng, rec, single_cases(topo), "single", after, tail)
    if c["name"] == "standard":
        segs += drive_engine(eng, rec, dual_cases(topo, dual_n, seed), "dual", after, tail)
        segs += drive_engine(eng, rec, node_cases(topo, node_reps), "node", after, tail)
    lg0, p0 = rec.n, eng.deep_probes
    nz = bench_noise(eng, rounds)
    lg1, normal_probes = rec.n, eng.deep_probes - p0
    interval = eng.plan.interval_s
    eng.stop()
    shutil.rmtree(log_dir, ignore_errors=True)
    nodeep, nz_nd = _no_deep_run(model, c, seed, out, rounds, after, tail, dual_n, node_reps)
    wall_engine = round(time.time() - t0, 1)
    ev = Evidence(enc0, rec.arrays())
    rows = []

    def add(name, sg, al):
        x = row_for(al, sg["inj"], sg["case"], after, interval)
        del x["m"], x["hit"]
        f = sg["case"]["faults"][0]
        pred = next((f"{a[2]} {a[3]}" for a in al if a[0] >= sg["inj"]), "no alarm")
        rows.append(dict(method=name, suite=sg["suite"], cond=c["name"], seed=seed, link=f.link, type=f.type,
                         value=f.value, pred=pred, probes=sg.get("probes", 0) if name.startswith("edgefdl") else 0,
                         recheck=sg.get("recheck_probes", 0) if name.startswith("edgefdl") else 0, **x))
    for sg in segs:
        add("edgefdl", sg, sg["alarms"])
    for sg in nodeep:
        add("edgefdl_no_deep", sg, sg["alarms"])
    fa = {"edgefdl": dict(false_alarms=nz["false_alarms"], rounds=nz["rounds"]),
          "edgefdl_no_deep": dict(false_alarms=nz_nd["false_alarms"], rounds=nz_nd["rounds"])}
    t1 = time.time()
    groups: Dict = {}
    for label, m, v, p in cfg:
        groups.setdefault((m, json.dumps(p, sort_keys=True)), (m, p, []))[2].append((label, v))
    for m, p, want in groups.values():
        for sg in segs:
            res = run_method(m, ev, sg["s"], sg["e"], p)
            for label, v in want:
                add(label, sg, res[v])
        res = run_method(m, ev, lg0, lg1, p)
        for label, v in want:
            fa[label] = dict(false_alarms=len(res[v]), rounds=rounds)
    return dict(cond=c["name"], seed=seed, rows=rows, fa=fa, wall_engine=wall_engine,
                wall_baselines=round(time.time() - t1, 1), rounds=rec.n, detector_triggers=nz["detector_triggers"],
                filtered_by_agent=nz["filtered_by_agent"], normal_run_deep_probes=normal_probes)


def _no_deep_run(model, c, seed, out, rounds, after, tail, dual_n, node_reps) -> Tuple[List[Dict], Dict]:
    """同种子、同条件、不用任何精测的 EdgeFDL（light_only）：用例与误报统计都跟主运行相同，回答"只看基线方法看到的
    这条轻量测量流时结果如何"。研判和恢复复核都不精测，rows 里的 probes 恒为 0。"""
    from .bench import bench_noise
    from .robust import _engine
    log_dir = os.path.join(out, "logs", f"{c['name']}_{seed}_nd")
    eng, topo = _engine(model, c, seed, log_dir)
    rec = Recorder(FeatureEncoder(eng.topo, Baseline(**eng.enc.base.to_dict())))
    rec.tap(eng.backend)
    light_only(eng)
    segs = drive_engine(eng, rec, single_cases(topo), "single", after, tail)
    if c["name"] == "standard":
        segs += drive_engine(eng, rec, dual_cases(topo, dual_n, seed), "dual", after, tail)
        segs += drive_engine(eng, rec, node_cases(topo, node_reps), "node", after, tail)
    nz = bench_noise(eng, rounds)
    assert eng.deep_probes == 0, "不用精测的对照行发生了精测"
    eng.stop()
    shutil.rmtree(log_dir, ignore_errors=True)
    return segs, nz


def lam_points(best: Dict, trace: Dict) -> Dict[str, Dict]:
    """NetBouncer + EMS 选中点只改 λ 的其余网格点 {标签: 网格行}：验证 F1 几乎相同的设置，测试集上也要报告。"""
    b = best.get("netbouncer", {}).get("+ems")
    if b is None:
        return {}
    same = lambda p: all(p[k] == v for k, v in b["params"].items() if k != "lam")
    return {f"netbouncer+ems:lam{r['params']['lam']}": r for r in trace["netbouncer+ems"]
            if same(r["params"]) and r["params"]["lam"] != b["params"]["lam"]}


def configs(best: Dict, low: Optional[Dict] = None, extra: Optional[Dict] = None) -> List[Tuple]:
    """(标签, 方法, 变体, 参数)。逐目标规则不指名链路，没有 +ems 变体。tuned = 验证集 F1 最优，lowfa = 低误报工作点，
    extra = lam_points() 的结果。"""
    cfg = []
    for m, d in DEFAULTS.items():
        for v in ("", "+ems") if m != "threshold" else ("",):
            cfg.append((f"{m}{v}:default", m, v, d))
            if m in best and v in best[m]:
                cfg.append((f"{m}{v}:tuned", m, v, best[m][v]["params"]))
            if low and f"{m}{v}" in low:
                cfg.append((f"{m}{v}:lowfa", m, v, low[f"{m}{v}"]["params"]))
    for label, r in (extra or {}).items():
        cfg.append((label, "netbouncer", "+ems", r["params"]))
    return cfg


# ---------------------------------------------------------------------- 汇总
def pct(xs) -> Optional[float]:
    xs = list(xs)
    return round(100.0 * sum(map(float, xs)) / len(xs), 2) if xs else None


def agg(rows: Sequence[Dict]) -> Dict:
    if not rows:
        return {}
    ts = [r["t"] for r in rows if r["t"] is not None]
    tr = [r["t_right"] for r in rows if r["t_right"] is not None]
    ns = [r["n"] for r in rows]
    nl = not rows[0]["method"].startswith("threshold")         # 逐目标规则不指名链路：链路类指标不适用，记 null 而非 0
    return dict(n=len(rows), detected_pct=pct(r["det"] for r in rows),
                link_pct=pct(r["first_link"] for r in rows) if nl else None,
                link_type_pct=pct(r["first_joint"] for r in rows) if nl else None,
                all_link_pct=pct(r["all_link"] for r in rows) if nl else None,
                all_type_pct=pct(r["all_joint"] for r in rows) if nl else None,
                exact_pct=pct(r["exact"] for r in rows) if nl else None,
                alarms_mean=round(float(np.mean(ns)), 3), alarms_max=int(max(ns)),
                wrong_alarms_mean=round(float(np.mean([r["wrong"] for r in rows])), 3),
                pre_fault_alarms=int(sum(r["pre"] for r in rows)),
                time_to_alarm_mean_s=round(float(np.mean(ts)), 1) if ts else None,
                time_to_alarm_max_s=float(max(ts)) if ts else None,
                time_to_right_link_mean_s=round(float(np.mean(tr)), 1) if tr else None)


def summarize(results: List[Dict]) -> Dict:
    rows = [r for x in results for r in x["rows"]]
    methods = sorted({r["method"] for r in rows})
    conds = list(dict.fromkeys(x["cond"] for x in results))
    by: Dict = {}
    for r in rows:
        by.setdefault((r["method"], r["suite"], r["cond"]), []).append(r)
    out: Dict = dict(single={}, dual={}, node={}, false_alarms={}, by_type={}, by_link={}, per_seed={})
    for m in methods:
        out["single"][m] = {}
        out["by_type"][m] = {}
        out["by_link"][m] = {}
        out["per_seed"][m] = {}
        for c in conds:
            rs = by.get((m, "single", c), [])
            out["single"][m][c] = agg(rs)
            out["by_type"][m][c] = {t: agg([r for r in rs if r["type"] == t]) for t in FAULT_TYPES}
            if c == "standard" or m == "edgefdl":
                out["by_link"][m][c] = {l: agg([r for r in rs if r["link"] == l])
                                        for l in sorted({r["link"] for r in rs}, key=lambda s: int(s[1:]))}
            out["per_seed"][m][c] = {str(s): agg([r for r in rs if r["seed"] == s])
                                     for s in sorted({r["seed"] for r in rs})}
        out["dual"][m] = agg(by.get((m, "dual", "standard"), []))
        out["node"][m] = agg(by.get((m, "node", "standard"), []))
    for x in results:
        for m, f in x["fa"].items():
            d = out["false_alarms"].setdefault(m, {}).setdefault(x["cond"], dict(counts=[], days=0.0))
            d["counts"].append(f["false_alarms"])
            d["days"] += f["rounds"] * 5.0 / 86400
    for m, cs in out["false_alarms"].items():
        for c, d in cs.items():
            d["per_day"] = round(sum(d["counts"]) / d["days"], 3)
    out["overall"] = {}
    for m in methods:
        rs = [r for r in rows if r["method"] == m and r["suite"] == "single"]
        ts = [r["t"] for r in rs if r["t"] is not None]
        cs = out["false_alarms"].get(m, {})
        out["overall"][m] = dict(faults=len(rs), correct=int(sum(r["first_joint"] for r in rs)),
                                 link_type_pct=pct(r["first_joint"] for r in rs),
                                 time_to_alarm_mean_s=round(float(np.mean(ts)), 1) if ts else None,
                                 false_alarms=int(sum(sum(d["counts"]) for d in cs.values())),
                                 simulated_days=round(sum(d["days"] for d in cs.values()), 3))
    return out


def paired_gap(rows: Sequence[Dict], a: str, b: str, n: int = 20000) -> Dict:
    """a 减 b 的单故障 link+type 正确率（百分点）：同种子同条件逐例配对，按固定场景整组重采样（场景才是独立单元）得 95% 区间。"""
    key = lambda r: (r["cond"], r["seed"], r["link"], r["type"])
    x = {key(r): bool(r["first_joint"]) for r in rows if r["method"] == a and r["suite"] == "single"}
    y = {key(r): bool(r["first_joint"]) for r in rows if r["method"] == b and r["suite"] == "single"}
    assert set(x) == set(y) and x, (a, b)
    scen = sorted({k[2:] for k in x})
    d = np.zeros(len(scen))
    for i, s in enumerate(scen):
        d[i] = sum(int(x[k]) - int(y[k]) for k in x if k[2:] == s)
    boot = d[np.random.default_rng(0).integers(len(d), size=(n, len(d)))].sum(1) * 100 / len(x)
    return dict(a=a, b=b, faults=len(x), only_a=int(sum(x[k] and not y[k] for k in x)),
                only_b=int(sum(y[k] and not x[k] for k in x)), gap_points=round(float(d.sum()) * 100 / len(x), 2),
                ci95=[round(float(q), 2) for q in np.percentile(boot, [2.5, 97.5])], resampled_scenarios=len(scen))


def run_compare(model: str, out: str, workers: int = 24, seeds: Sequence[int] = TEST_SEEDS, val_seed: int = VAL_SEED,
                rounds: int = 17280, val_rounds: int = 4320, dual_n: int = 60, node_reps: int = 2, after: int = AFTER,
                tail: int = TAIL, grid: Optional[Dict] = None, conds: Optional[List[str]] = None,
                val_dual: int = 30, val_node: int = 1) -> Dict:
    from .cli import load_bundle
    t0 = time.time()
    os.makedirs(out, exist_ok=True)
    ag, topo, base, plan, meta = load_bundle(model)
    interval = plan.interval_s
    grid = grids() if grid is None else grid
    total_scen = (plan.window + after + tail) * 84
    cl_val = [c for c in conditions(total_scen + val_rounds) if not conds or c["name"] in conds]
    cl_test = [c for c in conditions(rounds) if not conds or c["name"] in conds]
    ctx = mp.get_context("spawn")
    with ctx.Pool(workers) as pool:
        print(f"验证流 {len(cl_val)} 个条件，种子 {val_seed}", flush=True)
        val_paths = pool.map(_val_trace, [(topo.to_dict(), base.to_dict(), plan, c, val_seed + i, out, val_rounds, after,
                                           tail, val_dual, val_node, i) for i, c in enumerate(cl_val)], chunksize=1)
        print(f"  用时 {time.time() - t0:.0f}s；网格 {sum(len(v) for v in grid.values())} 组参数", flush=True)
        jobs = [(m, p, val_paths, after, interval, val_rounds) for m, ps in grid.items() for p in ps]
        best: Dict = {}
        trace: Dict = {}
        for k, (m, p, res) in enumerate(pool.imap_unordered(_grid_job, jobs, chunksize=1)):
            for v, r in res.items():
                b = best.setdefault(m, {}).get(v)
                trace.setdefault(f"{m}{v}", []).append(dict(params=p, **r))
                if b is None or r["f1"] > b["f1"] or (r["f1"] == b["f1"] and str(p) < str(b["params"])):
                    best[m][v] = dict(params=p, **r)
            if (k + 1) % 100 == 0:
                print(f"  网格 {k + 1}/{len(jobs)}，{time.time() - t0:.0f}s", flush=True)
        for m in best:
            print(f"  [{m}] ", {v: (r['f1'], r['params']) for v, r in best[m].items()}, flush=True)
        low = {k: pick_low_fa(v) for k, v in trace.items()}
        for k, r in low.items():
            print(f"  [{k}] 低误报点 F1={r['f1']} 误报/天={r['fa_per_day']} {r['params']}", flush=True)
        lams = lam_points(best, trace)
        for k, r in lams.items():
            print(f"  [{k}] F1={r['f1']} 误报/天={r['fa_per_day']} {r['params']}", flush=True)
        with open(os.path.join(out, "tuning.pkl"), "wb") as f:
            pickle.dump(dict(best=best, trace=trace, low=low, lams=lams), f, protocol=4)
        cfg = configs(best, low, lams)
        tj = [(model, c, s * 100 + i, cfg, rounds, after, tail, dual_n, node_reps, out)
              for i, c in enumerate(cl_test) for s in seeds]
        print(f"测试 {len(tj)} 个作业，{len(cfg)} 种基线配置", flush=True)
        results = []
        for k, r in enumerate(pool.imap_unordered(_test_job, tj, chunksize=1)):
            results.append(r)
            with open(os.path.join(out, "results.pkl"), "wb") as f:      # 汇总出错时不必重跑
                pickle.dump(results, f, protocol=4)
            print(f"  [{k + 1}/{len(tj)}] {r['cond']} seed={r['seed']} 引擎 {r['wall_engine']}s 基线 {r['wall_baselines']}s "
                  f"累计 {time.time() - t0:.0f}s", flush=True)
    with open(os.path.join(out, "run.pkl"), "wb") as f:
        pickle.dump(dict(cl_test=cl_test, grid=grid, meta=meta, topo=topo.name, plan_window=plan.window), f, protocol=4)
    res = finish(model, out, results, grid, best, trace, low, lams, cl_test, meta, topo.name, interval, plan.window, dict(
        seeds=seeds, val_seed=val_seed, rounds=rounds, val_rounds=val_rounds, after=after, tail=tail, dual_n=dual_n,
        node_reps=node_reps, val_dual=val_dual, val_node=val_node), t0)
    for p in val_paths:
        os.remove(p)
    print(markdown(res), flush=True)
    return res


def finish(model, out, results, grid, best, trace, low, lams, cl_test, meta, topo_name, interval, window, a, t0) -> Dict:
    """汇总并写文件。单独成函数：tuning.pkl、results.pkl 还在时可以不重跑仿真，直接重算。"""
    seeds, val_seed, rounds, val_rounds = a["seeds"], a["val_seed"], a["rounds"], a["val_rounds"]
    after, tail, dual_n, node_reps, val_dual, val_node = (a["after"], a["tail"], a["dual_n"], a["node_reps"], a["val_dual"],
                                                          a["val_node"])
    names = [c["name"] for c in cl_test]
    results.sort(key=lambda x: (names.index(x["cond"]), x["seed"]))
    res = dict(config=dict(model=model, model_version=meta.get("version"), topology=topo_name, backend="sim",
                           test_seeds=[int(s) for s in seeds], val_seed=val_seed, job_seed="seed*100+condition index",
                           conditions=[dict(name=c["name"], bk=c.get("bk", {}), sigma=c.get("sigma"),
                                            relearn=bool(c.get("relearn"))) for c in cl_test],
                           rounds_per_normal_run=rounds, val_rounds_per_normal_run=val_rounds, interval_s=interval,
                           pre_rounds=window, fault_rounds=after, tail_rounds=tail, baseline_hold_rounds=HOLD,
                           dual_trials_per_seed=dual_n, node_reps_per_seed=node_reps,
                           val_dual_trials=val_dual, val_node_reps=val_node,
                           rule_thresholds=dict(cp_k=CP_K, bw_drop=BW_DROP, bw_k=BW_K),
                           type_rule=dict(jitter_ratio=JITTER_RATIO, rtt_floor_ms=RTT_FLOOR_MS),
                           defaults=DEFAULTS, grid_sizes={m: len(v) for m, v in grid.items()}, env=_env()),
               tuning={f"{m}{v}": dict(params=r["params"], val_f1=r["f1"], val_tp=r["tp"], val_fp=r["fp"], val_fn=r["fn"],
                                       val_fa_per_day=r["fa_per_day"], grid_edges=grid_edges(r["params"], grid[m]),
                                       top5=sorted(trace[f"{m}{v}"], key=lambda x: -x["f1"])[:5])
                       for m, vs in best.items() for v, r in vs.items()},
               tuning_low_fa={k: dict(params=r["params"], val_f1=r["f1"], val_fa_per_day=r["fa_per_day"],
                                      grid_edges=grid_edges(r["params"], grid[k.replace("+ems", "")]))
                              for k, r in low.items()},
               sensitivity={k: dict(params=r["params"], val_f1=r["f1"], val_tp=r["tp"], val_fp=r["fp"], val_fn=r["fn"],
                                    val_fa_per_day=r["fa_per_day"]) for k, r in lams.items()})
    res.update(summarize(results))
    edge = [r for x in results for r in x["rows"] if r["method"] == "edgefdl"]
    assert edge, "没有 EdgeFDL 结果"
    res["consistency"] = consistency(model, edge)
    es = [r for r in edge if r["suite"] == "single" and r["cond"] == "standard"]
    es1 = [r for r in edge if r["suite"] == "single"]
    res["edgefdl_deep_probes_per_fault"] = dict(
        condition="standard, 84 single-fault scenarios", all=round(float(np.mean([r["probes"] for r in es])), 3),
        alarm_clearing_rechecks=round(float(np.mean([r["recheck"] for r in es])), 3),
        by_condition={c: probe_stats([r for r in es1 if r["cond"] == c]) for c in names},
        all_conditions=probe_stats(es1),
        normal_run_per_day={c: round(sum(x["normal_run_deep_probes"] for x in results if x["cond"] == c)
                                     / max(1e-9, sum(1 for x in results if x["cond"] == c) * rounds * interval / 86400), 2)
                            for c in names},
        no_deep_row_probes=int(sum(r["probes"] for x in results for r in x["rows"] if r["method"] == "edgefdl_no_deep")))
    rows = [r for x in results for r in x["rows"]]
    res["paired"] = [paired_gap(rows, a_, b_) for a_, b_ in itertools.product(
        ("edgefdl", "edgefdl_no_deep"), ["netbouncer+ems:tuned"] + list(lams) + ["vote007+ems:tuned", "grouped+ems:tuned"])
        if any(r["method"] == b_ for r in rows)]
    res["window_tradeoff"] = window_tradeoff(trace)
    res["streams"] = [{k: x[k] for k in ("cond", "seed", "rounds", "wall_engine", "wall_baselines", "detector_triggers",
                                         "filtered_by_agent", "normal_run_deep_probes")} for x in results]
    res["wall_s"] = round(time.time() - t0, 1)
    with open(os.path.join(out, "compare.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    with open(os.path.join(out, "rows.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    return res


def window_tradeoff(trace: Dict) -> Dict:
    """回归与投票 + EMS 规则每种窗口长度的验证集最优点：更短的窗口能否换来更快的告警，只看验证集。"""
    out: Dict = {}
    for name, key in (("netbouncer+ems", "n"), ("vote007+ems", "epoch")):
        for r in sorted(trace.get(name, []), key=lambda x: (x["f1"], str(x["params"]))):
            out.setdefault(name, {})[str(r["params"][key])] = dict(val_f1=r["f1"], val_fa_per_day=r["fa_per_day"], params=r["params"])
    return out


def probe_stats(rs: Sequence[Dict]) -> Dict:
    """每故障精测次数（含恢复复核）与用过精测的故障占比。"""
    return dict(per_fault=round(float(np.mean([r["probes"] for r in rs])), 3),
                rechecks_per_fault=round(float(np.mean([r["recheck"] for r in rs])), 3),
                faults_with_probe_pct=pct(r["probes"] > 0 for r in rs),
                faults_with_diagnosis_probe_pct=pct(r["probes"] > r["recheck"] for r in rs))


def _env() -> Dict:
    import platform
    import torch
    return dict(python=platform.python_version(), numpy=np.__version__, torch=torch.__version__, cpu_count=os.cpu_count())


def consistency(model: str, edge: List[Dict]) -> Dict:
    """与已发布 bench.json 的 84/84 对照：本框架里 EdgeFDL 的标准条件结果、逐种子通过数与全部未中用例。"""
    p = os.path.join(os.path.dirname(os.path.abspath(model)), "bench.json")
    pub = None
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            rows = json.load(f)["scenarios"]["rows"]
        pub = dict(n=len(rows), type_and_link_ok=sum(r["type_ok"] and r["loc_ok"] for r in rows),
                   mean_time_to_alarm_s=round(float(np.mean([r["time_to_alarm_s"] for r in rows])), 1))
    std = [r for r in edge if r["suite"] == "single" and r["cond"] == "standard"]
    seeds = sorted({r["seed"] for r in std})
    miss = [{k: r[k] for k in ("cond", "seed", "link", "type", "pred", "t", "n")}
            for r in edge if r["suite"] == "single" and not r["first_joint"]]
    return dict(published_bench=pub, harness_standard_per_seed={
        str(s): sum(r["first_joint"] for r in std if r["seed"] == s) for s in seeds}, harness_misses=miss)


def markdown(res: Dict, cond: str = "standard") -> str:
    """标准条件 84 场景摘要（打印到终端；文档表格由 tools/figures/compare.py 读 compare.json 生成）。"""
    head = "| method | detected | link | link+type | alarms mean/max | FA/day | time to alarm |"
    out = [head, "|---|---|---|---|---|---|---|"]
    for m in res["single"]:
        a = res["single"][m].get(cond) or {}
        if not a:
            continue
        fa = res["false_alarms"].get(m, {}).get(cond, {}).get("per_day")
        out.append(f"| {m} | {a['detected_pct']} | {a['link_pct']} | {a['link_type_pct']} | "
                   f"{a['alarms_mean']}/{a['alarms_max']} | {fa} | {a['time_to_alarm_mean_s']} |")
    return "\n".join(out)
