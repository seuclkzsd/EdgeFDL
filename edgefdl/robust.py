# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""鲁棒性测试（仿真后端）：在比训练与标准测试更苛刻的条件下考察定位准确率与无效告警。

  干扰强度  瞬时噪声概率提高到标准条件的 2~3 倍，干扰持续 2~3 轮（10~15 s）；
  测量缺陷  随机缺失部分目标或整轮的测量结果；路由器对 ICMP 限速、随机丢弃探测应答
            （分"基线学习时已存在"与"上线后才出现"两种）；
  故障幅度  低于训练区间下限的轻微故障、高于上限的严重故障；
  背景波动  现网背景抖动大于仿真（现场重学基线、模型不变），忙时全网时延缓慢抬升；
  多故障    两条链路同时故障、设备整机宕机（其全部链路同时中断）。
离线项按触发对齐采样生成测试集，按 Agent.evaluate 的端到端口径评估（单次研判）；
在线项驱动总控引擎（判决、按需精测、复核、告警、基线跟踪），按仿真时间运行。
"""
from __future__ import annotations

import glob
import json
import math
import multiprocessing as mp
import os
import random
import time
from collections import Counter
from typing import Dict, List, Optional

import numpy as np

from .faults.model import DRIFT_FACTORS, FAULT_TYPES, Fault
from .vnet.sim import SimBackend


class HarshSim(SimBackend):
    """在仿真后端之上叠加现网常见缺陷（ICMP 限速丢包 icmp_drop 由 SimBackend 自带）。

    hold         干扰固定持续轮数（1 即单轮尖峰）
    miss_target  每轮每个目标测量结果缺失的概率（采集超时）
    miss_round   整轮测量缺失的概率（采集进程重启、SNMP 超时）
    swing_ms     忙时全网每条链路单向时延的缓慢抬升幅度，period 轮内按 sin² 先升后降
    """

    def __init__(self, topo, plan=None, seed=0, hold=1, miss_target=0.0, miss_round=0.0,
                 swing_ms=0.0, period=17280, **kw):
        super().__init__(topo, plan, seed=seed, **kw)
        if hold > 1:
            self.hold_p = tuple([0.0] * (hold - 1) + [1.0])
        self.miss_target, self.miss_round = miss_target, miss_round
        self.swing_ms, self.period = swing_ms, period
        self._r = 0

    def link_arrays(self):
        delay, jit_mu, jit_sd, loss, bw, bw_eff = super().link_arrays()
        if self.swing_ms:
            delay = delay + self.swing_ms * math.sin(math.pi * (self._r % self.period) / self.period) ** 2
        return delay, jit_mu, jit_sd, loss, bw, bw_eff

    def measure_round(self, round_id, transient=None):
        self._r = round_id
        rr = super().measure_round(round_id, transient)
        if self.miss_round and self.rng.random() < self.miss_round:
            rr.pings, rr.bw, rr.lsdb = {}, {}, {}
            return rr
        if self.miss_target:
            for tid in list(rr.pings):
                if self.rng.random() < self.miss_target:
                    del rr.pings[tid]
        return rr


def jittered(topo_d: dict, sigma: float) -> dict:
    d = json.loads(json.dumps(topo_d))
    for l in d["links"]:
        l["jitter_ms"] = sigma
    return d


def scenario_fault(topo, lid: str, ft: str) -> Optional[Fault]:
    """与 bench 场景测试相同的取值；路由漂移取能改变监测路径的最大倍率。"""
    from .bench import SCENARIO_VALUE
    from .faults.generator import drift_allowed, drift_effective
    if ft != "route_drift":
        return Fault(ft, lid, SCENARIO_VALUE[ft])
    if not drift_allowed(topo, lid):
        return None
    cands = [Fault(ft, lid, f) for f in DRIFT_FACTORS[::-1] if drift_effective(topo, Fault(ft, lid, f))]
    return cands[0] if cands else None


def _relearn(topo_d, plan, seed, bk=None):
    """现场重学基线：bk 中的背景条件（如 ICMP 限速）在基线学习期已存在。"""
    from .faults.generator import learn_baseline
    from .topology import from_dict
    b = HarshSim(from_dict(topo_d), plan, seed=seed, **(bk or {}))
    b.up()
    return learn_baseline(b).to_dict()


def _site(topo, base, plan, c, seed):
    """按条件 c 得到现场拓扑与基线：sigma 改背景抖动，relearn 表示基线学习时缺陷已存在。"""
    topo_d, base_d = topo.to_dict(), base.to_dict()
    if c.get("sigma"):
        topo_d = jittered(topo_d, c["sigma"])
    if c.get("sigma") or c.get("relearn"):
        base_d = _relearn(topo_d, plan, seed + 7, c.get("bk") if c.get("relearn") else None)
    return topo_d, base_d


# ---------------------------------------------------------------------- 离线：样本生成 + 端到端评估
def _gen(job):
    topo_d, base_d, plan, seed, faults, n, bk, gk = job
    from .agent.features import Baseline
    from .faults.generator import SampleGenerator
    from .topology import from_dict
    topo = from_dict(topo_d)
    b = HarshSim(topo, plan, seed=seed, **bk)
    b.up()
    g = SampleGenerator(topo, b, Baseline(**base_d), plan, seed=seed, **dict(dict(rand=False), **gk))
    return g.generate(n, log_every=0, faults=faults)


def offline_jobs(topo, base, plan, c, n=0, faults=None, seed=11, chunk=400):
    topo_d, base_d = _site(topo, base, plan, c, seed)
    todo = list(faults) if faults is not None else [None] * n
    return [(topo_d, base_d, plan, seed * 1000 + k // chunk, todo[k:k + chunk] if faults is not None else None,
             len(todo[k:k + chunk]), c.get("bk", {}), c.get("gk", {})) for k in range(0, len(todo), chunk)]


def _dataset(parts, topo):
    from .agent.ddqn import Dataset
    from .faults.generator import fault_space
    lidx = {l: i for i, l in enumerate(topo.link_ids())}
    d = {k: np.concatenate([p[k] for p in parts]) for k in parts[0] if k != "manifest"}
    d["link_ids"] = np.array(topo.link_ids())
    d["drift_links"] = np.array(sorted({lidx[f.link] for f in fault_space(topo) if f.type == "route_drift"}), dtype=int)
    return Dataset(d)


def brief(m: Dict) -> Dict:
    c, o = m["per_category"], m["overall"]
    out = {k: round(c[k]["joint_acc"], 4) for k in ("链路中断", "性能劣化", "路由漂移") if k in c}
    out.update(误报率=round(o["false_alarm_rate"], 4) if o["false_alarm_rate"] is not None else None,
               漏报率=round(o["miss_rate"], 4) if o["miss_rate"] is not None else None,
               平均精测次数=round(o["avg_probes"], 3), n=o["n"])
    return out


SWEEP = {                                  # 训练区间：delay 5–100 ms，loss 5%–50%，jitter 5–50 ms，带宽剩余 5%–50%
    "delay": [1, 2, 3, 5, 100, 200, 500],
    "loss": [0.01, 0.02, 0.03, 0.05, 0.5, 0.7, 0.9],
    "jitter": [1, 2, 3, 5, 50, 100],
    "bandwidth": [0.9, 0.8, 0.7, 0.5, 0.05, 0.01],
}


def sweep_faults(topo, reps: int) -> List[Fault]:
    return [Fault(t, lid, v) for t, vals in SWEEP.items() for v in vals for lid in topo.link_ids() for _ in range(reps)]


def sweep_table(ag, ds) -> List[Dict]:
    import torch
    rows = []
    yt, yv = ds.y_type.numpy(), ds.y_value.numpy()
    for ti, t in enumerate(FAULT_TYPES):
        for v in SWEEP.get(t, []):
            m = (yt == ti) & np.isclose(yv, v)
            if not m.any():
                continue
            r = ag.evaluate(ds.subset(torch.as_tensor(m)))["per_type"][t]
            rows.append(dict(type=t, value=v, n=r["n"], detect_rate=round(r["detect_rate"], 4),
                             type_acc=round(r["type_acc"], 4), joint_acc=round(r["joint_acc"], 4)))
    return rows


# ---------------------------------------------------------------------- 在线：总控引擎
def _engine(model, c, seed, log_dir):
    import torch
    from .agent.features import Baseline, FeatureEncoder
    from .agent.infer import Policy
    from .cli import load_bundle
    from .core.orchestrator import Engine
    from .topology import from_dict
    torch.set_num_threads(1)
    ag, topo, base, plan, _ = load_bundle(model)
    topo_d, base_d = _site(topo, base, plan, c, seed)
    topo, base = from_dict(topo_d), Baseline(**base_d)
    b = HarshSim(topo, plan, seed=seed, **c.get("bk", {}))
    b.up()
    return Engine(topo, b, Policy(ag, FeatureEncoder(topo, base)), plan, log_dir, **c.get("ek", {})), topo


def _alarm_key(a: Dict):
    return ("node_down", a["node"]) if a["fault_type"] == "node_down" else (a["fault_type"], a["link_id"])


def _active(eng) -> Dict[tuple, str]:
    """当前活动告警 {(类型, 链路或设备): 告警号}。"""
    with eng.lock:
        return {_alarm_key(a): a["alarm_id"] for a in eng.alarms}


def _new_alarms(eng, seq: int) -> List[Dict]:
    """自告警序号 seq 之后新发出的全部告警（按发出顺序；告警历史为倒序，同一轮可能发出多条）。"""
    return list(eng.history)[:eng.alarm_seq - seq][::-1]


def multi_fault(eng, cases: List[Dict], after: int = 40, repair: int = 60, partial: bool = False) -> Dict:
    """cases: dict(faults, truth={(类型, 链路或设备)}, ok=可接受的中间告警（设备宕机升级前的链路中断）, links,
    groups=[(真值, [故障...]), ...] 可选，缺省每处故障一组)。
    记录 after 轮内发出的每条告警：first_ok 首条告警在真值内，no_wrong_alarm 全部告警在可接受集合内，
    all_reported 真值全部报出。partial=True 且恰有两组故障时，若 after 轮末两组都有活动告警，修复其中一组
    （按用例序号轮流），repair 轮内：被修复的告警须恢复且不再出现，另一条须一直保持（同一告警号），
    且不得新发任何告警，才算逐条恢复成功（partial_clear_ok 按全部用例计）。另统计故障期间每轮精测次数。"""
    from .bench import _reset
    rows = []
    for ci, c in enumerate(cases):
        _reset(eng)
        for _ in range(eng.plan.window):
            eng.step()
        for f in c["faults"]:
            eng.backend.inject(f)
        seen, t_first, seq, p0 = [], None, eng.alarm_seq, eng.deep_probes
        for k in range(1, after + 1):
            eng.step()
            for a in _new_alarms(eng, seq):
                seen.append(_alarm_key(a))
                t_first = t_first or k
            seq = eng.alarm_seq
        truth, ok = c["truth"], c["truth"] | c.get("ok", set())
        first_links = set(seen[0][1].split("/")) if seen and seen[0][0] != "node_down" else set()
        tl = {f.link: f.type for f in c["faults"]}
        row = dict(truth=sorted(truth), alarms=seen, t_first=t_first,
                   first_ok=bool(seen) and seen[0] in truth,
                   first_loc_ok=bool(seen) and (bool(first_links & c["links"]) or seen[0] in truth),
                   no_wrong_alarm=bool(seen) and set(seen) <= ok,
                   all_reported=truth <= set(seen),
                   n_wrong=sum(x not in ok for x in seen),
                   cross_wired=any(x not in ok and x[1] in tl and x[0] in {t for l, t in tl.items() if l != x[1]}
                                   for x in seen),
                   probes=eng.deep_probes - p0)
        groups = c.get("groups") or [((f.type, f.link), [f]) for f in c["faults"]]
        if partial and len(groups) == 2:
            act = _active(eng)
            row["both_active"] = truth <= set(act)
            if row["both_active"]:
                (kx, fx), (kk, _) = groups[ci % 2], groups[1 - ci % 2]
                for f in fx:
                    eng.backend.clear(f.link)
                t_clr, back, kept, extra, p1 = None, False, True, [], eng.deep_probes
                r0, s0 = eng.round, eng.alarm_seq
                for k in range(1, repair + 1):
                    eng.step()
                    extra += [_alarm_key(a) for a in _new_alarms(eng, seq)]
                    seq = eng.alarm_seq
                    cur = _active(eng)
                    if t_clr is None and kx not in cur:
                        t_clr = k
                    elif t_clr is not None and kx in cur:
                        back = True
                    kept = kept and cur.get(kk) == act[kk]
                gone = [(list(_alarm_key(a)), a.get("cleared_why"), a.get("cleared_round", 0) - r0)
                        for a in list(eng.history)[:eng.alarm_seq - s0 + 8] if a.get("cleared_round", 0) > r0]
                row.update(repaired=list(kx), t_partial_clear=t_clr, stayed_clear=not back, kept_other=kept,
                           repair_new_alarms=extra, repair_probes=eng.deep_probes - p1, repair_clears=gone[::-1],
                           partial_ok=t_clr is not None and not back and kept and not extra)
        rows.append(row)
    ts = [r["t_first"] for r in rows if r["t_first"]]
    agg = {k: float(np.mean([r[k] for r in rows])) for k in ("first_ok", "first_loc_ok", "no_wrong_alarm", "all_reported")}
    agg = dict(n=len(rows), alarmed=float(np.mean([bool(r["alarms"]) for r in rows])), **agg,
               mean_time_to_alarm_s=round(float(np.mean(ts)) * eng.plan.interval_s, 1) if ts else None,
               mean_alarms_per_case=float(np.mean([len(r["alarms"]) for r in rows])),
               extra_alarm_rate=float(np.mean([len(r["alarms"]) > len(r["truth"]) for r in rows])),
               wrong_per_case=float(np.mean([r["n_wrong"] for r in rows])),
               cross_wired=float(np.mean([r["cross_wired"] for r in rows])),
               probes_per_round=float(sum(r["probes"] for r in rows) / (after * len(rows))))
    if partial:
        both = [r for r in rows if r.get("both_active")]
        tc = [r["t_partial_clear"] for r in both if r.get("t_partial_clear")]
        agg.update(both_active=float(np.mean([bool(r.get("both_active")) for r in rows])),
                   partial_clear_ok=float(np.mean([bool(r.get("partial_ok")) for r in rows])),
                   partial_clear_ok_given_both=float(np.mean([r["partial_ok"] for r in both])) if both else None,
                   partial_kept_other=float(np.mean([r["kept_other"] for r in both])) if both else None,
                   mean_partial_clear_s=round(float(np.mean(tc)) * eng.plan.interval_s, 1) if tc else None,
                   repair_new_alarms=int(sum(len(r.get("repair_new_alarms", [])) for r in rows)),
                   repair_probes_per_round=float(sum(r["repair_probes"] for r in both) / (repair * len(both)))
                   if both else None)
    agg["extra_probes"] = getattr(eng, "extra_probes", 0)
    return dict(summary={k: (round(v, 4) if isinstance(v, float) else v) for k, v in agg.items()}, rows=rows)


def single_cases(topo) -> List[Dict]:
    """与场景测试相同的 84 个单故障，故障持续 40 轮：考察残差研判会不会追加多余告警、告警会不会中途恢复后重发。"""
    out = []
    for lid in topo.link_ids():
        for ft in FAULT_TYPES:
            f = scenario_fault(topo, lid, ft)
            if f:
                out.append(dict(faults=[f], truth={(ft, lid)}, links={lid}))
    return out


def dual_cases(topo, n: int, seed: int) -> List[Dict]:
    rng = random.Random(seed)
    out = []
    while len(out) < n:
        a, b = rng.sample(topo.link_ids(), 2)
        fa, fb = scenario_fault(topo, a, rng.choice(FAULT_TYPES)), scenario_fault(topo, b, rng.choice(FAULT_TYPES))
        if fa and fb:
            out.append(dict(faults=[fa, fb], truth={(fa.type, a), (fb.type, b)}, links={a, b}))
    return out


def node_cases(topo, reps: int) -> List[Dict]:
    """网管服务器唯一上联设备宕机时网管与全网失联（LSDB 也无从获取），不在本测试范围内。"""
    up = {l.other(topo.vantage) for l in topo.links if topo.vantage in (l.a, l.b)}
    out = []
    for n, v in topo.nodes.items():
        if not v.transit or up == {n}:
            continue
        links = {l.id for l in topo.links if n in (l.a, l.b)}
        for _ in range(reps):
            out.append(dict(faults=[Fault("link_down", l) for l in sorted(links)], truth={("node_down", n)},
                            ok={("link_down", l) for l in links}, links=links))
    return out


def node_dual_cases(topo, reps: int, seed: int) -> List[Dict]:
    """设备宕机 + 另一条链路（不属于该设备、非网管上联）同时故障。两处中断合起来恰好隔离另一台设备的组合
    与该设备宕机在观测上无法区分（见已知局限），不计入。"""
    from .faults.model import FAULT_TYPES as FT
    rng = random.Random(seed)
    up = {l.other(topo.vantage) for l in topo.links if topo.vantage in (l.a, l.b)}
    upl = {l.id for l in topo.links if topo.vantage in (l.a, l.b)}
    out = []
    for n, v in topo.nodes.items():
        if not v.transit or up == {n}:
            continue
        links = {l.id for l in topo.links if n in (l.a, l.b)}
        others = [l for l in topo.link_ids() if l not in links and l not in upl]
        k = 0
        while k < reps:
            f = scenario_fault(topo, rng.choice(others), rng.choice([t for t in FT if t != "route_drift"]))
            if f is None:
                continue
            down = links | ({f.link} if f.type == "link_down" else set())
            if f.type == "link_down" and any(
                    m != n and v2.transit and all(x.id in down for x in topo.links
                                                  if m in (x.a, x.b) and topo.nodes[x.other(m)].transit)
                    for m, v2 in topo.nodes.items()):
                continue
            nf = [Fault("link_down", l) for l in sorted(links)]
            out.append(dict(faults=nf + [f], truth={("node_down", n), (f.type, f.link)},
                            ok={("link_down", l) for l in links}, links=links | {f.link},
                            groups=[(("node_down", n), nf), ((f.type, f.link), [f])]))
            k += 1
    return out


def node_back(eng, after: int = 40, repair: int = 60) -> Dict:
    """设备宕机后设备恢复运行、只剩其一条链路中断：repair 轮内设备宕机告警须恢复，并改报该链路中断（最终活动告警
    恰为该链路中断，期间不新发其他告警）。逐台设备、逐条链路各一例。"""
    from .bench import _reset
    topo = eng.topo
    up = {l.other(topo.vantage) for l in topo.links if topo.vantage in (l.a, l.b)}
    rows = []
    for n, v in topo.nodes.items():
        if not v.transit or up == {n}:
            continue
        links = sorted(l.id for l in topo.links if n in (l.a, l.b))
        for keep in links:
            _reset(eng)
            for _ in range(eng.plan.window):
                eng.step()
            for l in links:
                eng.backend.inject(Fault("link_down", l))
            seq = eng.alarm_seq
            for _ in range(after):
                eng.step()
            before = [_alarm_key(a) for a in _new_alarms(eng, seq)]
            node_ok = ("node_down", n) in _active(eng)
            seq = eng.alarm_seq
            for l in links:
                if l != keep:
                    eng.backend.clear(l)
            new, t_ok = [], None
            for k in range(1, repair + 1):
                eng.step()
                new += [_alarm_key(a) for a in _new_alarms(eng, seq)]
                seq = eng.alarm_seq
                if t_ok is None and set(_active(eng)) == {("link_down", keep)}:
                    t_ok = k
            end = sorted(_active(eng))
            rows.append(dict(node=n, kept=keep, before=before, node_ok=node_ok, new=new, end=end, t_downgrade=t_ok,
                             ok=end == [("link_down", keep)] and set(new) <= {("link_down", keep)}))
    tt = [r["t_downgrade"] for r in rows if r["t_downgrade"]]
    agg = dict(n=len(rows), node_reported=float(np.mean([r["node_ok"] for r in rows])),
               downgrade_ok=float(np.mean([r["ok"] for r in rows])),
               stale_node=float(np.mean([("node_down", r["node"]) in r["end"] for r in rows])),
               mean_time_to_downgrade_s=round(float(np.mean(tt)) * eng.plan.interval_s, 1) if tt else None)
    return dict(summary={k: (round(v, 4) if isinstance(v, float) else v) for k, v in agg.items()}, rows=rows)


def _online(job):
    name, kind, model, c, seed, out, arg = job
    from .bench import bench_noise, bench_scenarios
    log_dir = os.path.join(out, "logs", f"{kind}_{name}")
    eng, topo = _engine(model, c, seed, log_dir)
    t0 = time.time()
    if kind == "long":
        r = bench_noise(eng, arg)
        res = {k: r[k] for k in ("rounds", "sim_hours", "detector_triggers", "filtered_by_agent", "false_alarms",
                                 "false_alarms_per_day", "false_alarm_exposure_pct", "engine_errors")}
        res["alarms"] = dict(Counter(f"{a['link_id']} {a['fault_type']}" for a in eng.history).most_common(8))
    elif kind == "scen":
        rows = bench_scenarios(eng, topo)["rows"]
        ta = [r["time_to_alarm_s"] for r in rows if r["time_to_alarm_s"] is not None]
        tc = [r["time_to_clear_s"] for r in rows if r["time_to_clear_s"] is not None]
        tct = {t: [r["time_to_clear_s"] for r in rows if r["type"] == t and r["time_to_clear_s"] is not None]
               for t in FAULT_TYPES}
        res = dict(n=len(rows), alarmed=round(float(np.mean([r["alarmed"] for r in rows])), 4),
                   joint_acc=round(float(np.mean([r["type_ok"] and r["loc_ok"] for r in rows])), 4),
                   wrong=[f"{r['link']} {r['type']} -> {r['pred']}" for r in rows if not (r["type_ok"] and r["loc_ok"])],
                   mean_time_to_alarm_s=round(float(np.mean(ta)), 1) if ta else None,
                   max_time_to_alarm_s=max(ta) if ta else None,
                   mean_time_to_clear_s=round(float(np.mean(tc)), 2) if tc else None,
                   time_to_clear_by_type={t: round(float(np.mean(v)), 2) for t, v in tct.items() if v},
                   not_cleared=sum(r["alarmed"] and r["time_to_clear_s"] is None for r in rows),
                   extra_alarms=int(sum(max(0, r["n_alarms"] - 1) for r in rows)),
                   rows=[[r["link"], r["type"], r["pred"], r["time_to_alarm_s"], r["time_to_clear_s"], r["n_alarms"]]
                         for r in rows])
    elif kind == "dual":
        res = multi_fault(eng, dual_cases(topo, arg, seed), partial=True)
    elif kind == "single":
        res = multi_fault(eng, single_cases(topo))
    elif kind == "node_dual":
        res = multi_fault(eng, node_dual_cases(topo, arg, seed), partial=True)
    elif kind == "node_back":
        res = node_back(eng)
    else:
        res = multi_fault(eng, node_cases(topo, arg))
    eng.stop()
    for f in glob.glob(os.path.join(log_dir, "measure_log.csv*")):
        os.remove(f)
    res["wall_s"] = round(time.time() - t0, 1)
    return name, kind, res


# ---------------------------------------------------------------------- 测试矩阵
BASE_NOISE = 0.15                          # 在线基准：与 bench 抗干扰长跑相同
NZ = dict(noise_prob=BASE_NOISE)


def C(name, bk=None, **kw):
    return dict(name=name, bk=dict(NZ, **(bk or {})), **kw)


def online_matrix(rounds: int, ek: Dict):
    sw = lambda a: C(f"忙时抬升{a:g}ms", dict(swing_ms=a, period=rounds), ek=ek)
    long = [C("基准（噪声15%）", ek=ek), C("噪声30%", dict(noise_prob=0.3), ek=ek),
            C("噪声50%", dict(noise_prob=0.5), ek=ek), C("干扰持续2轮", dict(hold=2), ek=ek),
            C("干扰持续3轮", dict(hold=3), ek=ek), C("目标缺测10%", dict(miss_target=0.1), ek=ek),
            C("整轮缺测5%", dict(miss_round=0.05), ek=ek),
            C("ICMP限速2%（基线已含）", dict(icmp_drop=0.02), relearn=True, ek=ek),
            C("ICMP限速5%（基线已含）", dict(icmp_drop=0.05), relearn=True, ek=ek),
            C("ICMP限速2%（上线后出现）", dict(icmp_drop=0.02), ek=ek),
            C("ICMP限速5%（上线后出现）", dict(icmp_drop=0.05), ek=ek),
            C("背景抖动0.5ms", sigma=0.5, ek=ek), C("背景抖动1ms", sigma=1.0, ek=ek),
            sw(0.25), sw(0.5), sw(1.0)]
    scen = [C("基准（噪声15%）", ek=ek), C("噪声50%", dict(noise_prob=0.5), ek=ek),
            C("干扰持续3轮", dict(hold=3), ek=ek), C("目标缺测10%", dict(miss_target=0.1), ek=ek),
            C("整轮缺测5%", dict(miss_round=0.05), ek=ek),
            C("ICMP限速5%（基线已含）", dict(icmp_drop=0.05), relearn=True, ek=ek),
            C("ICMP限速5%（上线后出现）", dict(icmp_drop=0.05), ek=ek),
            C("背景抖动1ms", sigma=1.0, ek=ek)]
    return [(c, "long") for c in long] + [(c, "scen") for c in scen] + \
        [(C("双链路同时故障", ek=ek), "dual"), (C("设备整机宕机", ek=ek), "node"), (C("单故障持续观察", ek=ek), "single"),
         (C("设备宕机+链路故障", ek=ek), "node_dual"), (C("设备恢复仍有链路中断", ek=ek), "node_back")]


def offline_matrix():
    P = lambda name, bk=None, **kw: dict(name=name, bk=bk or {}, **kw)
    return [P("标准条件"), P("域随机化（训练条件）", gk=dict(rand=True)),
            P("噪声50%", gk=dict(p_transient=0.5)), P("噪声75%", gk=dict(p_transient=0.75)),
            P("干扰持续2轮", dict(hold=2)), P("干扰持续3轮", dict(hold=3)),
            P("目标缺测5%", dict(miss_target=0.05)), P("目标缺测10%", dict(miss_target=0.1)),
            P("目标缺测20%", dict(miss_target=0.2)), P("整轮缺测5%", dict(miss_round=0.05)),
            P("整轮缺测10%", dict(miss_round=0.1)),
            P("ICMP限速2%（基线已含）", dict(icmp_drop=0.02), relearn=True),
            P("ICMP限速5%（基线已含）", dict(icmp_drop=0.05), relearn=True),
            P("ICMP限速1%（上线后出现）", dict(icmp_drop=0.01)), P("ICMP限速2%（上线后出现）", dict(icmp_drop=0.02)),
            P("ICMP限速5%（上线后出现）", dict(icmp_drop=0.05)),
            P("背景抖动0.2ms", sigma=0.2), P("背景抖动0.5ms", sigma=0.5), P("背景抖动1ms", sigma=1.0)]


def run_robust(model: str, out: str, workers: int = 32, n: int = 4000, sweep_reps: int = 20,
               rounds: int = 17280, dual_n: int = 300, node_reps: int = 10, node_dual_reps: int = 10,
               seed: Optional[int] = None,
               only: Optional[str] = None, ek: Optional[Dict] = None, kinds: Optional[List[str]] = None) -> Dict:
    """seed 缺省取环境变量 EDGEFDL_ROBUST_SEED（再缺省 21）；kinds 只跑指定的在线项
    （long/scen/dual/node/single/node_dual/node_back）。"""
    if seed is None:
        seed = int(os.environ.get("EDGEFDL_ROBUST_SEED", 21))
    import torch
    from .cli import load_bundle
    os.makedirs(out, exist_ok=True)
    ag, topo, base, plan, meta = load_bundle(model)
    torch.set_num_threads(8)
    t0 = time.time()
    ek = ek or {}
    res: Dict = {"config": dict(model=model, version=meta.get("version"), n=n, sweep_reps=sweep_reps, rounds=rounds,
                                dual_n=dual_n, node_reps=node_reps, node_dual_reps=node_dual_reps, seed=seed,
                                base_noise=BASE_NOISE, engine=ek)}
    with mp.get_context("spawn").Pool(workers) as pool:
        on = []
        if only in (None, "online"):
            arg = dict(long=rounds, scen=0, dual=dual_n, node=node_reps, single=0, node_dual=node_dual_reps, node_back=0)
            on = [pool.apply_async(_online, ((c["name"], kind, model, c, seed + i, out, arg[kind]),))
                  for i, (c, kind) in enumerate(online_matrix(rounds, ek)) if not kinds or kind in kinds]
        if only in (None, "offline"):
            off = {c["name"]: pool.map_async(_gen, offline_jobs(topo, base, plan, c, n=n, seed=seed + 100 + i))
                   for i, c in enumerate(offline_matrix())}
            sw = pool.map_async(_gen, offline_jobs(topo, base, plan, dict(gk=dict(p_normal=0.0)),
                                                   faults=sweep_faults(topo, sweep_reps), seed=seed + 300))
            res["offline"] = {}
            for name, r in off.items():
                res["offline"][name] = brief(ag.evaluate(_dataset(r.get(), topo)))
                print(f"  离线[{name}]", res["offline"][name], flush=True)
            res["sweep"] = sweep_table(ag, _dataset(sw.get(), topo))
            for r in res["sweep"]:
                print("  幅度", r, flush=True)
        res["online"] = {}
        for r in on:
            name, kind, v = r.get()
            res["online"].setdefault(kind, {})[name] = v
            print(f"  在线[{kind}][{name}]", v.get("summary", {k: v[k] for k in v if k != "wrong"}), flush=True)
    res["wall_s"] = round(time.time() - t0, 1)
    with open(os.path.join(out, "robust.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    return res
