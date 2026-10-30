# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""指标自测：

1. 推理时延：单轮状态推理（一次前向）与一次完整研判（含特征计算）的 mean/p99/max，torch 与 numpy 两种引擎；
2. 抗干扰长跑：正常网络叠加瞬时噪声连续运行 N 轮（默认 17280 轮 = 24h@5s），统计无效告警、引擎异常；
3. 资源占用：每轮计算耗时 / 测量周期 → CPU 占用率；常驻内存；测量带宽占用（含精测）；
4. 场景测试：逐链路注入链路中断、时延(RTT 异常)、丢包、抖动、带宽劣化、路由漂移，
   统计告警是否正确及"故障发生→告警"时间、"故障消除→告警恢复"时间。
仿真后端下按"仿真时间"运行（不 sleep），真实后端可用 serve 做 72h 实时压测。
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import time
from typing import Dict, List

import numpy as np

from .faults.model import CATEGORY, DRIFT_FACTORS, FAULT_TYPES, TYPE_LABEL, Fault

def _rss_mb() -> float:
    try:
        import psutil
        return psutil.Process().memory_info().rss / 2 ** 20
    except ImportError:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


SCENARIO_VALUE = {"link_down": 1.0, "delay": 20.0, "loss": 0.15, "jitter": 15.0, "bandwidth": 0.2}


def _engine(model, log_dir, noise, seed, confirm=2):
    from .cli import build_engine
    ns = argparse.Namespace(seed=seed, noise=noise, engine="torch", sinks=None, confirm=confirm)
    eng, topo, backend = build_engine(model, "sim", log_dir, ns)
    return eng, topo, backend


def _reset(eng):
    eng.backend.clear()
    eng.det.reset()
    eng.window.clear()
    eng.pending = None
    eng.diag_due = None
    if eng.active:
        eng._clear("场景切换")


def bench_latency(eng, n: int = 2000) -> Dict:
    from .agent.infer import Policy
    out = {}
    for _ in range(eng.plan.window):
        eng.step()
    window = list(eng.window)
    for engine in ("torch", "numpy"):
        pol = Policy(eng.policy.agent, eng.enc, engine=engine)
        fwd, total = [], []
        for _ in range(n):
            t0 = time.perf_counter()
            r = pol.diagnose(window, probe_fn=eng.backend.deep_probe)
            total.append((time.perf_counter() - t0) * 1000)
            fwd.append(r["infer_ms"])
        f, t = np.array(fwd), np.array(total)
        out[engine] = dict(forward_mean_ms=round(float(f.mean()), 3), forward_p99_ms=round(float(np.percentile(f, 99)), 3),
                           forward_max_ms=round(float(f.max()), 3), diagnose_mean_ms=round(float(t.mean()), 3),
                           diagnose_p99_ms=round(float(np.percentile(t, 99)), 3))
    out["params"] = int(sum(p.numel() for p in eng.policy.agent.net.parameters()))
    return out


def bench_noise(eng, rounds: int) -> Dict:
    _reset(eng)
    a0, s0 = eng.alarm_seq, eng.suppressed
    trig = held = 0
    cpu0 = time.process_time()
    t0 = time.perf_counter()
    for _ in range(rounds):
        was = eng.det.active
        eng.step()
        trig += int(eng.det.active and not was)
        held += int(eng.active is not None)          # 无效告警的持续时长：一条告警挂一整天只算 1 次，但暴露率为 100%
    wall = time.perf_counter() - t0
    cpu = time.process_time() - cpu0
    sim_hours = rounds * eng.plan.interval_s / 3600
    return dict(rounds=rounds, sim_hours=round(sim_hours, 2), noise_prob=eng.backend.noise_prob,
                detector_triggers=trig, filtered_by_agent=eng.suppressed - s0,
                false_alarms=eng.alarm_seq - a0, false_alarms_per_day=round((eng.alarm_seq - a0) / sim_hours * 24, 3),
                false_alarm_exposure_pct=round(100 * held / rounds, 3),
                engine_errors=eng.errors, wall_s=round(wall, 1),
                compute_ms_per_round=round(cpu / rounds * 1000, 3),
                cpu_pct_of_one_core=round(100 * cpu / rounds / eng.plan.interval_s, 4),
                rss_mb=round(_rss_mb(), 1),
                overhead=eng.overhead())


