# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""状态特征：把滑动窗口内的测量结果相对"正常基线"编码成每条链路一组特征。

设计要点
- 链路级证据：链路两端接口地址的可达性与"远端-近端"差分（单点网管即可做链路层析）。
- 路由矩阵对比（网络层析）：经过该链路的目标集合 vs 未经过集合的劣化程度之差。
- 路径占用变化 + LSDB 开销/邻接：区分链路中断与路由漂移。
- 持续性：异常在窗口内出现的比例，用于抑制瞬时噪声。
- 丢包按"超出基线丢包率"判定：设备对 ICMP 限速等造成的背景丢包计入基线，不视为异常。
- 多故障残差：给定冻结的残差基线（ResidualRef）时，先扣除已确认告警所能解释的偏离再编码；不给定时
  编码结果与单告警版本逐位相同。
特征按链路排列、与拓扑规模无关，供共享权重的智能体网络使用。
"""
from __future__ import annotations

import json
import math
import warnings
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..measure.model import DeepProbe, RoundResult
from ..topology import Topology

LIGHT_FEATURES = [
    "end_unreach_mean", "end_unreach_both", "far_drtt", "diff_drtt", "diff_loss", "diff_djit",
    "far_persist", "tomo_drtt", "tomo_loss", "tomo_djit", "tomo_unreach", "paths_lost",
    "paths_gained", "bw_ratio", "has_bw", "adj_down", "cost_ratio", "is_host_link", "base_usage",
    "far_recent", "tomo_recent",
]
GLOBAL_FEATURES = [
    "frac_unreach", "frac_path_changed", "mean_persist", "max_persist", "frac_adj_down",
    "mean_loss", "median_drtt", "max_drtt",
]
DEEP_FEATURES = ["revealed", "unreach", "loss", "drtt", "djit", "bw_ratio"]
LI = {n: i for i, n in enumerate(LIGHT_FEATURES)}
EV_DRTT, EV_DJIT, EV_LOSS, EV_BW = 1.1, 0.69, 0.02, -0.22    # 直接证据阈值：远近端 RTT 差 2 ms、mdev 差 1 ms、丢包 2%、带宽 0.8


def sgnlog(x):
    return np.sign(x) * np.log1p(np.abs(x))


def deep_evidence(d, ftype: str) -> Optional[bool]:
    """精测编码 d（FeatureEncoder.deep）是否支持该故障类型；精测不能说明的类型（路由漂移）返回 None。"""
    if ftype == "link_down":
        return bool(d[1] > 0.5)
    if d[1] > 0.5:                                   # 两端互 ping 不通：数据面劣化无从判断
        return None
    if ftype == "delay":
        return bool(d[3] > EV_DRTT)
    if ftype == "loss":
        return bool(d[2] > EV_LOSS)
    if ftype == "jitter":
        return bool(d[4] > EV_DJIT)
    if ftype == "bandwidth":
        return bool(d[5] < EV_BW)
    return None


def link_evidence(f, ftype: str, d=None, lid=None) -> bool:
    """链路自身的直接证据（远近端差分、邻接/开销、带宽估计，及精测 d）是否支持该故障类型。
    用于研判结论的交叉错配校验（类型取自一处故障、链路取自另一处）与全网恢复时的告警去留。"""
    g = lambda n: float(f[LI[n]])
    if d is not None and d[0] > 0.5 and deep_evidence(d, ftype):     # 已精测：精测是该链路最直接的证据
        return True
    if ftype == "link_down":
        return g("adj_down") > 0.5 or g("end_unreach_both") > 0.5
    if ftype == "route_drift":
        return abs(g("cost_ratio")) > 0.05
    if ftype == "bandwidth":                         # 窗口内无该链路带宽估计时无从否定
        return g("has_bw") < 0.5 or g("bw_ratio") < EV_BW
    if ftype == "delay":
        return g("diff_drtt") > EV_DRTT
    if ftype == "loss":
        return g("diff_loss") > EV_LOSS
    if ftype == "jitter":                            # 只认方差：时延劣化也会抬高 RTT，不能作抖动证据
        return g("diff_djit") > EV_DJIT
    return True


def poisson_sf(k: float, lam: float) -> float:
    """P(X >= k)，X ~ Poisson(lam)：丢包计数的显著性（各目标基线丢包率很小，按泊松近似）。"""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    k = int(math.ceil(k))
    term, cdf = math.exp(-lam), 0.0
    for j in range(k):
        cdf += term
        term *= lam / (j + 1)
    return float(max(0.0, 1.0 - cdf))                # lam 常为 numpy 标量：返回 Python float，比较结果才是 bool


class ResidualRef:
    """多故障残差基线：已确认告警所能解释的偏离，研判"还有没有别的故障"前从测量中扣除。

    数据面告警（时延/丢包/抖动）：由各告警链路"远端-近端"差分联立解出每穿越一次的效应（RTT 增量、方差增量、
    对数存活率），逐轮按目标往返穿越次数（拓扑 + 当前 LSDB 推算，含 traceroute 看不到的回程）叠加扣除，
    同一路径上的另一处故障不被吸收；
    路由类告警（中断/漂移）：改道目标按基线逐链路 RTT 做路径代数折算期望 RTT，冻结改道后的路径作参照，
    因它不可达的目标不再计为异常；告警链路自身的邻接/开销取冻结值、带宽估计不再参与。"""

    def __init__(self, enc: "FeatureEncoder"):
        L, T = enc.L, enc.T
        self.links: set = set()
        self.d_rtt, self.d_var, self.lsurv = np.zeros(L), np.zeros(L), np.zeros(L)     # 每穿越一次
        self.reroute = np.zeros(T, dtype=bool)
        self.unreach = np.zeros(T, dtype=bool)
        self.paths = list(enc.base.paths)
        self.U = enc.U.copy()
        self.cost = enc.base.cost.copy()
        self.adj_down = np.zeros(L, dtype=bool)
        self.bw_mask = np.zeros(L, dtype=bool)
        self.link_rtt = enc.link_rtt()


def min_lost(n: int, p0: np.ndarray, alpha: float = 0.01) -> np.ndarray:
    """单轮 n 个探测中至少丢几个才算显著超出基线丢包率 p0（二项分布右尾概率 < alpha）。
    基线无丢包时为 1，即任何丢包都算异常。"""
    k = np.arange(n + 1)
    p = np.clip(np.asarray(p0, float), 0.0, 0.999)[:, None]
    pmf = np.array([math.comb(n, j) for j in k])[None, :] * p ** k * (1 - p) ** (n - k)
    sf = pmf[:, ::-1].cumsum(1)[:, ::-1]                      # P(X >= k)
    ok = (sf < alpha) & (k >= 1)
    return np.where(ok.any(1), ok.argmax(1), n + 1)


class Baseline:
    """正常运行期学习到的基线（物理网络上线时由"基线学习期"自动获得）。"""

    def __init__(self, rtt, mdev, paths, bw, cost, deep_rtt, deep_mdev, deep_bw, loss=None):
        self.rtt = np.asarray(rtt, float)
        self.mdev = np.asarray(mdev, float)
        self.loss = np.zeros(len(self.rtt)) if loss is None else np.asarray(loss, float)   # 旧模型包无此项
        self.paths = [tuple(p) for p in paths]
        self.bw = np.asarray(bw, float)
        self.cost = np.asarray(cost, float)
        self.deep_rtt = np.asarray(deep_rtt, float)
        self.deep_mdev = np.asarray(deep_mdev, float)
        self.deep_bw = np.asarray(deep_bw, float)

    @classmethod
    def fit(cls, topo: Topology, rounds: Sequence[RoundResult],
            deeps: Sequence[Dict[str, DeepProbe]]) -> "Baseline":
        targets = topo.targets()
        lids = topo.link_ids()
        rtt, mdev, paths, loss = [], [], [], []
        for t in targets:
            # 背景丢包（如设备 ICMP 限速）表现为零星单包丢失；单轮丢 2 个及以上的轮次（突发丢包干扰、短时中断）
            # 不计入，并以 5% 封顶，避免基线学习期的偶发事件掩盖之后的轻度丢包故障
            ps = [r.pings[t.id] for r in rounds if t.id in r.pings and r.pings[t.id].sent]
            ps = [p for p in ps if p.sent - p.recv <= 1]
            sent = sum(p.sent for p in ps)
            loss.append(min(0.05, sum(p.sent - p.recv for p in ps) / sent) if sent else 0.0)
            xs = [r.pings[t.id] for r in rounds if t.id in r.pings and r.pings[t.id].recv > 0]
            rtt.append(float(np.median([x.rtt_avg for x in xs])) if xs else float("nan"))
            mdev.append(float(np.median([x.rtt_mdev for x in xs])) if xs else float("nan"))
            ps = [x.path for x in xs if x.path]
            paths.append(max(set(ps), key=ps.count) if ps else ())
        bw, cost = [], []
        for i, lid in enumerate(lids):
            est = [r.bw[lid] for r in rounds if lid in r.bw]
            bw.append(float(np.median(est)) if est else topo.link(lid).bw_mbps)
            cs = [r.lsdb[lid].cost for r in rounds if lid in r.lsdb]
            cost.append(float(np.median(cs)) if cs else float(topo.link(lid).cost))
        drtt, dmdev, dbw = [], [], []
        for lid in lids:
            ds = [d[lid] for d in deeps if lid in d and d[lid].recv > 0]
            drtt.append(float(np.median([d.rtt_avg for d in ds])) if ds else 2 * topo.link(lid).delay_ms)
            dmdev.append(float(np.median([d.rtt_mdev for d in ds])) if ds else 0.1)
            dbw.append(float(np.median([d.bw_mbps for d in ds])) if ds else topo.link(lid).bw_mbps)
        return cls(rtt, mdev, paths, bw, cost, drtt, dmdev, dbw, loss)

    def to_dict(self) -> dict:
        return {k: (v.tolist() if isinstance(v, np.ndarray) else [list(p) for p in v])
                for k, v in self.__dict__.items()}

    def save(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> "Baseline":
        with open(path, encoding="utf-8") as f:
            return cls(**json.load(f))


class FeatureEncoder:
    def __init__(self, topo: Topology, base: Baseline):
        self.topo = topo
        self.base = base
        self.targets = topo.targets()
        self.tidx = {t.id: i for i, t in enumerate(self.targets)}
        self.lids = topo.link_ids()
        self.lidx = {l: i for i, l in enumerate(self.lids)}
        T, L = len(self.targets), len(self.lids)
        self.T, self.L = T, L
        # 每条链路的端点接口目标：按基线 RTT 区分近端/远端
        self.near = np.full(L, -1)
        self.far = np.full(L, -1)
        for li, lid in enumerate(self.lids):
            nqa = [self.tidx[t.id] for t in self.targets if t.link == lid and t.kind == "nqa"]
            if nqa:                       # 盲区链路：NQA 直接测得该链路，本身就是"远端-近端"差分
                self.far[li] = nqa[0]
                continue
            ends = [self.tidx[t.id] for t in self.targets if t.link == lid]
            ends.sort(key=lambda ti: base.rtt[ti])
            self.far[li] = ends[-1]
            if len(ends) > 1:
                self.near[li] = ends[0]
        self._path_cache: Dict[tuple, np.ndarray] = {}
        self.U = np.stack([self.path_usage(p) for p in base.paths], 1)       # (L, T)
        self.fixed_usage = {}
        for ti, t in enumerate(self.targets):
            if t.kind == "nqa":
                self.U[:, ti] = False
                self.U[self.lidx[t.link], ti] = True
                self.fixed_usage[ti] = self.U[:, ti].copy()
        self.base_usage = self.U.sum(1)
        self.is_host = np.array([float(not (topo.nodes[topo.link(l).a].transit and
                                            topo.nodes[topo.link(l).b].transit)) for l in self.lids])
        self.thr_rtt = np.maximum(2.0, 4 * np.nan_to_num(base.mdev, nan=0.1) + 1.0)
        self.thr_mdev = np.maximum(1.0, 3 * np.nan_to_num(base.mdev, nan=0.1) + 0.5)
        self._mlost: Dict[int, np.ndarray] = {}
        self._rtt0, self._deep0 = base.rtt.copy(), base.deep_rtt.copy()      # 基线跟踪的起点
        self._route_cache: dict = {}
        self._trav_cache: Dict[tuple, np.ndarray] = {}
        self.A0 = self.traversal({})

    def link_rtt(self) -> np.ndarray:
        """每条链路往返一次贡献的基线 RTT（远端-近端，取当前跟踪后的基线），改道目标的期望 RTT 按路径代数折算。"""
        rb = np.nan_to_num(self.base.rtt, nan=0.0)
        return np.clip(rb[self.far] - np.where(self.near >= 0, rb[np.maximum(self.near, 0)], 0.0), 0, None)

    def traversal(self, lsdb) -> np.ndarray:
        """按 LSDB（邻接/开销）由拓扑推算每个目标一次往返穿越各链路的次数 (T, L)，含 traceroute 看不到的回程；
        不可达目标整行为 0。"""
        down = frozenset(l for l, e in lsdb.items() if l in self.lidx and not e.adj_up)
        cost = tuple(sorted((l, e.cost) for l, e in lsdb.items()
                            if l in self.lidx and abs(e.cost - self.topo.link(l).cost) > 0.5))
        A = self._trav_cache.get((down, cost))
        if A is None:
            A = np.zeros((self.T, self.L))
            for ti, t in enumerate(self.targets):
                fwd, rev = self.topo.route_to_target(self.topo.vantage, t, set(down), dict(cost), self._route_cache)
                for lid in self.topo.path_links(fwd) + self.topo.path_links(rev):
                    A[ti, self.lidx[lid]] += 1
            self._trav_cache[(down, cost)] = A
        return A

    @staticmethod
    def last_lsdb(window: Sequence[RoundResult]):
        for rr in reversed(window):                 # 整轮缺测时取最近一次
            if rr.lsdb:
                return rr.lsdb
        return {}

    @property
    def dims(self) -> Tuple[int, int, int]:
        return len(LIGHT_FEATURES), len(DEEP_FEATURES), len(GLOBAL_FEATURES)

    def path_usage(self, path: Tuple[str, ...]) -> np.ndarray:
        u = self._path_cache.get(path)
        if u is None:
            u = np.zeros(self.L, dtype=bool)
            if path:
                try:
                    for lid in self.topo.path_links([self.topo.vantage] + list(path)):
                        u[self.lidx[lid]] = True
                except KeyError:          # traceroute 中出现拓扑外设备，忽略
                    pass
            self._path_cache[path] = u
        return u

    # ------------------------------------------------------------------
    def window_arrays(self, window: Sequence[RoundResult]):
        W, T = len(window), self.T
        sent = np.zeros((W, T))
        recv = np.zeros((W, T))
        rtt = np.full((W, T), np.nan)
        mdev = np.full((W, T), np.nan)
        for w, rr in enumerate(window):
            for tid, ps in rr.pings.items():
                ti = self.tidx.get(tid)
                if ti is None:
                    continue
                sent[w, ti], recv[w, ti] = ps.sent, ps.recv
                if ps.recv:
                    rtt[w, ti], mdev[w, ti] = ps.rtt_avg, ps.rtt_mdev
        return sent, recv, rtt, mdev

    def anomaly_matrix(self, sent, recv, rtt, mdev, p_exp=None, zero_ok=None, widen=None) -> np.ndarray:
        """逐轮逐目标的异常标记（去噪器与特征共用同一判据）。
        残差研判时：p_exp 为计入已确认告警后的期望丢包率（取代基线丢包率作二项检验），zero_ok 标出完全不通
        已由告警解释的目标，widen 为已扣除的抖动标准差（单轮 5 个包的均值/方差随之起伏，阈值相应放宽）。"""
        mlost = np.zeros_like(sent)
        for n in np.unique(sent[sent > 0]).astype(int):
            if p_exp is not None:
                m = min_lost(n, p_exp)
            else:
                if n not in self._mlost:
                    self._mlost[n] = min_lost(n, self.base.loss)
                m = self._mlost[n]
            mlost = np.where(sent == n, m[None, :], mlost)
        thr_rtt, thr_mdev = self.thr_rtt, self.thr_mdev
        if widen is not None:
            thr_rtt, thr_mdev = thr_rtt + 1.1 * widen, thr_mdev + 1.2 * widen
        with np.errstate(invalid="ignore"):
            if zero_ok is None:
                an = (recv == 0) | (sent - recv >= mlost)
            else:
                an = ((recv == 0) & ~zero_ok[None, :]) | (sent - recv >= mlost)
            an |= np.nan_to_num(rtt - self.base.rtt[None, :], nan=0.0) > thr_rtt[None, :]
            an |= np.nan_to_num(mdev - self.base.mdev[None, :], nan=0.0) > thr_mdev[None, :]
        return an & (sent > 0)

    def current_paths(self, window: Sequence[RoundResult]) -> List[Optional[tuple]]:
        """最近一次 traceroute 的路径（不可达记为空路径，窗口内无记录为 None）。"""
        cur: List[Optional[tuple]] = [None] * self.T
        for rr in reversed(window):
            for tid, ps in rr.pings.items():
                ti = self.tidx.get(tid)
                if ti is not None and cur[ti] is None and (ps.path or (ps.sent and ps.recv == 0)):
                    cur[ti] = ps.path
            if all(p is not None for p in cur):
                break
        return cur

    def light(self, window: Sequence[RoundResult], ref: Optional[ResidualRef] = None) -> Tuple[np.ndarray, np.ndarray]:
        """ref 为空即相对原始基线编码；给定 ref 时编码扣除已确认告警效应后的残差（见 ResidualRef）。"""
        return self._encode(window, ref)[:2]

    def _encode(self, window: Sequence[RoundResult], ref: Optional[ResidualRef] = None):
        sent, recv, rtt, mdev = self.window_arrays(window)
        b = self.base
        has = sent.sum(0) > 0                      # 测量缺失（未发出探测）不算不可达，也不算丢包
        p_exp = zero_ok = widen = None
        if ref is None:
            loss_w = np.where(has, 1 - recv.sum(0) / np.maximum(sent.sum(0), 1) - b.loss, 0.0)   # 超出基线的丢包
        else:                                      # 扣除已确认告警解释的效应（按当前往返穿越次数叠加）
            A = self.traversal(self.last_lsdb(window))
            off = A @ ref.d_rtt + ref.reroute * ((A - self.A0) @ (ref.link_rtt / 2))
            surv = np.exp(A @ ref.lsurv)
            rtt = rtt - off[None, :]
            widen = np.sqrt(A @ ref.d_var)
            raw_mdev = mdev
            with np.errstate(invalid="ignore"):
                mdev = np.sqrt(np.maximum(mdev ** 2 - widen[None, :] ** 2, 0.0))
            p_exp = np.where(ref.unreach, 1.0, 1 - (1 - b.loss) * surv)
            zero_ok = ref.unreach | (p_exp ** np.maximum(sent.max(0), 1) > 0.02)
            res = 1 - np.clip(recv.sum(0) / np.maximum(sent.sum(0), 1) / np.maximum(surv, 1e-6), 0, 1)
            loss_w = np.where(has & ~(ref.unreach & (recv.sum(0) == 0)), res - b.loss, 0.0)
        tried = sent[-3:] > 0
        unreach = ((recv[-3:] == 0) & tried).sum(0) / np.maximum(tried.sum(0), 1)
        if zero_ok is not None:
            unreach = np.where(zero_ok, 0.0, unreach)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)        # 整列不可达时 nanmedian 告警
            drtt = np.nan_to_num(np.nanmedian(rtt - b.rtt[None, :], 0), nan=0.0)
            djit = np.nan_to_num(np.nanmedian(mdev - b.mdev[None, :], 0), nan=0.0)
            if ref is not None:
                # 扣除后的残差带有已告警故障本身的抽样起伏（单轮 5 个包）：经过抖动告警链路的目标，窗口中位 RTT
                # 的标准误约 0.25×扣除的标准差，方差按窗口中位在方差域扣除并再留 0.7 倍余量；经过丢包告警链路的
                # 目标，残差丢包按二项标准误收缩。不经过告警链路的目标不受影响。
                w2 = widen ** 2
                drtt = np.sign(drtt) * np.maximum(np.abs(drtt) - 0.5 * widen, 0.0)
                v = np.nan_to_num(np.nanmedian(raw_mdev ** 2, 0), nan=0.0) - 1.7 * w2
                djit = np.where(w2 > 0, np.sqrt(np.maximum(v, 0.0)) - b.mdev, djit)
                q = np.clip(1 - (1 - b.loss) * surv, 0, 1)
                se = np.sqrt(q * (1 - q) / np.maximum(sent.sum(0), 1)) / np.maximum(surv, 1e-6)
                loss_w = np.where(surv < 0.999, np.sign(loss_w) * np.maximum(np.abs(loss_w) - 1.5 * se, 0.0), loss_w)
        ok = ~np.isnan(rtt).all(0)
        an = self.anomaly_matrix(sent, recv, rtt, mdev, p_exp, zero_ok, widen)
        persist = an.mean(0)

        # 最近一次 traceroute 的路径
        U0 = self.U if ref is None else ref.U
        ref_paths = b.paths if ref is None else ref.paths
        cur_paths = self.current_paths(window)
        Ucur = np.zeros_like(self.U)
        changed = np.zeros(self.T)
        for ti, p in enumerate(cur_paths):
            if ti in self.fixed_usage:
                Ucur[:, ti] = self.fixed_usage[ti]
            elif p is None:
                Ucur[:, ti] = U0[:, ti]
            else:
                Ucur[:, ti] = self.path_usage(p)
                changed[ti] = float(p != ref_paths[ti] and len(p) > 0)

        L = self.L
        F = np.zeros((L, len(LIGHT_FEATURES)), dtype=np.float32)
        far, near = self.far, self.near
        has_near = near >= 0
        nn = np.where(has_near, near, far)
        ends_unreach = np.where(has_near, (unreach[far] + unreach[nn]) / 2, unreach[far])
        F[:, 0] = ends_unreach
        F[:, 1] = np.where(has_near, np.minimum(unreach[far], unreach[nn]), unreach[far])
        F[:, 2] = sgnlog(drtt[far])
        F[:, 3] = sgnlog(drtt[far] - np.where(has_near, drtt[nn], 0.0))
        F[:, 4] = loss_w[far] - np.where(has_near, loss_w[nn], 0.0)
        F[:, 5] = sgnlog(djit[far] - np.where(has_near, djit[nn], 0.0))
        F[:, 6] = persist[far]
        # 网络层析：经过 vs 未经过该链路的目标
        U = U0.astype(float)
        rch = (recv.sum(0) > 0).astype(float)
        nU = 1 - U

        def contrast(x, mask):
            a = (U * mask) @ x / np.maximum((U * mask).sum(1), 1)
            c = (nU * mask) @ x / np.maximum((nU * mask).sum(1), 1)
            return a - c

        F[:, 7] = sgnlog(contrast(drtt, rch[None, :]))
        F[:, 8] = contrast(loss_w, rch[None, :])
        F[:, 9] = sgnlog(contrast(djit, rch[None, :]))
        F[:, 10] = contrast(unreach, np.ones((1, self.T)))
        lost = (U0 & ~Ucur).sum(1)
        gained = (Ucur & ~U0).sum(1)
        F[:, 11] = lost / np.maximum(self.base_usage if ref is None else U0.sum(1), 1)
        F[:, 12] = gained / self.T
        # 轻量带宽估计
        for li, lid in enumerate(self.lids):
            est = [rr.bw[lid] for rr in window if lid in rr.bw]
            if est and not (ref is not None and ref.bw_mask[li]):
                F[li, 13] = float(np.clip(math.log(max(np.median(est), 1e-3) / b.bw[li]), -5, 1))
                F[li, 14] = 1.0
            ls = next((rr.lsdb[lid] for rr in reversed(window) if lid in rr.lsdb), None)   # 整轮缺测时取最近一次
            if ls is not None:
                if ref is None:
                    F[li, 15] = float(not ls.adj_up)
                    F[li, 16] = float(np.clip(math.log(max(ls.cost, 1) / max(b.cost[li], 1)), -4, 4))
                else:
                    F[li, 15] = float(not ls.adj_up and not ref.adj_down[li])
                    F[li, 16] = float(np.clip(math.log(max(ls.cost, 1) / max(ref.cost[li], 1)), -4, 4))
        F[:, 17] = self.is_host
        F[:, 18] = self.base_usage / self.T
        # 最近两轮是否仍异常：区分"持续中的故障"与"已经过去的噪声"
        recent = an[-2:].mean(0)
        F[:, 19] = recent[far]
        F[:, 20] = contrast(recent, np.ones((1, self.T)))

        last0 = (recv[-1] == 0) & (sent[-1] > 0)
        if zero_ok is not None:
            last0 = last0 & ~zero_ok
        G = np.array([
            last0.mean(),
            changed.mean(),
            persist.mean(),
            persist.max(),
            F[:, 15].mean(),
            loss_w.mean(),
            float(sgnlog(np.median(drtt[ok]))) if ok.any() else 0.0,
            float(sgnlog(np.max(drtt[ok]))) if ok.any() else 0.0,
        ], dtype=np.float32)
        return F, G, an, changed

    # ------------------------------------------------------------------ 多故障残差
    def residual_activity(self, window: Sequence[RoundResult], ref: ResidualRef, k_on: int = 3) -> List[str]:
        """残差上的持续性判决（与 Detector 同判据）：返回仍未被已有告警解释的持续异常，空表示无新异常。"""
        F, _, an, changed = self._encode(window, ref)
        out = []
        hit = np.where(an.sum(0) >= k_on)[0]
        if len(hit):
            out.append("残差持续异常: " + ",".join(self.targets[i].id for i in hit[:5]))
        if changed.any():
            out.append("残差路径变化: " + ",".join(self.targets[i].id for i in np.where(changed > 0)[0][:5]))
        cp = np.where((F[:, 15] > 0.5) | (np.abs(F[:, 16]) > 0.05))[0]
        if len(cp):
            out.append("残差控制面变化: " + ",".join(self.lids[i] for i in cp))
        bw = np.where((F[:, 14] > 0.5) & (F[:, 13] < math.log(0.7)))[0]
        if len(bw):
            out.append("残差带宽下降: " + ",".join(self.lids[i] for i in bw))
        return out

    def residual_ref(self, window: Sequence[RoundResult], alarms: Sequence[Tuple[str, str]]) -> ResidualRef:
        """按当前窗口冻结残差基线；alarms 为已确认告警 [(类型, 链路)]（设备宕机按其各链路中断给出），
        须在故障稳定后（窗口内全为故障后测量）调用。"""
        sent, recv, rtt, mdev = self.window_arrays(window)
        b = self.base
        cur = self.current_paths(window)
        ref = ResidualRef(self)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            dr = np.nan_to_num(np.nanmedian(rtt - b.rtt[None, :], 0), nan=0.0)
            dv = np.nan_to_num(np.nanmedian(mdev ** 2 - b.mdev[None, :] ** 2, 0), nan=0.0)
        has = sent.sum(0) > 0
        ls = np.where(has, np.log(np.clip(recv.sum(0) / np.maximum(sent.sum(0), 1) / (1 - b.loss), 1e-3, 1.0)), 0.0)
        gone = (recv[-3:] == 0).all(0) & (sent[-3:] > 0).any(0)
        lsdb = self.last_lsdb(window)
        A = self.traversal(lsdb)
        dp = sorted({self.lidx[l] for ft, l in alarms if ft in ("delay", "loss", "jitter")})
        if dp:                                 # 各告警链路远近端差分 = Σ_l (A[远端,l]-A[近端,l])·e_l，联立求每穿越一次的效应
            D = np.array([A[self.far[li]] - (A[self.near[li]] if self.near[li] >= 0 else 0) for li in dp])[:, dp]
            for x, arr, lo, hi in ((dr, ref.d_rtt, 0, None), (dv, ref.d_var, 0, None), (ls, ref.lsurv, None, 0)):
                rhs = np.array([x[self.far[li]] - (x[self.near[li]] if self.near[li] >= 0 else 0) for li in dp])
                arr[dp] = np.clip(np.linalg.lstsq(D, rhs, rcond=None)[0], lo, hi)
        rl = {lid for ft, lid in alarms if ft in ("link_down", "route_drift")}
        # 路由类告警只解释"仅这些告警链路变化时"路由模型预测的不可达与改道；同时存在的其他中断造成的不可达不算在内
        Aal = self.traversal({l: e for l, e in lsdb.items() if l in rl}) if rl else self.A0
        for ft, lid in alarms:
            li = self.lidx[lid]
            ref.links.add(lid)
            e = lsdb.get(lid)
            if e is not None:
                ref.cost[li], ref.adj_down[li] = e.cost, not e.adj_up
            ref.bw_mask[li] = True
            if ft in ("link_down", "route_drift"):
                for ti, t in enumerate(self.targets):
                    p = cur[ti]
                    if not (self.A0[ti, li] or A[ti, li] or t.link == lid or (p and self.path_usage(p)[li])):
                        continue
                    if gone[ti] and not Aal[ti].any():
                        ref.unreach[ti] = True
                        ref.U[:, ti] = False
                    elif not gone[ti] and ti not in self.fixed_usage and (
                            np.any(A[ti] != self.A0[ti]) or (p and p != b.paths[ti])):
                        ref.reroute[ti] = True
                        if p:
                            ref.paths[ti] = p
                            ref.U[:, ti] = self.path_usage(p)
        return ref

    def ends(self, lid: str, A: np.ndarray) -> Optional[Tuple[int, int]]:
        """(经过该链路的一端, 不经过的一端)；另一处故障改道使远端不再经过时换用近端，两端都经过/都不经过为 None。
        单端链路（NQA、网管上联）不经过的一端记 -1。"""
        li = self.lidx[lid]
        far, near = self.far[li], self.near[li]
        cf = A[far, li] > 0
        cn = near >= 0 and A[near, li] > 0
        if cf and not cn:
            return far, near
        if cn and not cf:
            return near, far
        return None

    def loss_excess(self, window: Sequence[RoundResult], lid: str, ref: Optional[ResidualRef] = None,
                    alpha: float = 0.01) -> Optional[bool]:
        """经过该链路的一端相对不经过的一端的窗口超额丢包是否显著（泊松近似的单侧检验）：不经过的一端的实测
        存活率校正共同上游的丢包，ref 给出时再扣除已告警链路的冻结效应。两端无法区分时返回 None。"""
        e = self.ends(lid, self.traversal(self.last_lsdb(window)))
        if e is None:
            return None
        x, y = e
        sent, recv, _, _ = self.window_arrays(window)
        A = self.traversal(self.last_lsdb(window))
        surv = np.exp(A @ ref.lsurv) if ref is not None else np.ones(self.T)
        b = self.base
        nx = sent[:, x].sum()
        if nx == 0:
            return None
        s0 = (1 - b.loss[x]) * surv[x]
        if y >= 0 and sent[:, y].sum() > 0:
            ny = sent[:, y].sum()
            s0 *= min(1.0, ((recv[:, y].sum() + 0.5) / (ny + 1)) / ((1 - b.loss[y]) * surv[y]))
        lost = nx - recv[:, x].sum()
        return bool(lost / nx - (1 - s0) > EV_LOSS and poisson_sf(lost, nx * (1 - s0)) < alpha)

    def live_loss(self, window: Sequence[RoundResult], A: np.ndarray, links: Sequence[str], min_n: int = 100) -> np.ndarray:
        """现场背景丢包率 (T,)：窗口内不经过任何告警链路、按路由可达的目标，按设备接口/主机两类合并估计；
        与基线学习一致，单轮丢 2 个及以上（突发干扰）或整轮不通的不计入。样本不足的类别取 0（设备类见下）。"""
        sent, recv, _, _ = self.window_arrays(window)
        li = [self.lidx[l] for l in links if l in self.lidx]
        out = (A[:, li].sum(1) == 0) & A.any(1) if li else A.any(1)
        lost = sent - recv
        keep = (sent > 0) & (lost <= 1)
        router = np.array([self.topo.nodes[t.node].transit for t in self.targets])
        bg = np.zeros(self.T)
        rate = lambda sel: (float((lost * (keep & sel[None, :])).sum() / max((sent * (keep & sel[None, :])).sum(), 1)),
                            (sent * (keep & sel[None, :])).sum())
        for kind in (router, ~router):
            r, n = rate(out & kind)
            if n >= min_n:
                bg[kind] = min(0.05, r)
            elif kind is router:
                # 告警链路覆盖几乎全部目标（如网管上联）：链路丢包对设备与主机目标一视同仁，设备目标高出主机目标
                # 的部分归为设备自身的背景丢包（ICMP 限速只作用于设备应答）
                (rr_, nr), (rh, nh) = rate(A.any(1) & router), rate(A.any(1) & ~router)
                if nr >= min_n and nh >= min_n:
                    bg[router] = min(0.05, max(0.0, rr_ - rh))
        return bg

    def loss_strength(self, window: Sequence[RoundResult], lid: str, A: np.ndarray) -> float:
        """丢包告警的强度估计（每穿越一次的对数存活率）：窗口内经过该链路的目标相对基线的合并存活率，
        按平均穿越次数折算；限定在 3%~50% 丢包之间（训练区间下限 5%，留余量）。"""
        li = self.lidx[lid]
        sent, recv, _, _ = self.window_arrays(window)
        sel = (A[:, li] > 0) & (sent.sum(0) > 0)
        if not sel.any():
            return math.log(0.85)
        s = (recv.sum(0)[sel].sum() / sent.sum(0)[sel].sum()) / float(np.mean(1 - self.base.loss[sel]))
        return float(np.clip(math.log(max(s, 1e-3)) / float(np.mean(A[sel, li])), math.log(0.5), math.log(0.97)))

    def round_evidence(self, rr: RoundResult, ftype: str, lid: str, A: np.ndarray,
                       ref: Optional[ResidualRef] = None, own: float = math.log(0.85),
                       bg: Optional[np.ndarray] = None):
        """逐轮的告警链路自身证据（相对原始基线，单轮测量），供逐条恢复计数。
        中断/漂移/带宽/时延/抖动返回 True（仍有证据）/ False（证据消失）/ None（本轮无从判断：缺测、无带宽估计、
        两端都不经过或都经过该链路）；时延/抖动取"经过该链路的一端 - 不经过的一端"的差分（另一处故障改道使远端
        不再经过该链路时自动换用近端，差分反号）。丢包返回 (丢包数, 期望背景丢包数, 探测数)，由调用方跨轮累计检验：
        单轮每目标只有 5 个包，单个目标的单轮证据不足以判断丢包已恢复，取所有经过该链路的目标合并计数。
        ref 给出时扣除其余告警链路的冻结效应（不含本链路），本链路效应仍计入证据。own 为丢包告警的估计强度；
        bg 为现场实测的背景丢包率（见 live_loss），高于基线时以之为准（如上线后才开启的 ICMP 限速）。"""
        li = self.lidx[lid]
        b = self.base
        ls = rr.lsdb.get(lid)
        if ftype == "link_down":
            if ls is not None and not ls.adj_up:
                return True
            # 两端接口都不通也算证据，但须按当前路由两端本应可达（否则是别处中断所致，如网管上联中断）
            ends = [t for t in (self.far[li], self.near[li]) if t >= 0]
            ps = [rr.pings.get(self.targets[t].id) for t in ends]
            seen = all(p is not None and p.sent for p in ps)
            if seen and all(p.recv == 0 for p in ps) and all(A[t].any() for t in ends):
                return True
            return None if ls is None and not seen else False
        if ftype == "route_drift":
            if ls is None:
                return None
            return abs(math.log(max(ls.cost, 1) / max(b.cost[li], 1))) > 0.05
        if ftype == "bandwidth":
            if lid not in rr.bw:
                return None
            return math.log(max(rr.bw[lid], 1e-3) / b.bw[li]) < EV_BW
        others = None
        if ref is not None:
            others = dict(d_rtt=ref.d_rtt.copy(), d_var=ref.d_var.copy(), lsurv=ref.lsurv.copy())
            for v in others.values():
                v[li] = 0.0
        if ftype == "loss":
            # (丢包数, 背景期望丢包数, 似然比对数)：背景 = 基线丢包率 + 其余告警的冻结效应；似然比为"本链路仍按
            # own（每穿越一次的对数存活率）丢包"对"已恢复"
            lost = lam = llr = 0.0
            n = 0
            for ti in np.where(A[:, li] > 0)[0]:
                p = rr.pings.get(self.targets[ti].id)
                if p is None or not p.sent or (ref is not None and ref.unreach[ti]):
                    continue
                p0 = b.loss[ti] if bg is None else max(b.loss[ti], bg[ti])
                s0 = (1 - p0) * (math.exp(float(A[ti] @ others["lsurv"])) if others else 1.0)
                s1 = s0 * math.exp(float(A[ti, li]) * own)
                k = p.sent - p.recv
                lost += k
                lam += p.sent * (1 - s0)
                llr += k * math.log((1 - s1) / max(1 - s0, 1e-4)) + p.recv * math.log(s1 / s0)
                n += p.sent
            return (lost, lam, llr) if n else None
        e = self.ends(lid, A)
        if e is None:
            return None
        x, y = e
        dl = A - self.A0
        lr = self.link_rtt() / 2

        def resid(ti):
            if ti < 0:
                return 0.0, 0.0
            p = rr.pings.get(self.targets[ti].id)
            if p is None or not p.recv:
                return None
            off = float(dl[ti] @ lr) if dl[ti].any() else 0.0
            var = 0.0
            if others:
                off += float(A[ti] @ others["d_rtt"])
                var = float(A[ti] @ others["d_var"])
            r = p.rtt_avg - off - b.rtt[ti]
            j = math.sqrt(max(p.rtt_mdev ** 2 - var, 0.0)) - b.mdev[ti]
            return r, j
        rx, ry = resid(x), resid(y)
        if rx is None or ry is None:
            return None
        if ftype == "delay":
            return bool(sgnlog(rx[0] - ry[0]) > EV_DRTT)
        if ftype == "jitter":
            return bool(sgnlog(rx[1] - ry[1]) > EV_DJIT)
        return None

    def track(self, rr: RoundResult, eta: float = 1 / 360, frozen: Optional[np.ndarray] = None):
        """RTT 基线跟踪：由总控在平稳期调用，以指数滑动平均吸收缓慢变化（如忙时全网时延整体抬升）。
        单轮偏离不超过判决阈值的目标直接跟踪；其余目标（受活动告警影响的 frozen、偏离过大、本轮缺测）不直接跟踪，
        而按直接跟踪目标的单位跳数共模漂移平移，既不吸收突发故障，也不会在整体变化中滞后。
        精测基线按同一单位跳数漂移平移（链路往返一次即一跳），避免精测把整体抬升误读为链路劣化。"""
        b = self.base
        upd = np.zeros(self.T, dtype=bool)
        for tid, ps in rr.pings.items():
            ti = self.tidx.get(tid)
            if ti is None or not ps.recv or (frozen is not None and frozen[ti]) or b.rtt[ti] != b.rtt[ti]:
                continue
            d = ps.rtt_avg - b.rtt[ti]
            if abs(d) < self.thr_rtt[ti]:
                b.rtt[ti] += eta * d
                upd[ti] = True
        hops = np.maximum(self.U.sum(0), 1)
        d = b.rtt - self._rtt0
        free = upd & ~np.isnan(d)
        if free.sum() >= 3:
            g = float(np.median(d[free] / hops[free]))           # 单位跳数共模漂移（ms）
            stale = ~upd & ~np.isnan(self._rtt0)
            b.rtt[stale] = self._rtt0[stale] + g * hops[stale]
            b.deep_rtt = self._deep0 + g

    def deep(self, dp: DeepProbe, ref: Optional[ResidualRef] = None) -> np.ndarray:
        li = self.lidx[dp.link]
        b = self.base
        if ref is not None and dp.link in ref.links:      # 已告警链路：扣除冻结的自身效应，带宽不再参与
            if dp.recv == 0:
                return np.array([1, 0, 0, 0, 0, 0], dtype=np.float32)
            loss = 1 - min(1.0, (1 - dp.loss) / math.exp(2 * ref.lsurv[li]))      # 精测往返各穿越一次
            mdev = math.sqrt(max(dp.rtt_mdev ** 2 - 2 * ref.d_var[li], 0.0))
            return np.array([1.0, 0.0, loss, sgnlog(dp.rtt_avg - 2 * ref.d_rtt[li] - b.deep_rtt[li]),
                             sgnlog(mdev - b.deep_mdev[li]), 0.0], dtype=np.float32)
        if dp.recv == 0:
            return np.array([1, 1, 1, 0, 0, np.clip(math.log(max(dp.bw_mbps, 1e-3) / b.deep_bw[li]), -5, 1)],
                            dtype=np.float32)
        return np.array([
            1.0, 0.0, dp.loss,
            sgnlog(dp.rtt_avg - b.deep_rtt[li]),
            sgnlog(dp.rtt_mdev - b.deep_mdev[li]),
            np.clip(math.log(max(dp.bw_mbps, 1e-3) / b.deep_bw[li]), -5, 1),
        ], dtype=np.float32)
