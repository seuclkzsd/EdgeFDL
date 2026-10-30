# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""全量故障样本生成：在等价虚拟网络上按"链路 × 故障类型 × 取值"枚举注入，
记录故障发生后的测量窗口与逐链路精测结果，编码后存为训练集。

每个样本 = 一个研判窗口（W 轮轻量测量，故障在窗口内第 onset 轮发生）+ 全部链路的精测结果；
智能体在训练时按需"揭示"精测结果，从而学会在带宽开销与定位精度之间权衡。
窗口中随机叠加瞬时噪声，正常样本中也有，以训练抗干扰能力。

域随机化（仅训练集）：干扰持续 1~3 轮，部分样本叠加设备 ICMP 限速造成的背景丢包
（一半按"基线学习期已存在"计入基线），使智能体对现网中比标准条件更复杂的干扰不敏感；
部分样本取自背景抖动更大的网络（各自重学基线，与现场部署时重学基线一致）。
独立测试集按标准条件生成（plain）。
"""
from __future__ import annotations

import csv
import os
import random
import time
from collections import deque
from typing import Dict, List, Optional

import numpy as np

from ..agent.features import Baseline, FeatureEncoder
from ..measure.model import MeasurePlan
from ..topology import Topology
from .model import FAULT_TYPES, Fault, enumerate_faults, sample_value

TYPE_INDEX = {t: i for i, t in enumerate(FAULT_TYPES)}
NORMAL = len(FAULT_TYPES)          # 标签中的"正常"类
RAND_HOLD = (0.6, 0.25, 0.15)      # 域随机化：干扰持续 1/2/3 轮的概率
RAND_ICMP = (0.25, 0.005, 0.03)    # 域随机化：25% 的样本叠加 0.5%~3% 的 ICMP 限速丢包，其中一半计入基线
RAND_JITTER = ((0.55, None), (0.15, 0.3), (0.15, 0.6), (0.15, 1.0))   # 域随机化：链路背景抖动 ms（None 为原值）


def drift_allowed(topo: Topology, lid: str) -> bool:
    l = topo.link(lid)
    return topo.nodes[l.a].transit and topo.nodes[l.b].transit


def drift_effective(topo: Topology, f: Fault) -> bool:
    """路由漂移须真的改变至少一条被监测路径，否则只是开销变化、不构成漂移。"""
    if f.type != "route_drift":
        return True
    cost = {f.link: max(1, round(topo.link(f.link).cost * f.value))}
    for t in topo.targets():
        if topo.route_to_target(topo.vantage, t)[0] != topo.route_to_target(topo.vantage, t, cost=cost)[0]:
            return True
    return False


def fault_space(topo: Topology, per_type: int = 3, seed: int = 0) -> List[Fault]:
    out = []
    for f in enumerate_faults(topo.link_ids(), per_type, random.Random(seed)):
        if f.type == "route_drift" and not (drift_allowed(topo, f.link) and drift_effective(topo, f)):
            continue
        out.append(f)
    return out


def learn_baseline(backend, rounds: int = 40, deep_reps: int = 3) -> Baseline:
    backend.clear()
    rr = [backend.measure_round(i, transient=[]) if backend.name == "sim" else backend.measure_round(i)
          for i in range(rounds)]
    deeps = [{lid: backend.deep_probe(lid) for lid in backend.topo.link_ids()} for _ in range(deep_reps)]
    return Baseline.fit(backend.topo, rr, deeps)


class SampleGenerator:
    """触发对齐采样：先跑满一个正常窗口，再注入故障并持续测量，直到去噪判决器触发，
    取触发时刻的窗口作为样本——与线上"判决器唤醒智能体"的时刻完全一致。"""

    def __init__(self, topo: Topology, backend, base: Baseline, plan: Optional[MeasurePlan] = None,
                 seed: int = 0, p_normal: float = 0.15, p_transient: float = 0.25, max_wait: int = 24,
                 settle: int = 2, rand: bool = True):
        from ..core.denoise import Detector
        self.topo, self.backend, self.base = topo, backend, base
        self.plan = plan or backend.plan
        self.enc = FeatureEncoder(topo, base)
        self.det = Detector(self.enc, window=self.plan.window)
        self.rng = random.Random(seed)
        self.p_normal = p_normal
        self.p_transient = p_transient
        self.max_wait = max_wait
        self.settle = settle                 # 触发后再观察几轮才研判（与在线引擎一致）
        self.rand = rand and backend.name == "sim"
        self.loss0 = base.loss.copy()
        self.levels = [(None, self.enc, self.det, self.loss0)]
        if self.rand:
            backend.hold_p = RAND_HOLD
            self.jit0 = {l.id: l.jitter_ms for l in topo.links}
            for _, sigma in RAND_JITTER[1:]:              # 背景抖动更大的网络：各自重学基线
                self._set_jitter(sigma)
                enc = FeatureEncoder(topo, learn_baseline(backend))
                self.levels.append((sigma, enc, Detector(enc, window=self.plan.window), enc.base.loss.copy()))
            self._set_jitter(None)
        self.space = fault_space(topo, seed=seed)
        self.by_type: Dict[str, List[Fault]] = {}
        for f in self.space:
            self.by_type.setdefault(f.type, []).append(f)
        self.drift_links = sorted({f.link for f in self.by_type.get("route_drift", [])})

    def _set_jitter(self, sigma: Optional[float]):
        for l in self.topo.links:
            l.jitter_ms = self.jit0[l.id] if sigma is None else max(self.jit0[l.id], sigma)

    def draw_fault(self) -> Optional[Fault]:
        if self.rng.random() < self.p_normal:
            return None
        t = self.rng.choice(list(self.by_type))
        if t in ("delay", "loss", "jitter", "bandwidth"):
            return Fault(t, self.rng.choice(self.topo.link_ids()), sample_value(t, self.rng))
        return self.rng.choice(self.by_type[t])

    def one(self, fault: Optional[Fault], round0: int = 0, raw_writer=None, sample_id: int = 0):
        """返回 (light, glob, deep, info)；info 含 noisy/detected/det_rounds。"""
        W = self.plan.window
        b, det = self.backend, self.det
        hist: deque = deque(maxlen=W)
        noise: deque = deque(maxlen=W)
        r = [round0]

        def tick():
            if b.name == "sim":
                tr = b.random_transient(self.p_transient)
                rr = b.measure_round(r[0], transient=tr)
            else:
                tr, rr = [], b.measure_round(r[0])
            r[0] += 1
            hist.append(rr)
            noise.append(bool(tr))
            if raw_writer is not None:
                raw_writer.write_round(rr, sample_id=sample_id, fault=fault)
            return det.update(rr)

        if self.rand:
            k = self.rng.choices(range(len(RAND_JITTER)), weights=[w for w, _ in RAND_JITTER])[0]
            sigma, self.enc, self.det, loss0 = self.levels[k]
            self._set_jitter(sigma)
            det = self.det
            p, lo, hi = RAND_ICMP
            b.icmp_drop = self.rng.uniform(lo, hi) if self.rng.random() < p else 0.0
            known = b.icmp_drop and self.rng.random() < 0.5
            self.enc.base.loss = loss0 + (np.where(b.router_tgt, b.icmp_drop, 0.0) if known else 0.0)
            self.enc._mlost.clear()                  # 判决器与特征共用编码器，基线丢包率同步生效
        for _attempt in range(5):                     # 预热窗口被噪声触发时重来
            b.clear()
            det.reset()
            for _ in range(W + self.rng.randint(0, 2)):
                tick()
            if not det.active:
                break
        detected, waited = False, 0
        busy = det.active                     # 预热重试后判决器仍处于活动（持续干扰、限速丢包）
        if fault:
            b.inject(fault)
        if busy:
            # 与在线引擎一致：判决器活动期间不再有触发沿，按复核周期（6 轮）定期研判，故障已持续 3~6 轮
            waited = self.rng.randint(self.settle + 1, 6) - self.settle
            for _ in range(waited):
                tick()
            detected = True
        else:
            for waited in range(1, self.max_wait + 1):
                if tick()["trigger"]:
                    detected = True
                    break
        if detected:
            for _ in range(self.settle):
                tick()
            detected = det.active             # 观察期内已平息的视为瞬时扰动，不唤醒智能体
        deep = np.stack([self.enc.deep(b.deep_probe(lid)) for lid in self.topo.link_ids()])
        light, glob = self.enc.light(list(hist))
        b.clear()
        return light, glob, deep, dict(noisy=any(noise), detected=detected, det_rounds=waited + self.settle)

    def generate(self, n: int, out: Optional[str] = None, raw_csv: Optional[str] = None, log_every: int = 2000,
                 faults: Optional[List[Optional[Fault]]] = None, round0: int = 0, raw=None) -> dict:
        """生成 n 个样本；out 给定时写 npz 与样本清单 CSV（可追溯）。faults 给定时按清单逐个生成。
        raw：已打开的 CsvStore（分块生成时多块共用一份逐轮原始测量日志，由调用方关闭）。"""
        from ..measure.store import CsvStore
        own = raw is None and bool(raw_csv)
        raw = CsvStore(raw_csv, self.topo) if own else raw
        Ls, Gs, Ds, yt, yl, yv, nz, dt, dr, man = [], [], [], [], [], [], [], [], [], []
        todo = faults if faults is not None else [self.draw_fault() for _ in range(n)]
        t0 = time.time()
        lidx = {l: i for i, l in enumerate(self.topo.link_ids())}
        span = self.plan.window + 2 + self.max_wait + self.settle
        for i, f in enumerate(todo):
            sid = round0 + i
            light, glob, deep, info = self.one(f, round0=sid * span, raw_writer=raw, sample_id=sid)
            Ls.append(light); Gs.append(glob); Ds.append(deep)
            yt.append(TYPE_INDEX[f.type] if f else NORMAL)
            yl.append(lidx[f.link] if f else -1)
            yv.append(f.value if f else 0.0)
            nz.append(info["noisy"]); dt.append(info["detected"]); dr.append(info["det_rounds"])
            man.append([sid, f.type if f else "normal", f.link if f else "", f.value if f else "",
                        int(info["noisy"]), int(info["detected"]), info["det_rounds"]])
            if log_every and (i + 1) % log_every == 0:
                print(f"  已生成 {i + 1}/{len(todo)} 个样本  {time.time() - t0:.1f}s", flush=True)
        if own:
            raw.close()
        data = dict(light=np.stack(Ls), glob=np.stack(Gs), deep=np.stack(Ds),
                    y_type=np.array(yt), y_link=np.array(yl), y_value=np.array(yv, dtype=np.float32),
                    noisy=np.array(nz), detected=np.array(dt), det_rounds=np.array(dr),
                    manifest=np.array(man, dtype=object))
        if out:
            save_dataset(self.topo, self.enc, self.drift_links, data, out)
        return data


MANIFEST_HEADER = ["sample_id", "fault_type", "fault_link", "fault_value", "transient_noise",
                   "detected", "detect_rounds"]


def save_dataset(topo: Topology, enc: FeatureEncoder, drift_links, data: dict, out: str):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    lidx = {l: i for i, l in enumerate(topo.link_ids())}
    with open(os.path.splitext(out)[0] + "_samples.csv", "w", newline="", encoding="utf-8") as mf:
        mw = csv.writer(mf)
        mw.writerow(MANIFEST_HEADER)
        mw.writerows(data["manifest"].tolist())
    arrays = {k: v for k, v in data.items() if k != "manifest"}
    np.savez_compressed(out, **arrays, link_ids=np.array(topo.link_ids()), is_host=enc.is_host,
                        drift_links=np.array([lidx[l] for l in drift_links], dtype=int))


CHUNK = 2000


def chunks(n: int, seed: int, chunk: int = CHUNK):
    """分块与各块种子：第 k 块（chunk 个样本，样本号从 k×chunk 起）用种子 seed×100003+k。
    并行与顺序生成都按此分块，同一种子得到逐样本相同的数据集，与进程数无关。"""
    return [(seed * 100003 + k, min(chunk, n - k * chunk), k * chunk) for k in range((n + chunk - 1) // chunk)]


def _worker(args, raw=None):
    topo_dict, base_dict, plan, seed, n, round0, rand = args
    from ..topology import from_dict
    from ..vnet.sim import SimBackend
    topo = from_dict(topo_dict)
    b = SimBackend(topo, plan, seed=seed)
    b.up()
    g = SampleGenerator(topo, b, Baseline(**base_dict), plan, seed=seed, rand=rand)
    return g.generate(n, log_every=0, round0=round0, raw=raw)


def _merge_save(topo: Topology, base: Baseline, parts: List[dict], out: Optional[str]) -> dict:
    data = {key: np.concatenate([p[key] for p in parts]) for key in parts[0]}
    if out:
        enc = FeatureEncoder(topo, base)
        drift = sorted({f.link for f in fault_space(topo) if f.type == "route_drift"})
        save_dataset(topo, enc, drift, data, out)
    return data


def generate_parallel(topo: Topology, base: Baseline, n: int, out: str, plan: Optional[MeasurePlan] = None,
                      seed: int = 0, workers: int = 8, chunk: int = CHUNK, rand: bool = True,
                      raw_csv: Optional[str] = None) -> dict:
    """仿真后端分块生成：每块一套独立的仿真网络（种子见 chunks），workers>1 时多进程并行；
    workers≤1 或要输出逐轮原始测量 CSV（raw_csv）时在本进程按块顺序生成，数据集与并行时相同。"""
    import multiprocessing as mp
    plan = plan or MeasurePlan()
    jobs = [(topo.to_dict(), base.to_dict(), plan, s, m, r0, rand) for s, m, r0 in chunks(n, seed, chunk)]
    if workers > 1 and not raw_csv:
        with mp.get_context("fork").Pool(min(workers, len(jobs))) as pool:
            parts = pool.map(_worker, jobs)
    else:
        from ..measure.store import CsvStore
        raw = CsvStore(raw_csv, topo) if raw_csv else None
        try:
            parts = []
            for j in jobs:
                parts.append(_worker(j, raw))
                print(f"  已生成 {j[5] + j[4]}/{n} 个样本", flush=True)
        finally:
            if raw:
                raw.close()
    return _merge_save(topo, base, parts, out)


def generate_on(backend, topo: Topology, base: Baseline, n: int, out: Optional[str], seed: int = 0,
                chunk: int = CHUNK, rand: bool = True, raw_csv: Optional[str] = None) -> dict:
    """在一套已启动的虚拟网络（netns / LXD）上按同样的分块与种子顺序生成。"""
    from ..measure.store import CsvStore
    raw = CsvStore(raw_csv, topo) if raw_csv else None
    try:
        parts = [SampleGenerator(topo, backend, base, seed=s, rand=rand).generate(m, round0=r0, raw=raw)
                 for s, m, r0 in chunks(n, seed, chunk)]
    finally:
        if raw:
            raw.close()
    return _merge_save(topo, base, parts, out)