def bench_scenarios(eng, topo, max_rounds: int = 60) -> Dict:
    from .faults.generator import drift_allowed, drift_effective
    rows: List[Dict] = []
    W = eng.plan.window
    for lid in topo.link_ids():
        for ft in FAULT_TYPES:
            if ft == "route_drift":
                if not drift_allowed(topo, lid):
                    continue
                cands = [Fault(ft, lid, f) for f in DRIFT_FACTORS[::-1] if drift_effective(topo, Fault(ft, lid, f))]
                if not cands:
                    continue
                f = cands[0]
            else:
                f = Fault(ft, lid, SCENARIO_VALUE[ft])
            _reset(eng)
            for _ in range(W):
                eng.step()
            pre_alarms = eng.alarm_seq
            eng.backend.inject(f)
            t_alarm, alarm = None, None
            for k in range(1, max_rounds + 1):
                eng.step()
                if eng.alarm_seq > pre_alarms:
                    t_alarm, alarm = k, eng.active
                    break
            eng.backend.clear()
            t_clear = None
            if alarm:
                for k in range(1, max_rounds + 1):
                    eng.step()
                    if eng.active is None:
                        t_clear = k
                        break
            ok_t = bool(alarm and alarm["fault_type"] == ft)
            ok_l = bool(alarm and alarm["link_id"] == lid)
            rows.append(dict(link=lid, type=ft, value=f.value, alarmed=bool(alarm), type_ok=ok_t, loc_ok=ok_l,
                             pred=(f"{alarm['link_id']} {alarm['fault_type']}" if alarm else "无告警"),
                             time_to_alarm_s=None if t_alarm is None else t_alarm * eng.plan.interval_s,
                             time_to_clear_s=None if t_clear is None else t_clear * eng.plan.interval_s,
                             probes=alarm["probes_used"] if alarm else 0,
                             n_alarms=eng.alarm_seq - pre_alarms))       # 含故障消除后、恢复前新发的告警
    agg = {}
    for key, sel in [(TYPE_LABEL[t], lambda r, t=t: r["type"] == t) for t in FAULT_TYPES] + \
                    [(c, lambda r, c=c: CATEGORY[r["type"]] == c) for c in ("链路中断", "性能劣化", "路由漂移")]:
        rs = [r for r in rows if sel(r)]
        if not rs:
            continue
        ta = [r["time_to_alarm_s"] for r in rs if r["time_to_alarm_s"] is not None]
        agg[key] = dict(n=len(rs), type_acc=round(float(np.mean([r["type_ok"] for r in rs])), 4),
                        joint_acc=round(float(np.mean([r["type_ok"] and r["loc_ok"] for r in rs])), 4),
                        mean_time_to_alarm_s=round(float(np.mean(ta)), 1) if ta else None,
                        max_time_to_alarm_s=max(ta) if ta else None)
    return dict(rows=rows, summary=agg)


def run_bench(model: str, out: str, rounds: int = 17280, scenarios: bool = True, seed: int = 7,
              noise: float = 0.15) -> Dict:
    os.makedirs(out, exist_ok=True)
    eng, topo, backend = _engine(model, os.path.join(out, "logs"), noise, seed)
    res = {"latency": bench_latency(eng)}
    print("  推理时延:", res["latency"], flush=True)
    res["noise"] = bench_noise(eng, rounds)
    print("  抗干扰长跑:", res["noise"], flush=True)
    if scenarios:
        res["scenarios"] = bench_scenarios(eng, topo)
        print("  场景测试:", res["scenarios"]["summary"], flush=True)
    eng.stop()
    lat = res["latency"]["torch"]
    nz = res["noise"]
    res["summary"] = {
        "单轮推理时延 p99 (ms, 目标≤100)": lat["forward_p99_ms"],
        "完整研判时延 p99 (ms, 含特征与按需精测调用)": lat["diagnose_p99_ms"],
        "模型参数量": res["latency"]["params"],
        f"抗干扰长跑 {nz['sim_hours']}h 无效告警数": nz["false_alarms"],
        "判决器触发后被智能体过滤次数": nz["filtered_by_agent"],
        "引擎异常次数": nz["engine_errors"],
        "测量带宽占用 % (最窄链路, 目标<5%)": nz["overhead"].get("pct_of_min_link"),
        "测量带宽占用 % (网管上联)": nz["overhead"].get("pct_of_uplink"),
        "单核 CPU 占用 % (计算部分)": nz["cpu_pct_of_one_core"],
        "常驻内存 MB": nz["rss_mb"],
    }
    if scenarios:
        for k, v in res["scenarios"]["summary"].items():
            res["summary"][f"场景[{k}] 类型/类型+定位准确率, 平均发现时延 s"] = \
                f"{v['type_acc']:.2%} / {v['joint_acc']:.2%}, {v['mean_time_to_alarm_s']}"
    with open(os.path.join(out, "bench.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    return res
