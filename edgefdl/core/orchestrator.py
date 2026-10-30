# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""系统总控：周期测量 → 日志落盘 → 去噪判决 → 智能体研判（按需精测）→ 告警/处置建议 → 外部集成。

无告警的平稳期内跟踪基线（吸收忙时整体抬升等缓慢变化）；链路中断告警在一端设备全部邻接断开时升级为设备宕机告警。

多故障（multi=True）：首条告警按原始基线研判，并校验结论在其链路上有该类型的直接证据（交叉错配校验）；
告警稳定后冻结残差基线（features.ResidualRef），此后在扣除已确认告警效应的残差上再研判，屏蔽已告警链路，
持续出现新的、有直接证据的结论即追加告警（复核更严）。各告警按其链路自身的逐轮证据（相对原始基线）逐条恢复，
其余告警保持；全网恢复时清除证据已消失或无法跟踪的告警。multi=False 时与单告警版本行为完全一致。

单线程测量循环 + 线程安全快照，供 Web/API 读取。所有异常被捕获并计数，保证 7×24 不中断。
"""
from __future__ import annotations

import copy
import math
import os
import threading
import time
import traceback
from collections import deque
from typing import Dict, List, Optional

import numpy as np

from ..agent.features import LIGHT_FEATURES, FeatureEncoder, deep_evidence, link_evidence, poisson_sf
from ..agent.infer import Policy
from ..agent.knowledge import advise, advise_node
from ..faults.model import CATEGORY
from ..measure.model import MeasurePlan
from ..measure.store import AlarmStore, CsvStore
from ..topology import Topology
from .alarms import SEVERITY
from .denoise import Detector


_PROC = None
SEV_RANK = {"critical": 3, "major": 2, "minor": 1}
DATA_PLANE = ("delay", "loss", "jitter")
PROBE_TYPES = ("link_down", "bandwidth") + DATA_PLANE      # 精测能直接说明的故障类型


def _proc_usage():
    global _PROC
    try:
        import psutil
        if _PROC is None:                     # cpu_percent 以同一 Process 对象两次调用之间为统计区间
            _PROC = psutil.Process()
        p = _PROC
        return dict(cpu_pct=p.cpu_percent(None) / max(1, psutil.cpu_count()), rss_mb=p.memory_info().rss / 2 ** 20,
                    sys_mem_total_mb=psutil.virtual_memory().total / 2 ** 20)
    except Exception:
        return {}


def _links(a: Dict) -> List[str]:
    return a.get("links") or [a["link_id"]]


class Engine:
    def __init__(self, topo: Topology, backend, policy: Policy, plan: MeasurePlan, log_dir: str,
                 sinks=None, model_version: str = "", recheck_every: int = 6, confirm_n: int = 2,
                 settle: int = 2, confirm_gap: int = 2, track: bool = True, rebase_after: int = 360,
                 multi: bool = True, confirm_sec: int = 3, clear_n: int = 3, rebase_wait: int = 4,
                 loss_llr: float = math.log(1e-5), loss_llr_probe: float = math.log(0.1), probe_every: int = 6,
                 sec_k: int = 3, sec_expire: int = 12, sec_neutral: bool = True, sec_hold: bool = True,
                 node_back: int = 18, rot_gate: bool = True, relearn_mask: bool = True):
        self.topo, self.backend, self.policy, self.plan = topo, backend, policy, plan
        self.enc: FeatureEncoder = policy.enc
        self.det = Detector(self.enc, window=plan.window)
        self.window: deque = deque(maxlen=plan.window)
        os.makedirs(log_dir, exist_ok=True)
        self.log_dir = log_dir
        self.mstore = CsvStore(os.path.join(log_dir, "measure_log.csv"), topo)
        self.astore = AlarmStore(os.path.join(log_dir, "alarm_log.csv"))
        self.sinks = sinks or []
        self.model_version = model_version
        self.recheck_every = recheck_every
        self.confirm_n = confirm_n            # 连续 n 次研判结论一致才发告警（抑制偶发误判）
        self.confirm_gap = confirm_gap        # 复核研判与上一次研判至少间隔的轮数
        self.track = track                    # 平稳期基线跟踪
        self.rebase_after = rebase_after      # 丢包基线重学的观察期（360 轮 = 30 min），见 _watch_loss
        self.loss_state, self.loss_since = None, 0
        self.loss_hist: deque = deque(maxlen=rebase_after)
        self.loss_churn: deque = deque(maxlen=16)   # 判决器本次持续活动期间的丢包告警 (轮次, 链路)
        self.calm = True                      # 最近一次研判结论为正常（判决器仍活动时也允许跟踪）
        self.pending: Optional[Dict] = None
        self.settle = settle                  # 判决器触发后再观察几轮才研判
        self.diag_due: Optional[int] = None
        # 多告警
        self.multi = multi                    # False：同一时刻只有一条活动告警（单告警版本行为）
        self.confirm_sec = confirm_sec        # 追加告警（残差研判）需连续一致的次数
        self.clear_n = clear_n                # 告警链路自身证据连续消失 n 轮即逐条恢复（带宽按 n 次估计中取判决器的 bw_k 次）
        self.rebase_wait = rebase_wait        # 告警增减后等窗口内全为新状态下的测量再冻结残差基线
        self.loss_llr = loss_llr              # 丢包告警逐条恢复还须"仍按告警强度丢包"对"已恢复"的似然比低于此值
        self.loss_llr_probe = loss_llr_probe  # 似然比已倾向恢复（低于此值）而未达上一门限时，精测一次确认
        self.probe_every = probe_every        # 两端都不经过告警链路（盲区）时每隔几轮精测一次
        self.sec_k = sec_k                    # 残差持续性判决：窗口内至少几轮异常（同 Detector 的 k_on）
        self.sec_expire = sec_expire          # 待复核的追加结论在残差暂无异常时最多保留几轮
        self.sec_neutral = sec_neutral        # 残差研判为正常/缺证据时不清零待复核结论（中度丢包时有时无）
        self.sec_hold = sec_hold              # 已有待复核结论时，残差暂无异常也照常复核
        self.node_back = node_back            # 设备已恢复而仍有链路中断、研判迟迟未降为链路中断告警时，几轮后清除设备宕机告警
        self.rot_gate = rot_gate              # 丢包轮换票须告警链路自身窗口内已无显著丢包（真实故障不会被当作背景变化）
        self.relearn_mask = relearn_mask      # 丢包基线重学时，告警期间受其影响的目标该轮不计入
        self.loss_aff: deque = deque(maxlen=rebase_after)   # 多告警：loss_hist 各轮受活动告警影响的目标
        self.sec_pending: Optional[Dict] = None
        self.rot_pending: Optional[Dict] = None
        self.node_pending: Optional[Dict] = None
        self.ref = None                       # 残差基线（ResidualRef），无活动告警或待冻结时为空
        self._ref_last = None                 # 上一次冻结的残差基线（新基线冻结前供逐条恢复沿用，见 _trk_ref）
        self.rebase_due: Optional[int] = None
        self._trk: Dict[str, Dict] = {}       # 告警号 → 逐条恢复的跟踪状态
        self._bg = np.zeros(self.enc.T)       # 现场背景丢包率（逐条恢复用，见 FeatureEncoder.live_loss）
        self.deep_probes = 0                  # 精测次数累计（含研判与恢复复核）
        self.extra_probes = 0                 # 其中恢复复核的精测（每轮全引擎至多 1 次）
        self.lock = threading.RLock()
        self.round = 0
        self.alarm_seq = 0
        self.alarms: List[Dict] = []          # 当前活动告警（单告警模式至多 1 条）
        self.history: deque = deque(maxlen=200)
        self.events: deque = deque(maxlen=200)
        self.bytes_hist: deque = deque(maxlen=120)
        self.series: Dict[str, deque] = {t.id: deque(maxlen=120) for t in self.enc.targets}
        self.link_health: Dict[str, float] = {l: 0.0 for l in self.enc.lids}
        self.last_diag_round = -10 ** 9
        self.errors = 0
        self.suppressed = 0
        self.started = time.time()
        self.id_prefix = f"A{int(self.started) % 100000:05d}{os.getpid() % 100:02d}"   # 同一秒启动的多个引擎不重号
        self.round_ms: deque = deque(maxlen=200)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        _proc_usage()

    # ------------------------------------------------------------------ 活动告警
    @property
    def active(self) -> Optional[Dict]:
        """主告警（界面/接口兼容）：活动告警中最严重的，同级取最新。"""
        with self.lock:
            if not self.alarms:
                return None
            return max(self.alarms, key=lambda a: (SEV_RANK.get(a["severity"], 0), a["first_round"], a["alarm_id"]))

    def active_list(self) -> List[Dict]:
        """活动告警副本，按严重度、新旧排序（首条即 active）。"""
        with self.lock:
            al = sorted(self.alarms, key=lambda a: (SEV_RANK.get(a["severity"], 0), a["first_round"], a["alarm_id"]),
                        reverse=True)
            return [dict(a) for a in al]

    # ------------------------------------------------------------------ 主循环
    def step(self):
        t0 = time.perf_counter()
        rr = self.backend.measure_round(self.round)
        self.mstore.write_round(rr)
        probe = None
        with self.lock:
            self.round += 1
            self.window.append(rr)
            self.bytes_hist.append([rr.round_id, rr.probe_bytes])
            for tid, ps in rr.pings.items():
                if tid in self.series:
                    self.series[tid].append((rr.round_id, ps.rtt_avg if ps.recv else None, ps.loss))
            st = self.det.update(rr)
            if self.track and not self.pending and not self.sec_pending and (
                    self.alarms or not st["active"] or self.calm):
                self.enc.track(rr, frozen=self._alarm_mask())
            if self.multi and not st["active"]:
                self.loss_churn.clear()               # 轮换只统计判决器本次持续活动期间（含外部复位判决器）
            if self.rebase_after:
                self._watch_loss(rr, st)
            if len(self.window) >= 2:
                light, _ = self.enc.light(list(self.window))
                self._update_health(light)
            if self.multi and self.alarms:
                probe = self._track_alarms(rr)
            if self.multi and st["recovered"]:
                self._reset_pending()
                if self.alarms:
                    probe = self._recover(probe)
            if self.multi and self.rebase_due is not None and self.round >= self.rebase_due \
                    and len(self.window) == self.plan.window:
                self._rebase()
                self.diag_due = self.round            # 残差基线就绪后立即残差研判一次
        if st["trigger"]:
            self.diag_due = self.round + self.settle
        since = self.round - self.last_diag_round
        due = self.diag_due is not None and self.round >= self.diag_due
        if due:
            self.diag_due = None
        need = st["active"] and (due or (self.diag_due is None and (
            since >= self.recheck_every or ((self.pending or self.sec_pending or self.rot_pending or self.node_pending)
                                            and since >= self.confirm_gap))))
        if need and len(self.window) == self.plan.window:
            self._diagnose(st)
        else:
            self.policy.keepalive()
        if probe is not None:
            self._recheck_probe(*probe)
        if st["recovered"] and not self.multi:
            with self.lock:
                self._reset_pending()
                if self.alarms:
                    self._clear("测量指标恢复至基线")
        self.round_ms.append((time.perf_counter() - t0) * 1000)

    def _reset_pending(self):
        self.pending = None
        self.sec_pending = None
        self.rot_pending = None
        self.node_pending = None
        self.diag_due = None
        self.calm = True
        self.loss_churn.clear()               # 轮换只统计判决器持续活动期间互相替换的丢包结论

    def set_baseline(self, base):
        """上线基线学习：用现场实测基线替换模型包自带的基线（编码器、判决器随之重建）。"""
        with self.lock:
            self.enc = FeatureEncoder(self.topo, base)
            self.policy.enc = self.enc
            self.det = Detector(self.enc, window=self.plan.window)
            self.window.clear()
            self.ref, self.rebase_due = None, (self.round + self.rebase_wait) if self.alarms else None
            self._ref_last = None

    def _alarm_mask(self) -> Optional[np.ndarray]:
        """受活动告警影响的目标：基线路径经过告警链路，或就是告警链路/设备自身的地址。"""
        if not self.alarms:
            return None
        m = np.zeros(self.enc.T, dtype=bool)
        for a in self.alarms:
            links = _links(a)
            li = [self.enc.lidx[l] for l in links if l in self.enc.lidx]
            m |= self.enc.U[li].any(0)
            for ti, t in enumerate(self.enc.targets):
                if t.link in links or t.node == a.get("node"):
                    m[ti] = True
        return m

    def _relearn_loss(self, min_n: int = 100):
        b = self.enc.base
        S = np.array([s for s, _ in self.loss_hist])
        lost = S - np.array([r for _, r in self.loss_hist])
        keep = (S > 0) & (lost <= 1)
        if not (self.multi and self.relearn_mask and len(self.loss_aff) == len(self.loss_hist)):
            n = (S * keep).sum(0)
            b.loss = np.where(n > 0, np.minimum(0.05, (lost * keep).sum(0) / np.maximum(n, 1)), b.loss)
            self.enc._mlost.clear()
            return
        # 多告警：告警期间受其影响的目标（路径经过告警链路）该轮不计入，故障丢包不学进背景；剩余样本不足的目标
        # 取同类（设备接口 / 主机）样本充足目标的中位（ICMP 限速等背景丢包作用于同类全部目标），都不足则保持原值
        keep &= ~np.array([np.zeros(self.enc.T, bool) if m is None else m for m in self.loss_aff])
        n = (S * keep).sum(0)
        new = np.minimum(0.05, (lost * keep).sum(0) / np.maximum(n, 1))
        router = np.array([self.topo.nodes[t.node].transit for t in self.enc.targets])
        for kind in (router, ~router):
            good = kind & (n >= min_n)
            fill = kind & (n < min_n)
            new[fill] = float(np.median(new[good])) if good.any() else b.loss[fill]
        b.loss = new
        self.enc._mlost.clear()

    def _watch_loss(self, rr, st: Dict):
        """背景丢包基线随现场变化重学（如设备上线后开启或关闭 ICMP 限速）：判决器持续活动 rebase_after 轮
        而始终没有一致的研判结论（无告警），或持续平静 rebase_after 轮而基线仍含背景丢包时，按这段时间的实测
        重学各目标背景丢包率。与基线学习一致，单轮丢 2 个及以上的轮次（突发干扰）不计入，上限 5%。"""
        sent, recv, _, _ = self.enc.window_arrays([rr])
        self.loss_hist.append((sent[0], recv[0]))
        if self.multi:
            self.loss_aff.append(self._alarm_mask())
        lossy = [a for a in self.alarms if a["fault_type"] == "loss"]
        churn = len({l for r, l in self.loss_churn if self.round - r <= self.rebase_after}) >= 3
        if lossy and (churn or (self.multi and self._bg_shift())):
            # 丢包结论在 ≥3 条链路之间轮换，或（多告警模式）不经过任何告警链路的设备目标普遍出现超出基线的丢包：
            # 不是单条链路故障，而是全网背景丢包变化（如设备开始 ICMP 限速）
            if self.multi and not churn:          # 背景变化期间仍有真实丢包故障：自身仍有显著超额丢包的告警保留
                win = list(self.window)
                lossy = [a for a in lossy if not self._own_loss(a, win)]
            self._relearn_loss()
            self.loss_churn.clear()
            self.det.reset()
            self.pending, self.diag_due, self.calm = None, None, True
            why = ("丢包结论在多条链路间轮换" if churn else "设备目标普遍出现超出基线的丢包") + "，判定为背景丢包变化并已重学丢包基线"
            if self.multi:
                for a in lossy:
                    self._clear(why, alarm=a)
            else:
                self._clear(why)
            self.events.appendleft(dict(ts=time.time(), round=self.round, kind="filtered",
                                        msg=("多条链路轮番出现丢包结论" if churn else "设备目标普遍出现超出基线的丢包")
                                            + f"，判定为背景丢包变化，按近 {len(self.loss_hist)} 轮实测"
                                            f"重学背景丢包基线（最大 {self.enc.base.loss.max():.1%}）"))
            return
        state = None if self.alarms else ("busy" if st["active"] else "calm")
        if state != self.loss_state:
            self.loss_state, self.loss_since = state, self.round
            return
        b = self.enc.base
        if state is None or self.round - self.loss_since < self.rebase_after or (state == "calm" and not b.loss.any()):
            return
        self._relearn_loss()
        self.loss_since = self.round
        if state == "busy":
            self.det.reset()
            self.pending, self.diag_due, self.calm = None, None, True
        self.events.appendleft(dict(ts=time.time(), round=self.round, kind="filtered",
                                    msg=("判决器持续活动而无一致研判结论" if state == "busy" else "网络持续平稳") +
                                        f"，按近 {self.rebase_after} 轮实测重学背景丢包基线（最大 {b.loss.max():.1%}）"))

    def _bg_shift(self, rounds: int = 30, frac: float = 0.7, min_targets: int = 6) -> bool:
        """背景丢包变化（多告警模式）：近 rounds 轮内，不经过任何告警链路（含这段时间内已恢复的）、按路由可达的
        设备接口目标中，至少 frac 比例各自的丢包率超出基线 2% 以上、中位不超过 8%，且最早与最近各 1/3 时段的中位都
        超出 2%（单轮丢 2 个及以上的突发不计）。链路故障只影响经过它的目标，设备 ICMP 限速则作用于全部设备目标；
        多告警模式下告警按自身证据逐条恢复、很少"轮换"，需要这一直接判据。"""
        if len(self.loss_hist) < rounds:
            return False
        h = list(self.loss_hist)[-rounds:]
        S = np.array([x for x, _ in h])
        lost = S - np.array([y for _, y in h])
        keep = (S > 0) & (lost <= 1)
        A = self.enc.traversal(self.enc.last_lsdb(self.window))
        recent = {l for a in self.history if self.round - a.get("cleared_round", self.round) <= rounds for l in _links(a)}
        li = [self.enc.lidx[l] for l in recent | {l for a in self.alarms for l in _links(a)} if l in self.enc.lidx]
        sel = A.any(1) & (A[:, li].sum(1) == 0) & np.array([self.topo.nodes[t.node].transit for t in self.enc.targets])
        n = (S * keep).sum(0)
        sel &= n >= 50
        if sel.sum() < min_targets:
            return False
        rate = lambda a, b: (lost[a:b] * keep[a:b]).sum(0)[sel] / np.maximum((S[a:b] * keep[a:b]).sum(0)[sel], 1)
        ex = rate(0, rounds) - self.enc.base.loss[sel]
        third = rounds // 3                       # 较早与新近两段都普遍超出：持续的背景变化，而非刚修复的故障
        early, late = rate(0, third) - self.enc.base.loss[sel], rate(rounds - third, rounds) - self.enc.base.loss[sel]
        return bool((ex > 0.02).mean() >= frac and np.median(ex) <= 0.08    # 中度以上的丢包是链路故障而非背景
                    and np.median(early) > 0.02 and np.median(late) > 0.02)

    def _own_loss(self, a: Dict, window, alpha: float = 0.01) -> bool:
        """丢包告警链路自身在窗口内仍有显著超额丢包（逐轮证据合并的泊松检验，背景含现场实测背景丢包）。"""
        A = self.enc.traversal(self.enc.last_lsdb(window))
        k = self._trk.get(a["alarm_id"], {})
        own = k.get("own") if "own" in k else self.enc.loss_strength(window, a["link_id"], A)
        lost = lam = 0.0
        for rr in window:
            ev = self.enc.round_evidence(rr, "loss", a["link_id"], A, self._trk_ref(), own=own, bg=self._bg)
            if ev:
                lost, lam = lost + ev[0], lam + ev[1]
        return bool(poisson_sf(lost, lam) < alpha)

    def _update_health(self, light):
        # 0 正常 / 0.5 可疑 / 1 异常：只取链路本身的直接证据（两端可达性、远近端差分、邻接、开销、带宽），
        # 不用层析对比特征——故障引起的绕行会把它扩散到上游链路，着色会误导值守人员
        f = {n: light[:, i] for i, n in enumerate(LIGHT_FEATURES)}
        h = np.maximum.reduce([f["end_unreach_both"], np.clip(f["diff_loss"] * 10, 0, 1),
                               np.clip(f["diff_drtt"] / 2, 0, 1), np.clip(f["diff_djit"] / 2, 0, 1),
                               f["adj_down"], np.clip(np.abs(f["cost_ratio"]), 0, 1),
                               np.clip(-f["bw_ratio"] * f["has_bw"], 0, 1) * (f["bw_ratio"] < -0.35)])
        for i, lid in enumerate(self.enc.lids):
            self.link_health[lid] = float(np.round(h[i], 2))

    # ------------------------------------------------------------------ 多告警：逐条恢复
    def _node_up(self, n: str, rr) -> Optional[bool]:
        """设备本轮是否已恢复运行：环回地址有应答或任一与相邻路由设备的 OSPF 邻接恢复为 True，
        邻接仍全部断开（或环回不通）为 False，本轮缺测为 None。"""
        adj = [x.id for x in self.topo.links if n in (x.a, x.b) and self.topo.nodes[x.other(n)].transit]
        lo = next((t for t in self.enc.targets if t.node == n and t.kind == "lo"), None)
        p = rr.pings.get(lo.id) if lo else None
        if (p is not None and p.sent and p.recv > 0) or any(x in rr.lsdb and rr.lsdb[x].adj_up for x in adj):
            return True
        return False if rr.lsdb or (p is not None and p.sent) else None

    def _evidence(self, a: Dict, rr, A):
        """告警链路自身的本轮证据（features.round_evidence）。设备宕机：全部链路恢复才算证据消失；设备已恢复运行
        而仍有链路中断时无从判断（不计数），由研判降为链路中断告警（_diagnose_more），超时清除（_track_alarms）。"""
        if a["fault_type"] == "node_down":
            ev = [self.enc.round_evidence(rr, "link_down", l, A) for l in a["links"]]
            if all(e is False for e in ev):
                return False
            if self._node_up(a["node"], rr):
                return None
            return True if any(e is True for e in ev) else None
        k = self._trk[a["alarm_id"]]
        if a["fault_type"] == "loss" and "own" not in k:
            k["own"] = self.enc.loss_strength(list(self.window), a["link_id"], A)
        return self.enc.round_evidence(rr, a["fault_type"], a["link_id"], A, self._trk_ref(), own=k.get("own", 0.0),
                                       bg=self._bg)

    def _trk_ref(self):
        """逐条恢复扣除其余告警效应所用的残差基线。告警增减后 rebase_wait 轮内新基线尚未冻结：沿用上一次冻结的，
        只保留仍活动告警链路的效应——否则这几轮的差分仍含另一条告警的效应（如 L6、L10 同时时延，L10 远端经过
        L6 一次、近端往返两次，差分 L10-L6≈0），新发的告警会在新基线冻结前被误判为证据消失而清除。"""
        if self.ref is not None or self._ref_last is None:
            return self.ref
        r = copy.copy(self._ref_last)
        live = {l for a in self.alarms for l in _links(a)}
        gone = np.array([l not in live for l in self.enc.lids])
        r.d_rtt, r.d_var, r.lsurv = (np.where(gone, 0.0, v) for v in (r.d_rtt, r.d_var, r.lsurv))
        return r

    def _track_alarms(self, rr):
        """逐条恢复（持锁调用）：每轮按告警链路自身证据计数，连续 clear_n 轮证据消失即恢复该条，其余告警保持。
        - 丢包：跨轮累计经过该链路的全部目标的丢包数，与背景（基线丢包率 + 其余告警的冻结效应）做泊松检验，
          显著即仍有证据；恢复还须"仍按告警发出时估计的强度丢包"对"已恢复"的似然比低于 loss_llr。只经过一个
          目标、只穿越一次的链路（接入环）单轮偶然全收的概率近一半，似然比下降慢：证据已倾向恢复（似然比
          < loss_llr_probe）而未达门限时精测一次确认；
        - 带宽：只在有估计的轮次计数，须连续 bw_k 次估计正常；
        - 本轮无从判断（缺测、两端都不经过该链路）不计数；连续 probe_every 轮（盲区）或 3×clear_n 轮
          （发出时即缺直接证据的告警）无从判断时精测一次复核。
        精测与告警矛盾即恢复，支持则重新计数。每轮全引擎至多精测 1 次，返回待精测的 (告警号, 链路)，
        由主循环在锁外执行。"""
        A = self.enc.traversal(rr.lsdb or self.enc.last_lsdb(self.window))
        self._bg = self.enc.live_loss(list(self.window), A, [l for a in self.alarms for l in _links(a)])
        probe = None
        for a in list(self.alarms):
            k = self._trk.setdefault(a["alarm_id"], dict(quiet=0, unknown=0, lost=0.0, lam=0.0, llr=0.0,
                                                         probed=self.round))
            ft = a["fault_type"]
            if not a.get("evident", True):            # 发出时即缺直接证据：不逐轮计数，按全网恢复清除或精测复核
                k["unknown"] += 1
                ev = None
            else:
                ev = self._evidence(a, rr, A)
                if isinstance(ev, tuple):             # 丢包：跨轮累计检验
                    lost, lam, llr = ev
                    k["lost"] += lost
                    k["lam"] += lam
                    k["llr"] += llr
                    ev = bool(poisson_sf(k["lost"], k["lam"]) < 0.01)
                if ev is True:
                    self._reset_trk(k)
                elif ev is False:
                    k["quiet"] += 1
                    k["unknown"] = 0
                else:
                    k["unknown"] += 1
            if ft == "node_down":
                k["back"] = k.get("back", 0) + 1 if ev is None and self._node_up(a["node"], rr) else 0
                if k["back"] >= self.node_back:
                    self._clear("设备已恢复运行，剩余链路中断另行研判", alarm=a)
                    continue
            need = self.det.bw_k if ft == "bandwidth" else self.clear_n
            if ev is False and k["quiet"] >= need:
                if ft != "loss" or k["llr"] < self.loss_llr:
                    self._clear("告警链路自身证据恢复至基线", alarm=a)
                    continue
                if probe is None and k["llr"] < self.loss_llr_probe and self.round - k["probed"] >= self.probe_every:
                    probe = (a["alarm_id"], a["link_id"])
            wait = 3 * self.clear_n if not a.get("evident", True) else self.probe_every
            if probe is None and ft in PROBE_TYPES and k["unknown"] >= wait and self.round - k["probed"] >= wait:
                probe = (a["alarm_id"], a["link_id"])
        return probe

    @staticmethod
    def _reset_trk(k: Dict):
        k["quiet"] = k["unknown"] = 0
        k["lost"] = k["lam"] = k["llr"] = 0.0

    def _recheck_probe(self, aid: str, lid: str):
        """恢复复核精测（锁外执行）：精测与告警类型矛盾即恢复该条，支持则重新计数。"""
        with self.lock:
            a = next((x for x in self.alarms if x["alarm_id"] == aid), None)
            if a is None:
                return
            self._trk[aid]["probed"] = self.round
            self.extra_probes += 1
        d = self.enc.deep(self._probe(lid))
        li = self.enc.lidx[lid]
        if a["fault_type"] == "loss":             # 精测丢包按链路两端设备的背景丢包率放宽（ICMP 限速同样作用于精测）
            ends = [ti for ti, t in enumerate(self.enc.targets) if t.link == lid]   # 精测应答来自链路两端设备
            p0 = max([max(self.enc.base.loss[t], self._bg[t]) for t in ends] + [0.0])
            ev = bool(d[1] > 0.5 or d[2] > 0.02 + 2 * p0)
        else:
            ev = deep_evidence(d, a["fault_type"])
        with self.lock:
            if a not in self.alarms:
                return
            if ev is True:
                self._reset_trk(self._trk[aid])
            elif ev is False:
                self._clear("精测复核未见该故障，告警恢复", alarm=a)

    def _recover(self, probe=None):
        """判决器宣告全网恢复（持锁调用）：窗口内链路自身证据已消失、发出时即缺直接证据、或差分已无法跟踪
        （另一处故障改道使远端不再经过该链路）的告警清除；窗口里还剩故障消除前的一轮时以逐轮证据为准。
        证据仍在的保留，由逐条恢复处理——k-of-n 判决器偶尔会在中度丢包下误判平静（只经过一个目标的链路尤甚），
        不能连带清掉仍存在的故障；其中丢包/带宽告警逐轮证据已倾向恢复的，本轮精测一次确认（返回待精测项）。"""
        window = list(self.window)
        light = self.enc.light(window)[0]
        A = self.enc.traversal(self.enc.last_lsdb(window))
        for a in list(self.alarms):
            ft = a["fault_type"]
            k = self._trk.get(a["alarm_id"], {})
            if ft == "node_down":
                keep = any(link_evidence(light[self.enc.lidx[l]], "link_down") for l in a["links"])
            else:
                li = self.enc.lidx[a["link_id"]]
                trackable = ft not in DATA_PLANE or (A[self.enc.far[li], li] > 0 and not (
                    self.enc.near[li] >= 0 and A[self.enc.near[li], li] > 0))
                keep = a.get("evident", True) and trackable
                if keep and ft == "loss":             # 丢包以跨轮似然比为准，未达门限时精测确认
                    keep = k.get("llr", 0.0) >= self.loss_llr
                    if keep and probe is None:
                        probe = (a["alarm_id"], a["link_id"])
                elif keep:
                    keep = link_evidence(light[li], ft)
                    if keep and k.get("quiet", 0) >= 1:
                        if ft != "bandwidth":
                            keep = False
                        elif probe is None:
                            probe = (a["alarm_id"], a["link_id"])
            if not keep:
                self._clear("测量指标恢复至基线", alarm=a)
        return probe

    def _rebase(self):
        """冻结残差基线（持锁调用）：设备宕机按其各链路中断计入。"""
        self.rebase_due = None
        self.sec_pending = None
        al = [(("link_down", l) if a["fault_type"] == "node_down" else (a["fault_type"], l))
              for a in self.alarms for l in _links(a)]
        self.ref = self.enc.residual_ref(list(self.window), al) if al else None
        self._ref_last = self.ref

    # ------------------------------------------------------------------ 研判
    def _probe(self, link: str):
        dp = self.backend.deep_probe(link)
        self.mstore.write_deep(dp, self.round, self.topo.vantage)
        with self.lock:
            self.deep_probes += 1
            if self.bytes_hist:                   # 精测字节计入当前轮
                self.bytes_hist[-1][1] += dp.probe_bytes
        return dp

    def _diagnose(self, st: Dict):
        self.last_diag_round = self.round
        if not self.multi:
            return self._diagnose_single(st)
        if self.alarms:
            return self._diagnose_more(st)
        res = self.policy.diagnose(list(self.window), probe_fn=self._probe, gate=True)
        if res["type"] == "link_down":
            res = self._node_down(res)
        with self.lock:
            self.calm = res["type"] == "normal"
            if res["type"] == "normal":
                self.pending = None
                self.suppressed += 1
                self.events.appendleft(dict(ts=time.time(), round=self.round, kind="filtered",
                                            msg="判决器触发，智能体研判为正常波动，已抑制告警",
                                            reason=st.get("reason", [])))
                return
            if not self._confirm(res, "pending", self.confirm_n, f"疑似 {res['link']} {res['type_cn']}，等待下一轮复核"):
                return
            self._raise(res, st)

    def _confirm(self, res: Dict, slot: str, n: int, msg: str) -> bool:
        """连续 n 次一致的结论才放行（持锁调用）；slot 为 pending（首条）或 sec_pending（追加）。"""
        key = (res["type"], res["link"])
        p = getattr(self, slot)
        if p and p["key"] == key:
            p["n"] += 1
        else:
            p = dict(key=key, n=1, first=self.round)
            setattr(self, slot, p)
        if p["n"] < n:
            self.events.appendleft(dict(ts=time.time(), round=self.round, kind="pending", msg=msg))
            return False
        setattr(self, slot, None)
        return True

    def _diagnose_more(self, st: Dict):
        """已有告警时：原始基线研判只刷新同结论告警的置信度（及链路中断细化为设备宕机）；新告警只来自残差研判。"""
        window = list(self.window)
        res = self.policy.diagnose(window, gate=True)     # 只作刷新，不精测（精测留给残差研判）
        if res["type"] == "link_down":
            res = self._node_down(res)
        with self.lock:
            self.calm = res["type"] == "normal"
            self.pending = None
            for a in self.alarms:
                if a["fault_type"] == res["type"] and a["link_id"] == res["link"]:
                    a["confidence"], a["last_round"] = res["confidence"], self.round
            if res["type"] == "node_down" and self._upgrade_node(res, st):
                return
            # 设备宕机告警的设备已恢复运行（研判未再升级为设备宕机），而其某条链路仍中断：连续 confirm_n 次一致即
            # 降为该链路的中断告警（与单告警模式"研判结论变化"的替换相同）
            na = next((a for a in self.alarms if a["fault_type"] == "node_down" and res["type"] == "link_down"
                       and res.get("evident") and res["link"] in a["links"] and self._node_up(a["node"], window[-1])),
                      None)
            if na is None:
                self.node_pending = None
            elif self._confirm(res, "node_pending", self.confirm_n, f"设备 {na['node']} 已恢复运行，仍疑似 {res['link']} 中断"):
                self._clear("设备已恢复运行，降为链路中断告警", replaced=True, alarm=na)
                self._raise(res, st)
                return
            # 只有一条（丢包）告警时，单告警模式下会"替换"它的结论（另一条链路的丢包，连续 confirm_n 次）计入
            # 丢包轮换，背景丢包变化（如设备开始 ICMP 限速）时同样能触发丢包基线重学。轮换票只在告警链路自身窗口内
            # 已无显著超额丢包时计入：真实丢包故障仍在时，不精测的刷新研判偶尔指向别的链路，不能据此判为背景变化
            # （否则重学会把故障丢包学进基线，从此不再告警）
            lossy = [a for a in self.alarms if a["fault_type"] == "loss"]
            if res["type"] == "loss" and res.get("evident") and len(lossy) == 1 and len(self.alarms) == 1 \
                    and res["link"] != lossy[0]["link_id"] and not (self.rot_gate and self._own_loss(lossy[0], window)):
                if self._confirm(res, "rot_pending", self.confirm_n, f"原始基线研判另指向 {res['link']} 丢包"):
                    self.loss_churn.append((self.round, res["link"]))
            else:
                self.rot_pending = None
            ref, alarmed = self.ref, sorted({l for a in self.alarms for l in _links(a)})
        if ref is None:                           # 新告警的残差基线尚未冻结
            return
        act = self.enc.residual_activity(window, ref, self.sec_k)
        if not act:                               # 残差 k-of-n 判决偶有间断：待复核结论不清零，超时作废
            with self.lock:
                if self.sec_pending and self.round - self.sec_pending["first"] > self.sec_expire:
                    self.sec_pending = None
                if not (self.sec_pending and self.sec_hold):
                    return
        r2 = self.policy.diagnose(window, probe_fn=self._probe, ref=ref, gate=self._sec_gate(window, ref),
                                  exclude=alarmed)
        if r2["type"] == "link_down":
            r2 = self._node_down(r2)
        with self.lock:
            if self.ref is not ref:               # 研判期间告警有增减
                return
            if r2["type"] == "normal" or not r2.get("evident") or r2["link"] in alarmed:
                # 残差上的"正常"或缺直接证据的结论不计数；neutral=False 时清零待复核结论
                if not self.sec_neutral or (self.sec_pending and self.round - self.sec_pending["first"] > self.sec_expire):
                    self.sec_pending = None
                return
            if r2["type"] == "node_down" and any(a.get("node") == r2["node"] for a in self.alarms):
                self.sec_pending = None
                return
            if not self._confirm(r2, "sec_pending", self.confirm_sec,
                                 f"扣除已有告警后仍疑似 {r2['link']} {r2['type_cn']}，等待复核"):
                return
            r2["secondary"] = True
            st2 = dict(st, reason=st.get("reason", []) + act)
            if r2["type"] == "node_down" and self._upgrade_node(r2, st2):
                return
            self._raise(r2, st2)

    def _sec_gate(self, window, ref):
        """追加告警的直接证据校验：丢包须经过该链路的一端相对另一端（扣除已告警效应）显著超额——残差中
        两端都带有已告警故障的抽样起伏，按固定 2% 判断远近端差容易误认；其余类型同首条告警。"""
        def ok(f, ftype, d, lid):
            if ftype == "loss":
                if d is not None and d[0] > 0.5 and deep_evidence(d, "loss"):
                    return True
                v = self.enc.loss_excess(window, lid, ref)
                return link_evidence(f, ftype, d) if v is None else v
            return link_evidence(f, ftype, d)
        return ok

    def _upgrade_node(self, res: Dict, st: Dict) -> bool:
        """设备宕机结论覆盖已有的链路中断告警时，以一条设备宕机告警替换它们（持锁调用），返回是否替换。
        该设备已有宕机告警时不算替换：调用方照常继续（残差研判其余链路的故障）。"""
        if any(a.get("node") == res["node"] for a in self.alarms):
            return False
        cov = [a for a in self.alarms if a["fault_type"] == "link_down" and a["link_id"] in res["links"]]
        if not cov:
            return False
        self.pending = None
        for a in cov:
            self._clear("升级为设备宕机告警", replaced=True, alarm=a)
        self._raise(res, st)
        return True

    def _diagnose_single(self, st: Dict):
        res = self.policy.diagnose(list(self.window), probe_fn=self._probe)
        if res["type"] == "link_down":
            res = self._node_down(res)
        with self.lock:
            self.calm = res["type"] == "normal"
            a = self.alarms[0] if self.alarms else None
            if res["type"] == "normal":
                self.pending = None
                if not a:
                    self.suppressed += 1
                    self.events.appendleft(dict(ts=time.time(), round=self.round, kind="filtered",
                                                msg="判决器触发，智能体研判为正常波动，已抑制告警",
                                                reason=st.get("reason", [])))
                return
            same = a and a["fault_type"] == res["type"] and a["link_id"] == res["link"]
            if a and a["fault_type"] == "link_down" and res["type"] == "node_down" and a["link_id"] in res["links"]:
                self.pending = None           # 已确认的链路中断细化为设备宕机，无需再次复核
                self._clear("升级为设备宕机告警", replaced=True)
                self._raise(res, st)
                return
            if same:
                a["confidence"] = res["confidence"]
                a["last_round"] = self.round
                return
            key = (res["type"], res["link"])
            if self.pending and self.pending["key"] == key:
                self.pending["n"] += 1
            else:
                self.pending = dict(key=key, n=1)
            if self.pending["n"] < self.confirm_n:
                self.events.appendleft(dict(ts=time.time(), round=self.round, kind="pending",
                                            msg=f"疑似 {res['link']} {res['type_cn']}，等待下一轮复核"))
                return
            self.pending = None
            if self.alarms:
                self._clear("研判结论变化", replaced=True)
            self._raise(res, st)

    def _node_down(self, res: Dict) -> Dict:
        """链路中断的一端设备（路由器/交换机）与相邻路由设备之间的全部（≥2 条）OSPF 邻接均已断开、且其环回地址
        不可达 → 设备宕机。接主机的末端链路没有 OSPF 邻居，宕机设备的旧 LSA 在老化前仍显示其为 up，不作判据。"""
        last = next((rr for rr in reversed(self.window) if rr.lsdb), None)     # 整轮缺测时取最近一次
        if last is None:
            return res
        l = self.topo.link(res["link"])
        for n in (l.a, l.b):
            if not self.topo.nodes[n].transit:
                continue
            links = [x.id for x in self.topo.links if n in (x.a, x.b)]
            adj = [x.id for x in self.topo.links if n in (x.a, x.b) and self.topo.nodes[x.other(n)].transit]
            lo = [t for t in self.enc.targets if t.node == n and t.kind == "lo"]
            lo_rr = next((rr for rr in reversed(self.window) if lo and lo[0].id in rr.pings), None)
            lo_down = not lo or (lo_rr is not None and lo_rr.pings[lo[0].id].recv == 0)
            if len(adj) >= 2 and lo_down and all(x in last.lsdb and not last.lsdb[x].adj_up for x in adj):
                ev = [f"设备 {n} 与相邻路由设备的全部 {len(adj)} 条 OSPF 邻接（{'、'.join(adj)}）均已断开"]
                if lo:
                    ev.append(f"设备环回地址 {lo[0].ip} 不可达")
                return dict(res, type="node_down", type_cn="设备宕机", link="/".join(links), node=n, links=links,
                            evidence=ev + res.get("evidence", []))
        return res

    # ------------------------------------------------------------------ 告警发出 / 恢复
    def _raise(self, res: Dict, st: Dict):
        if res["type"] == "node_down":
            return self._raise_node(res, st)
        if res["type"] == "loss" and not res.get("secondary"):     # 追加告警是并存的另一处故障，不算轮换
            self.loss_churn.append((self.round, res["link"]))
        self.alarm_seq += 1
        lid = res["link"]
        l = self.topo.link(lid)
        cur_cost = None
        if self.window and lid in self.window[-1].lsdb:
            cur_cost = self.window[-1].lsdb[lid].cost
        adv = advise(self.topo, res["type"], lid, cur_cost)
        sites = sorted({s for s in (self.topo.nodes[l.a].site, self.topo.nodes[l.b].site) if s})
        alarm = dict(alarm_id=f"{self.id_prefix}-{self.alarm_seq:04d}", event="raise",
                     severity=SEVERITY.get(res["type"], "minor"), category=CATEGORY[res["type"]],
                     fault_type=res["type"], fault_type_cn=res["type_cn"], link_id=lid,
                     endpoints=f"{l.a}:{l.a_if}({l.a_ip.split('/')[0]}) <-> {l.b}:{l.b_if}({l.b_ip.split('/')[0]})",
                     site="/".join(sites), confidence=res["confidence"],
                     evidence=dict(items=res.get("evidence", []), trigger=st.get("reason", []),
                                   candidates=res["candidates"], probes=res["probes"]),
                     repair=" | ".join(s["step"] for s in adv["steps"]), probes_used=len(res["probes"]),
                     infer_ms=res["infer_ms"], round_id=self.round, model=self.model_version)
        self._emit(alarm, adv, res)
        self.events.appendleft(dict(ts=time.time(), round=self.round, kind="alarm",
                                    msg=f"告警 {alarm['alarm_id']}：{lid} {res['type_cn']}（置信度 {res['confidence']:.2f}）"
                                        + ("，扣除已有告警后发现" if res.get("secondary") else "")))

    def _raise_node(self, res: Dict, st: Dict):
        self.alarm_seq += 1
        n = res["node"]
        nd = self.topo.nodes[n]
        adv = advise_node(self.topo, n)
        alarm = dict(alarm_id=f"{self.id_prefix}-{self.alarm_seq:04d}", event="raise",
                     severity=SEVERITY["node_down"], category=CATEGORY["node_down"],
                     fault_type="node_down", fault_type_cn="设备宕机", link_id=res["link"], node=n, links=res["links"],
                     endpoints=f"{n}（{nd.vendor} {nd.model}）".replace(" ）", "）"), site=nd.site,
                     confidence=res["confidence"],
                     evidence=dict(items=res.get("evidence", []), trigger=st.get("reason", []),
                                   candidates=res["candidates"], probes=res["probes"]),
                     repair=" | ".join(s["step"] for s in adv["steps"]), probes_used=len(res["probes"]),
                     infer_ms=res["infer_ms"], round_id=self.round, model=self.model_version)
        self._emit(alarm, adv, res)
        self.events.appendleft(dict(ts=time.time(), round=self.round, kind="alarm",
                                    msg=f"告警 {alarm['alarm_id']}：设备 {n} 宕机（{res['link']} 全部中断）"))

    def _emit(self, alarm: Dict, adv: Dict, res: Dict):
        """落盘、外发并登记为活动告警（持锁调用）。单告警模式由调用方先清除原告警。"""
        self.astore.write(alarm)
        for s in self.sinks:
            s.send(alarm)
        alarm = dict(alarm, advice=adv, ts=time.time(), first_round=self.round, last_round=self.round,
                     ground_truth=[f.label for f in self.backend.faults()] if self.backend.name in ("sim", "netns") else None)
        if self.multi:
            alarm.update(secondary=bool(res.get("secondary")), evident=bool(res.get("evident", True)))
            self.ref, self.rebase_due, self.sec_pending = None, self.round + self.rebase_wait, None
        self.alarms.append(alarm)
        self.history.appendleft(alarm)

    def _clear(self, why: str, replaced: bool = False, alarm: Optional[Dict] = None):
        """alarm 为空时清除全部活动告警，否则只清除该条（其余告警的残差基线待窗口刷新后重建）。"""
        with self.lock:
            for a in ([alarm] if alarm is not None else list(self.alarms)):
                if a not in self.alarms:
                    continue
                rec = {k: a[k] for k in ("alarm_id", "severity", "category", "fault_type", "fault_type_cn", "link_id",
                                         "endpoints", "site", "confidence", "model")}
                rec.update(event="clear", repair=why, round_id=self.round)
                self.astore.write(rec)
                for s in self.sinks:
                    s.send(rec)
                a["cleared_ts"] = time.time()
                a["cleared_why"] = why
                a["cleared_round"] = self.round
                self.alarms.remove(a)
                self._trk.pop(a["alarm_id"], None)
                self.events.appendleft(dict(ts=time.time(), round=self.round, kind="clear",
                                            msg=f"告警 {a['alarm_id']} 恢复：{why}"))
            if self.multi:
                self.sec_pending = None
                self.ref = None
                self.rebase_due = (self.round + self.rebase_wait) if self.alarms else None
                if not self.alarms:
                    self.rot_pending = None
                    self._ref_last = None
                if not any(x["fault_type"] == "node_down" for x in self.alarms):
                    self.node_pending = None

    # ------------------------------------------------------------------ 线程
    def run(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.step()
            except Exception:
                self.errors += 1
                traceback.print_exc()
            self._stop.wait(max(0.0, self.plan.interval_s - (time.time() - t0)))

    def start(self):
        self._thread = threading.Thread(target=self.run, daemon=True, name="edgefdl-engine")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self.mstore.close()
        self.astore.close()

    # ------------------------------------------------------------------ 快照
    def overhead(self) -> Dict:
        """测量带宽占用：最近若干轮注入字节（含精测）/ 轮数×周期，对比最窄链路与网管上联带宽。
        按轮次而非墙钟计时，仿真加速运行时同样成立。最窄链路口径假设全部测量流量都经过它，偏保守。"""
        with self.lock:
            hist = [tuple(x) for x in self.bytes_hist]
        if not hist:
            return {}
        span = (hist[-1][0] - hist[0][0] + 1) * self.plan.interval_s
        bps = sum(b for _, b in hist) * 8 / span
        min_bw = min(l.bw_mbps for l in self.topo.links) * 1e6
        up = [l for l in self.topo.links if self.topo.vantage in (l.a, l.b)]
        up_bw = (up[0].bw_mbps if up else min_bw / 1e6) * 1e6
        return dict(kbps=round(bps / 1e3, 1), pct_of_min_link=round(100 * bps / min_bw, 3),
                    pct_of_uplink=round(100 * bps / up_bw, 3))

    def snapshot(self) -> Dict:
        with self.lock:
            last = self.window[-1] if self.window else None
            al = self.active_list()
            return dict(
                round=self.round, uptime_s=round(time.time() - self.started), backend=self.backend.name,
                model=self.model_version, interval_s=self.plan.interval_s,
                detector=dict(active=self.det.active, reason=self.det.last_reason),
                active_alarm=al[0] if al else None, active_alarms=al, multi_alarm=self.multi,
                link_health=dict(self.link_health),
                link_state={lid: dict(adj_up=e.adj_up, cost=e.cost) for lid, e in (last.lsdb.items() if last else [])},
                unreachable=[tid for tid, p in (last.pings.items() if last else []) if p.recv == 0],
                ground_truth=[dict(f.to_dict(), label=f.label) for f in self.backend.faults()],
                infer=self.policy.latency_stats(), overhead=self.overhead(), resource=_proc_usage(),
                round_ms=round(float(np.mean(self.round_ms)), 1) if self.round_ms else None,
                errors=self.errors, suppressed=self.suppressed, alarms_total=self.alarm_seq,
                events=list(self.events)[:30])

    def alarm_history(self, n: int = 50) -> List[Dict]:
        with self.lock:
            return [dict(a) for a in list(self.history)[:n]]

    def target_series(self, link: str) -> Dict:
        with self.lock:
            out = {}
            for t in self.enc.targets:
                if t.link == link:
                    out[t.id] = [dict(round=r, rtt=(None if v is None or (isinstance(v, float) and math.isnan(v)) else round(v, 2)),
                                      loss=round(lo, 3)) for r, v, lo in self.series[t.id]]
            return out
