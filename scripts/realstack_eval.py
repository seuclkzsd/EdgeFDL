#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""真实协议栈在线准确率测试：与 serve 相同的在线引擎 + 模型包，跑在 netns 后端（真实内核协议栈、FRR OSPF、tc）上。

与 edgefdl/bench.py 的场景测试相同的 84 个单故障（15 条链路 × 5 类 + 9 条路由器互联链路的路由漂移），外加设备宕机、
双故障与无故障时段。按墙钟运行：每 5 s 一轮，故障在轮间随机时刻注入，时延按墙钟计。

用法（宿主机，需 Docker 与带 PyTorch 的实验镜像，见 scripts/realstack_run.sh）：
  scripts/realstack_run.sh                             # 分片并行全量运行
  scripts/lab.sh run python3 scripts/realstack_eval.py run --sets single --only L4:delay --out runs/lab/try
  python3 scripts/realstack_eval.py merge runs/realstack/* --out results/realstack.json --raw results/realstack

run 的产物：<out>/result.json（每完成一个场景就覆盖写入，中途退出不丢已完成的场景）、<out>/logs/measure_log.csv、
<out>/logs/alarm_log.csv。逐场景记录里的 round_inject / round_clear 对应日志的 round_id，可据此回放。
"""
from __future__ import annotations

import argparse
import glob
import gzip
import hashlib
import json
import os
import platform
import random
import resource
import statistics
import subprocess
import sys
import time
import traceback
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from edgefdl.faults.injector import qdisc_cmds  # noqa: E402
from edgefdl.faults.model import CATEGORY, FAULT_TYPES, TYPE_LABEL, Fault  # noqa: E402
from edgefdl.robust import dual_cases, node_cases, scenario_fault  # noqa: E402

SETS = ("normal", "single", "node", "dual")
TYPE_EN = {"link_down": "Link down", "delay": "Delay", "loss": "Loss", "jitter": "Jitter",
           "bandwidth": "Bandwidth", "route_drift": "Route drift"}
CATEGORY_TARGET = {"链路中断": ("Link down", 0.98), "性能劣化": ("Performance degradation", 0.95),
                   "路由漂移": ("Route drift", 0.95)}      # 设计目标，与 docs/results.md 相同


def single_faults(topo) -> List[Fault]:
    """与 bench_scenarios 相同的场景、相同的顺序。"""
    return [f for f in (scenario_fault(topo, lid, ft) for lid in topo.link_ids() for ft in FAULT_TYPES) if f]


def shard_of(items: Sequence, shard: int, shards: int) -> List:
    """轮转分片：相邻场景（同一链路的不同故障类型）落到不同分片，每片都是各链路、各类型的混合。"""
    return list(items[shard::shards])


def parse_only(text: str) -> List[tuple]:
    """'L4:delay,L9:loss' → [('L4', 'delay'), ('L9', 'loss')]"""
    return [tuple(x.split(":", 1)) for x in text.split(",") if x]


# ---------------------------------------------------------------------- 按墙钟运行
class Pacer:
    """每 interval 秒一轮，语义同 Engine.run：一轮超时则下一轮立即开始，异常只计数不中断。

    Engine.step 被实例属性覆盖，所以本脚本和复用的 robust.multi_fault 都按墙钟跑。"""

    def __init__(self, eng):
        self.eng, self.step_raw = eng, eng.step
        self.interval = eng.plan.interval_s
        self.next_t = time.time()
        self.durations: List[float] = []
        self.probe_bytes = 0
        self.path_unknown = 0            # 全部目标累计：可达而逐跳路径缺失的次数（traceroute 丢跳）
        self.target_rounds = 0
        eng.step = self.step

    def remaining(self) -> float:
        return max(0.0, self.next_t - time.time())

    def align(self, phase: float) -> None:
        """下一轮对齐到整 interval + phase：各分片的测量错开，traceroute 不扎堆（见 realstack_run.sh）。"""
        self.next_t = (int(time.time() / self.interval) + 1) * self.interval + phase

    def step(self) -> None:
        d = self.next_t - time.time()
        if d > 0:
            time.sleep(d)
        t0 = time.time()
        try:
            self.step_raw()
        except Exception:                # noqa: BLE001
            self.eng.errors += 1
            traceback.print_exc()
        self.durations.append(time.time() - t0)
        self.next_t = t0 + self.interval
        if self.eng.window:
            rr = self.eng.window[-1]
            self.probe_bytes += rr.probe_bytes
            self.path_unknown += sum(1 for k, p in rr.pings.items()
                                     if p.recv and not p.path and not k.startswith("nqa."))
            self.target_rounds += len(rr.pings)


class Transients:
    """真实链路上的瞬时干扰，与仿真后端 Transient 同分布：每轮以 prob 概率在一条无故障链路上叠加一轮时延尖峰
    （10~150 ms）或突发丢包（20%~80%），只覆盖该轮的测量。"""

    def __init__(self, backend, prob: float, seed: int):
        self.b, self.prob = backend, prob
        self.rng = random.Random(seed)
        self.raw = backend.measure_round
        self.count = 0
        backend.measure_round = self.measure

    def measure(self, round_id: int):
        lids = [l for l in self.b.topo.link_ids() if l not in self.b.active]
        if not lids or self.rng.random() >= self.prob:
            return self.raw(round_id)
        lid = self.rng.choice(lids)
        f = Fault("delay", lid, self.rng.uniform(10, 150)) if self.rng.random() < 0.5 \
            else Fault("loss", lid, self.rng.uniform(0.2, 0.8))
        self.b._run_cmds(qdisc_cmds(self.b.topo, lid, f))
        self.count += 1
        try:
            return self.raw(round_id)
        finally:
            self.b._run_cmds(qdisc_cmds(self.b.topo, lid, None))


def settle(eng, stats: Dict, timeout: float = 90.0) -> Optional[float]:
    """等 OSPF 与数据面稳定；超时说明上一步留下了异常状态，重建整张网络后继续，次数记入结果。"""
    b = eng.backend
    try:
        return round(b.wait_stable(timeout), 2)
    except TimeoutError as e:
        print(f"!! {e}；重建虚拟网络", flush=True)
        stats["rebuilds"] = stats.get("rebuilds", 0) + 1
        b.down()
        b.up()
        b.wait_converged(120)
        b.wait_stable(120)
        return None


def brief_alarm(a: Dict) -> Dict:
    return dict(type=a["fault_type"], link=a["link_id"], node=a.get("node"), confidence=a.get("confidence"),
                round=a.get("first_round"), probes=a.get("probes_used"))


def fill_window(eng) -> int:
    """跑满一个判决窗口（与 bench 相同），返回其间发出的告警数（无效告警）。"""
    seq = eng.alarm_seq
    for _ in range(eng.plan.window):
        eng.step()
    n = eng.alarm_seq - seq
    if n and eng.active:
        eng._clear("场景切换")
    return n


def wait_clear(eng, max_rounds: int):
    """故障已清除：等全部告警恢复，返回 (是否恢复, 轮数)。"""
    for k in range(1, max_rounds + 1):
        eng.step()
        if eng.active is None:
            return True, k
    return False, max_rounds


def run_single(eng, f: Fault, pacer: Pacer, rng: random.Random, stats: Dict, max_rounds: int) -> Dict:
    from edgefdl.bench import _reset
    from edgefdl.robust import _new_alarms
    b, I = eng.backend, eng.plan.interval_s
    _reset(eng)
    wait0 = settle(eng, stats)
    pre = fill_window(eng)
    time.sleep(rng.random() * pacer.remaining())      # 故障发生在两轮之间的随机时刻
    seq, p0, s0 = eng.alarm_seq, eng.deep_probes, eng.suppressed
    b.inject(f)
    t_inj, r_inj = time.time(), eng.round
    first, k_alarm = None, None
    for k in range(1, max_rounds + 1):
        eng.step()
        if eng.alarm_seq > seq:
            first, k_alarm = _new_alarms(eng, seq)[0], k
            break
    t_held = time.time() - t_inj
    up = b.adj_uptime(f.link) if f.type != "link_down" else None
    b.clear()
    t_clr, r_clr = time.time(), eng.round
    cleared, k_clear = (False, None)
    if first is not None:
        cleared, k_clear = wait_clear(eng, max_rounds)
    new = _new_alarms(eng, seq)
    if eng.active:
        eng._clear("场景切换")
    return dict(
        link=f.link, type=f.type, value=f.value, label=f.label,
        alarmed=first is not None, alarm_type=first["fault_type"] if first else None,
        alarm_link=first["link_id"] if first else None,
        type_ok=bool(first and first["fault_type"] == f.type), loc_ok=bool(first and first["link_id"] == f.link),
        time_to_alarm_s=round(first["ts"] - t_inj, 2) if first else None, rounds_to_alarm=k_alarm,
        cleared=cleared, time_to_clear_s=round(next(a for a in new if a["alarm_id"] == first["alarm_id"])
                                               .get("cleared_ts", t_clr) - t_clr, 2) if cleared else None,
        rounds_to_clear=k_clear, confidence=first.get("confidence") if first else None,
        probes=first["probes_used"] if first else 0, deep_probes=eng.deep_probes - p0,
        filtered_by_agent=eng.suppressed - s0, n_alarms=len(new), alarms=[brief_alarm(a) for a in new],
        wrong_alarms=sum(1 for a in new if (a["fault_type"], a["link_id"]) != (f.type, f.link)),
        pre_alarms=pre, wait_stable_s=wait0, adj_full_s=None if up is None else round(up, 2),
        adj_flapped=None if up is None else bool(up < t_held - 1.0) if f.type != "link_down" else None,
        round_inject=r_inj, round_clear=r_clr, t_inject=round(t_inj, 3), interval_s=I)


def run_multi(eng, case: Dict, ci: int, pacer: Pacer, stats: Dict, after: int, repair: int, partial: bool,
              max_rounds: int) -> Dict:
    """复用 robust.multi_fault（与仿真相同的判定），按墙钟跑一个用例，并补测全部故障清除后的恢复。"""
    from edgefdl.robust import multi_fault
    if partial and ci % 2:                            # multi_fault 按用例序号轮流修复，这里每次只传一个用例
        case = dict(case, faults=case["faults"][::-1])
    eng.backend.clear()
    wait0 = settle(eng, stats)
    r0 = eng.round
    r = multi_fault(eng, [case], after=after, repair=repair, partial=partial)["rows"][0]
    r_clr = eng.round
    eng.backend.clear()
    cleared, k = wait_clear(eng, max_rounds)
    if eng.active:
        eng._clear("场景切换")
    r.update(faults=[x.label for x in case["faults"]], cleared=cleared,
             rounds_to_clear=k, wait_stable_s=wait0, round_start=r0, round_clear=r_clr,
             truth=[list(x) for x in r["truth"]], alarms=[list(x) for x in r["alarms"]],
             t_first_s=None if r["t_first"] is None else r["t_first"] * eng.plan.interval_s)
    return r


def run_normal(eng, rounds: int, label: str, noise: float, out: Dict, save) -> None:
    """无故障时段：只跑引擎，发出的任何告警都是无效告警。结果写入 out，每 60 轮保存一次，中途退出不丢。"""
    from edgefdl.bench import _reset
    from edgefdl.robust import _new_alarms
    _reset(eng)
    seq, s0, trig, held, t0 = eng.alarm_seq, eng.suppressed, 0, 0, time.time()
    for k in range(1, rounds + 1):
        was = eng.det.active
        eng.step()
        trig += int(eng.det.active and not was)
        held += int(eng.active is not None)
        if k % 60 == 0 or k == rounds:
            new = _new_alarms(eng, seq)
            out.update(label=label, rounds=k, wall_s=round(time.time() - t0, 1), noise_prob=noise,
                       detector_triggers=trig, filtered_by_agent=eng.suppressed - s0, false_alarms=len(new),
                       alarms=[brief_alarm(a) for a in new], false_alarm_exposure_rounds=held)
            save()


def versions() -> Dict:
    def out(argv):
        try:
            return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  universal_newlines=True, timeout=5).stdout.strip().splitlines()[0]
        except (OSError, IndexError, subprocess.TimeoutExpired):
            return ""
    import numpy
    import torch
    cpu = ""
    try:
        with open("/proc/cpuinfo") as f:
            cpu = next(l.split(":", 1)[1].strip() for l in f if l.startswith("model name"))
    except (OSError, StopIteration):
        pass
    return dict(kernel=platform.release(), frr=out(["/usr/lib/frr/zebra", "--version"]), python=platform.python_version(),
                torch=torch.__version__, numpy=numpy.__version__, iperf3=out(["iperf3", "--version"]),
                iproute2=out(["ip", "-V"]), cpu=cpu, cpus_allowed=len(os.sched_getaffinity(0)),
                host_cpus=os.cpu_count())


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def cmd_run(a) -> None:
    import torch
    from edgefdl.cli import build_engine
    from edgefdl.faults.generator import learn_baseline
    torch.set_num_threads(1)
    os.makedirs(a.out, exist_ok=True)
    # 与 serve --backend netns 相同的构造：模型包自带拓扑、基线与测量计划
    ns = argparse.Namespace(seed=0, noise=0.0, engine="torch", sinks=None, confirm=2)
    eng, topo, backend = build_engine(a.model, "netns", os.path.join(a.out, "logs"), ns)
    res: Dict = dict(shard=a.shard, shards=a.shards, sets=a.sets.split(","), rows=[], node=[], dual=[], normal=[],
                     conditions=dict(versions(), model=a.model, model_sha256=sha256(a.model),
                                     model_version=eng.model_version, topology=topo.name,
                                     plan=eng.plan.__dict__.copy(), ospf_hello_s=backend.ospf_timers[0],
                                     ospf_dead_s=backend.ospf_timers[1], baseline="learned" if a.learn_rounds else "bundle",
                                     learn_rounds=a.learn_rounds, max_rounds=a.max_rounds, seed=a.seed,
                                     image=a.image, commit=a.commit, loadavg_start=os.getloadavg(),
                                     started=time.strftime("%Y-%m-%d %H:%M:%S %z")))
    stats: Dict = dict(rebuilds=0)
    path = os.path.join(a.out, "result.json")

    def save():
        res["stats"] = dict(stats, rounds=eng.round, engine_errors=eng.errors, deep_probes=eng.deep_probes,
                            extra_probes=eng.extra_probes, filtered_by_agent=eng.suppressed,
                            transients=trans.count if trans else 0,
                            round_s_mean=round(statistics.mean(pacer.durations), 3) if pacer.durations else None,
                            round_s_max=round(max(pacer.durations), 3) if pacer.durations else None,
                            rounds_over_interval=sum(d > eng.plan.interval_s for d in pacer.durations),
                            path_unknown=pacer.path_unknown, target_rounds=pacer.target_rounds,
                            probe_kbps=round(pacer.probe_bytes * 8 / max(1, len(pacer.durations)) /
                                             eng.plan.interval_s / 1e3, 1),
                            infer=eng.policy.latency_stats(), cpu_s_self=round(time.process_time(), 1),
                            cpu_s_children=round(sum(os.times()[2:4]), 1),
                            rss_mb=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
                            wall_s=round(time.time() - t_start, 1))
        res["conditions"]["loadavg_end"] = os.getloadavg()
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1, default=str)
        os.replace(path + ".tmp", path)

    t_start = time.time()
    trans = None
    pacer = Pacer(eng)
    try:
        conv = backend.wait_converged(120)
        stable = backend.wait_stable(120)
        print(f"== 分片 {a.shard}/{a.shards}：OSPF 收敛 {conv:.1f}s，稳态 {stable:.1f}s", flush=True)
        res["conditions"].update(ospf_convergence_s=round(conv, 1))
        if a.learn_rounds:
            eng.set_baseline(learn_baseline(backend, rounds=a.learn_rounds))
            print(f"== 现场基线学习 {a.learn_rounds} 轮完成", flush=True)
        pacer.align(a.phase)
        rng = random.Random(a.seed * 1000 + a.shard)
        faults = single_faults(topo)
        if a.only:
            want = set(parse_only(a.only))
            faults = [f for f in faults if (f.link, f.type) in want]
        sets = a.sets.split(",")
        if "single" in sets:
            for f in shard_of(faults, a.shard, a.shards):
                t0 = time.time()
                r = run_single(eng, f, pacer, rng, stats, a.max_rounds)
                res["rows"].append(r)
                print(f"[{len(res['rows'])}] {f.label}: 告警 {r['alarm_link']} {r['alarm_type']} "
                      f"用时 {r['time_to_alarm_s']}s，恢复 {r['time_to_clear_s']}s，共 {r['n_alarms']} 条，"
                      f"场景耗时 {time.time() - t0:.0f}s", flush=True)
                save()
        if "node" in sets:
            for ci, c in enumerate(shard_of(node_cases(topo, a.node_reps), a.shard, a.shards)):
                r = run_multi(eng, c, ci, pacer, stats, a.node_after, 0, False, a.max_rounds)
                res["node"].append(r)
                print(f"[设备宕机 {ci}] {sorted(c['truth'])} → {r['alarms']} 恢复={r['cleared']}", flush=True)
                save()
        if "dual" in sets:
            for ci, c in enumerate(shard_of(dual_cases(topo, a.dual, a.seed), a.shard, a.shards)):
                r = run_multi(eng, c, ci, pacer, stats, a.dual_after, a.dual_repair, True, a.max_rounds)
                res["dual"].append(r)
                print(f"[双故障 {ci}] {r['faults']} → {r['alarms']} 恢复={r['cleared']}", flush=True)
                save()
        if "normal" in sets:
            noise = a.noise if a.shard % 2 else 0.0      # 奇数分片叠加瞬时干扰，偶数分片不加
            backend.clear()
            settle(eng, stats)
            if noise:
                trans = Transients(backend, noise, a.seed + a.shard)
            r = {}
            res["normal"].append(r)
            run_normal(eng, a.normal_rounds, f"shard{a.shard}", noise, r, save)
            print("== 无故障时段", r, flush=True)
    finally:
        save()
        eng.stop()
        backend.down()
    print(f"== 分片 {a.shard} 完成，引擎异常 {eng.errors} 次，用时 {time.time() - t_start:.0f}s", flush=True)


# ---------------------------------------------------------------------- 汇总
def _mean(xs, nd=1):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), nd) if xs else None


def group_summary(rows: List[Dict]) -> Dict:
    n = len(rows)
    ta = [r["time_to_alarm_s"] for r in rows if r["time_to_alarm_s"] is not None]
    tc = [r["time_to_clear_s"] for r in rows if r["time_to_clear_s"] is not None]
    return dict(n=n, alarmed=sum(r["alarmed"] for r in rows), type_ok=sum(r["type_ok"] for r in rows),
                joint_ok=sum(r["type_ok"] and r["loc_ok"] for r in rows),
                joint_acc=round(sum(r["type_ok"] and r["loc_ok"] for r in rows) / n, 4) if n else None,
                cleared=sum(r["cleared"] for r in rows), wrong_alarms=sum(r["wrong_alarms"] for r in rows),
                mean_time_to_alarm_s=_mean(ta), max_time_to_alarm_s=max(ta) if ta else None,
                mean_time_to_clear_s=_mean(tc), max_time_to_clear_s=max(tc) if tc else None)


def summarize(rows: List[Dict]) -> Dict:
    """逐类型与三大类（链路中断 / 性能劣化 / 路由漂移）的统计；未命中逐条列出。"""
    by_type = {t: group_summary([r for r in rows if r["type"] == t]) for t in FAULT_TYPES
               if any(r["type"] == t for r in rows)}
    by_cat = {}
    for cat, (en, target) in CATEGORY_TARGET.items():
        rs = [r for r in rows if CATEGORY[r["type"]] == cat]
        if rs:
            s = group_summary(rs)
            by_cat[en] = dict(s, target=target, meets_target=s["joint_acc"] >= target)
    miss = [dict(link=r["link"], type=r["type"], value=r["value"], alarm=[r["alarm_type"], r["alarm_link"]],
                 alarms=r["alarms"]) for r in rows if not (r["type_ok"] and r["loc_ok"])]
    return dict(overall=group_summary(rows), by_type=by_type, by_category=by_cat, misses=miss)


def summarize_multi(rows: List[Dict], partial: bool) -> Dict:
    if not rows:
        return {}
    n = len(rows)
    out = dict(n=n, first_ok=sum(r["first_ok"] for r in rows), all_reported=sum(r["all_reported"] for r in rows),
               no_wrong_alarm=sum(r["no_wrong_alarm"] for r in rows), cleared=sum(r["cleared"] for r in rows),
               mean_time_to_first_alarm_s=_mean([r["t_first_s"] for r in rows]))
    if partial:
        both = [r for r in rows if r.get("both_active")]
        out.update(both_active=len(both), partial_clear_ok=sum(bool(r.get("partial_ok")) for r in both))
    return out


def summarize_normal(runs: List[Dict], interval_s: float) -> Dict:
    out = {}
    for key, sel in (("quiet", lambda r: not r["noise_prob"]), ("transients", lambda r: r["noise_prob"])):
        rs = [r for r in runs if sel(r)]
        if not rs:
            continue
        rounds = sum(r["rounds"] for r in rs)
        fa = sum(r["false_alarms"] for r in rs)
        out[key] = dict(runs=len(rs), rounds=rounds, hours=round(rounds * interval_s / 3600, 2),
                        noise_prob=rs[0]["noise_prob"], false_alarms=fa,
                        false_alarms_per_day=round(fa / (rounds * interval_s / 86400), 2),
                        detector_triggers=sum(r["detector_triggers"] for r in rs),
                        filtered_by_agent=sum(r["filtered_by_agent"] for r in rs),
                        false_alarm_exposure_rounds=sum(r["false_alarm_exposure_rounds"] for r in rs),
                        alarms=[a for r in rs for a in r["alarms"]])
    return out


def merge(parts: List[Dict]) -> Dict:
    rows = [dict(r, shard=p["shard"], run=p.get("run")) for p in parts for r in p["rows"]]
    rows.sort(key=lambda r: (int(r["link"][1:]), FAULT_TYPES.index(r["type"])))
    node = [dict(r, shard=p["shard"], run=p.get("run")) for p in parts for r in p["node"]]
    dual = [dict(r, shard=p["shard"], run=p.get("run")) for p in parts for r in p["dual"]]
    normal = [dict(r, shard=p["shard"], run=p.get("run")) for p in parts for r in p["normal"]]
    interval = parts[0]["conditions"]["plan"]["interval_s"]
    st = [p.get("stats", {}) for p in parts]
    total = lambda k: sum(s.get(k, 0) for s in st)
    pre = sum(r["pre_alarms"] for r in rows)
    pre_rounds = sum(1 for _ in rows) * parts[0]["conditions"]["plan"]["window"]
    stats = dict(rounds=total("rounds"), engine_errors=total("engine_errors"), deep_probes=total("deep_probes"),
                 extra_probes=total("extra_probes"), rebuilds=total("rebuilds"),
                 rounds_over_interval=total("rounds_over_interval"),
                 round_s_mean=_mean([s.get("round_s_mean") for s in st], 3),
                 round_s_max=max([s.get("round_s_max") or 0 for s in st]),
                 path_unknown_rate=round(total("path_unknown") / max(1, total("target_rounds")), 5),
                 probe_kbps_mean=_mean([s.get("probe_kbps") for s in st]),
                 infer_p99_ms_max=max([(s.get("infer") or {}).get("p99_ms", 0) or 0 for s in st]),
                 pre_fault_alarms=pre, pre_fault_rounds=pre_rounds,
                 loadavg_start_max=max(p["conditions"]["loadavg_start"][0] for p in parts))
    return dict(
        description="Online accuracy on the real protocol stack: the online engine of `edgefdl serve` with the released "
                    "model on the netns backend (Linux namespaces, FRR OSPF, tc netem and tbf), wall-clock 5 s rounds.",
        created=time.strftime("%Y-%m-%d %H:%M:%S %z"),
        conditions=dict({k: v for k, v in parts[0]["conditions"].items()
                         if k not in ("loadavg_start", "loadavg_end", "started")},
                        containers=len(parts),
                        shards=sorted({(p.get("run"), p["shard"], p["shards"]) for p in parts},
                                      key=lambda x: (str(x[0]), x[1]))),
        summary=dict(single=summarize(rows), node=summarize_multi(node, False), dual=summarize_multi(dual, True),
                     normal=summarize_normal(normal, interval), engine=stats),
        scenarios=rows, node_cases=node, dual_cases=dual, normal_runs=normal,
        per_container=[dict(run=p.get("run"), shard=p["shard"], sets=p["sets"], stats=p.get("stats"),
                            started=p["conditions"].get("started"),
                            loadavg_start=p["conditions"].get("loadavg_start"),
                            loadavg_end=p["conditions"].get("loadavg_end")) for p in parts])


def cmd_merge(a) -> None:
    parts, raw, total = [], [], 0
    for d in a.dirs:
        for p in sorted(glob.glob(os.path.join(d, "**", "result.json"), recursive=True)):
            with open(p, encoding="utf-8") as f:
                r = json.load(f)
            r["run"] = os.path.relpath(os.path.dirname(p), os.path.dirname(os.path.abspath(d.rstrip("/"))))
            parts.append(r)
            raw.append((r["run"], os.path.join(os.path.dirname(p), "logs")))
    if not parts:
        raise SystemExit("没有找到 result.json")
    res = merge(parts)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
        f.write("\n")
    if a.raw:                            # 原始日志 gzip 后发布（文件头 mtime 置 0，内容不变则字节不变）
        os.makedirs(a.raw, exist_ok=True)
        for run, logs in raw:
            for kind in ("measure", "alarm"):
                src = os.path.join(logs, f"{kind}_log.csv")
                if not os.path.exists(src):
                    continue
                dst = os.path.join(a.raw, f"{run.replace(os.sep, '_')}_{kind}.csv.gz")
                with open(src, "rb") as f, open(dst, "wb") as g, \
                        gzip.GzipFile(filename="", fileobj=g, mode="wb", compresslevel=9, mtime=0) as z:
                    z.write(f.read())
                total += os.path.getsize(dst)
        print(f"原始日志 {total / 1e6:.2f} MB -> {a.raw}")
    s = res["summary"]
    print(json.dumps(dict(overall=s["single"].get("overall"), by_category=s["single"].get("by_category"),
                          misses=s["single"].get("misses"), node=s["node"], dual=s["dual"],
                          normal=s["normal"], engine=s["engine"]), ensure_ascii=False, indent=1))


def markdown_table(res: Dict) -> str:
    """docs/results.md 的逐类型表，数字全部取自 results/realstack.json。"""
    s = res["summary"]["single"]
    head = ("| Fault type | Scenarios | Alarmed | Type right | Type + link right | Mean time to alarm | "
            "Max time to alarm | Alarm cleared | Mean time to clear |\n|---|---|---|---|---|---|---|---|---|\n")
    fmt = lambda name, g: (f"| {name} | {g['n']} | {g['alarmed']} | {g['type_ok']} | {g['joint_ok']} "
                           f"({100 * g['joint_acc']:.1f}%) | {g['mean_time_to_alarm_s']} s | "
                           f"{g['max_time_to_alarm_s']} s | {g['cleared']} | {g['mean_time_to_clear_s']} s |\n")
    out = head
    for t, g in s["by_type"].items():
        out += fmt(TYPE_EN[t], g)
    out += fmt("**All**", s["overall"])
    return out


def cmd_table(a) -> None:
    with open(a.json, encoding="utf-8") as f:
        print(markdown_table(json.load(f)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="真实协议栈在线准确率测试")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("run", help="在实验容器内跑一个分片")
    p.add_argument("--model", default="results/model.pt")
    p.add_argument("--out", required=True)
    p.add_argument("--sets", default="single", help=f"逗号分隔：{','.join(SETS)}")
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--only", help="只跑指定单故障场景，如 L4:delay,L9:loss（调试用）")
    p.add_argument("--max-rounds", type=int, default=60, help="等告警、等恢复各自的轮数上限（同 bench）")
    p.add_argument("--normal-rounds", type=int, default=180, help="无故障时段轮数")
    p.add_argument("--noise", type=float, default=0.0,
                   help="无故障时段每轮瞬时干扰概率（在线仿真取 0.15），只作用于奇数分片，偶数分片不加干扰")
    p.add_argument("--node-reps", type=int, default=1, help="每台设备宕机次数")
    p.add_argument("--node-after", type=int, default=24, help="设备宕机持续轮数")
    p.add_argument("--dual", type=int, default=24, help="双故障用例总数（各分片均分）")
    p.add_argument("--dual-after", type=int, default=30, help="双故障持续轮数")
    p.add_argument("--dual-repair", type=int, default=24, help="修复其中一处后的观察轮数")
    p.add_argument("--learn-rounds", type=int, default=0, help="上线基线学习轮数（同 serve --learn-rounds，0 用模型包基线）")
    p.add_argument("--seed", type=int, default=21)
    p.add_argument("--phase", type=float, default=0.0, help="本分片测量轮的起始相位 s：各分片的 traceroute 共用内核全局 ICMP 限速")
    p.add_argument("--image", default=os.environ.get("EDGEFDL_LAB_IMAGE", ""))
    p.add_argument("--commit", default="")
    p.set_defaults(fn=cmd_run)
    p = sp.add_parser("merge", help="合并各分片结果")
    p.add_argument("dirs", nargs="+")
    p.add_argument("--out", default="results/realstack.json")
    p.add_argument("--raw", help="把原始 CSV 日志 gzip 到这个目录")
    p.set_defaults(fn=cmd_merge)
    p = sp.add_parser("table", help="由结果 JSON 输出文档用的逐类型表")
    p.add_argument("json", nargs="?", default="results/realstack.json")
    p.set_defaults(fn=cmd_table)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
