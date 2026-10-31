#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""量化 live 后端缺测的影响：仿真后端按 bench 的同一套场景与噪声长跑，只是把某些测量去掉。

与 live 后端一样处理缺测：不写入 RoundResult、Backend.can_* 返回 False，引擎屏蔽相应动作；traceroute 每轮轮转 5 个
目标（full 除外）。用的是原模型，不重训。
  full      全部测量（对照）
  no_lsdb   没有 LSDB（未给 --lsdb-cmd）
  no_ends   没有包列带宽估计和精测（未给 --endpoints），LSDB 还在
  ping_only 只有 ping 和 traceroute，盲区链路的 NQA 也没有（live 后端的默认形态）
  infer     同 ping_only，加 --infer-state：由 ping 与 traceroute 推断链路中断和开销变化

用法：python scripts/live_missing.py --out results/live_missing.json --rounds 17280
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from edgefdl.vnet.sim import SimBackend  # noqa: E402

MODES = {"full": (False, False, False), "no_lsdb": (True, False, False), "no_ends": (False, True, False),
         "ping_only": (True, True, True), "infer": (True, True, True)}


class MaskedSim(SimBackend):
    def __init__(self, topo, plan, drop_lsdb, drop_ends, drop_nqa, trace_budget=0, **kw):
        super().__init__(topo, plan, **kw)
        self.drop_lsdb, self.drop_ends = drop_lsdb, drop_ends
        self.nqa = [t.id for t in topo.targets() if t.kind == "nqa"] if drop_nqa else []
        self.infer = None
        self.budget = trace_budget                  # 与 live 后端一样按预算轮转 traceroute；0 为全部目标
        self.cursor = 0

    def measure_round(self, round_id, transient=None):
        rr = super().measure_round(round_id, transient)
        if self.drop_lsdb:
            rr.lsdb = {}
        if self.drop_ends:
            rr.bw = {}
        for tid in self.nqa:
            rr.pings.pop(tid, None)
        if self.budget:
            ids = [t.id for t in self.topo.targets() if t.kind != "nqa"]
            keep = {ids[(self.cursor + i) % len(ids)] for i in range(self.budget)}
            self.cursor = (self.cursor + self.budget) % len(ids)
            for tid, ps in rr.pings.items():
                if tid not in keep and tid not in self.nqa:
                    ps.path = ()
        if self.infer is not None:
            rr.lsdb = self.infer.update(rr)
        return rr

    def can_probe(self, link):
        return not self.drop_ends

    can_bw = can_probe

    def can_lsdb(self):
        return not self.drop_lsdb


def run_mode(a):
    mode, model, rounds, seed, noise = a
    import torch
    torch.set_num_threads(1)
    from edgefdl.agent.features import FeatureEncoder
    from edgefdl.agent.infer import Policy
    from edgefdl.bench import bench_noise, bench_scenarios
    from edgefdl.cli import load_bundle
    from edgefdl.core.orchestrator import Engine
    ag, topo, base, plan, meta = load_bundle(model)
    drop_lsdb, drop_ends, drop_nqa = MODES[mode]
    b = MaskedSim(topo, plan, drop_lsdb, drop_ends, drop_nqa, trace_budget=0 if mode == "full" else 5,
                  seed=seed, noise_prob=noise)
    b.up()
    if mode == "infer":
        from edgefdl.vnet.live import StateInference
        b.infer = StateInference(topo)
        b.infer.set_reference({t.id: p for t, p in zip(topo.targets(), base.paths)})
    with tempfile.TemporaryDirectory() as tmp:
        eng = Engine(topo, b, Policy(ag, FeatureEncoder(topo, base)), plan, tmp, model_version=meta.get("version", ""))
        t0 = time.time()
        nz = bench_noise(eng, rounds) if rounds else {}
        sc = bench_scenarios(eng, topo)
        eng.stop()
    nz = {k: nz[k] for k in ("rounds", "sim_hours", "detector_triggers", "filtered_by_agent", "false_alarms",
                             "false_alarm_exposure_pct", "engine_errors") if k in nz}
    rows = sc["rows"]
    wrong = [dict(link=r["link"], type=r["type"], pred=r["pred"]) for r in rows if not (r["type_ok"] and r["loc_ok"])]
    stuck = [dict(link=r["link"], type=r["type"], pred=r["pred"]) for r in rows
             if r["alarmed"] and r["time_to_clear_s"] is None]
    out = dict(noise=nz, scenarios=len(rows), correct=len(rows) - len(wrong), summary=sc["summary"],
               not_correct=wrong, not_cleared_within_60_rounds=stuck, wall_s=round(time.time() - t0, 1))
    print(mode, f"{out['correct']}/{out['scenarios']}", nz.get("false_alarms"), flush=True)
    return mode, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="results/model.pt")
    ap.add_argument("--out", default="results/live_missing.json")
    ap.add_argument("--rounds", type=int, default=17280, help="噪声长跑轮数（默认 = 24h @5s）")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--noise", type=float, default=0.15)
    ap.add_argument("--modes", default=",".join(MODES))
    a = ap.parse_args()
    modes = a.modes.split(",")
    with mp.get_context("spawn").Pool(len(modes)) as pool:
        res = dict(pool.map(run_mode, [(m, a.model, a.rounds, a.seed, a.noise) for m in modes]))
    doc = dict(conditions=dict(backend="sim with measurements removed, handled as on the live backend",
                               model=a.model, topology="configs/demo_topology.yaml (the bundle's)",
                               scenarios="same 84 single-fault scenarios as bench (held until alarm, at most 60 rounds)",
                               noise_prob=a.noise, seed=a.seed, noise_rounds=a.rounds,
                               modes={"full": "all measurements (control)", "no_lsdb": "no LSDB",
                                      "no_ends": "no packet trains, no deep probes; LSDB present",
                                      "ping_only": "ping and traceroute from the EMS only; no LSDB, no packet trains, "
                                                   "no deep probes, no NQA target of the blind link",
                                      "trace_budget": "5 traceroutes per round, in rotation, in every mode but full",
                                      "infer": "as ping_only, with link and cost state inferred from ping and "
                                               "traceroute (--infer-state)"}),
               modes={m: res[m] for m in modes})
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print("写出", a.out)


if __name__ == "__main__":
    main()
