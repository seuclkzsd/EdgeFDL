# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""抗干扰：k-of-n 持续性判决 + 迟滞，决定何时唤醒智能体研判、何时宣告恢复。

单轮的时延尖峰、突发丢包只会让个别目标在一轮里异常，达不到 k_on 轮，不会触发；
控制面事件（邻接断开、开销变化）连续 cp_k 轮即触发；带宽须连续 bw_k 次估计低于阈值。
样本生成与在线运行共用这一判决器，保证训练分布与线上一致（触发对齐采样）。
"""
from __future__ import annotations

from collections import deque
from typing import Dict, List

import numpy as np

from ..agent.features import FeatureEncoder
from ..measure.model import RoundResult


class Detector:
    def __init__(self, enc: FeatureEncoder, window: int = 6, k_on: int = 3, k_off: int = 1,
                 cp_k: int = 2, bw_drop: float = 0.7, bw_k: int = 2):
        self.enc = enc
        self.W, self.k_on, self.k_off, self.cp_k = window, k_on, k_off, cp_k
        self.bw_drop, self.bw_k = bw_drop, bw_k
        self.reset()

    def reset(self):
        self.flags: deque = deque(maxlen=self.W)        # 每轮 (T,) 目标异常
        self.cp: deque = deque(maxlen=self.W)           # 每轮 (L,) 控制面异常
        self.bw_hist: Dict[str, deque] = {l: deque(maxlen=self.bw_k) for l in self.enc.lids}
        self.active = False
        self.last_reason: List[str] = []

    def _round_flags(self, rr: RoundResult):
        e = self.enc
        sent, recv, rtt, mdev = e.window_arrays([rr])
        an = e.anomaly_matrix(sent, recv, rtt, mdev)[0]
        for tid, ps in rr.pings.items():
            ti = e.tidx.get(tid)
            if ti is not None and ps.path and e.base.paths[ti] and ps.path != e.base.paths[ti]:
                an[ti] = True
        cp = np.zeros(e.L, dtype=bool)
        for li, lid in enumerate(e.lids):
            ls = rr.lsdb.get(lid)
            if ls is not None and (not ls.adj_up or abs(ls.cost - e.base.cost[li]) > 0.5):
                cp[li] = True
            if lid in rr.bw:
                self.bw_hist[lid].append(rr.bw[lid] < self.bw_drop * e.base.bw[li])
        return an, cp

    def update(self, rr: RoundResult) -> dict:
        """返回 {'trigger': 本轮是否新进入异常, 'active': 当前是否异常, 'recovered': 本轮是否恢复, 'reason': [...]}"""
        an, cp = self._round_flags(rr)
        self.flags.append(an)
        self.cp.append(cp)
        cnt = np.sum(self.flags, 0)
        cp_cnt = np.sum(self.cp, 0)
        reason = []
        e = self.enc
        hit_t = np.where(cnt >= self.k_on)[0]
        if len(hit_t):
            reason.append("数据面持续异常: " + ",".join(e.targets[i].id for i in hit_t[:5]))
        hit_c = np.where(cp_cnt >= self.cp_k)[0]
        if len(hit_c):
            reason.append("控制面变化: " + ",".join(e.lids[i] for i in hit_c))
        hit_b = [l for l, h in self.bw_hist.items() if len(h) == self.bw_k and all(h)]
        if hit_b:
            reason.append("带宽持续下降: " + ",".join(hit_b))
        anomalous = bool(reason)
        trigger = recovered = False
        if anomalous and not self.active:
            self.active = trigger = True
        elif self.active and not anomalous:
            calm = (cnt.max() if len(cnt) else 0) <= self.k_off and cp_cnt.max() == 0 and not any(
                any(h) for h in self.bw_hist.values())
            if calm:
                self.active, recovered = False, True
        self.last_reason = reason
        return {"trigger": trigger, "active": self.active, "recovered": recovered, "reason": reason}
